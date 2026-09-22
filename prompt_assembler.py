"""Cache-aware prompt construction for Slay the Spire macro advice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from macro_wire import encode_candidate_catalog, wire_schema


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _ordered_compact(value):
    """Serialize provider input without destroying intentional key order."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _digest_bytes(*chunks):
    digest = hashlib.sha256()
    for chunk in chunks:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        digest.update(chunk)
    return digest.hexdigest()


class PromptAssembler:
    """Compile stable-prefix messages and one volatile decision suffix.

    The message boundary after the role pack is intentional.  A warm-up input
    can end at exactly that boundary, allowing DeepSeek's prefix cache to
    persist the reusable portion without putting volatile run identifiers in
    front of it.
    """

    ROLE_FILES = {
        "IRONCLAD": "ironclad.json",
        "THE_SILENT": "silent.json",
        "DEFECT": "defect.json",
    }

    # Keep only genuinely slow-changing facts in the provider-cache prefix.
    # Act/boss/key progress and planning state are decision context: they can
    # change at a route transition and therefore invalidate every later
    # prefix if placed here.  Deck facts and the relic catalog change much
    # less often and are the useful reusable part of adjacent calls.
    STABLE_STATE_KEYS = (
        "ascension", "relic_catalog", "deck_card_facts", "deck",
    )

    def __init__(self, root, config):
        self.root = Path(root)
        self.config = config
        prompt_root = self.root / "prompts" / config.prompt_version.replace("-", "_")
        knowledge_root = (
            self.root / "knowledge" / config.knowledge_version.replace("-", "_")
        )
        self.global_prompt = (prompt_root / "global.txt").read_text(
            encoding="utf-8"
        ).strip()
        self.decision_policy = json.loads(
            (prompt_root / "decision_policy.json").read_text(encoding="utf-8")
        )
        self.role_knowledge = {
            role: json.loads(
                (prompt_root / "roles" / filename).read_text(encoding="utf-8")
            )
            for role, filename in self.ROLE_FILES.items()
        }
        self.game_knowledge = {
            name: json.loads(
                (knowledge_root / f"{name}.json").read_text(encoding="utf-8")
            )
            for name in ("cards", "relics", "events", "bosses")
        }
        common_text = _canonical({
            "prompt_version": config.prompt_version,
            "schema_version": config.schema_version,
            "snapshot_schema_version": config.snapshot_schema_version,
            "cache_layout_version": config.cache_layout_version,
            "goal": "DEFEAT_HEART",
            "wire_schema": wire_schema(),
            "decision_policy": self.decision_policy,
            "game_knowledge": self.game_knowledge,
        })
        self._common_message = "STATIC_POLICY_AND_KNOWLEDGE\n" + common_text
        self._output_contract_message = (
            "STATIC_OUTPUT_CONTRACT\n"
            "Return exactly one JSON object with these fields and no others: "
            "schema_version, advice_id, choice_id, confidence, rankings, "
            "reason_codes, rationale. schema_version must equal the supplied "
            "schema_version. advice_id must exactly equal binding.advice_id. "
            "choice_id must be one supplied candidates[].candidate_id and must "
            "tie for the highest rankings score. rankings must contain every "
            "supplied candidate_id exactly once with no duplicates; each score "
            "must be a finite number from 0 through 100, higher is better. "
            "Copy candidate_id values verbatim; never substitute a card name, "
            "relic name, label, fact_ref, or display text for an ID. "
            "confidence must be a finite number from 0.0 through 1.0. "
            "reason_codes must be unique and drawn only from the supplied "
            "decision_policy reason_codes. rationale must be a non-empty "
            "concise string grounded in the supplied state. Before returning, "
            "verify rankings contains exactly output_guard.ranking_count "
            "entries and exactly the candidate_id values in candidates. Do not emit "
            "markdown, comments, extra keys, or hidden reasoning."
        )
        self.prompt_hash = _digest_bytes(
            self.global_prompt,
            self._common_message,
            self._output_contract_message,
        )[:16]
        self.knowledge_hash = _digest_bytes(
            _canonical(self.game_knowledge),
            _canonical(self.role_knowledge),
        )[:16]

    def profile(self):
        return {
            "prompt_hash": self.prompt_hash,
            "knowledge_hash": self.knowledge_hash,
            "cache_layout_version": self.config.cache_layout_version,
        }

    def _role_message(self, character):
        character = str(character or "").upper()
        if character not in self.role_knowledge:
            raise ValueError(f"unsupported macro-adviser character: {character}")
        return "STATIC_CHARACTER_KNOWLEDGE\n" + _canonical(
            self.role_knowledge[character]
        )

    def stable_input(self, character):
        return [
            {"role": "system", "content": self.global_prompt},
            {"role": "user", "content": self._common_message},
            {"role": "user", "content": self._role_message(character)},
            {"role": "user", "content": self._output_contract_message},
        ]

    def snapshot_hash(
        self,
        decision_type,
        character,
        state,
        candidates,
        rule_choice_id,
        hard_constraints,
    ):
        return _digest_bytes(_canonical({
            "snapshot_schema_version": self.config.snapshot_schema_version,
            "decision_type": decision_type,
            "character": str(character),
            "state": state,
            "candidates": candidates,
            "rule_choice_id": str(rule_choice_id),
            "hard_constraints": hard_constraints,
        }))[:24]

    def advice_id(self, binding, snapshot_hash):
        return _digest_bytes(_canonical({
            "attempt_id": binding.get("attempt_id"),
            "run_id": binding.get("run_id"),
            "state_seq": binding.get("state_seq"),
            "decision_id": binding.get("decision_id"),
            "phase": binding.get("phase"),
            "request_state_hash": snapshot_hash,
            "advisor_revision": self.config.advisor_revision,
            "prompt_hash": self.prompt_hash,
            "knowledge_hash": self.knowledge_hash,
            "expected_release": self.config.expected_release,
        }))[:32]

    def build(
        self,
        *,
        decision_type,
        character,
        state,
        candidates,
        rule_choice_id,
        hard_constraints,
        binding,
        effort,
        max_output_tokens,
        model=None,
        temperature=None,
    ):
        candidate_ids = [str(item["candidate_id"]) for item in candidates]
        if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("macro candidates must have unique candidate_id values")
        snapshot_hash = self.snapshot_hash(
            decision_type,
            character,
            state,
            candidates,
            rule_choice_id,
            hard_constraints,
        )
        advice_id = self.advice_id(binding, snapshot_hash)
        candidate_fact_catalog, visible_candidates = encode_candidate_catalog(
            candidates
        )

        # Insertion order is the cache layout. Slowly changing run state gets
        # its own message before the volatile suffix: adjacent reward, event,
        # shop, grid and relic decisions can then reuse the same deck/relic
        # prefix even when HP, gold, hand facts or candidates change.
        state = dict(state or {})
        stable_state = {
            key: state[key]
            for key in self.STABLE_STATE_KEYS
            if key in state
        }
        volatile_state = {
            key: value
            for key, value in state.items()
            if key not in stable_state
        }
        dynamic = {
            "state": state,
            "decision_type": str(decision_type),
            "hard_constraints": list(hard_constraints or []),
            "candidate_fact_catalog": candidate_fact_catalog,
            "candidates": visible_candidates,
            "output_guard": {
                "ranking_count": len(candidate_ids),
            },
            "binding": {
                "advice_id": advice_id,
            },
        }
        dynamic_text = json.dumps(
            {
                **dynamic,
                "state": volatile_state,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(dynamic_text) > self.config.max_input_characters:
            raise ValueError("macro decision snapshot exceeds configured input limit")

        reason_codes = list(self.decision_policy.get("reason_codes") or [])
        response_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schema_version", "advice_id", "choice_id", "confidence",
                "rankings", "reason_codes", "rationale",
            ],
            "properties": {
                "schema_version": {"type": "string", "enum": [self.config.schema_version]},
                "advice_id": {"type": "string", "enum": [advice_id]},
                "choice_id": {"type": "string", "enum": candidate_ids},
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Calibrated probability from 0.0 to 1.0 that choice_id "
                        "is the best legal candidate."
                    ),
                },
                "rankings": {
                    "type": "array",
                    "description": (
                        "Every supplied candidate_id must appear exactly once; "
                        "candidate_id values must not repeat."
                    ),
                    "minItems": len(candidate_ids),
                    "maxItems": len(candidate_ids),
                    "uniqueItems": True,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["candidate_id", "score"],
                        "properties": {
                            "candidate_id": {
                                "type": "string",
                                "enum": candidate_ids,
                                "description": (
                                    "A supplied candidate_id. Each id must appear "
                                    "exactly once in rankings."
                                ),
                            },
                            "score": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 100.0,
                                "description": (
                                    "Normalized strategic score from 0 to 100; "
                                    "higher is better."
                                ),
                            },
                        },
                    },
                },
                "reason_codes": {
                    "type": "array",
                    "description": (
                        "Use only the allowed enum values; do not invent or "
                        "repeat reason codes."
                    ),
                    "maxItems": 6,
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": reason_codes},
                },
                "rationale": {"type": "string"},
            },
        }
        requested_model = str(model or self.config.model)
        stable_state_text = (
            "STATE_STABLE_CONTEXT\n"
            + _ordered_compact(stable_state)
        )
        body = {
            "model": requested_model,
            "input": self.stable_input(character)
            + [{"role": "user", "content": stable_state_text}]
            + [{"role": "user", "content": dynamic_text}],
            "reasoning": {"effort": effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sts_macro_advice",
                    "strict": True,
                    "schema": response_schema,
                }
            },
            "max_output_tokens": int(max_output_tokens),
        }
        if effort == "none" and temperature is not None:
            body["temperature"] = float(temperature)
        return {
            "advice_id": advice_id,
            "snapshot_hash": snapshot_hash,
            "candidate_ids": candidate_ids,
            "reason_codes": reason_codes,
            "dynamic": dynamic,
            "stable_state": stable_state,
            "body": body,
            "requested_model": requested_model,
        }

    def warmup_body(self, character, model=None):
        return {
            "model": str(model or self.config.model),
            # The warm-up must end on the exact reusable prefix boundary.  A
            # synthetic tail would make the first live decision diverge before
            # its volatile suffix and defeat the purpose of the request.
            "input": self.stable_input(character),
            "reasoning": {"effort": "none"},
            # Chat JSON mode can add provider-side formatting context before
            # the visible messages.  The warm-up must request that same mode
            # or its otherwise identical visible prefix will not seed the
            # first live decision.  Responses transport also accepts this
            # deliberately generic object schema; warm-up output is ignored.
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sts_macro_warmup",
                    "strict": False,
                    "schema": {"type": "object"},
                }
            },
            "max_output_tokens": 1,
            "temperature": 0.0,
        }
