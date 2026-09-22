"""Fail-safe DeepSeek Responses API client for macro decision ranking."""

from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from macro_wire import decode_candidate_facts


CACHE_HIT_PRICE_PER_MILLION = 0.0028
CACHE_MISS_PRICE_PER_MILLION = 0.14
OUTPUT_PRICE_PER_MILLION = 0.28
LOCAL_CACHE_SCHEMA_VERSION = "advice-cache-v2"
MAX_USAGE_TOKENS = 1_000_000_000


def _safe_nonnegative_int(value):
    """Parse provider usage counters without letting telemetry break play."""

    if isinstance(value, bool):
        return 0
    try:
        return min(MAX_USAGE_TOKENS, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


class DeepSeekRequestError(RuntimeError):
    def __init__(
        self,
        category,
        *,
        status=None,
        retryable=False,
        cause_type=None,
        os_errno=None,
    ):
        super().__init__(category)
        self.category = str(category)
        self.status = status
        self.retryable = bool(retryable)
        self.cause_type = str(cause_type) if cause_type else None
        try:
            self.os_errno = int(os_errno) if os_errno is not None else None
        except (TypeError, ValueError, OverflowError):
            self.os_errno = None

    def safe_details(self):
        """Return diagnostics which cannot contain keys, URLs, or payloads."""

        return {
            key: value
            for key, value in {
                "exception_type": self.cause_type,
                "errno": self.os_errno,
                "http_status": self.status,
            }.items()
            if value is not None
        }


class DeepSeekResponsesClient:
    def __init__(self, config, api_key=None, opener=None):
        self.config = config
        self.api_key = api_key if api_key is not None else os.environ.get(
            "DEEPSEEK_API_KEY", ""
        )
        if opener is not None:
            self.opener = opener
        elif getattr(config, "proxy_url", None):
            proxy_handler = urllib.request.ProxyHandler({
                "http": config.proxy_url,
                "https": config.proxy_url,
            })
            self.opener = urllib.request.build_opener(proxy_handler).open
        else:
            self.opener = urllib.request.urlopen

    @property
    def available(self):
        return bool(str(self.api_key or "").strip())

    def request(self, body, timeout_seconds):
        return self._post("/responses", body, timeout_seconds)

    def request_json_fallback(self, body, timeout_seconds):
        """Use stable Chat JSON mode if Responses schema negotiation fails."""

        effort = str((body.get("reasoning") or {}).get("effort") or "none")
        messages = [dict(item) for item in (body.get("input") or [])]
        embedded_contract = any(
            str(item.get("content") or "").startswith(
                "STATIC_OUTPUT_CONTRACT\n"
            )
            for item in messages
            if isinstance(item, dict)
        )
        format_spec = ((body.get("text") or {}).get("format") or {})
        response_schema = format_spec.get("schema")
        if isinstance(response_schema, dict) and not embedded_contract:
            properties = response_schema.get("properties") or {}
            candidate_ids = list(
                ((properties.get("choice_id") or {}).get("enum") or [])
            )
            advice_ids = list(
                ((properties.get("advice_id") or {}).get("enum") or [])
            )
            schema_versions = list(
                ((properties.get("schema_version") or {}).get("enum") or [])
            )
            reason_codes = list(
                (((properties.get("reason_codes") or {}).get("items") or {}).get("enum") or [])
            )
            example_scores = [
                round(100.0 * (len(candidate_ids) - index) / max(1, len(candidate_ids)), 3)
                for index in range(len(candidate_ids))
            ]
            example = {
                "schema_version": schema_versions[0] if schema_versions else "schema-version",
                "advice_id": advice_ids[0] if advice_ids else "advice-id",
                "choice_id": candidate_ids[0] if candidate_ids else "candidate-id",
                "confidence": 0.8,
                "rankings": [
                    {"candidate_id": candidate_id, "score": score}
                    for candidate_id, score in zip(candidate_ids, example_scores)
                ],
                "reason_codes": reason_codes[:1],
                "rationale": "Concise evidence from the supplied state.",
            }
            messages.append({
                "role": "user",
                "content": (
                    "STRICT_OUTPUT_CONTRACT\nReturn exactly one JSON object "
                    "matching this JSON Schema. Do not add or omit fields:\n"
                    + json.dumps(
                        response_schema,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\nSCORE_SEMANTICS\nEvery rankings.score must be a "
                    "finite normalized utility from 0 through 100, higher is "
                    "better. Do not copy an unbounded or negative local_score. "
                    "choice_id must be tied for the highest score. Rank each "
                    "listed candidate at most once.\nEXAMPLE_JSON_SHAPE\n"
                    + json.dumps(
                        example,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            })
        chat_body = {
            "model": body.get("model"),
            "messages": messages,
            "max_tokens": int(body.get("max_output_tokens") or 512),
            "thinking": {
                "type": "disabled" if effort == "none" else "enabled"
            },
            "stream": False,
        }
        if isinstance(response_schema, dict):
            chat_body["response_format"] = {"type": "json_object"}
        if effort != "none":
            chat_body["reasoning_effort"] = (
                "max" if effort == "max" else "high"
            )
        elif isinstance(body.get("temperature"), (int, float)):
            chat_body["temperature"] = float(body["temperature"])
        response = self._post(
            "/chat/completions", chat_body, timeout_seconds
        )
        choices = response.get("choices")
        adapter_error = None
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            content = None
            adapter_error = "invalid_response_shape"
        else:
            message = choices[0].get("message")
            content = (
                message.get("content") if isinstance(message, dict) else None
            )
            if not isinstance(content, str):
                content = None
                adapter_error = "invalid_response_shape"
        return {
            "model": response.get("model"),
            "system_fingerprint": response.get("system_fingerprint"),
            "output_text": content,
            "usage": response.get("usage") or {},
            "transport": "chat_json_fallback",
            "adapter_error": adapter_error,
        }

    def _post(self, path, body, timeout_seconds):
        if not self.available:
            raise DeepSeekRequestError("missing_api_key")
        payload = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url + path,
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + str(self.api_key),
                "Content-Type": "application/json",
                "User-Agent": "sts-cli-deepseek-macro/1",
            },
        )
        try:
            response = self.opener(request, timeout=float(timeout_seconds))
            raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            raise DeepSeekRequestError(
                (
                    "auth" if status in {401, 403}
                    else "account" if status == 402
                    else "http_error"
                ),
                status=status,
                retryable=status == 429 or status >= 500,
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            raise DeepSeekRequestError(
                "network_error",
                retryable=True,
                cause_type=type(reason or exc).__name__,
                os_errno=getattr(reason, "errno", None),
            ) from None
        except TimeoutError as exc:
            raise DeepSeekRequestError(
                "timeout",
                retryable=True,
                cause_type=type(exc).__name__,
                os_errno=getattr(exc, "errno", None),
            ) from None
        except OSError as exc:
            raise DeepSeekRequestError(
                "network_error",
                retryable=True,
                cause_type=type(exc).__name__,
                os_errno=getattr(exc, "errno", None),
            ) from None
        except Exception as exc:
            # Provider/client failures must never escape into game control.
            raise DeepSeekRequestError(
                "network_error",
                retryable=True,
                cause_type=type(exc).__name__,
            ) from None
        if len(raw) > self.config.max_response_bytes:
            raise DeepSeekRequestError("response_too_large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise DeepSeekRequestError("invalid_response_json") from None
        if not isinstance(decoded, dict):
            raise DeepSeekRequestError("invalid_response_shape")
        return decoded


def _extract_output_text(response):
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts = []
    output = response.get("output")
    if output is None:
        output = []
    if not isinstance(output, list):
        raise DeepSeekRequestError("invalid_response_shape")
    for item in output:
        if not isinstance(item, dict):
            continue
        contents = item.get("content")
        if contents is None:
            contents = []
        if not isinstance(contents, list):
            raise DeepSeekRequestError("invalid_response_shape")
        for content in contents:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _usage_summary(response):
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage.get("input_tokens_details") or usage.get(
        "prompt_tokens_details"
    )
    details = details if isinstance(details, dict) else {}
    input_tokens = _safe_nonnegative_int(
        usage.get(
            "input_tokens",
            usage.get("prompt_tokens", usage.get("total_input_tokens", 0)),
        )
    )
    output_tokens = _safe_nonnegative_int(
        usage.get("output_tokens", usage.get("completion_tokens", 0))
    )
    # DeepSeek has exposed both ``prompt_cache_*`` and OpenAI-compatible
    # ``*_tokens_details`` names over time.  Normalize all known aliases so
    # a valid provider hit is not reported as a miss merely because the
    # transport changed shape.
    cache_hit = _safe_nonnegative_int(
        usage.get(
            "prompt_cache_hit_tokens",
            usage.get(
                "prompt_cache_read_tokens",
                usage.get(
                    "cache_read_input_tokens",
                    usage.get("cache_read_tokens", details.get("cached_tokens", 0)),
                ),
            ),
        )
        or 0
    )
    explicit_miss = usage.get(
        "prompt_cache_miss_tokens",
        usage.get(
            "prompt_cache_write_tokens",
            usage.get(
                "cache_creation_input_tokens",
                details.get("cache_creation_input_tokens"),
            ),
        ),
    )
    cache_miss = _safe_nonnegative_int(
        max(0, input_tokens - cache_hit)
        if explicit_miss is None
        else explicit_miss
    )
    accounted_input = cache_hit + cache_miss
    if input_tokens > accounted_input:
        # Unclassified prompt tokens are conservatively billed at the cache
        # miss rate instead of silently disappearing from the run budget.
        cache_miss = min(
            MAX_USAGE_TOKENS,
            cache_miss + input_tokens - accounted_input,
        )
        accounted_input = cache_hit + cache_miss
    input_tokens = max(input_tokens, accounted_input)
    cost = (
        cache_hit * CACHE_HIT_PRICE_PER_MILLION
        + cache_miss * CACHE_MISS_PRICE_PER_MILLION
        + output_tokens * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000
    total_prompt = cache_hit + cache_miss
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
        "prompt_cache_hit_ratio": (
            round(cache_hit / total_prompt, 6) if total_prompt else None
        ),
        "estimated_cost_usd": round(cost, 8),
    }


_CANDIDATE_ALIAS_FIELDS = (
    "card_id", "relic_id", "potion_id", "option_id", "event_id",
    "boss_id", "node_id", "id", "name", "label", "text", "symbol",
)


def _candidate_aliases(package):
    """Build unambiguous display-name aliases for the strict candidate IDs.

    The wire contract deliberately makes ``candidate_id`` authoritative, but
    compact Chat JSON responses occasionally copy a card/relic name instead.
    Repair only an unambiguous alias backed by the local candidate facts.  An
    ambiguous name remains invalid and therefore still falls back safely.
    """

    candidate_ids = {
        str(candidate_id) for candidate_id in package.get("candidate_ids") or []
    }
    dynamic = package.get("dynamic") or {}
    visible = dynamic.get("candidates") or []
    catalog = dynamic.get("candidate_fact_catalog") or []
    aliases = {}
    ambiguous = set()

    def register(value, candidate_id):
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value in candidate_ids:
            return
        key = value.casefold()
        if key in ambiguous:
            return
        previous = aliases.get(key)
        if previous is None:
            aliases[key] = candidate_id
        elif previous != candidate_id:
            aliases.pop(key, None)
            ambiguous.add(key)

    for candidate in visible:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id not in candidate_ids:
            continue
        facts = {}
        fact_ref = candidate.get("fact_ref")
        if isinstance(fact_ref, int) and 0 <= fact_ref < len(catalog):
            encoded = catalog[fact_ref]
            if isinstance(encoded, dict):
                try:
                    decoded = decode_candidate_facts(encoded)
                except (TypeError, ValueError, KeyError):
                    decoded = encoded
                if isinstance(decoded, dict):
                    facts = decoded
        for source in (candidate, facts):
            for field_name in _CANDIDATE_ALIAS_FIELDS:
                register(source.get(field_name), candidate_id)
    return aliases


def _canonical_candidate_id(value, package, aliases=None):
    """Return the exact local candidate ID when a safe alias is available."""

    if not isinstance(value, str):
        return value
    raw = value.strip()
    candidate_ids = {
        str(candidate_id) for candidate_id in package.get("candidate_ids") or []
    }
    if raw in candidate_ids:
        return raw
    aliases = aliases if aliases is not None else _candidate_aliases(package)
    return aliases.get(raw.casefold(), raw)


def _validated_advice(response, package, schema_version):
    text = _extract_output_text(response)
    if not text:
        raise DeepSeekRequestError("empty_output")
    if "```" in text:
        raise DeepSeekRequestError("markdown_output")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise DeepSeekRequestError("invalid_advice_json") from None
    if not isinstance(value, dict):
        raise DeepSeekRequestError("invalid_advice_shape")
    required = {
        "schema_version", "advice_id", "choice_id", "confidence",
        "rankings", "reason_codes", "rationale",
    }
    if set(value) != required:
        raise DeepSeekRequestError("invalid_advice_fields")
    # ``schema_version`` is a fixed echo, not model-controlled decision data.
    # Chat JSON can reproduce this harmless metadata incorrectly after a long
    # reasoning trace.  Require the field and a sane shape, then bind the
    # response to our authoritative local schema.  Advice id, candidate ids,
    # complete rankings and choice consistency remain strict below.
    response_schema_version = value.get("schema_version")
    if not isinstance(response_schema_version, str) or not response_schema_version:
        raise DeepSeekRequestError("schema_mismatch")
    if value.get("advice_id") != package["advice_id"]:
        raise DeepSeekRequestError("advice_id_mismatch")
    candidate_ids = set(package["candidate_ids"])
    aliases = _candidate_aliases(package)
    choice_id = _canonical_candidate_id(value.get("choice_id"), package, aliases)
    if choice_id not in candidate_ids:
        raise DeepSeekRequestError("unknown_choice")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DeepSeekRequestError("invalid_confidence")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise DeepSeekRequestError("invalid_confidence")
    rankings = value.get("rankings")
    if not isinstance(rankings, list) or not rankings:
        raise DeepSeekRequestError("invalid_rankings")
    seen = set()
    normalized = []
    for ranking in rankings:
        if (
            not isinstance(ranking, dict)
            or not {"candidate_id", "score"}.issubset(ranking)
        ):
            raise DeepSeekRequestError("invalid_ranking")
        # Chat JSON can attach explanatory metadata to an otherwise complete
        # ranking item.  It never influences fusion, so preserve the strict
        # binding, uniqueness and finite-score checks below while ignoring
        # that untrusted annotation rather than wasting a valid response.
        candidate_id = _canonical_candidate_id(
            ranking.get("candidate_id"), package, aliases
        )
        score = ranking.get("score")
        if candidate_id not in candidate_ids or candidate_id in seen:
            raise DeepSeekRequestError("invalid_ranking_candidate")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise DeepSeekRequestError("invalid_ranking_score")
        score = float(score)
        if not math.isfinite(score):
            raise DeepSeekRequestError("invalid_ranking_score")
        seen.add(candidate_id)
        normalized.append({"candidate_id": candidate_id, "score": score})
    if seen != set(candidate_ids):
        raise DeepSeekRequestError("incomplete_rankings")
    highest = max(item["score"] for item in normalized)
    chosen_score = next(
        (item["score"] for item in normalized if item["candidate_id"] == choice_id),
        None,
    )
    if chosen_score is None or chosen_score != highest:
        raise DeepSeekRequestError("choice_ranking_mismatch")
    reason_codes = value.get("reason_codes")
    if not isinstance(reason_codes, list) or any(
        not isinstance(item, str) for item in reason_codes
    ):
        raise DeepSeekRequestError("invalid_reason_codes")
    allowed_reason_codes = set(package.get("reason_codes") or [])
    # These labels are audit-only and never influence the selected action.
    # Chat JSON mode does not enforce the embedded enum, so keep the strict
    # shape check above while repairing harmless duplicate/unknown labels.
    # Rejecting a fully bound, complete ranking for an invented annotation
    # wastes a paid response without improving action safety.
    normalized_reason_codes = []
    for item in reason_codes:
        if (
            item in allowed_reason_codes
            and item not in normalized_reason_codes
        ):
            normalized_reason_codes.append(item)
    rationale = value.get("rationale")
    if not isinstance(rationale, str):
        raise DeepSeekRequestError("invalid_rationale")
    # The downstream fusion consumes rank order, not score magnitude.  Chat
    # JSON mode guarantees valid JSON but does not enforce our JSON Schema, so
    # preserve any finite model ordering and normalize unbounded scales rather
    # than discarding an otherwise coherent recommendation.
    lowest = min(item["score"] for item in normalized)
    highest = max(item["score"] for item in normalized)
    if lowest < 0.0 or highest > 100.0:
        # Scaling first avoids overflow for coherent but extreme finite scores
        # such as +/-1e308, whose direct subtraction would become infinity.
        scale = max(1.0, abs(lowest), abs(highest))
        scaled_lowest = lowest / scale
        span = highest / scale - scaled_lowest
        for item in normalized:
            score = (
                100.0 if span == 0.0
                else (item["score"] / scale - scaled_lowest) * 100.0 / span
            )
            item["score"] = round(max(0.0, min(100.0, score)), 6)
            if not math.isfinite(item["score"]):
                raise DeepSeekRequestError("invalid_ranking_score")
    normalized.sort(key=lambda item: (
        -item["score"],
        0 if item["candidate_id"] == choice_id else 1,
        item["candidate_id"],
    ))
    return {
        "choice_id": choice_id,
        "confidence": confidence,
        "rankings": normalized,
        "reason_codes": normalized_reason_codes[:6],
        # Rationale is audit-only and never influences the selected action.
        # Bound it locally instead of discarding an otherwise valid ranking.
        "rationale": rationale[:240],
    }


def _validated_cached_advice(cached, package, schema_version):
    """Revalidate local cache content through the live response contract."""

    if (
        not isinstance(cached, dict)
        or cached.get("cache_schema_version") != LOCAL_CACHE_SCHEMA_VERSION
        or cached.get("advice_id") != package["advice_id"]
    ):
        raise DeepSeekRequestError("invalid_local_cache")
    response = {
        "output_text": json.dumps({
            "schema_version": cached.get("schema_version"),
            "advice_id": cached.get("advice_id"),
            "choice_id": cached.get("choice_id"),
            "confidence": cached.get("confidence"),
            "rankings": cached.get("rankings"),
            "reason_codes": cached.get("reason_codes"),
            "rationale": cached.get("rationale"),
        }, ensure_ascii=False, separators=(",", ":")),
    }
    return _validated_advice(response, package, schema_version)


class LocalAdviceCache:
    def __init__(self, root):
        self.root = Path(root)
        self._memory = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._memory:
                return self._memory[key]
        path = self.root / f"{key}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        with self._lock:
            self._memory[key] = value
        return value

    def put(self, key, value):
        value = dict(value)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{key}.{uuid.uuid4().hex}.tmp"
        target = self.root / f"{key}.json"
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        with self._lock:
            self._memory[key] = value

    def verify_release_fingerprint(self, release, fingerprint):
        """Persist the first observed fingerprint and reject silent drift."""

        if fingerprint is None:
            return True
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("invalid model fingerprint")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "release-fingerprints.json"
        with self._lock:
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                values = {}
            except (OSError, json.JSONDecodeError, RecursionError) as exc:
                raise OSError("fingerprint store unavailable") from exc
            if not isinstance(values, dict):
                raise OSError("fingerprint store has invalid shape")
            known = values.get(str(release))
            if known is not None:
                if not isinstance(known, str) or not known.strip():
                    raise OSError("fingerprint store has invalid value")
                return known == fingerprint
            values[str(release)] = fingerprint
            temporary = self.root / f".release.{uuid.uuid4().hex}.tmp"
            temporary.write_text(
                json.dumps(values, ensure_ascii=True, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            return True


@dataclass
class AdvisorResult:
    status: str
    advice_id: str | None = None
    rule_choice_id: str | None = None
    model_choice_id: str | None = None
    confidence: float | None = None
    rankings: list = field(default_factory=list)
    reason_codes: list = field(default_factory=list)
    rationale: str = ""
    error_class: str | None = None
    error_details: dict = field(default_factory=dict)
    latency_ms: int = 0
    requested_model: str | None = None
    response_model: str | None = None
    system_fingerprint: str | None = None
    usage: dict = field(default_factory=dict)
    snapshot_hash: str | None = None
    local_cache_hit: bool = False
    transport: str | None = None

    def as_dict(self):
        return {
            "status": self.status,
            "advice_id": self.advice_id,
            "rule_choice_id": self.rule_choice_id,
            "model_choice_id": self.model_choice_id,
            "confidence": self.confidence,
            "rankings": self.rankings,
            "reason_codes": self.reason_codes,
            "rationale": self.rationale,
            "error_class": self.error_class,
            "error_details": dict(self.error_details),
            "latency_ms": self.latency_ms,
            "requested_model": self.requested_model,
            "response_model": self.response_model,
            "system_fingerprint": self.system_fingerprint,
            "usage": self.usage,
            "snapshot_hash": self.snapshot_hash,
            "local_cache_hit": self.local_cache_hit,
            "transport": self.transport,
        }


class DeepSeekMacroAdvisor:
    def __init__(
        self,
        config,
        assembler,
        *,
        client=None,
        cache=None,
        callback=None,
    ):
        self.config = config
        self.assembler = assembler
        self.client = client or DeepSeekResponsesClient(config)
        self.cache = cache or LocalAdviceCache(
            assembler.root / ".deepseek-advisor-cache"
        )
        self.callback = callback
        self.calls = 0
        self.warmup_calls = 0
        self.total_wait_seconds = 0.0
        self.total_cost_usd = 0.0
        self.consecutive_failures = 0
        self.failure_history = deque(maxlen=config.failure_window)
        self.circuit_open = False
        self.disabled = False
        self._request_lock = threading.Lock()
        self._queue = queue.Queue(maxsize=32)
        self._worker = None
        self._closed = False
        self._warmups = set()
        # One remote consultation is enough for an unchanged decision
        # surface.  The key intentionally ignores transport-only sequence
        # fields, while the state_seq check below still lets an exact cache
        # hit behave as before.
        self._last_consultation = {}
        self.adaptive_suppressions = 0
        self._force_chat_transport = False
        if config.mode == "shadow" and self.client.available:
            self._start_worker()

    def profile(self):
        return {**self.config.profile(), **self.assembler.profile()}

    def _start_worker(self):
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="deepseek-macro-shadow",
            daemon=True,
        )
        self._worker.start()

    def _emit(self, result, *, decision_type=None, binding=None, warmup=False):
        if self.callback is None:
            return
        record = {
            "record_type": "cache_warmup" if warmup else "model_advice",
            "decision_type": decision_type,
            "binding": dict(binding or {}),
            "advisor": result.as_dict(),
            "advisor_profile": self.profile(),
        }
        try:
            self.callback(record)
        except Exception:
            # Auditing must not be able to interrupt gameplay.
            return

    def prewarm(self, character):
        if (
            self.config.mode == "off"
            or not self.config.cache_warmup
            or not self.client.available
            or self._closed
        ):
            return False
        models = {
            str(self.config.decision_policy(scope).model or self.config.model)
            for scope in self.config.assist_scopes
        }
        queued = False
        self._start_worker()
        for model in sorted(models):
            key = (
                model,
                self.assembler.prompt_hash,
                self.assembler.knowledge_hash,
                str(character),
            )
            if key in self._warmups:
                continue
            self._warmups.add(key)
            try:
                self._queue.put_nowait(
                    ("warmup", (str(character), model), None)
                )
                queued = True
            except queue.Full:
                break
        return queued

    def _worker_loop(self):
        while not self._closed:
            try:
                kind, payload, metadata = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if kind == "warmup":
                    if isinstance(payload, tuple):
                        self._execute_warmup(*payload)
                    else:
                        self._execute_warmup(payload)
                elif kind == "shadow":
                    package, meta = payload, metadata
                    result = self._execute(
                        package,
                        meta["rule_choice_id"],
                        meta["policy"],
                        meta["decision_type"],
                    )
                    if result.status == "recommended":
                        result.status = "shadow"
                    self._emit(
                        result,
                        decision_type=meta["decision_type"],
                        binding=meta["binding"],
                    )
            except Exception:
                # A malformed optional client/cache implementation must not
                # kill the sole shadow worker and strand queue.join forever.
                result = AdvisorResult(
                    status="fallback",
                    error_class="advisor_exception",
                    requested_model=self.config.model,
                )
                if kind == "warmup":
                    self._emit(result, warmup=True)
                elif kind == "shadow":
                    meta = metadata or {}
                    self._emit(
                        result,
                        decision_type=meta.get("decision_type"),
                        binding=meta.get("binding"),
                    )
            finally:
                self._queue.task_done()

    def _execute_warmup(self, character, model=None):
        model = str(model or self.config.model)
        transport = (
            "chat_json"
            if self.config.primary_transport == "chat_json"
            else "chat_json_fallback"
            if self._force_chat_transport
            else "responses"
        )
        with self._request_lock:
            blocker = self._budget_blocker(warmup=True)
            if blocker is not None:
                result = AdvisorResult(
                    status="fallback",
                    error_class=blocker,
                    requested_model=model,
                    transport=transport,
                )
            else:
                started = time.monotonic()
                try:
                    body = self.assembler.warmup_body(character, model=model)
                    self.warmup_calls += 1
                    if transport in {"chat_json", "chat_json_fallback"}:
                        if not hasattr(self.client, "request_json_fallback"):
                            raise DeepSeekRequestError("chat_json_unavailable")
                        response = self.client.request_json_fallback(
                            body,
                            min(
                                4.0,
                                self.config.max_total_wait_seconds
                                - self.total_wait_seconds,
                            ),
                        )
                    else:
                        response = self.client.request(
                            body,
                            min(
                                4.0,
                                self.config.max_total_wait_seconds
                                - self.total_wait_seconds,
                            ),
                        )
                    usage = _usage_summary(response)
                    elapsed = time.monotonic() - started
                    result = AdvisorResult(
                        status="ready",
                        latency_ms=int(elapsed * 1000),
                        requested_model=model,
                        response_model=response.get("model"),
                        system_fingerprint=response.get("system_fingerprint"),
                        usage=usage,
                        transport=transport,
                    )
                    self.total_cost_usd += float(
                        usage.get("estimated_cost_usd") or 0
                    )
                except Exception as exc:
                    elapsed = time.monotonic() - started
                    category = (
                        exc.category
                        if isinstance(exc, DeepSeekRequestError)
                        else "advisor_exception"
                    )
                    details = (
                        exc.safe_details()
                        if isinstance(exc, DeepSeekRequestError)
                        else {"exception_type": type(exc).__name__}
                    )
                    result = AdvisorResult(
                        status="fallback",
                        error_class=category,
                        error_details=details,
                        latency_ms=int(elapsed * 1000),
                        requested_model=model,
                        transport=transport,
                    )
                self.total_wait_seconds += elapsed
        self._emit(result, warmup=True)

    def _budget_blocker(self, decision_type=None, warmup=False):
        if self.disabled:
            return "advisor_disabled"
        if self.circuit_open:
            return "circuit_open"
        policy = self.config.decision_policy(decision_type)
        critical = bool(policy.critical) and not warmup
        if self.calls >= self.config.max_calls_per_run:
            return "call_budget"
        if self.total_wait_seconds >= self.config.max_total_wait_seconds:
            return "wait_budget"
        if self.total_cost_usd >= self.config.max_cost_per_run_usd:
            return "cost_budget"
        if not critical:
            call_reserve = min(
                self.config.critical_call_reserve,
                max(0, self.config.max_calls_per_run - 1),
            )
            wait_reserve = min(
                self.config.critical_wait_reserve_seconds,
                max(0.0, self.config.max_total_wait_seconds - 0.001),
            )
            cost_reserve = min(
                self.config.critical_cost_reserve_usd,
                max(0.0, self.config.max_cost_per_run_usd - 0.000001),
            )
            if (
                self.calls
                >= self.config.max_calls_per_run
                - call_reserve
            ):
                return "reserved_call_budget"
            if (
                self.total_wait_seconds
                >= self.config.max_total_wait_seconds
                - wait_reserve
            ):
                return "reserved_wait_budget"
            if (
                self.total_cost_usd
                >= self.config.max_cost_per_run_usd
                - cost_reserve
            ):
                return "reserved_cost_budget"
        return None

    def _within_budget(self, decision_type=None):
        return self._budget_blocker(decision_type) is None

    @staticmethod
    def _strip_consultation_volatility(value):
        """Remove audit/transport identifiers from a review-surface key."""

        volatile = {
            "state_seq", "decision_id", "decision_digest", "generated_at",
            "attempt_id", "run_id", "selection_id", "selection_digest",
            "advice_id", "snapshot_hash",
        }
        if isinstance(value, dict):
            return {
                str(key): DeepSeekMacroAdvisor._strip_consultation_volatility(item)
                for key, item in sorted(value.items())
                if str(key) not in volatile
            }
        if isinstance(value, list):
            return [
                DeepSeekMacroAdvisor._strip_consultation_volatility(item)
                for item in value
            ]
        return value

    @classmethod
    def _consultation_surface_key(
        cls, decision_type, character, state, candidates, rule_choice_id
    ):
        payload = {
            "decision_type": str(decision_type or "").upper(),
            "character": str(character or ""),
            "state": cls._strip_consultation_volatility(state or {}),
            "candidates": cls._strip_consultation_volatility(candidates or []),
            "rule_choice_id": str(rule_choice_id),
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _local_review_metrics(candidates, rule_choice_id):
        scores = {
            str(item.get("candidate_id")): float(item.get("local_score"))
            for item in candidates
            if isinstance(item, dict)
            and isinstance(item.get("local_score"), (int, float))
            and not isinstance(item.get("local_score"), bool)
            and math.isfinite(float(item.get("local_score")))
        }
        alternatives = [
            score for candidate_id, score in scores.items()
            if candidate_id != str(rule_choice_id)
        ]
        if str(rule_choice_id) not in scores or not alternatives:
            return None, False
        margin = scores[str(rule_choice_id)] - max(alternatives)
        return margin, bool(margin >= 0.0)

    @staticmethod
    def _explicit_review_required(state, candidates, hard_constraints):
        context = (state or {}).get("decision_context")
        context = context if isinstance(context, dict) else {}
        if any(
            context.get(key) is True
            for key in ("requires_model_review", "high_impact", "uncertain")
        ):
            return True
        if context.get("risk_flags"):
            return True
        if any(
            str(item.get("uncertainty") or "").strip()
            for item in candidates
            if isinstance(item, dict)
        ):
            return True
        return any(
            str(value).strip().upper() in {
                "REVIEW", "REQUIRE_MODEL_REVIEW", "HIGH_IMPACT",
            }
            for value in hard_constraints
        )

    def _adaptive_skip_reason(
        self, *, decision_type, policy, state, candidates,
        rule_choice_id, hard_constraints, binding,
    ):
        """Return a local-only skip reason for low-value repeat reviews."""

        if policy.critical or policy.always_consult:
            return None
        explicit_review = self._explicit_review_required(
            state, candidates, hard_constraints
        )
        signature = self._consultation_surface_key(
            decision_type, binding.get("character"), state,
            candidates, rule_choice_id,
        )
        previous = self._last_consultation.get(decision_type)
        current_seq = binding.get("state_seq")
        if (
            previous is not None
            and previous[0] == signature
            and previous[1] != current_seq
            and not explicit_review
        ):
            self.adaptive_suppressions += 1
            return "adaptive_repeat_surface"
        margin, nonnegative = self._local_review_metrics(
            candidates, rule_choice_id
        )
        if explicit_review or margin is None or not nonnegative:
            return None
        base = policy.consult_margin
        if base is None:
            base = policy.regret_cap
        try:
            # A truly dominant local choice has no useful remote information
            # value.  Apply this before the assist/shadow split: shadow mode
            # is still a billable remote consultation and should sample the
            # disputed boundary, not every obvious local win.  Critical and
            # explicitly risky surfaces returned above still bypass it.
            if float(base or 0.0) > 0.0 and margin >= float(base):
                self.adaptive_suppressions += 1
                return "deterministic_margin"
            # A two-option ordinary reward has much less information value
            # than a surface with several independently legal choices.  Keep
            # this adjustment bounded so candidate count can refine, but
            # never dominate, the per-type threshold.
            candidate_count = sum(
                1 for item in candidates if isinstance(item, dict)
            )
            complexity = min(
                1.35,
                1.0 + max(0, candidate_count - 2) * max(
                    0.0, float(policy.candidate_complexity_multiplier)
                ),
            )
            adaptive_margin = float(base or 0.0) * max(
                0.25, float(policy.adaptive_margin_multiplier)
            ) * complexity
        except (TypeError, ValueError):
            adaptive_margin = float(base or 0.0)
        if adaptive_margin > 0.0 and margin >= adaptive_margin:
            self.adaptive_suppressions += 1
            return "adaptive_local_confidence"
        return None

    def _remember_consultation(self, decision_type, signature, binding):
        self._last_consultation[decision_type] = (
            signature, binding.get("state_seq")
        )

    def _record_failure(self, error):
        self.failure_history.append(False)
        if error.category in {"auth", "account"}:
            self.disabled = True
        infrastructure_failure = (
            error.category in {"network_error", "timeout"}
            or (
                error.category == "http_error"
                and (error.status == 429 or int(error.status or 0) >= 500)
            )
        )
        if infrastructure_failure:
            self.consecutive_failures += 1
        else:
            # Content-contract failures do not combine with a later provider
            # timeout to disable every decision scope for the rest of a run.
            self.consecutive_failures = 0
        failure_ratio = 1.0 - (
            sum(self.failure_history) / max(1, len(self.failure_history))
        )
        if (
            infrastructure_failure
            and self.consecutive_failures >= self.config.max_consecutive_failures
        ):
            self.circuit_open = True
        if (
            len(self.failure_history) >= self.config.failure_window
            and failure_ratio > self.config.max_failure_ratio
        ):
            self.circuit_open = True

    def _record_success(self):
        self.consecutive_failures = 0
        self.failure_history.append(True)

    def _execute(self, package, rule_choice_id, policy, decision_type=None):
        requested_model = str(
            package.get("requested_model") or policy.model or self.config.model
        )
        fingerprint_release = (
            f"{self.config.expected_release}:{requested_model}"
        )
        try:
            cached = self.cache.get(package["advice_id"])
        except Exception:
            cached = None
        if cached is not None:
            try:
                advice = _validated_cached_advice(
                    cached, package, self.config.schema_version
                )
                response_model = cached.get("response_model")
                allowed_response_models = {
                    requested_model.lower(),
                    str(self.config.expected_release).lower(),
                }
                if (
                    not isinstance(response_model, str)
                    or not response_model.strip()
                    or response_model.lower() not in allowed_response_models
                ):
                    raise DeepSeekRequestError("invalid_local_cache")
                if not self.cache.verify_release_fingerprint(
                    fingerprint_release,
                    cached.get("system_fingerprint"),
                ):
                    raise DeepSeekRequestError("invalid_local_cache")
            except Exception:
                # A corrupt or stale cache entry is only a cache miss.  It must
                # never become an authoritative gameplay recommendation.
                cached = None
            else:
                return AdvisorResult(
                    status="recommended",
                    advice_id=package["advice_id"],
                    rule_choice_id=rule_choice_id,
                    model_choice_id=advice["choice_id"],
                    confidence=advice["confidence"],
                    rankings=advice["rankings"],
                    reason_codes=advice["reason_codes"],
                    rationale=advice["rationale"],
                    requested_model=requested_model,
                    response_model=response_model,
                    system_fingerprint=cached.get("system_fingerprint"),
                    # Provider tokens and cost were charged on the original
                    # request.  A disk hit is intentionally zero-usage.
                    usage={},
                    snapshot_hash=package["snapshot_hash"],
                    local_cache_hit=True,
                    transport=cached.get("transport"),
                )
        blocker = self._budget_blocker(decision_type)
        if blocker is not None:
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=blocker,
                requested_model=requested_model,
                snapshot_hash=package["snapshot_hash"],
            )
        started = time.monotonic()
        available_wait = max(
            0.0,
            self.config.max_total_wait_seconds - self.total_wait_seconds,
        )
        deadline = started + min(float(policy.timeout_seconds), available_wait)
        last_error = None
        response = None
        transport = None
        provider_wait = 0.0
        use_chat_transport = (
            self.config.primary_transport == "chat_json"
            or self._force_chat_transport
        )
        with self._request_lock:
            for attempt in range(self.config.retry_count + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = DeepSeekRequestError("timeout", retryable=True)
                    break
                attempt_blocker = self._budget_blocker(decision_type)
                if attempt_blocker is not None:
                    if last_error is None:
                        last_error = DeepSeekRequestError(attempt_blocker)
                    break
                try:
                    self.calls += 1
                    call_started = time.monotonic()
                    if use_chat_transport:
                        if not hasattr(self.client, "request_json_fallback"):
                            raise DeepSeekRequestError("chat_json_unavailable")
                        transport = (
                            "chat_json"
                            if self.config.primary_transport == "chat_json"
                            else "chat_json_fallback"
                        )
                        response = self.client.request_json_fallback(
                            package["body"], remaining
                        )
                    else:
                        transport = "responses"
                        response = self.client.request(
                            package["body"], remaining
                        )
                    provider_wait += time.monotonic() - call_started
                    break
                except DeepSeekRequestError as exc:
                    provider_wait += time.monotonic() - call_started
                    last_error = exc
                    if (
                        not use_chat_transport
                        and exc.status in {400, 422}
                        and hasattr(self.client, "request_json_fallback")
                    ):
                        # Once Responses rejects the schema contract, keep all
                        # retries for this decision on Chat JSON.  Re-probing
                        # the known-incompatible endpoint only burns deadline.
                        use_chat_transport = True
                        self._force_chat_transport = True
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            last_error = DeepSeekRequestError(
                                "timeout", retryable=True
                            )
                            break
                        if self._budget_blocker(decision_type) is not None:
                            break
                        try:
                            self.calls += 1
                            transport = "chat_json_fallback"
                            call_started = time.monotonic()
                            response = self.client.request_json_fallback(
                                package["body"], remaining
                            )
                            provider_wait += time.monotonic() - call_started
                            break
                        except DeepSeekRequestError as fallback_exc:
                            provider_wait += time.monotonic() - call_started
                            last_error = fallback_exc
                        except Exception:
                            provider_wait += time.monotonic() - call_started
                            last_error = DeepSeekRequestError(
                                "advisor_exception"
                            )
                    active_error = last_error or exc
                    if (
                        not active_error.retryable
                        or attempt >= self.config.retry_count
                    ):
                        break
                except Exception:
                    provider_wait += time.monotonic() - call_started
                    last_error = DeepSeekRequestError("advisor_exception")
                    break
        elapsed = time.monotonic() - started
        self.total_wait_seconds += provider_wait
        if response is None:
            error = last_error or DeepSeekRequestError("unknown_error")
            if error.category not in {
                "advisor_disabled", "circuit_open", "call_budget",
                "wait_budget", "cost_budget", "reserved_call_budget",
                "reserved_wait_budget", "reserved_cost_budget",
            }:
                self._record_failure(error)
            if (
                error.category == "http_error"
                and error.status in {400, 404, 422}
                and transport in {"chat_json", "chat_json_fallback"}
            ):
                # A Chat request/model contract rejected by the provider is
                # stable for this process; retrying it at every reward only
                # adds latency and cost without any new information.
                self.circuit_open = True
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                error_details=error.safe_details(),
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        # A syntactically or semantically invalid answer is still a billable
        # provider response and must consume the run cost budget exactly once.
        usage = _usage_summary(response)
        self.total_cost_usd += float(usage.get("estimated_cost_usd") or 0)
        response_model = response.get("model")
        allowed_response_models = {
            requested_model.lower(),
            str(self.config.expected_release).lower(),
        }
        if (
            not isinstance(response_model, str)
            or not response_model.strip()
            or response_model.lower() not in allowed_response_models
        ):
            error = DeepSeekRequestError("unexpected_response_model")
            self._record_failure(error)
            self.circuit_open = True
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                response_model=response_model,
                system_fingerprint=response.get("system_fingerprint"),
                usage=usage,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        adapter_error = response.get("adapter_error")
        if adapter_error:
            error = DeepSeekRequestError(str(adapter_error))
            self._record_failure(error)
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                response_model=response_model,
                system_fingerprint=response.get("system_fingerprint"),
                usage=usage,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        try:
            fingerprint_matches = self.cache.verify_release_fingerprint(
                fingerprint_release,
                response.get("system_fingerprint"),
            )
        except Exception:
            error = DeepSeekRequestError("fingerprint_store_error")
            self._record_failure(error)
            self.circuit_open = True
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                response_model=response_model,
                system_fingerprint=response.get("system_fingerprint"),
                usage=usage,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        if not fingerprint_matches:
            error = DeepSeekRequestError("model_fingerprint_changed")
            self._record_failure(error)
            self.circuit_open = True
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                response_model=response_model,
                system_fingerprint=response.get("system_fingerprint"),
                usage=usage,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        try:
            advice = _validated_advice(
                response, package, self.config.schema_version
            )
        except DeepSeekRequestError as error:
            self._record_failure(error)
            return AdvisorResult(
                status="fallback",
                advice_id=package["advice_id"],
                rule_choice_id=rule_choice_id,
                error_class=error.category,
                latency_ms=int(elapsed * 1000),
                requested_model=requested_model,
                response_model=response.get("model"),
                system_fingerprint=response.get("system_fingerprint"),
                usage=usage,
                snapshot_hash=package["snapshot_hash"],
                transport=transport,
            )
        self._record_success()
        stored = {
            **advice,
            "cache_schema_version": LOCAL_CACHE_SCHEMA_VERSION,
            "schema_version": self.config.schema_version,
            "advice_id": package["advice_id"],
            "response_model": response.get("model"),
            "system_fingerprint": response.get("system_fingerprint"),
            "usage": usage,
            "transport": transport,
        }
        try:
            self.cache.put(package["advice_id"], stored)
        except (OSError, TypeError, ValueError):
            # A cache write failure must not discard a valid in-memory answer.
            pass
        return AdvisorResult(
            status="recommended",
            advice_id=package["advice_id"],
            rule_choice_id=rule_choice_id,
            model_choice_id=advice["choice_id"],
            confidence=advice["confidence"],
            rankings=advice["rankings"],
            reason_codes=advice["reason_codes"],
            rationale=advice["rationale"],
            latency_ms=int(elapsed * 1000),
            requested_model=requested_model,
            response_model=response.get("model"),
            system_fingerprint=response.get("system_fingerprint"),
            usage=usage,
            snapshot_hash=package["snapshot_hash"],
            transport=transport,
        )

    def advise(
        self,
        *,
        decision_type,
        character,
        state,
        candidates,
        rule_choice_id,
        hard_constraints=None,
        binding=None,
    ):
        decision_type = str(decision_type or "").upper()
        policy = self.config.decision_policy(decision_type)
        requested_model = str(policy.model or self.config.model)
        hard_constraints = list(hard_constraints or [])
        binding = dict(binding or {})
        rule_choice_id = str(rule_choice_id)
        candidate_ids = [str(item.get("candidate_id")) for item in candidates]
        if rule_choice_id not in candidate_ids:
            return AdvisorResult(
                status="fallback",
                rule_choice_id=rule_choice_id,
                error_class="rule_choice_not_candidate",
                requested_model=requested_model,
            )
        if self.config.mode == "off":
            return AdvisorResult(
                status="skipped",
                rule_choice_id=rule_choice_id,
                error_class="advisor_off",
                requested_model=requested_model,
            )
        if (
            self.config.mode == "assist"
            and decision_type not in set(self.config.assist_scopes)
        ):
            return AdvisorResult(
                status="skipped",
                rule_choice_id=rule_choice_id,
                error_class="scope_disabled",
                requested_model=requested_model,
            )
        if not self.client.available:
            return AdvisorResult(
                status="fallback",
                rule_choice_id=rule_choice_id,
                error_class="missing_api_key",
                requested_model=requested_model,
            )
        if len(candidate_ids) < 2:
            return AdvisorResult(
                status="skipped",
                rule_choice_id=rule_choice_id,
                error_class="single_candidate",
                requested_model=requested_model,
            )
        if (
            decision_type == "GRID"
            and int(((state or {}).get("decision_context") or {}).get(
                "selection_count", 1
            ) or 1) > 1
        ):
            return AdvisorResult(
                status="skipped",
                rule_choice_id=rule_choice_id,
                error_class="multi_select_local_only",
                requested_model=requested_model,
            )
        adaptive_reason = self._adaptive_skip_reason(
            decision_type=decision_type,
            policy=policy,
            state=state,
            candidates=candidates,
            rule_choice_id=rule_choice_id,
            hard_constraints=hard_constraints,
            binding=binding,
        )
        if adaptive_reason is not None:
            return AdvisorResult(
                status="skipped",
                rule_choice_id=rule_choice_id,
                error_class=adaptive_reason,
                requested_model=requested_model,
            )
        try:
            package = self.assembler.build(
                decision_type=decision_type,
                character=character,
                state=state,
                candidates=candidates,
                rule_choice_id=rule_choice_id,
                hard_constraints=hard_constraints,
                binding=binding,
                effort=policy.effort,
                max_output_tokens=policy.max_output_tokens,
                model=requested_model,
                temperature=policy.temperature,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return AdvisorResult(
                status="fallback",
                rule_choice_id=rule_choice_id,
                error_class=type(exc).__name__,
                requested_model=requested_model,
            )
        if self.config.mode == "shadow":
            self._start_worker()
            metadata = {
                "rule_choice_id": rule_choice_id,
                "policy": policy,
                "decision_type": decision_type,
                "binding": binding,
            }
            try:
                self._queue.put_nowait(("shadow", package, metadata))
                self._remember_consultation(
                    decision_type,
                    self._consultation_surface_key(
                        decision_type, binding.get("character"), state,
                        candidates, rule_choice_id,
                    ),
                    binding,
                )
                return AdvisorResult(
                    status="shadow_queued",
                    advice_id=package["advice_id"],
                    rule_choice_id=rule_choice_id,
                    requested_model=requested_model,
                    snapshot_hash=package["snapshot_hash"],
                )
            except queue.Full:
                return AdvisorResult(
                    status="fallback",
                    advice_id=package["advice_id"],
                    rule_choice_id=rule_choice_id,
                    error_class="shadow_queue_full",
                    requested_model=requested_model,
                    snapshot_hash=package["snapshot_hash"],
                )
        result = self._execute(package, rule_choice_id, policy, decision_type)
        self._remember_consultation(
            decision_type,
            self._consultation_surface_key(
                decision_type, binding.get("character"), state,
                candidates, rule_choice_id,
            ),
            binding,
        )
        return result

    def close(self, wait=False):
        if wait and self._worker is not None:
            try:
                self._queue.join()
            except Exception:
                pass
        self._closed = True
