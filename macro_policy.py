"""Versioned configuration for the optional DeepSeek macro adviser.

The policy is deliberately environment-selectable but secret-free.  Every
field that can change a gameplay decision is exposed through ``profile`` so
autoplay can bind it into the decision fingerprint.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path


VALID_MODES = {"off", "shadow", "assist"}
VALID_TRANSPORTS = {"responses", "chat_json"}
LOCAL_ENV_KEYS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE",
    "DEEPSEEK_PROXY_URL",
    "DEEPSEEK_API_TRANSPORT",
    "DEEPSEEK_CACHE_WARMUP",
    "STS_MACRO_ADVISOR_MODE",
    "STS_MACRO_ADVISOR_SCOPES",
})


def load_local_env(path):
    """Load the project's secret-free settings and API key without logging it.

    Existing process environment values always win.  Keeping the allowlist
    small prevents an accidentally copied ``.env`` from changing unrelated
    process behaviour.
    """

    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError(f"local environment file cannot be read: {path}") from exc

    loaded = []
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(
                f"invalid local environment entry at {path}:{line_number}"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in LOCAL_ENV_KEYS:
            raise ValueError(
                f"unsupported local environment key at {path}:{line_number}: {key}"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


@dataclass(frozen=True)
class DecisionPolicy:
    confidence: float
    regret_cap: float
    bonus_cap: float
    effort: str
    timeout_seconds: float
    max_output_tokens: int
    always_consult: bool = False
    min_model_margin: float = 12.0
    direct_confidence: float = 0.85
    direct_model_margin: float = 20.0
    model: str | None = None
    temperature: float | None = 0.0
    consult_margin: float | None = None
    critical: bool = False
    # A small adaptive band avoids paying for remote calls when the local
    # ranking is already comfortably ahead, while still consulting on close
    # calls and explicit uncertainty.  Critical scopes bypass this band.
    adaptive_margin_multiplier: float = 0.75
    # Each additional legal option makes a genuinely close local ranking
    # harder to trust.  This expands the adaptive review band only for that
    # decision type; explicit risk still bypasses the band entirely.
    candidate_complexity_multiplier: float = 0.0


LATEST_MODEL = "deepseek-v4-flash"


DEFAULT_DECISION_POLICIES = {
    # Every scope uses the same official 0731 model. Ordinary, reversible
    # choices disable thinking. Although the provider supports high thinking,
    # live 0731 Flash probes exceeded 35 seconds on compact two/three-option
    # decisions, so reliable bounded none-thinking beats repeated fallback.
    "CARD_REWARD": DecisionPolicy(
        0.70, 12.0, 12.0, "none", 8.0, 1536,
        model=LATEST_MODEL, temperature=0.0, consult_margin=10.0,
        adaptive_margin_multiplier=0.45,
        candidate_complexity_multiplier=0.10,
    ),
    "BOSS_RELIC": DecisionPolicy(
        0.75, 18.0, 18.0, "none", 10.0, 2048, True,
        model=LATEST_MODEL, temperature=0.0, critical=True,
    ),
    "SHOP": DecisionPolicy(
        0.78, 10.0, 10.0, "none", 8.0, 2048,
        model=LATEST_MODEL, temperature=0.0, consult_margin=5.0,
        adaptive_margin_multiplier=0.50,
        candidate_complexity_multiplier=0.10,
    ),
    "SAPPHIRE_KEY": DecisionPolicy(
        0.80, 10.0, 8.0, "none", 10.0, 2048,
        model=LATEST_MODEL, temperature=0.0, consult_margin=5.0, critical=True,
    ),
    "EVENT": DecisionPolicy(
        0.80, 8.0, 8.0, "none", 6.0, 1536,
        model=LATEST_MODEL, temperature=0.0, consult_margin=4.0,
    ),
    "MAP": DecisionPolicy(
        0.80, 12.0, 12.0, "none", 8.0, 2048,
        model=LATEST_MODEL, temperature=0.0, consult_margin=5.0,
    ),
    "CAMPFIRE": DecisionPolicy(
        0.70, 8.0, 8.0, "none", 8.0, 2048, True,
        model=LATEST_MODEL, temperature=0.0,
    ),
    "GRID": DecisionPolicy(
        0.78, 10.0, 10.0, "none", 6.0, 1536,
        model=LATEST_MODEL, temperature=0.0, consult_margin=4.0,
        adaptive_margin_multiplier=0.50,
        candidate_complexity_multiplier=0.10,
    ),
    "NEOW": DecisionPolicy(
        0.80, 12.0, 12.0, "none", 8.0, 2048, True,
        model=LATEST_MODEL, temperature=0.0,
    ),
    "RUN_PLAN": DecisionPolicy(
        0.70, 14.0, 12.0, "none", 8.0, 2048, True,
        model=LATEST_MODEL, temperature=0.0, critical=True,
    ),
}


@dataclass(frozen=True)
class MacroPolicyConfig:
    mode: str = "off"
    base_url: str = "https://api.deepseek.com"
    # Operational routing only: excluded from repr/profile so proxy credentials
    # cannot enter logs or decision fingerprints.
    proxy_url: str | None = field(default=None, repr=False, compare=False)
    primary_transport: str = "responses"
    model: str = LATEST_MODEL
    expected_release: str = "DeepSeek-V4-Flash-0731"
    advisor_revision: str = "deepseek-macro-v16"
    prompt_version: str = "sts-macro-v1"
    schema_version: str = "sts-advice-v1"
    snapshot_schema_version: str = "sts-snapshot-v6"
    cache_layout_version: str = "prefix-layout-v9"
    consult_scheduler_version: str = "adaptive-consult-v3"
    knowledge_version: str = "sts-basegame-v1"
    cache_warmup: bool = True
    max_calls_per_run: int = 30
    max_total_wait_seconds: float = 120.0
    max_cost_per_run_usd: float = 0.05
    critical_call_reserve: int = 4
    critical_wait_reserve_seconds: float = 12.0
    critical_cost_reserve_usd: float = 0.01
    max_input_characters: int = 60_000
    max_response_bytes: int = 65_536
    failure_window: int = 20
    max_consecutive_failures: int = 3
    max_failure_ratio: float = 0.20
    retry_count: int = 1
    assist_scopes: tuple = (
        "CARD_REWARD", "BOSS_RELIC", "SHOP", "SAPPHIRE_KEY", "EVENT",
        "MAP", "CAMPFIRE", "GRID", "NEOW", "RUN_PLAN",
    )
    decision_policies: dict = field(
        default_factory=lambda: dict(DEFAULT_DECISION_POLICIES)
    )

    @classmethod
    def from_env(cls):
        mode = str(os.environ.get("STS_MACRO_ADVISOR_MODE", "off")).strip().lower()
        if mode not in VALID_MODES:
            raise ValueError(
                "STS_MACRO_ADVISOR_MODE must be one of off, shadow, assist"
            )
        warmup_value = str(
            os.environ.get("DEEPSEEK_CACHE_WARMUP", "true")
        ).strip().lower()
        raw_scopes = str(
            os.environ.get(
                "STS_MACRO_ADVISOR_SCOPES",
                "CARD_REWARD,BOSS_RELIC,SHOP,SAPPHIRE_KEY,EVENT,MAP,"
                "CAMPFIRE,GRID,NEOW,RUN_PLAN",
            )
        ).strip()
        if raw_scopes.lower() == "all":
            assist_scopes = tuple(sorted(DEFAULT_DECISION_POLICIES))
        else:
            assist_scopes = tuple(
                item.strip().upper()
                for item in raw_scopes.split(",")
                if item.strip()
            )
        unknown_scopes = set(assist_scopes) - set(DEFAULT_DECISION_POLICIES)
        if unknown_scopes:
            raise ValueError(
                "unknown STS_MACRO_ADVISOR_SCOPES: "
                + ", ".join(sorted(unknown_scopes))
            )
        primary_transport = str(
            os.environ.get("DEEPSEEK_API_TRANSPORT", "chat_json")
        ).strip().lower()
        if primary_transport not in VALID_TRANSPORTS:
            raise ValueError(
                "DEEPSEEK_API_TRANSPORT must be one of responses, chat_json"
            )
        proxy_url = str(os.environ.get("DEEPSEEK_PROXY_URL", "")).strip()
        if proxy_url:
            try:
                parsed_proxy = urllib.parse.urlsplit(proxy_url)
                # Accessing ``port`` also validates malformed/non-numeric ports.
                parsed_proxy.port
            except ValueError:
                raise ValueError(
                    "DEEPSEEK_PROXY_URL must be a valid HTTP(S) proxy URL"
                ) from None
            if (
                parsed_proxy.scheme.lower() not in {"http", "https"}
                or not parsed_proxy.hostname
                or parsed_proxy.path not in {"", "/"}
                or parsed_proxy.query
                or parsed_proxy.fragment
            ):
                raise ValueError(
                    "DEEPSEEK_PROXY_URL must be a valid HTTP(S) proxy URL"
                )
        return cls(
            mode=mode,
            base_url=str(
                os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
            ).rstrip("/"),
            proxy_url=proxy_url or None,
            primary_transport=primary_transport,
            cache_warmup=warmup_value not in {"0", "false", "no", "off"},
            assist_scopes=assist_scopes,
        )

    def decision_policy(self, decision_type):
        return self.decision_policies.get(
            str(decision_type or "").upper(),
            DecisionPolicy(0.85, 0.0, 0.0, "none", 4.0, 512),
        )

    def profile(self):
        return {
            "mode": self.mode,
            "base_url": self.base_url,
            "primary_transport": self.primary_transport,
            "model": self.model,
            "expected_release": self.expected_release,
            "advisor_revision": self.advisor_revision,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "snapshot_schema_version": self.snapshot_schema_version,
            "cache_layout_version": self.cache_layout_version,
            "consult_scheduler_version": self.consult_scheduler_version,
            "knowledge_version": self.knowledge_version,
            "cache_warmup": self.cache_warmup,
            "max_calls_per_run": self.max_calls_per_run,
            "max_total_wait_seconds": self.max_total_wait_seconds,
            "max_cost_per_run_usd": self.max_cost_per_run_usd,
            "critical_call_reserve": self.critical_call_reserve,
            "critical_wait_reserve_seconds": self.critical_wait_reserve_seconds,
            "critical_cost_reserve_usd": self.critical_cost_reserve_usd,
            "max_input_characters": self.max_input_characters,
            "max_response_bytes": self.max_response_bytes,
            "failure_window": self.failure_window,
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_failure_ratio": self.max_failure_ratio,
            "retry_count": self.retry_count,
            "assist_scopes": list(self.assist_scopes),
            "decision_policies": {
                name: {
                    "confidence": policy.confidence,
                    "regret_cap": policy.regret_cap,
                    "bonus_cap": policy.bonus_cap,
                    "effort": policy.effort,
                    "timeout_seconds": policy.timeout_seconds,
                    "max_output_tokens": policy.max_output_tokens,
                    "always_consult": policy.always_consult,
                    "min_model_margin": policy.min_model_margin,
                    "direct_confidence": policy.direct_confidence,
                    "direct_model_margin": policy.direct_model_margin,
                    "model": policy.model,
                    "temperature": policy.temperature,
                    "consult_margin": policy.consult_margin,
                    "critical": policy.critical,
                    "adaptive_margin_multiplier": policy.adaptive_margin_multiplier,
                    "candidate_complexity_multiplier": (
                        policy.candidate_complexity_multiplier
                    ),
                }
                for name, policy in sorted(self.decision_policies.items())
            },
        }
