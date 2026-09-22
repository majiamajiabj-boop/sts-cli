"""Read-only audit of schema-v2 autonomous decision traces."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path


try:
    import independent_oracle as _independent_oracle
except Exception as _independent_oracle_exc:  # pragma: no cover - exercised via patch
    _independent_oracle = None
    _INDEPENDENT_ORACLE_IMPORT_ERROR = repr(_independent_oracle_exc)
else:
    _INDEPENDENT_ORACLE_IMPORT_ERROR = None

_EXPECTED_INDEPENDENT_ORACLE_VERSION = getattr(
    _independent_oracle, "ORACLE_VERSION", None
)
_EXPECTED_ORACLE_COVERAGE_CONTRACT_VERSION = getattr(
    _independent_oracle, "ORACLE_COVERAGE_CONTRACT_VERSION", None
)
_EXPECTED_ORACLE_COVERAGE_KEYS = frozenset(getattr(
    _independent_oracle, "REQUIRED_COVERAGE_KEYS", set()
))
_EXPECTED_ORACLE_BINDING_FIELDS = tuple(getattr(
    _independent_oracle, "ATTEMPT_BINDING_FIELDS", ()
)) + ("terminal_state_seq",)
_EXPECTED_ORACLE_DISAGREEMENT_KINDS = frozenset(getattr(
    _independent_oracle, "DISAGREEMENT_KINDS", set()
))
_EXPECTED_BASE_GAME_MECHANISM_CONTRACT_VERSION = getattr(
    _independent_oracle, "BASE_GAME_MECHANISM_CONTRACT_VERSION", None
)

try:
    import death_replay as _death_replay
except Exception as _death_replay_exc:  # pragma: no cover - exercised via patch
    _death_replay = None
    _DEATH_REPLAY_IMPORT_ERROR = repr(_death_replay_exc)
else:
    _DEATH_REPLAY_IMPORT_ERROR = None


_SPIRE_ROOT = Path(__file__).resolve().parent / "src" / "spirecomm-master"
if str(_SPIRE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SPIRE_ROOT))
from spirecomm.ai import combat_predictor as _combat_predictor


def audit_engine_sha256():
    """Return the digest of the exact audit implementation in use.

    Audit reports are persisted beside the attempt they describe.  Without
    binding the report to this source, a later run can silently reuse a
    structurally valid report produced before a new detector was added (which
    is precisely how the old F39 card-play misses survived).  Hash only this
    module, never the trace; callers can therefore validate provenance without
    reopening an autoplay log.
    """

    digest = hashlib.sha256()
    with Path(__file__).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ALLOWED_DOOMED_TARGET_CARDS = {
    "Alchemize", "Bandage Up", "Bite", "Corpse Explosion", "Feed",
    "Genetic Algorithm", "HandOfGreed", "Lesson Learned", "Reaper",
    "RitualDagger", "Self Repair",
}

_SPLIT_MONSTER_IDS = {"slimeboss", "acidslimel", "spikeslimel"}
_DETERMINISTIC_ATTACK_HITS = {
    "daggerspray": 2,
    "glassknife": 2,
    "twinstrike": 2,
    "riddlewithholes": 5,
    "pummel": 4,
}

_DIRECT_DRAW_CARD_IDS = {
    "acrobatics", "backflip", "battletrance", "burningpact",
    "calculatedgamble", "compiledriver", "coolheaded", "dropkick",
    "escapeplan", "finesse", "flashofsteel", "offering", "pommelstrike",
    "prepared", "quickslash", "reboot", "skim", "shrugitoff",
}
_SUNDIAL_ACTION_CARD_IDS = _DIRECT_DRAW_CARD_IDS | {
    "adrenaline", "daggerthrow", "deepbreath", "expertise",
    "masterofstrategy", "overclock", "steampower", "sweepingbeam",
    "warcry",
}

# These cards have no useful branch effect without an occupied orb.  Keep an
# independent copy in the auditor so a stale terminal-plan continuation cannot
# silently pass just because the live planner once had an orb in an earlier
# frame.
_ORB_REQUIRED_CARD_IDS = {
    "barrage", "consume", "dualcast", "fission", "multicast",
    "recursion", "redo",
}

# Combat turns are intentionally not promoted to the compact non-combat
# DecisionCase corpus: a full turn contains many state transitions and cannot
# be safely replayed by the macro-choice resolver.  They still need a first
# class audit channel, however.  Keep the channel's taxonomy explicit so a
# run report cannot look "clean" merely because all of its replay cases were
# non-combat choices.
_COMBAT_STRATEGY_ISSUE_KINDS = frozenset({
    "affordable_attack_progress_ignored",
    "missed_shelled_parasite_progress",
    "choker_slots_without_attack_progress",
    "unconsumed_double_tap_setup",
    "false_combat_end_prediction",
    "terminal_plan_missing_orb_resource",
    "redundant_covered_block_play",
    "low_value_nob_enrage_skill",
    "avoidable_loss_end_turn_candidate",
    "end_turn_with_resources",
    "card_damage_overprediction",
    "card_damage_underprediction",
    "end_turn_damage_overprediction",
    "end_turn_damage_underprediction",
    "fairy_revival_prediction_mismatch",
    "turn_loss_component_mismatch",
    "next_turn_start_damage_underprediction",
    "late_attack_setup",
    "offering_at_choker_limit",
    "avoidable_voluntary_self_damage",
    "avoidable_smoke_bomb_omission",
    "missed_long_fight_scaling_setup",
    "missed_thousand_cuts_trigger",
    "passive_doom_claim_unproven",
    "forced_damage_source_exhaustion",
    "impossible_post_forced_exhaust_plan",
    "redundant_doomed_target",
    "time_eater_safe_reset_missed",
    "combat_target_binding_rejection",
    "barricade_liveness_stall",
    "persistent_attrition_race_missed",
})
_UNPROVEN_RANDOM_POTION_IDS = {
    "attackpotion", "colorlesspotion", "entropicbrew", "gamblersbrew",
    "powerpotion", "skillpotion", "swiftpotion",
}

# Potions with a combat effect that can plausibly change survival, tempo, or
# a long-fight engine.  Terminal frames serialize every potion as unusable
# after death, so auditing must key on identity rather than terminal can_use.
# Fairy is intentionally absent: retaining it at zero HP is its normal trigger
# path, and Elixir/empty slots are not generally actionable survival evidence.
_ACTIONABLE_TERMINAL_POTION_IDS = {
    "attackpotion", "blessingoftheforge", "blockpotion", "bloodpotion",
    "colorlesspotion", "cultistpotion", "cunningpotion",
    "dexteritypotion", "distilledchaos", "duplicationpotion",
    "energypotion", "entropicbrew", "essenceofdarkness",
    "essenceofsteel", "explosivepotion", "fearpotion", "firepotion",
    "focuspotion", "fruitjuice", "gamblersbrew", "ghostinajar",
    "heartofiron", "liquidbronze", "liquidmemories", "poisonpotion",
    "potionofcapacity", "powerpotion", "regenpotion", "skillpotion",
    "sneckooil", "speedpotion", "steroidpotion", "strengthpotion",
    "swiftpotion", "weakpotion",
}

_ARTIFACT_BLOCKED_DEBUFF_POTION_IDS = {
    "fearpotion", "poisonpotion", "weakpotion",
}

_REMOTE_USAGE_TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_read_tokens",
    "prompt_cache_write_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}
_CIRCUIT_SUPPRESSION_ERRORS = {"circuit_open", "circuit_or_budget"}
_BUDGET_SUPPRESSION_ERRORS = {
    "call_budget", "wait_budget", "cost_budget", "circuit_or_budget",
}
_ADVISOR_DISABLED_ERRORS = {"advisor_disabled"}


# The legacy audit was intentionally strongest around combat arithmetic.  A
# clean protocol trace could therefore look reassuring even when an
# irreversible macro decision had no auditable candidate comparison at all.
# Keep the macro gate explicit and deliberately small: reward collection is a
# sequence of independent buttons, while the phases below contain mutually
# exclusive strategic choices.
_STRATEGIC_NONCOMBAT_PHASES = {
    "BOSS_REWARD", "CARD_REWARD", "CHEST", "COMBAT_REWARD", "EVENT",
    "GRID", "HAND_SELECT", "MAP", "NEOW", "REST", "SAPPHIRE_KEY",
    "SHOP_ROOM", "SHOP_SCREEN",
}
_HIGH_IMPACT_NONCOMBAT_PHASES = {
    "BOSS_REWARD", "CARD_REWARD", "CHEST", "EVENT", "GRID",
    "HAND_SELECT", "MAP", "NEOW", "REST", "SAPPHIRE_KEY",
    "SHOP_ROOM", "SHOP_SCREEN",
}
_DEFAULT_DECISION_MARKERS = {
    "default", "unknown", "fallback", "firstoption", "first_option",
}

# Keep protocol vocabulary bounded and distinguish a recovery attempt from a
# failed recovery.  The controller uses a bounded, read-only resynchronization
# while a combat is still initializing; a matching started/succeeded pair is
# expected evidence of that handshake, not a release-protocol failure.  A
# timeout, stale command, or failed resynchronization remains fatal.
_KNOWN_PROTOCOL_EVENTS = {
    "ambiguous_timeout", "late_receipt_recovered",
    "late_receipt_unresolved", "missing_receipt_effect_verification_failed",
    "missing_receipt_effect_verified", "receipt_timeout",
    "recoverable_protocol_error", "stale_state_rejected",
    "failed_receipt_at_game_over",
    "state_resync_failed", "state_resync_started", "state_resync_succeeded",
    "state_resync_timeout", "timeout_state_advanced",
}
_FATAL_PROTOCOL_EVENTS = set(_KNOWN_PROTOCOL_EVENTS)


def _benign_resync_record_indexes(records):
    """Exempt only ordered, uniquely bound, successful readiness handshakes."""
    groups = {}
    for index, record in enumerate(records):
        request = record.get("state_request_id")
        if isinstance(request, str) and request:
            groups.setdefault(request, []).append((index, record))
    benign = set()
    bindings = (
        "attempt_id", "run_id", "decision_hash", "controller_hash",
        "before_seq", "decision_id", "phase", "reason", "resync_attempt",
    )
    for rows in groups.values():
        if len(rows) != 2:
            continue
        (start_index, start), (end_index, end) = rows
        if (start.get("event"), end.get("event")) != (
            "state_resync_started", "state_resync_succeeded",
        ):
            continue
        expected_phase = {
            "combat_initializing": "COMBAT_INITIALIZING",
            "hand_select_settling": "HAND_SELECT",
        }.get(start.get("reason"))
        if not expected_phase or start.get("phase") != expected_phase:
            continue
        if any(start.get(key) is None or start.get(key) != end.get(key)
               for key in bindings):
            continue
        before = start.get("before_seq")
        accepted = end.get("accepted_state_seq")
        result = end.get("result_state_seq")
        if not (type(before) is int and type(accepted) is int
                and type(result) is int and before <= accepted < result):
            continue
        if end.get("receipt_status") != "succeeded" or end.get("receipt_error"):
            continue
        if start.get("action") is not None or end.get("action") is not None:
            continue
        benign.update((start_index, end_index))
    return benign


_KNOWN_RECORD_TYPES = {
    "auxiliary_trace_pollution",
    "cache_warmup",
    "controller_start",
    "decision",
    "model_advice",
    "protocol_event",
    "terminal_result",
    "transition_settle",
}
_FOREIGN_AUXILIARY_RECORD_TYPES = {"cache_warmup", "model_advice"}
_FOREIGN_AUTHORITATIVE_RECORD_TYPES = {
    "controller_start", "decision", "protocol_event", "terminal_result",
}

_ANOMALY_BASE_IMPACT = {
    "critical": 100.0,
    "high": 75.0,
    "medium": 45.0,
    "low": 20.0,
}
_CRITICAL_ANOMALY_KINDS = {
    "ambiguous_timeout", "missing_receipt_effect_verification_failed",
    "decision_hash_mismatch", "operational_error_tainted_attempt",
    "state_resync_failed",
}
_HIGH_ANOMALY_KINDS = {
    "card_play_damage_underprediction", "death_with_actionable_potion_retained",
    "end_turn_damage_underprediction", "event_curse_probability_inconsistent",
    "false_combat_end_prediction", "high_impact_default_choice",
    "terminal_plan_missing_orb_resource",
    "noncombat_candidate_argmax_missed", "noncombat_invalid_model_override",
    "hand_select_plan_protection_unverified",
    "unconsumed_double_tap_setup", "choker_slots_without_attack_progress",
    "affordable_attack_progress_ignored", "combat_target_binding_rejection",
    "barricade_liveness_stall", "passive_doom_claim_unproven",
    "persistent_attrition_race_missed",
    "neow_lament_elite_route_missed",
    "neow_lament_route_evidence_missing",
}

_HAND_SELECT_PLAN_PROTECTION_VETO = (
    "exact_combat_plan_card_preservation"
)
_HAND_SELECT_IRREVERSIBLE_SCALING_IDS = frozenset({
    "accuracy", "afterimage", "athousandcuts", "barricade",
    "biasedcognition", "capacitor", "catalyst", "consume", "corruption",
    "creativeai", "darkembrace", "defragment", "demonform", "echoform",
    "envenom", "feelnopain", "footwork", "heatsinks", "inflame",
    "juggernaut", "limitbreak", "loop", "metallicize", "noxiousfumes",
    "rupture", "spotweakness", "storm", "wraithformv2",
})


class TraceAuditError(RuntimeError):
    """The bound attempt suffix cannot be audited without guessing."""


def _panic_button_is_emergency(hp_before, projected_loss):
    """Mirror the live planner's Panic Button reserve threshold.

    Panic Button is a legal block card, but its two-turn ``No Block``
    drawback means that treating it like an ordinary Defend creates audit
    noise on small, survivable losses.  Keep the audit aligned with the live
    policy: only call it mitigation when the current loss is lethal or
    reaches the same severe-loss threshold used by ``FastCombatPlanner``.
    """

    hp = max(1, int(hp_before or 1))
    loss = max(0, int(projected_loss or 0))
    if loss >= hp:
        return True
    severe_threshold = max(12, min(20, (hp + 2) // 3))
    return loss >= severe_threshold


def load_jsonl(path, decision_hash=None, attempt_id=None):
    records = []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                records.append({
                    "record_type": "trace_parse_error",
                    "decision_hash": decision_hash,
                    "attempt_id": attempt_id,
                    "line_number": line_number,
                    "error": "malformed_json",
                })
                continue
            if not isinstance(value, dict):
                records.append({
                    "record_type": "trace_parse_error",
                    "decision_hash": decision_hash,
                    "attempt_id": attempt_id,
                    "line_number": line_number,
                    "error": "record_not_object",
                })
                continue
            if decision_hash is not None and value.get("decision_hash") is None:
                records.append({
                    "record_type": "trace_binding_error",
                    "decision_hash": decision_hash,
                    "attempt_id": attempt_id,
                    "line_number": line_number,
                    "error": "decision_hash_missing",
                })
                continue
            if attempt_id is not None and value.get("attempt_id") is None:
                records.append({
                    "record_type": "trace_binding_error",
                    "decision_hash": decision_hash,
                    "attempt_id": attempt_id,
                    "line_number": line_number,
                    "error": "attempt_id_missing",
                })
                continue
            if (
                decision_hash is not None
                and value.get("decision_hash") != decision_hash
            ):
                continue
            if (
                attempt_id is not None
                and value.get("attempt_id") != attempt_id
            ):
                continue
            records.append(value)
    return records


def _resolve_attempt_trace_path(path, attempt_id):
    trace_path = Path(path)
    if isinstance(attempt_id, str) and Path(attempt_id).name == attempt_id:
        sidecar = (
            trace_path.parent / "logs" / "attempts" / attempt_id
            / "autoplay.log"
        )
        if sidecar.exists():
            return sidecar
    return trace_path


def _trace_lines_reverse(path, chunk_size=64 * 1024):
    """Yield non-empty JSONL records from the end in bounded blocks."""

    with Path(path).open("rb") as source:
        source.seek(0, 2)
        position = source.tell()
        remainder = b""
        while position:
            read_size = min(max(1024, int(chunk_size)), position)
            position -= read_size
            source.seek(position)
            block = source.read(read_size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def load_attempt_trace_suffix(path, decision_hash, attempt_id):
    """Load only the newest contiguous bound-attempt suffix.

    Production ``autoplay.log`` is close to a gigabyte.  Terminal auditing
    must neither scan all historical attempts nor silently find a stale
    attempt somewhere in the middle of that file.  Attempts are serialized,
    so the first different bound attempt is the authoritative boundary.
    Malformed current-suffix data and mixed decision hashes fail closed.
    """

    if not isinstance(decision_hash, str) or not decision_hash.strip():
        raise TraceAuditError("attempt audit requires a decision hash")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise TraceAuditError("attempt audit requires an attempt id")
    trace_path = _resolve_attempt_trace_path(path, attempt_id)
    if not trace_path.exists():
        raise TraceAuditError("attempt trace is missing")

    reversed_records = []
    foreign_auxiliary = []
    found_attempt = False
    found_controller_start = False
    found_terminal_result = False
    try:
        for raw_line in _trace_lines_reverse(trace_path):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TraceAuditError(
                    "current attempt trace contains malformed UTF-8"
                ) from exc
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceAuditError(
                    "current attempt trace contains malformed JSON"
                ) from exc
            if not isinstance(record, dict):
                raise TraceAuditError(
                    "current attempt trace contains a non-object record"
                )
            bound_attempt = record.get("attempt_id")
            if bound_attempt == attempt_id:
                found_attempt = True
                record_hash = record.get("decision_hash")
                if record_hash != decision_hash:
                    raise TraceAuditError(
                        "current attempt trace mixes decision hashes"
                    )
                if record.get("record_type") == "controller_start":
                    if record.get("decision_schema_version") != 2:
                        raise TraceAuditError(
                            "current attempt controller_start has an "
                            "unsupported schema"
                        )
                    found_controller_start = True
                if record.get("record_type") == "terminal_result":
                    if (
                        record.get("schema_version") != 2
                        or record.get("termination_kind")
                        not in {"game_over", "operational_error"}
                    ):
                        raise TraceAuditError(
                            "current attempt terminal_result is invalid"
                        )
                    found_terminal_result = True
                reversed_records.append(record)
            elif isinstance(bound_attempt, str) and bound_attempt:
                record_type = record.get("record_type")
                if record_type in _FOREIGN_AUXILIARY_RECORD_TYPES:
                    if found_controller_start:
                        break
                    foreign_auxiliary.append({
                        "record_type": "auxiliary_trace_pollution",
                        "decision_schema_version": 2,
                        "decision_hash": decision_hash,
                        "attempt_id": attempt_id,
                        "foreign_attempt_id": bound_attempt,
                        "foreign_record_type": record_type,
                        "foreign_decision_hash": record.get(
                            "decision_hash"
                        ),
                        "source_trace": str(trace_path),
                    })
                    continue
                if found_attempt:
                    break
                if record_type in _FOREIGN_AUTHORITATIVE_RECORD_TYPES:
                    raise TraceAuditError(
                        "requested attempt is not the current trace segment"
                    )
                # Unknown or state-changing future records must not be
                # silently skipped while deciding which attempt owns the
                # active suffix.
                raise TraceAuditError(
                    "requested attempt is followed by an unknown foreign "
                    "trace record"
                )
            else:
                raise TraceAuditError(
                    "current attempt trace contains an unbound record"
                )
    except OSError as exc:
        raise TraceAuditError("attempt trace could not be read") from exc

    if not found_attempt:
        raise TraceAuditError("attempt trace suffix is missing")
    if not found_controller_start:
        raise TraceAuditError(
            "current attempt trace is missing controller_start"
        )
    if not found_terminal_result:
        raise TraceAuditError(
            "current attempt trace is missing terminal_result"
        )
    reversed_records.reverse()
    # These observations deliberately bind to the requested audit, not to
    # the synthetic/test attempt that escaped into the shared trace.  They
    # are protocol issues in the report while no longer making the genuine
    # terminal attempt impossible to inspect.
    reversed_records.extend(reversed(foreign_auxiliary))
    return reversed_records


_EXTERNAL_AUDIT_STATUSES = {
    "clear", "inconclusive", "issues", "not_applicable",
}


def _external_audit_failure(channel, kind, error=None):
    issue = {
        "kind": kind,
        "severity": "P1",
        "audit_channel": channel,
    }
    if error:
        issue["error"] = str(error)
    return {
        "status": "issues",
        "issue_count": 1,
        "eligible_unknown_count": 0,
        "issues": [issue],
        "unknowns": [],
        **({"disagreement_count": 0} if channel == "independent_oracle" else {}),
    }


def _nonnegative_audit_count(value):
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
    return None


def _base_game_mechanism_contract_violations(value):
    """Validate the independently recomputed base-game mechanism receipt.

    The parent oracle's ``status`` and generic coverage counters are not
    sufficient evidence that this nested audit actually ran.  Keep this
    validator on the consumer side so deleting observations or changing the
    nested counts cannot turn a missing mechanism audit into a clear result.
    """

    violations = []

    def violation(field, observed, expected):
        violations.append({
            "field": field, "observed": observed, "expected": expected,
        })

    def binding_snapshot(counter):
        return [
            {"binding": list(binding), "count": count}
            for binding, count in sorted(
                counter.items(), key=lambda item: repr(item[0])
            )
        ]

    receipt = (
        value.get("base_game_mechanisms")
        if isinstance(value, dict) else None
    )
    if not isinstance(receipt, dict):
        violation(
            "base_game_mechanisms", type(receipt).__name__, "object",
        )
        return violations

    expected_version = _EXPECTED_BASE_GAME_MECHANISM_CONTRACT_VERSION
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
        or receipt.get("contract_version") != expected_version
    ):
        violation(
            "base_game_mechanisms.contract_version",
            receipt.get("contract_version"),
            expected_version or "installed mechanism contract version",
        )

    counts = {}
    for field in ("eligible", "evaluated"):
        count = _nonnegative_audit_count(receipt.get(field))
        if count is None:
            violation(
                f"base_game_mechanisms.{field}", receipt.get(field),
                "nonnegative integer",
            )
        counts[field] = count

    collections = {}
    for field in ("observations", "issues", "unknowns"):
        items = receipt.get(field)
        if not isinstance(items, list):
            violation(
                f"base_game_mechanisms.{field}", type(items).__name__,
                "list",
            )
            items = []
        collections[field] = items

    observation_bindings = {"clear": Counter(), "issues": Counter(),
                            "inconclusive": Counter()}
    valid_observations = True
    for index, observation in enumerate(collections["observations"]):
        prefix = f"base_game_mechanisms.observations.{index}"
        if not isinstance(observation, dict):
            violation(prefix, type(observation).__name__, "object")
            valid_observations = False
            continue
        mechanism = observation.get("mechanism")
        record_index = observation.get("record_index")
        status = observation.get("status")
        if not isinstance(mechanism, str) or not mechanism.strip():
            violation(
                f"{prefix}.mechanism", mechanism, "non-empty string",
            )
            valid_observations = False
        if (
            not isinstance(record_index, int)
            or isinstance(record_index, bool)
            or record_index < 0
        ):
            violation(
                f"{prefix}.record_index", record_index,
                "nonnegative integer",
            )
            valid_observations = False
        if status not in observation_bindings:
            violation(
                f"{prefix}.status", status,
                ["clear", "inconclusive", "issues"],
            )
            valid_observations = False
        elif (
            isinstance(mechanism, str) and mechanism.strip()
            and isinstance(record_index, int)
            and not isinstance(record_index, bool) and record_index >= 0
        ):
            observation_bindings[status][(record_index, mechanism)] += 1

    finding_bindings = {}
    public_finding_bindings = {}
    for field, expected_status in (
        ("issues", "issues"), ("unknowns", "inconclusive")
    ):
        bindings = Counter()
        public_bindings = Counter()
        for index, finding in enumerate(collections[field]):
            prefix = f"base_game_mechanisms.{field}.{index}"
            if not isinstance(finding, dict):
                violation(prefix, type(finding).__name__, "object")
                continue
            kind = finding.get("kind")
            mechanism = finding.get("mechanism")
            record_index = finding.get("record_index")
            if not isinstance(kind, str) or not kind.strip():
                violation(f"{prefix}.kind", kind, "non-empty string")
            if not isinstance(mechanism, str) or not mechanism.strip():
                violation(
                    f"{prefix}.mechanism", mechanism, "non-empty string",
                )
            if (
                not isinstance(record_index, int)
                or isinstance(record_index, bool)
                or record_index < 0
            ):
                violation(
                    f"{prefix}.record_index", record_index,
                    "nonnegative integer",
                )
            if (
                isinstance(mechanism, str) and mechanism.strip()
                and isinstance(record_index, int)
                and not isinstance(record_index, bool) and record_index >= 0
            ):
                bindings[(record_index, mechanism)] += 1
                if isinstance(kind, str) and kind.strip():
                    public_bindings[(record_index, mechanism, kind)] += 1
        finding_bindings[field] = bindings
        public_finding_bindings[field] = public_bindings
        if valid_observations and bindings != observation_bindings[expected_status]:
            violation(
                f"base_game_mechanisms.{field}.observation_bindings",
                binding_snapshot(bindings),
                binding_snapshot(observation_bindings[expected_status]),
            )

    eligible = counts["eligible"]
    evaluated = counts["evaluated"]
    observation_count = len(collections["observations"])
    issue_count = len(collections["issues"])
    unknown_count = len(collections["unknowns"])
    if eligible is not None and eligible != observation_count:
        violation(
            "base_game_mechanisms.eligible", eligible, observation_count,
        )
    clear_count = sum(observation_bindings["clear"].values())
    if evaluated is not None and valid_observations and evaluated != clear_count:
        violation(
            "base_game_mechanisms.evaluated", evaluated, clear_count,
        )
    if eligible is not None and evaluated is not None and (
        eligible != evaluated + issue_count + unknown_count
    ):
        violation(
            "base_game_mechanisms.conservation",
            {
                "eligible": eligible, "evaluated": evaluated,
                "issues": issue_count, "unknowns": unknown_count,
            },
            "eligible == evaluated + len(issues) + len(unknowns)",
        )

    # A nested finding must also be surfaced by the oracle's public channel;
    # otherwise the public zero counts could conceal a real mechanism result.
    outer_lists = {
        "issues": value.get("issues") if isinstance(value, dict) else None,
        "unknowns": value.get("unknowns") if isinstance(value, dict) else None,
    }
    for field in ("issues", "unknowns"):
        outer = outer_lists[field]
        if not isinstance(outer, list):
            continue  # The generic external-contract validator reports this.
        outer_bindings = Counter(
            (
                item.get("record_index"), item.get("mechanism"),
                item.get("kind"),
            )
            for item in outer if isinstance(item, dict)
        )
        missing = public_finding_bindings[field] - outer_bindings
        if missing:
            violation(
                f"base_game_mechanisms.{field}.public_bindings",
                binding_snapshot(outer_bindings),
                {
                    "must_include": binding_snapshot(
                        public_finding_bindings[field]
                    )
                },
            )

    return violations


def _normalize_external_audit(channel, value, *, disagreements=False):
    """Validate an external audit result without trusting its clear label."""

    if not isinstance(value, dict):
        return _external_audit_failure(
            channel, f"{channel}_result_not_object"
        )
    result = dict(value)
    issues = [
        dict(item) if isinstance(item, dict) else {
            "kind": f"{channel}_issue_not_object",
            "value_type": type(item).__name__,
        }
        for item in (value.get("issues") or [])
    ] if isinstance(value.get("issues", []), list) else []
    unknowns = [
        dict(item) if isinstance(item, dict) else {
            "kind": f"{channel}_unknown_not_object",
            "value_type": type(item).__name__,
        }
        for item in (value.get("unknowns") or [])
    ] if isinstance(value.get("unknowns", []), list) else []
    contract_issues = []

    def contract_issue(field, observed, expected=None):
        item = {
            "kind": f"{channel}_result_contract_violation",
            "severity": "P1",
            "audit_channel": channel,
            "field": field,
            "observed": observed,
        }
        if expected is not None:
            item["expected"] = expected
        contract_issues.append(item)

    if not isinstance(value.get("issues", []), list):
        contract_issue("issues", type(value.get("issues")).__name__, "list")
    if not isinstance(value.get("unknowns", []), list):
        contract_issue(
            "unknowns", type(value.get("unknowns")).__name__, "list"
        )

    reported_issues = _nonnegative_audit_count(value.get("issue_count"))
    if reported_issues is None:
        contract_issue("issue_count", value.get("issue_count"), "nonnegative integer")
        reported_issues = len(issues)
    elif reported_issues != len(issues):
        contract_issue("issue_count", reported_issues, len(issues))

    reported_unknowns = _nonnegative_audit_count(
        value.get("eligible_unknown_count")
    )
    if reported_unknowns is None:
        contract_issue(
            "eligible_unknown_count",
            value.get("eligible_unknown_count"),
            "nonnegative integer",
        )
        reported_unknowns = len(unknowns)
    elif reported_unknowns != len(unknowns):
        contract_issue(
            "eligible_unknown_count", reported_unknowns, len(unknowns)
        )

    disagreement_count = 0
    if disagreements:
        disagreement_count = _nonnegative_audit_count(
            value.get("disagreement_count")
        )
        if disagreement_count is None:
            contract_issue(
                "disagreement_count", value.get("disagreement_count"),
                "nonnegative integer",
            )
            disagreement_count = 0

        expected_version = _EXPECTED_INDEPENDENT_ORACLE_VERSION
        if (
            not isinstance(expected_version, str)
            or value.get("oracle_version") != expected_version
        ):
            contract_issue(
                "oracle_version", value.get("oracle_version"),
                expected_version or "installed independent oracle version",
            )
        expected_contract_version = (
            _EXPECTED_ORACLE_COVERAGE_CONTRACT_VERSION
        )
        if value.get("coverage_contract_version") != expected_contract_version:
            contract_issue(
                "coverage_contract_version",
                value.get("coverage_contract_version"),
                expected_contract_version,
            )
        expected_binding_fields = list(_EXPECTED_ORACLE_BINDING_FIELDS)
        if value.get("binding_fields") != expected_binding_fields:
            contract_issue(
                "binding_fields", value.get("binding_fields"),
                expected_binding_fields,
            )
        expected_keys = set(_EXPECTED_ORACLE_COVERAGE_KEYS)
        reported_required_keys = value.get("required_coverage_keys")
        if not (
            isinstance(reported_required_keys, list)
            and set(reported_required_keys) == expected_keys
        ):
            contract_issue(
                "required_coverage_keys", reported_required_keys,
                sorted(expected_keys),
            )
        coverage = value.get("coverage")
        if not isinstance(coverage, dict):
            contract_issue("coverage", type(coverage).__name__, "object")
            coverage = {}
        missing_coverage = sorted(expected_keys - set(coverage))
        extra_coverage = sorted(set(coverage) - expected_keys)
        if missing_coverage:
            contract_issue(
                "coverage.missing", missing_coverage, "none"
            )
        if extra_coverage:
            contract_issue("coverage.extra", extra_coverage, "none")
        for coverage_name in sorted(expected_keys & set(coverage)):
            bucket = coverage.get(coverage_name)
            if not isinstance(bucket, dict):
                contract_issue(
                    f"coverage.{coverage_name}", type(bucket).__name__,
                    "object",
                )
                continue
            counts = {}
            for field in ("eligible", "evaluated", "unknown", "issues"):
                count = _nonnegative_audit_count(bucket.get(field))
                if count is None:
                    contract_issue(
                        f"coverage.{coverage_name}.{field}",
                        bucket.get(field), "nonnegative integer",
                    )
                counts[field] = count
            if all(value is not None for value in counts.values()):
                expected_status = (
                    "issues" if counts["issues"]
                    else "inconclusive" if counts["unknown"]
                    else "clear" if counts["eligible"]
                    else "not_applicable"
                )
                if bucket.get("status") != expected_status:
                    contract_issue(
                        f"coverage.{coverage_name}.status",
                        bucket.get("status"), expected_status,
                    )
        expected_disagreement_kinds = set(
            _EXPECTED_ORACLE_DISAGREEMENT_KINDS
        )
        reported_disagreement_kinds = value.get("disagreement_kinds")
        if not (
            isinstance(reported_disagreement_kinds, list)
            and set(reported_disagreement_kinds)
            == expected_disagreement_kinds
        ):
            contract_issue(
                "disagreement_kinds", reported_disagreement_kinds,
                sorted(expected_disagreement_kinds),
            )
        independently_counted_disagreements = sum(
            1 for item in issues
            if item.get("kind") in expected_disagreement_kinds
        )
        if disagreement_count != independently_counted_disagreements:
            contract_issue(
                "disagreement_count", disagreement_count,
                independently_counted_disagreements,
            )
        blind_reviews = value.get("blind_reviews")
        if not isinstance(blind_reviews, list):
            contract_issue(
                "blind_reviews", type(blind_reviews).__name__, "list"
            )
        else:
            for index, review in enumerate(blind_reviews):
                if not isinstance(review, dict):
                    contract_issue(
                        f"blind_reviews.{index}", type(review).__name__,
                        "object",
                    )
                    continue
                if review.get("review_version") != (
                    "independent-blind-consequence-v1"
                ):
                    contract_issue(
                        f"blind_reviews.{index}.review_version",
                        review.get("review_version"),
                        "independent-blind-consequence-v1",
                    )
                if review.get("review_authority") != (
                    "independent_oracle_recompute"
                ):
                    contract_issue(
                        f"blind_reviews.{index}.review_authority",
                        review.get("review_authority"),
                        "independent_oracle_recompute",
                    )
                if review.get("evidence_scope") != (
                    "protocol_visible_consequences_only"
                ):
                    contract_issue(
                        f"blind_reviews.{index}.evidence_scope",
                        review.get("evidence_scope"),
                        "protocol_visible_consequences_only",
                    )
                if review.get("input_fields") != [
                    "choice_id", "structured_consequences",
                ]:
                    contract_issue(
                        f"blind_reviews.{index}.input_fields",
                        review.get("input_fields"),
                        ["choice_id", "structured_consequences"],
                    )
                expected_excluded = {
                    "local_score", "model_score", "selected",
                    "final_source", "override", "producer_reason",
                }
                reported_excluded = review.get("excluded_fields")
                if not (
                    isinstance(reported_excluded, list)
                    and set(reported_excluded) == expected_excluded
                ):
                    contract_issue(
                        f"blind_reviews.{index}.excluded_fields",
                        reported_excluded,
                        sorted(expected_excluded),
                    )
                candidate_ids = review.get("candidate_choice_ids")
                if not (
                    isinstance(candidate_ids, list)
                    and candidate_ids
                    and all(
                        isinstance(choice_id, str) and choice_id
                        for choice_id in candidate_ids
                    )
                    and len(candidate_ids) == len(set(candidate_ids))
                ):
                    contract_issue(
                        f"blind_reviews.{index}.candidate_choice_ids",
                        candidate_ids,
                        "non-empty unique string list",
                    )
                    candidate_ids = []
                if not (
                    isinstance(review.get("attempt_id"), str)
                    and review.get("attempt_id")
                ):
                    contract_issue(
                        f"blind_reviews.{index}.attempt_id",
                        review.get("attempt_id"), "non-empty string",
                    )
                if not (
                    isinstance(review.get("decision_hash"), str)
                    and review.get("decision_hash")
                ):
                    contract_issue(
                        f"blind_reviews.{index}.decision_hash",
                        review.get("decision_hash"), "non-empty string",
                    )
                if not (
                    isinstance(review.get("before_seq"), int)
                    and not isinstance(review.get("before_seq"), bool)
                ):
                    contract_issue(
                        f"blind_reviews.{index}.before_seq",
                        review.get("before_seq"), "integer",
                    )
                if not (
                    isinstance(review.get("reason"), str)
                    and review.get("reason").strip()
                ):
                    contract_issue(
                        f"blind_reviews.{index}.reason",
                        review.get("reason"), "non-empty string",
                    )
                if review.get("status") not in {"clear", "unknown"}:
                    contract_issue(
                        f"blind_reviews.{index}.status",
                        review.get("status"), "clear or unknown",
                    )
                elif review.get("status") == "clear" and (
                    not isinstance(review.get("recommended_choice_id"), str)
                    or review.get("recommended_choice_id") not in candidate_ids
                ):
                    contract_issue(
                        f"blind_reviews.{index}.recommended_choice_id",
                        review.get("recommended_choice_id"),
                        "member of candidate_choice_ids",
                    )

        for nested_violation in (
            _base_game_mechanism_contract_violations(value)
        ):
            contract_issue(
                nested_violation["field"], nested_violation["observed"],
                nested_violation["expected"],
            )

    status = value.get("status")
    if status not in _EXTERNAL_AUDIT_STATUSES:
        contract_issue("status", status, sorted(_EXTERNAL_AUDIT_STATUSES))
        status = "issues"
    if status == "clear" and (
        reported_issues or reported_unknowns or disagreement_count
    ):
        contract_issue(
            "status", status,
            "clear only when all channel counts are zero",
        )
    if status == "not_applicable" and (
        reported_issues or reported_unknowns or disagreement_count
    ):
        contract_issue(
            "status", status,
            "not_applicable only when all channel counts are zero",
        )

    issues.extend(contract_issues)
    effective_issue_count = max(reported_issues, len(issues))
    effective_unknown_count = max(reported_unknowns, len(unknowns))
    if issues or disagreement_count:
        status = "issues"
    elif unknowns or effective_unknown_count or status == "inconclusive":
        status = "inconclusive"

    result.update({
        "status": status,
        "issue_count": effective_issue_count,
        "eligible_unknown_count": effective_unknown_count,
        "issues": issues,
        "unknowns": unknowns,
    })
    if disagreements:
        result["disagreement_count"] = disagreement_count
    return result


def _load_attempt_artifacts(trace_path):
    directory = Path(trace_path).parent
    files = {
        "run_context": "run-context.json",
        "run_result": "run-result.json",
        "state": "terminal-state.json",
        "selection": "selection.json",
    }
    artifacts = {}
    for name, filename in files.items():
        path = directory / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            value = None
        artifacts[name] = value
    return artifacts


def _gold_death_diagnostic(records, artifacts):
    """Explain whether terminal gold was actually spendable in the run.

    A terminal snapshot is the only authoritative source for the final purse;
    decision records then tell us whether a shop transaction was reachable.
    Keep this as diagnostic evidence instead of an automatic strategy issue:
    dying with gold and never seeing a shop is a route/resource-availability
    fact, not proof that a purchase was deliberately skipped.
    """

    state = artifacts.get("state") if isinstance(artifacts, dict) else None
    game_state = state.get("game_state") if isinstance(state, dict) else None
    if not isinstance(game_state, dict):
        return {
            "status": "unavailable",
            "reason": "terminal_state_missing_game_state",
        }

    gold = game_state.get("gold")
    hp = game_state.get("current_hp")
    try:
        gold = int(gold) if gold is not None else None
    except (TypeError, ValueError):
        gold = None
    try:
        hp = int(hp) if hp is not None else None
    except (TypeError, ValueError):
        hp = None
    if gold is None or hp != 0 or gold < 300:
        return {
            "status": "not_applicable",
            "gold": gold,
            "current_hp": hp,
        }

    shop_records = [
        record for record in records
        if isinstance(record, dict)
        and str(record.get("phase", "")).upper() == "SHOP"
    ]
    gold_spends = []
    for record in shop_records:
        outcome = record.get("decision_outcome")
        if not isinstance(outcome, dict):
            outcome = record.get("authoritative_choice_settlement")
        if isinstance(outcome, dict):
            delta = outcome.get("gold_delta")
            try:
                if delta is not None and float(delta) < 0:
                    gold_spends.append(float(-delta))
            except (TypeError, ValueError):
                pass
        decision = record.get("decision")
        if isinstance(decision, dict):
            consequences = decision.get("consequences")
            if isinstance(consequences, dict):
                cost = (consequences.get("current_cost") or {}).get("gold")
                try:
                    if cost is not None and float(cost) > 0:
                        gold_spends.append(float(cost))
                except (TypeError, ValueError):
                    pass

    if not shop_records:
        reason = "death_without_reachable_shop"
    elif gold_spends:
        reason = "gold_spent_before_death"
    else:
        reason = "shop_reached_without_observed_gold_spend"
    return {
        "status": "high_gold_death",
        "reason": reason,
        "gold": gold,
        "current_hp": hp,
        "shop_decision_count": len(shop_records),
        "observed_gold_spend_count": len(gold_spends),
        "observed_gold_spent": round(sum(gold_spends), 3),
        "act": game_state.get("act"),
        "floor": game_state.get("floor"),
        "character": game_state.get("class"),
    }


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attempt_audit_receipt(trace_path, artifacts, decision_hash, attempt_id, records):
    trace_path = Path(trace_path)
    artifact_files = {
        "run_context": trace_path.parent / "run-context.json",
        "run_result": trace_path.parent / "run-result.json",
        "state": trace_path.parent / "terminal-state.json",
        "selection": trace_path.parent / "selection.json",
    }
    hashes = {}
    for name, path in artifact_files.items():
        try:
            hashes[name] = _sha256_file(path)
        except (FileNotFoundError, OSError):
            hashes[name] = None
    try:
        trace_hash = _sha256_file(trace_path)
    except (FileNotFoundError, OSError):
        trace_hash = None
    return {
        "receipt_schema_version": 1,
        "attempt_id": attempt_id,
        "decision_hash": decision_hash,
        "exact_attempt_suffix": True,
        "trace_path": str(trace_path.resolve()),
        "trace_sha256": trace_hash,
        "record_count": len(records),
        "artifact_sha256": hashes,
        "artifacts_complete": bool(
            all(isinstance(artifacts.get(name), dict) for name in artifact_files)
            and all(hashes.values())
        ),
    }


def _run_independent_oracle(
    records, decision_hash, attempt_id, *, artifacts
):
    if _independent_oracle is None or not callable(
        getattr(_independent_oracle, "audit_records", None)
    ):
        return _external_audit_failure(
            "independent_oracle",
            "independent_oracle_missing",
            _INDEPENDENT_ORACLE_IMPORT_ERROR,
        )
    try:
        value = _independent_oracle.audit_records(
            records, decision_hash, attempt_id, artifacts=artifacts
        )
    except Exception as exc:
        return _external_audit_failure(
            "independent_oracle",
            "independent_oracle_exception",
            f"{type(exc).__name__}: {exc}",
        )
    return _normalize_external_audit(
        "independent_oracle", value, disagreements=True
    )


def _run_death_replay(records, terminal):
    if _death_replay is None or not callable(
        getattr(_death_replay, "build_death_replay", None)
    ):
        return _external_audit_failure(
            "death_replay",
            "death_replay_missing",
            _DEATH_REPLAY_IMPORT_ERROR,
        )
    try:
        value = _death_replay.build_death_replay(
            records, terminal=terminal
        )
    except Exception as exc:
        return _external_audit_failure(
            "death_replay",
            "death_replay_exception",
            f"{type(exc).__name__}: {exc}",
        )
    return _normalize_external_audit("death_replay", value)


def _death_terminal(records):
    terminals = [
        record for record in records
        if isinstance(record, dict)
        and record.get("record_type") == "terminal_result"
    ]
    if len(terminals) != 1:
        return None
    terminal = terminals[0]
    hp = terminal.get("current_hp")
    if (
        terminal.get("termination_kind") == "game_over"
        and terminal.get("victory") is not True
        and isinstance(hp, (int, float))
        and not isinstance(hp, bool)
        and hp <= 0
    ):
        return terminal
    return None


def _merge_external_attempt_audits(
    report, independent_oracle, replay, *, death_observed
):
    """Merge required independent channels into one fail-closed report."""

    report = dict(report)
    base_status = report.get("audit_status")
    base_issue_count = _nonnegative_audit_count(report.get("issue_count"))
    base_unknown_count = _nonnegative_audit_count(
        report.get("eligible_unknown_count")
    )
    if base_issue_count is None or base_unknown_count is None:
        raise TraceAuditError("core audit returned invalid top-level counts")

    replay = dict(replay)
    replay["death_observed"] = bool(death_observed)
    replay_status = replay.get("status")
    if death_observed and replay_status == "not_applicable":
        item = {
            "kind": "death_replay_required_but_not_applicable",
            "severity": "P1",
            "audit_channel": "death_replay",
        }
        replay.setdefault("issues", []).append(item)
        replay["issue_count"] = max(
            int(replay.get("issue_count") or 0) + 1,
            len(replay["issues"]),
        )
        replay["status"] = "issues"

    external_issues = []
    issue_counts = Counter(report.get("issue_counts") or {})
    for channel, channel_report in (
        ("independent_oracle", independent_oracle),
        ("death_replay", replay),
    ):
        for item in channel_report.get("issues") or []:
            copied = dict(item)
            copied.setdefault("audit_channel", channel)
            copied.setdefault("severity", "P1")
            external_issues.append(copied)
            issue_counts[copied.get("kind") or f"{channel}_issue"] += 1

    oracle_issue_count = independent_oracle["issue_count"]
    replay_issue_count = replay["issue_count"]
    oracle_unknown_count = independent_oracle["eligible_unknown_count"]
    replay_unknown_count = replay["eligible_unknown_count"]
    disagreement_count = independent_oracle["disagreement_count"]
    aggregate_issue_count = (
        base_issue_count + oracle_issue_count + replay_issue_count
    )
    aggregate_unknown_count = (
        base_unknown_count + oracle_unknown_count + replay_unknown_count
    )

    replay_acceptable = replay.get("status") == "clear" or (
        not death_observed and replay.get("status") == "not_applicable"
    )
    external_statuses = {
        independent_oracle.get("status"), replay.get("status")
    }
    if (
        aggregate_issue_count
        or disagreement_count
        or "issues" in external_statuses
        or base_status == "issues"
    ):
        audit_status = "issues"
    elif (
        aggregate_unknown_count
        or base_status != "clear"
        or independent_oracle.get("status") != "clear"
        or not replay_acceptable
    ):
        audit_status = "inconclusive"
    else:
        audit_status = "clear"

    report.update({
        "core_audit_status": base_status,
        "independent_oracle": independent_oracle,
        "death_replay": replay,
        "death_observed": bool(death_observed),
        "oracle_disagreement_count": disagreement_count,
        "audit_status": audit_status,
        "issue_count": aggregate_issue_count,
        "eligible_unknown_count": aggregate_unknown_count,
        "issues": list(report.get("issues") or []) + external_issues,
        "issue_counts": dict(issue_counts),
    })
    report["release_gate_passed"] = _release_gate_passed(report)
    return report


def audit_attempt_trace(trace_path, decision_hash, attempt_id):
    """Stable terminal API for a bounded, fail-closed attempt audit."""

    resolved_trace_path = _resolve_attempt_trace_path(trace_path, attempt_id)
    records = load_attempt_trace_suffix(
        trace_path, decision_hash, attempt_id
    )
    artifacts = _load_attempt_artifacts(resolved_trace_path)
    report = audit_records(
        records, decision_hash, attempt_id=attempt_id
    )
    independent = _run_independent_oracle(
        records,
        decision_hash,
        attempt_id,
        artifacts=artifacts,
    )
    terminal = next(
        (
            record for record in records
            if isinstance(record, dict)
            and record.get("record_type") == "terminal_result"
        ),
        None,
    )
    replay = _run_death_replay(records, terminal)
    merged = _merge_external_attempt_audits(
        report,
        independent,
        replay,
        death_observed=_death_terminal(records) is not None,
    )
    terminal = terminal if isinstance(terminal, dict) else {}
    merged.update({
        "policy_version": terminal.get("policy_version"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character", terminal.get("class")),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get(
            "terminal_state_seq", terminal.get("state_seq")
        ),
        "termination_kind": terminal.get("termination_kind"),
        "gold_death_diagnostic": _gold_death_diagnostic(
            records, artifacts
        ),
        "audit_receipt": _attempt_audit_receipt(
            resolved_trace_path,
            artifacts,
            decision_hash,
            attempt_id,
            records,
        ),
    })
    merged["release_gate_passed"] = _release_gate_passed(merged)
    return merged


def _issue(record, kind, **details):
    return {
        "kind": kind,
        "attempt_id": record.get("attempt_id"),
        "run_id": record.get("run_id"),
        "seed": record.get("seed"),
        "act": record.get("act"),
        "floor": record.get("floor"),
        "phase": record.get("phase"),
        "turn": record.get("turn"),
        "before_seq": record.get("before_seq"),
        **details,
    }


def _doomed_target_has_material_attack(record):
    """Return whether a doomed target is still worth killing immediately.

    Poison/Corpse Explosion can make an enemy ``doomed`` before the next
    enemy phase, but spending a damage card is still correct when that enemy
    is attacking now and the card will actually kill it.  The old audit only
    looked at the doomed-id set and therefore reported these necessary kills
    as redundant (notably Poisoned Stab into a 4-HP Darkling at 1 HP).
    """

    target_id = record.get("enemy_instance_id")
    if not target_id:
        return False
    target = next(
        (
            monster for monster in record.get("monsters_before") or []
            if monster.get("enemy_instance_id") == target_id
        ),
        None,
    )
    if not isinstance(target, dict):
        return False
    intent = str(target.get("intent") or "").upper()
    incoming = max(0, int(target.get("move_adjusted_damage") or 0))
    if "ATTACK" not in intent or incoming <= 0:
        return False
    decision = record.get("decision") or {}
    card_damage = decision.get("card_damage")
    if card_damage is None:
        return False
    target_hp = max(0, int(target.get("current_hp") or 0))
    target_block = max(0, int(target.get("block") or 0))
    effective_damage = max(0, int(card_damage) - target_block)
    return target_hp > 0 and effective_damage >= target_hp


def _affordable_block_options(cards, energy, max_cards=None):
    """Return reachable ``(block, cards_played)`` mitigation pairs.

    The serialized block value already includes the current Dexterity/Frail
    state.  Keep card count alongside the energy DP because Time Eater can
    end the turn before a nominally affordable multi-card block sequence is
    actually playable.
    """

    energy = max(0, int(energy or 0))
    max_cards = (
        None if max_cards is None else max(0, int(max_cards or 0))
    )
    best = {(0, 0): 0}
    for card in cards:
        if not card.get("is_playable"):
            continue
        # New traces carry the authoritative base capability.  Old traces do
        # not, so ignore their ambiguous dynamic block instead of accusing the
        # policy based on Dexterity/Frail leaking onto a non-block card.
        base_block = card.get("base_block")
        if base_block is None or int(base_block or 0) < 0:
            continue
        block = max(0, int(card.get("block") or 0))
        if block <= 0:
            continue
        cost = int(card.get("cost") or 0)
        if cost == -1:
            block *= energy
            cost = energy
        else:
            cost = max(0, cost)
        if cost > energy:
            continue
        updated = dict(best)
        for (spent, cards_played), current_block in best.items():
            next_cards = cards_played + 1
            if (
                spent + cost > energy
                or max_cards is not None and next_cards > max_cards
            ):
                continue
            key = (spent + cost, next_cards)
            updated[key] = max(updated.get(key, 0), current_block + block)
        best = updated
    return [
        (block, cards_played)
        for (_spent, cards_played), block in best.items()
        if cards_played > 0 and block > 0
    ]


_SURVIVAL_DEBUFF_CARD_IDS = frozenset({
    "disarm", "darkshackles", "legssweep", "malaise", "piercingwail",
    "cripplingpoison", "shockwave", "weak",
})

_RANDOM_MULTI_ATTACK_CARD_IDS = frozenset({
    "ripandtear", "swordboomerang",
})


def _resource_card_has_survival_gain(
    card,
    monsters,
    *,
    hand=None,
    incoming_attack_hp_loss=None,
    player_block_before=None,
    passive_end_block=0,
    calipers_active=False,
):
    """Whether a playable card can reduce this turn's incoming HP loss.

    A raw ``damage > 0`` check is too broad for END-turn auditing: a cheap
    Sweeping Beam that merely removes a few points from a blocked Spheric
    Guardian can increase Thorns/Sharp Hide exposure while doing nothing to
    the current attack.  Only count immediate Block, an attack that is
    provably lethal through the target's current Block, or an explicit
    strength/Weakness debuff against a live attacker.  This keeps the audit
    focused on unexplained damage-taking ENDs instead of second-guessing the
    planner's risk budget.
    """

    if not isinstance(card, dict) or not card.get("is_playable"):
        return False
    attack_loss = _int_or_none(incoming_attack_hp_loss)
    attack_damage_is_mitigable = attack_loss is None or attack_loss > 0
    base_block = card.get("base_block")
    block = _safe_nonnegative_int(card.get("block"))
    if _normalized_id(card.get("id")) == "secondwind":
        # Second Wind's serialized Block is the amount gained for *each*
        # other non-Attack card exhausted, not a fixed packet.  Treating the
        # card by itself as a Defend creates false "unused resource" reports.
        exhaustible = sum(
            1
            for other in hand or []
            if other is not card
            and isinstance(other, dict)
            and str(other.get("type") or "").upper() != "ATTACK"
        )
        block *= exhaustible
        if block <= 0:
            return False
    current_block = _safe_nonnegative_int(player_block_before)
    passive_end_block = _safe_nonnegative_int(passive_end_block)
    if calipers_active and block > 0:
        retained_before = max(0, current_block - 15)
        retained_after = max(0, current_block + block - 15)
        if retained_after > retained_before:
            # Calipers turns otherwise-transient Block into persistent
            # defensive state.  This remains a real resource even when the
            # current attack is already covered.
            return True
    # Orichalcum/Metallicize are already the default end-turn mitigation.
    # A Defend which merely replaces that passive packet is not a positive
    # survival action.  This is deliberately based on the authoritative
    # *before* block, not on the projected post-card block, so a card cannot
    # claim value by double-counting the same end-turn relic trigger.
    incremental_block = max(0, block - passive_end_block)
    if (
        attack_damage_is_mitigable
        and block > 0
        and (
            current_block > 0
            or passive_end_block <= 0
            or incremental_block > 0
        )
        and (
            base_block is None
            or _int_or_none(base_block) is None
            or int(base_block) >= 0
        )
    ):
        return True

    live = [
        monster for monster in monsters or []
        if isinstance(monster, dict)
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
        and not monster.get("is_gone")
        and not monster.get("half_dead")
    ]
    attackers = [
        monster for monster in live
        if (
            "ATTACK" in str(monster.get("intent") or "").upper()
            and (
                _safe_nonnegative_int(monster.get("move_adjusted_damage")) > 0
                or _safe_nonnegative_int(monster.get("move_hits")) > 0
            )
        )
    ]
    card_id = _normalized_id(card.get("id"))
    if card_id in _SURVIVAL_DEBUFF_CARD_IDS:
        return attack_damage_is_mitigable and bool(attackers)

    damage = _safe_nonnegative_int(card.get("damage"))
    if damage <= 0 or str(card.get("type") or "").upper() != "ATTACK":
        return False
    if card_id in _RANDOM_MULTI_ATTACK_CARD_IDS:
        # A low-HP enemy does not prove that a random packet will hit it.
        # This is especially material against Spiker/Sharp Hide, where the
        # attempted rescue can cost more HP than ending the turn.  Without a
        # complete packet allocation, fail closed instead of declaring END
        # avoidable from the smallest enemy alone.
        return False
    # The compact card field is a conservative single-target damage estimate;
    # do not multiply it for random/AOE attacks when deciding whether an END
    # was avoidable.  A lethal line is unambiguous even under that estimate.
    for monster in live:
        hp = _safe_nonnegative_int(monster.get("current_hp"))
        enemy_block = _safe_nonnegative_int(monster.get("block"))
        if max(0, damage - enemy_block) >= hp:
            return True
    return False


def _fairy_revival_proves_block_neutral(record, extra_block):
    """Prove that extra Block cannot change the post-revival HP state."""

    extra_block = _int_or_none(extra_block)
    hp = _int_or_none((record.get("player_before") or {}).get("current_hp"))
    max_hp = _int_or_none((record.get("player_before") or {}).get("max_hp"))
    baseline_block = _int_or_none(record.get("projected_block_before"))
    if (
        extra_block is None or extra_block <= 0
        or hp is None or hp <= 0 or max_hp is None or max_hp <= 0
    ):
        return False
    if baseline_block is None or baseline_block < 0:
        return False
    relic_ids = {
        _normalized_id(value) for value in record.get("relic_ids_before") or []
    }
    if "markofthebloom" in relic_ids:
        return False
    potion_ids = {
        _normalized_id(potion.get("id"))
        for potion in record.get("potions_before") or []
        if isinstance(potion, dict)
    }
    if "fairypotion" not in potion_ids:
        return False
    power_ids = {
        _normalized_id(power.get("id") or power.get("name"))
        for power in (record.get("player_before") or {}).get("powers") or []
        if isinstance(power, dict) and int(power.get("amount") or 0) > 0
    }
    if power_ids & {"buffer", "bufferpower", "intangible", "intangibleplayer"}:
        return False
    # This exemption is intentionally limited to ordinary Defends.  A block
    # card which draws, exhausts, applies a debuff, or advances another engine
    # can be valuable even when its Block does not change the Fairy revival.
    playable_ids = {
        _normalized_id(value)
        for value in (record.get("end_turn_resources") or {}).get(
            "playable_card_ids", []
        )
    }
    if not playable_ids or not playable_ids <= {
        "defendr", "defendg", "defendb",
    }:
        return False
    if record.get("orichalcum_active_before") is True:
        return False
    # A Defend can still be offensive through play/block triggers.  Decline
    # the exemption for engines whose exact damage is not reconstructed here;
    # Juggernaut is safe only when one packet cannot kill any living attacker.
    if power_ids & {
        "athousandcuts", "thousandcuts", "panache",
    }:
        return False
    if relic_ids & {"letteropener"}:
        return False
    if any(
        max(0, _int_or_none(record.get(field)) or 0) > 0
        for field in (
            "projected_end_turn_hp_loss_before",
            "projected_next_turn_start_hp_loss_before",
        )
    ):
        return False
    packets = []
    living_enemy_hp = []
    for monster in record.get("monsters_before") or []:
        if not isinstance(monster, dict):
            return False
        if (
            monster.get("is_gone") is True
            or monster.get("half_dead") is True
            or (_int_or_none(monster.get("current_hp")) or 0) <= 0
        ):
            continue
        living_enemy_hp.append(_int_or_none(monster.get("current_hp")) or 0)
        intent = str(monster.get("intent") or "").upper()
        if "ATTACK" not in intent:
            continue
        damage = _int_or_none(monster.get("move_adjusted_damage"))
        hits = _int_or_none(monster.get("move_hits"))
        if damage is None or hits is None or damage < 0 or hits < 1:
            return False
        packets.extend([damage] * hits)
    if not packets:
        return False

    juggernaut = max(
        (
            _int_or_none(power.get("amount")) or 0
            for power in (record.get("player_before") or {}).get("powers") or []
            if isinstance(power, dict)
            and _normalized_id(power.get("id") or power.get("name"))
            in {"juggernaut", "juggernautpower"}
        ),
        default=0,
    )
    if juggernaut > 0 and any(
        0 < enemy_hp <= juggernaut for enemy_hp in living_enemy_hp
    ):
        return False

    revive_hp = max_hp * (60 if "sacredbark" in relic_ids else 30) // 100
    revive_hp = max(1, revive_hp)

    def final_state(block):
        current_hp = hp
        revived = False
        for packet in packets:
            absorbed = min(block, packet)
            block -= absorbed
            unblocked = packet - absorbed
            if "torii" in relic_ids and 1 < unblocked <= 5:
                unblocked = 1
            if "tungstenrod" in relic_ids and unblocked > 0:
                unblocked -= 1
            current_hp -= unblocked
            if current_hp <= 0:
                if revived:
                    return (True, False, 0)
                revived = True
                current_hp = revive_hp
        return (revived, True, current_hp)

    baseline = final_state(baseline_block)
    with_extra = final_state(baseline_block + extra_block)
    return baseline[0] is True and baseline == with_extra


def _passive_end_block(record):
    """Return the guaranteed end-turn Block already present in a frame.

    The value is intentionally limited to relic/power packets whose timing is
    encoded in the trace.  It must never infer a generic enemy intent or a
    future card; unknown state therefore remains zero/fail-closed.
    """

    record = record if isinstance(record, dict) else {}
    relic_ids = {
        _normalized_id(value)
        for value in (record.get("relic_ids_before") or [])
    }
    amount = 6 if "orichalcum" in relic_ids else 0
    powers = (record.get("player_before") or {}).get("powers") or []
    for power in powers:
        if not isinstance(power, dict):
            continue
        if _normalized_id(power.get("id") or power.get("name")) in {
            "metallicize", "metallicizepower",
        }:
            amount += _safe_nonnegative_int(power.get("amount"))
    return amount


def _max_affordable_block(cards, energy):
    """Return the largest serialized immediate block affordable this turn."""

    return max(
        (block for block, _cards_played in _affordable_block_options(cards, energy)),
        default=0,
    )


def _time_warp_context(record):
    """Return Time Eater's remaining cards and forced-end Strength damage.

    The audit only accuses a policy when a serialized mitigation line is
    definitely legal and reduces the pre-action attack loss.  Playing the
    final Time Warp card grants Time Eater two Strength before its attack, so
    that otherwise ordinary block can be exactly cancelled.  Model the
    explicit counter and hit count rather than treating a hand-energy knapsack
    as executable.
    """

    for monster in record.get("monsters_before") or []:
        if _normalized_id(monster.get("id")) != "timeeater":
            continue
        time_warp = next(
            (
                power for power in monster.get("powers") or []
                if _normalized_id(power.get("id")) in {
                    "timewarp", "timewarppower",
                }
            ),
            None,
        )
        if time_warp is None:
            continue
        played = _int_or_none(time_warp.get("amount"))
        if played is None or not 0 <= played < 12:
            continue
        remaining = 12 - played
        hits = max(0, int(monster.get("move_hits") or 0))
        return remaining, 2 * hits
    return None, 0


def _projected_end_turn_healing(record, preceding_hp_loss=0):
    """Reconstruct deterministic Regeneration for legacy and current traces."""

    explicit = _int_or_none(record.get("projected_end_turn_healing_before"))
    if explicit is not None:
        return max(0, explicit)
    player = record.get("player_before") or {}
    regeneration = max(
        (
            _safe_nonnegative_int(power.get("amount"))
            for power in player.get("powers") or []
            if isinstance(power, dict)
            and _normalized_id(power.get("id") or power.get("name"))
            in {"regeneration", "regenerationpower"}
        ),
        default=0,
    )
    if regeneration <= 0:
        return 0
    if any(
        _normalized_id(relic_id) == "magicflower"
        for relic_id in record.get("relic_ids_before") or []
    ):
        regeneration = (regeneration * 3 + 1) // 2
    current_hp = _int_or_none(
        player.get("current_hp")
        if player.get("current_hp") is not None
        else record.get("hp_before")
    )
    max_hp = _int_or_none(player.get("max_hp"))
    if current_hp is None or max_hp is None:
        return 0
    hp_at_heal = max(
        0, current_hp - max(0, int(preceding_hp_loss or 0))
    )
    if hp_at_heal <= 0:
        return 0
    return min(max(0, max_hp - hp_at_heal), regeneration)


def _max_affordable_direct_damage(cards, energy):
    """Independent lower bound for deterministic single-target damage.

    The audit deliberately ignores Choke triggers, poison, Vulnerable setup,
    draw and potions.  It flags a missed Split interrupt only when serialized
    attacks alone already prove that the threshold is affordable.
    """

    energy = max(0, int(energy or 0))
    best = [0] * (energy + 1)
    observable = False
    for card in cards:
        if not isinstance(card, dict) or not card.get("is_playable"):
            continue
        if str(card.get("type") or "").upper() != "ATTACK":
            continue
        damage = _int_or_none(card.get("damage"))
        cost = _int_or_none(card.get("cost"))
        if damage is None or cost is None or damage <= 0 or cost < 0:
            continue
        observable = True
        card_id = _normalized_id(card.get("id"))
        hits = _DETERMINISTIC_ATTACK_HITS.get(card_id, 1)
        if card_id == "pummel":
            hits += max(0, int(card.get("upgrades") or 0))
        guaranteed = max(0, damage) * hits
        if cost > energy:
            continue
        for available in range(energy, cost - 1, -1):
            best[available] = max(
                best[available], best[available - cost] + guaranteed
            )
    return max(best) if observable else None


def _record_affordable_attack_candidates(record, energy=None):
    """Return conservative, directly observable attack opportunities.

    This is deliberately smaller than the live planner's search.  It is an
    audit lower bound: only a serialized playable attack, its current cost,
    and its raw damage are used.  X-cost attacks are ignored at zero energy,
    while a positive-energy X attack is represented by its current raw
    damage field.  The helper is used to prove that an END/Choker decision
    had an executable damage-progress alternative; it is not a replacement
    for tactical scoring.
    """

    if energy is None:
        energy = _int_or_none(record.get("energy_before"))
    if energy is None:
        player = record.get("player_before") or {}
        energy = _int_or_none(player.get("energy"))
    if energy is None:
        return []
    energy = max(0, int(energy))
    candidates = []
    for card in record.get("hand_before") or []:
        if not isinstance(card, dict):
            continue
        if not card.get("is_playable"):
            continue
        if str(card.get("type") or "").upper() != "ATTACK":
            continue
        damage = _int_or_none(card.get("damage"))
        if damage is None:
            damage = _int_or_none(card.get("base_damage"))
        if damage is None or damage <= 0:
            continue
        raw_cost = _int_or_none(card.get("cost"))
        if raw_cost is None:
            continue
        if raw_cost == -1:
            if energy <= 0:
                continue
            cost = energy
        else:
            cost = max(0, raw_cost)
        if cost > energy:
            continue
        candidates.append({
            "id": card.get("id"),
            "card_instance_id": card.get("card_instance_id"),
            "cost": raw_cost,
            "damage": max(0, int(damage)),
            "has_target": bool(card.get("has_target")),
        })
    return candidates


def _record_enemy_reaction_hazard(record):
    """Whether attacking can carry a known immediate reaction hazard."""

    hazardous_ids = {
        "timeeater", "gremlinnob", "corruptheart", "spike", "spiker",
        "writhingmass",
    }
    hazardous_powers = {
        "beatofdeath", "beatofdeathpower", "thorns", "thornspower",
        "sharp hide", "sharphide", "sharphidepower",
        "compulsive", "compulsivepower",
    }
    for monster in record.get("monsters_before") or []:
        if not isinstance(monster, dict):
            continue
        if _safe_nonnegative_int(monster.get("current_hp")) <= 0:
            continue
        if _normalized_id(monster.get("id")) in hazardous_ids:
            return True
        for power in monster.get("powers") or []:
            if not isinstance(power, dict):
                continue
            power_id = _normalized_id(power.get("id") or power.get("name"))
            if power_id in hazardous_powers and _safe_nonnegative_int(
                power.get("amount")
            ) > 0:
                return True
    return False


def _deferred_attack_enemy_hp_loss(cohort, record_index, record):
    """Settle a repeated Attack after intervening selection screens.

    Double Tap/Echo Form can repeat Headbutt while each copy opens a GRID.
    The PLAY receipt then observes only the first hit.  Compare the planner's
    complete first-action prediction with the first subsequent combat frame,
    before another combat action is issued, and require exact enemy-instance
    continuity throughout.
    """

    search = ((record.get("decision") or {}).get("search") or {})
    outcome = record.get("decision_outcome") or {}
    if not (
        str(outcome.get("phase_after") or "").upper()
        in {"GRID", "HAND_SELECT"}
        and isinstance(search.get("first_action_resolution_count"), int)
        and not isinstance(search.get("first_action_resolution_count"), bool)
        and search.get("first_action_resolution_count") > 1
    ):
        return None
    before_by_id = {
        monster.get("enemy_instance_id"): monster
        for monster in (record.get("monsters_before") or [])
        if isinstance(monster, dict)
        and monster.get("enemy_instance_id") is not None
        and _int_or_none(monster.get("current_hp")) is not None
    }
    if not before_by_id:
        return None
    combat_id = record.get("combat_id")
    turn = _int_or_none(record.get("turn"))
    for following in cohort[record_index + 1:]:
        if not isinstance(following, dict):
            continue
        if following.get("attempt_id") != record.get("attempt_id"):
            return None
        following_monsters = following.get("monsters_before")
        if not isinstance(following_monsters, list) or not following_monsters:
            continue
        if (
            following.get("combat_id") != combat_id
            or _int_or_none(following.get("turn")) != turn
        ):
            return None
        after_by_id = {
            monster.get("enemy_instance_id"): monster
            for monster in following_monsters
            if isinstance(monster, dict)
            and monster.get("enemy_instance_id") is not None
            and _int_or_none(monster.get("current_hp")) is not None
        }
        if any(enemy_id not in after_by_id for enemy_id in before_by_id):
            return None
        return sum(
            max(
                0,
                int(before.get("current_hp") or 0)
                - int(after_by_id[enemy_id].get("current_hp") or 0),
            )
            for enemy_id, before in before_by_id.items()
        )
    return None


def _persistent_barricade_monster(record):
    """Return the sole live enemy whose Block survives the turn boundary.

    This is intentionally narrower than a generic ``Block > 0`` check.  The
    audit must not call a normal temporary-Block fight a liveness failure:
    attacking into ordinary Block can be wrong because of Thorns, Sharp Hide,
    Beat of Death, or a lethal incoming attack.  A live Barricade/Spheric
    Guardian is different—the Block is durable and therefore a repeated turn
    with neither HP loss nor a new record-low Block is observable no-progress.
    Multiple live enemies remain fail-closed because target selection and
    their independent Block lifecycles cannot be inferred from this compact
    turn trace alone.
    """

    live = [
        monster for monster in (record.get("monsters_before") or [])
        if isinstance(monster, dict)
        and not monster.get("is_gone")
        and not monster.get("half_dead")
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
    ]
    if len(live) != 1:
        return None
    monster = live[0]
    power_ids = {
        _normalized_id(power.get("id") or power.get("name"))
        for power in (monster.get("powers") or [])
        if isinstance(power, dict)
    }
    monster_id = _normalized_id(monster.get("id"))
    if not ({"barricade", "barricadepower"} & power_ids):
        # Some historical bridge frames omitted the intrinsic Barricade
        # power from Spheric Guardian while retaining the authoritative enemy
        # id and persistent Block.  Keep that compatibility path explicit and
        # do not generalize it to arbitrary enemies.
        if monster_id != "sphericguardian":
            return None
    return monster


def _record_attack_can_cross_current_block(record, candidates):
    """Prove at least one candidate can reach enemy HP, not just Block."""

    active = [
        monster for monster in (record.get("monsters_before") or [])
        if isinstance(monster, dict)
        and not monster.get("is_gone")
        and not monster.get("half_dead")
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
    ]
    if not active or not candidates:
        return False
    target_block = max(
        0,
        min(
            _safe_nonnegative_int(monster.get("block"))
            for monster in active
        ),
    )
    return any(
        int(candidate.get("damage") or 0) > target_block
        for candidate in candidates
    )


def _record_passive_doom_proof(record):
    """Recompute the narrow proof behind ``all_enemies_passively_doomed``.

    The live planner is allowed to end a turn when deterministic Poison,
    Stone Calendar, Combust, or end-of-turn Lightning kills every remaining
    enemy before an enemy action.
    A serialized reason is not proof by itself: old traces sometimes carried
    the reason while omitting the orb/power facts needed to verify it.  Keep
    this checker deliberately conservative.  Poison must kill every living
    target independently; Lightning is accepted only for one living target,
    where random orb targeting is deterministic.  Unknown or capped damage is
    never turned into a pass.
    """

    player = record.get("player_before")
    monsters = [
        monster for monster in (record.get("monsters_before") or [])
        if isinstance(monster, dict)
        and not monster.get("is_gone")
        and not monster.get("half_dead")
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
    ]
    if not isinstance(player, dict) or not monsters:
        return {
            "status": "unproven",
            "reason": "missing_living_enemy_or_player_state",
            "living_enemy_count": len(monsters),
        }
    hp_before = _int_or_none(record.get("hp_before"))
    if hp_before is None:
        hp_before = _int_or_none(player.get("current_hp"))
    end_turn_loss = _int_or_none(
        record.get("projected_end_turn_hp_loss_before")
    )
    if hp_before is None or end_turn_loss is None:
        return {
            "status": "unproven",
            "reason": "missing_player_end_turn_loss",
            "living_enemy_count": len(monsters),
        }
    if max(0, end_turn_loss) >= max(1, hp_before):
        return {
            "status": "unproven",
            "reason": "player_end_turn_loss_is_lethal",
            "living_enemy_count": len(monsters),
            "player_hp": hp_before,
            "player_end_turn_hp_loss": max(0, end_turn_loss),
        }

    def power_amount(monster, names):
        wanted = set(names)
        return max(
            (
                _safe_nonnegative_int(power.get("amount"))
                for power in monster.get("powers") or []
                if isinstance(power, dict)
                and _normalized_id(power.get("id") or power.get("name"))
                in wanted
            ),
            default=0,
        )

    poison_proof = []
    poison_complete = True
    for monster in monsters:
        hp = _safe_nonnegative_int(monster.get("current_hp"))
        if power_amount(monster, {"invincible", "invinciblepower"}) > 0:
            poison_complete = False
            break
        poison = power_amount(monster, {"poison"})
        intangible = power_amount(
            monster, {"intangible", "intangiblepower"}
        ) > 0
        poison_damage = 1 if intangible and poison > 0 else poison
        if poison_damage < hp:
            poison_complete = False
            break
        poison_proof.append({
            "enemy_instance_id": monster.get("enemy_instance_id"),
            "id": monster.get("id"),
            "source": "poison",
            "hp_before": hp,
            "poison_damage": poison_damage,
        })
    if poison_complete and poison_proof:
        return {
            "status": "proven",
            "source": "poison",
            "living_enemy_count": len(monsters),
            "targets": poison_proof,
        }

    relic_ids = {
        _normalized_id(relic_id)
        for relic_id in (record.get("relic_ids_before") or [])
    }
    turn = _int_or_none(record.get("turn"))
    if turn == 7 and "stonecalendar" in relic_ids:
        calendar_proof = []
        calendar_complete = True
        any_poison = False
        for monster in monsters:
            if power_amount(
                monster, {"invincible", "invinciblepower"}
            ) > 0:
                calendar_complete = False
                break
            hp = _safe_nonnegative_int(monster.get("current_hp"))
            block = _safe_nonnegative_int(monster.get("block"))
            intangible = power_amount(
                monster, {"intangible", "intangiblepower"}
            ) > 0
            packet = 1 if intangible else 52
            calendar_hp_damage = max(0, packet - block)
            poison = power_amount(monster, {"poison"})
            poison_damage = 1 if intangible and poison > 0 else poison
            any_poison = any_poison or poison_damage > 0
            if calendar_hp_damage + poison_damage < hp:
                calendar_complete = False
                break
            calendar_proof.append({
                "enemy_instance_id": monster.get("enemy_instance_id"),
                "id": monster.get("id"),
                "source": (
                    "stone_calendar_and_poison"
                    if poison_damage else "stone_calendar"
                ),
                "hp_before": hp,
                "block_before": block,
                "stone_calendar_packet": packet,
                "stone_calendar_hp_damage": calendar_hp_damage,
                "poison_damage": poison_damage,
            })
        if calendar_complete and calendar_proof:
            return {
                "status": "proven",
                "source": (
                    "poison_and_stone_calendar"
                    if any_poison else "stone_calendar"
                ),
                "living_enemy_count": len(monsters),
                "targets": calendar_proof,
            }

    # Combust is one deterministic end-of-turn packet to every living enemy.
    # Unlike Poison it must first cross Block, and Intangible caps the packet
    # to one.  Combining it with serialized Poison is still deterministic and
    # lets the audit prove mixed passive kills instead of trusting the live
    # planner's reason string.
    combust = power_amount(player, {"combust", "combustpower"})
    if combust > 0:
        combust_proof = []
        combust_complete = True
        any_poison = False
        for monster in monsters:
            if power_amount(
                monster, {"invincible", "invinciblepower"}
            ) > 0:
                combust_complete = False
                break
            hp = _safe_nonnegative_int(monster.get("current_hp"))
            block = _safe_nonnegative_int(monster.get("block"))
            intangible = power_amount(
                monster, {"intangible", "intangiblepower"}
            ) > 0
            packet = 1 if intangible else combust
            combust_hp_damage = max(0, packet - block)
            poison = power_amount(monster, {"poison"})
            poison_damage = 1 if intangible and poison > 0 else poison
            any_poison = any_poison or poison_damage > 0
            if combust_hp_damage + poison_damage < hp:
                combust_complete = False
                break
            combust_proof.append({
                "enemy_instance_id": monster.get("enemy_instance_id"),
                "id": monster.get("id"),
                "source": "combust_and_poison" if poison_damage else "combust",
                "hp_before": hp,
                "block_before": block,
                "combust_packet": packet,
                "combust_hp_damage": combust_hp_damage,
                "poison_damage": poison_damage,
            })
        if combust_complete and combust_proof:
            return {
                "status": "proven",
                "source": (
                    "poison_and_combust" if any_poison else "combust"
                ),
                "living_enemy_count": len(monsters),
                "targets": combust_proof,
            }

    # Lightning's random target is deterministic only with one living enemy.
    if len(monsters) != 1:
        return {
            "status": "unproven",
            "reason": "multiple_living_enemies_without_per_target_poison",
            "living_enemy_count": len(monsters),
        }
    target = monsters[0]
    if power_amount(target, {"invincible", "invinciblepower"}) > 0:
        return {
            "status": "unproven",
            "reason": "target_has_unresolved_damage_cap",
            "living_enemy_count": 1,
        }
    hp = _safe_nonnegative_int(target.get("current_hp"))
    block = _safe_nonnegative_int(target.get("block"))
    orbs = player.get("orbs")
    if not isinstance(orbs, list):
        return {
            "status": "unproven",
            "reason": "missing_orb_state",
            "living_enemy_count": 1,
        }
    lightning_packets = [
        _safe_nonnegative_int(orb.get("passive_amount"))
        for orb in orbs
        if isinstance(orb, dict)
        and _normalized_id(orb.get("id") or orb.get("orb_id"))
        == "lightning"
    ]
    if (
        lightning_packets
        and "cables" in relic_ids
        and _normalized_id((orbs[0] or {}).get("id")) == "lightning"
    ):
        lightning_packets.append(lightning_packets[0])
    if not lightning_packets:
        return {
            "status": "unproven",
            "reason": "no_deterministic_passive_damage",
            "living_enemy_count": 1,
        }
    intangible = power_amount(
        target, {"intangible", "intangiblepower"}
    ) > 0
    total_damage = 0
    for packet in lightning_packets:
        amount = 1 if intangible and packet > 0 else packet
        absorbed = min(block, amount)
        block -= absorbed
        total_damage += max(0, amount - absorbed)
        hp -= max(0, amount - absorbed)
        if hp <= 0:
            return {
                "status": "proven",
                "source": "single_target_lightning_orbs",
                "living_enemy_count": 1,
                "targets": [{
                    "enemy_instance_id": target.get("enemy_instance_id"),
                    "id": target.get("id"),
                    "hp_before": _safe_nonnegative_int(
                        target.get("current_hp")
                    ),
                    "block_before": _safe_nonnegative_int(
                        target.get("block")
                    ),
                    "passive_damage": total_damage,
                    "lightning_packets": lightning_packets,
                }],
            }
    return {
        "status": "unproven",
        "reason": "passive_damage_does_not_cross_hp_and_block",
        "living_enemy_count": 1,
        "passive_damage": total_damage,
        "target_hp": _safe_nonnegative_int(target.get("current_hp")),
        "target_block": _safe_nonnegative_int(target.get("block")),
        "lightning_packets": lightning_packets,
    }


def _deck_size(record):
    counts = (record.get("decision_context") or {}).get("deck_counts")
    if not isinstance(counts, dict):
        return None
    try:
        return sum(max(0, int(count or 0)) for count in counts.values())
    except (TypeError, ValueError):
        return None


def _normalized_id(value):
    return "".join(
        character
        for character in str(value or "").split("+", 1)[0].lower()
        if character.isalnum()
    )


def _retained_debuff_potion_blocked_by_artifact(potion_id, combat_record):
    """Prove that a retained targeted debuff potion had no legal effect.

    Artifact consumes Weak, Vulnerable, or Poison application before the
    debuff is applied.  Suppress the terminal-potion finding only when the
    last authoritative combat frame exposes every living target and each one
    has a positive Artifact amount.  Missing or malformed power evidence
    remains actionable/fail-closed.
    """

    if potion_id not in _ARTIFACT_BLOCKED_DEBUFF_POTION_IDS:
        return False
    if not isinstance(combat_record, dict):
        return False
    monsters = combat_record.get("monsters_before")
    if not isinstance(monsters, list):
        return False
    living = [
        monster for monster in monsters
        if isinstance(monster, dict)
        and isinstance(monster.get("current_hp"), (int, float))
        and not isinstance(monster.get("current_hp"), bool)
        and monster.get("current_hp") > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]
    if not living:
        return False
    for monster in living:
        powers = monster.get("powers")
        if not isinstance(powers, list):
            return False
        artifact_amounts = [
            _int_or_none(power.get("amount"))
            for power in powers
            if isinstance(power, dict)
            and _normalized_id(power.get("id", power.get("name")))
            == "artifact"
        ]
        if not artifact_amounts or not any(
            amount is not None and amount > 0 for amount in artifact_amounts
        ):
            return False
    return True


def _terminal_retained_potion_opportunities(retained_ids, combat_records):
    """Find exact earlier mitigation that was retained into the same death.

    A potion name alone does not prove that drinking it would have helped.
    Restrict the terminal finding to deterministic, serialized end-turn
    counterfactuals: a retained potion had to save material HP on a critical
    turn in the combat that subsequently killed the player.
    """

    retained_ids = set(retained_ids or ())
    if not retained_ids:
        return []
    opportunities = []
    seen = set()
    for record in combat_records or []:
        if not isinstance(record, dict) or record.get("action") != "end":
            continue
        hp = _int_or_none(record.get("hp_before"))
        player = record.get("player_before")
        player = player if isinstance(player, dict) else {}
        max_hp = _int_or_none(player.get("max_hp"))
        attack_loss = _int_or_none(
            record.get("projected_attack_hp_loss_before")
        )
        end_loss = _int_or_none(
            record.get("projected_end_turn_hp_loss_before")
        )
        next_loss = _int_or_none(
            record.get("projected_next_turn_start_hp_loss_before")
        )
        total_loss = _int_or_none(record.get("projected_hp_loss_before"))
        healing = _int_or_none(
            record.get("projected_end_turn_healing_before")
        )
        if (
            hp is None or hp <= 0 or max_hp is None or max_hp < hp
            or attack_loss is None or end_loss is None or next_loss is None
            or total_loss is None or total_loss <= 0
            or attack_loss + end_loss + next_loss != total_loss
        ):
            continue
        healing = max(0, healing or 0)
        post_turn_hp = hp - total_loss + healing
        critical_floor = max(6, (max_hp + 9) // 10)
        critical = total_loss >= hp or post_turn_hp <= critical_floor
        if not critical:
            continue

        potions = record.get("potions_before")
        if not isinstance(potions, list):
            continue
        present_ids = {
            _normalized_id(potion.get("id"))
            for potion in potions
            if isinstance(potion, dict)
        } & retained_ids
        if not present_ids:
            continue
        relic_ids = {
            _normalized_id(relic_id)
            for relic_id in record.get("relic_ids_before") or []
        }
        bark = 2 if "sacredbark" in relic_ids else 1
        block_amounts = {
            "blockpotion": 12 * bark,
            "essenceofsteel": 4 * bark,
            "heartofiron": 6 * bark,
        }

        reductions = {}
        for potion_id, block_amount in block_amounts.items():
            if potion_id in present_ids:
                reductions[potion_id] = min(attack_loss, block_amount)

        monsters = record.get("monsters_before")
        player_block = _int_or_none(record.get("player_block_before"))
        if player_block is None:
            player_block = _int_or_none(player.get("block"))
        if isinstance(monsters, list) and player_block is not None:
            raw_incoming = 0
            attack_hits = 0
            weak_reductions = []
            attack_surface_exact = True
            for monster in monsters:
                if not isinstance(monster, dict):
                    attack_surface_exact = False
                    break
                if (
                    monster.get("is_gone") is True
                    or monster.get("half_dead") is True
                    or (_int_or_none(monster.get("current_hp")) or 0) <= 0
                ):
                    continue
                intent = str(monster.get("intent") or "").upper()
                if "ATTACK" not in intent:
                    continue
                damage = _int_or_none(monster.get("move_adjusted_damage"))
                hits = _int_or_none(monster.get("move_hits"))
                if damage is None or damage < 0 or hits is None or hits <= 0:
                    attack_surface_exact = False
                    break
                raw_incoming += damage * hits
                attack_hits += hits
                powers = monster.get("powers")
                powers = powers if isinstance(powers, list) else []
                has_artifact = any(
                    _normalized_id(power.get("id", power.get("name")))
                    == "artifact"
                    and (_int_or_none(power.get("amount")) or 0) > 0
                    for power in powers if isinstance(power, dict)
                )
                already_weak = any(
                    _normalized_id(power.get("id", power.get("name")))
                    in {"weak", "weakened"}
                    and (_int_or_none(power.get("amount")) or 0) > 0
                    for power in powers if isinstance(power, dict)
                )
                if not has_artifact and not already_weak:
                    numerator, denominator = (
                        (3, 5) if "paperkrane" in relic_ids else (3, 4)
                    )
                    weakened = damage * numerator // denominator
                    weak_reductions.append((damage - weakened) * hits)
            baseline_attack = max(0, raw_incoming - max(0, player_block))
            if attack_surface_exact and baseline_attack == attack_loss:
                if "weakpotion" in present_ids and weak_reductions:
                    weak_raw = max(weak_reductions)
                    weakened_attack = max(
                        0,
                        raw_incoming - weak_raw - max(0, player_block),
                    )
                    reductions["weakpotion"] = max(
                        0, attack_loss - weakened_attack
                    )
                if "ghostinajar" in present_ids:
                    ghost_attack = max(
                        0, attack_hits - max(0, player_block)
                    )
                    reductions["ghostinajar"] = max(
                        0, attack_loss - ghost_attack
                    )

        for potion_id, reduction in reductions.items():
            reduction = max(0, int(reduction or 0))
            if reduction < 3:
                continue
            key = (
                record.get("combat_id"), _int_or_none(record.get("turn")),
                potion_id,
            )
            if key in seen:
                continue
            seen.add(key)
            candidate_loss = max(0, total_loss - reduction)
            if total_loss >= hp and candidate_loss >= hp:
                # Spending a potion on an otherwise unchanged, still-fatal
                # line is not actionable survival evidence.  A different
                # earlier critical turn may still prove the retention bug.
                continue
            opportunities.append({
                "potion_id": potion_id,
                "combat_id": record.get("combat_id"),
                "turn": _int_or_none(record.get("turn")),
                "before_seq": _int_or_none(record.get("before_seq")),
                "hp_before": hp,
                "max_hp": max_hp,
                "projected_hp_loss": total_loss,
                "projected_end_turn_healing": healing,
                "projected_hp_after": post_turn_hp,
                "protected_hp_loss": candidate_loss,
                "hp_loss_reduction": reduction,
                "prevents_immediate_lethal": (
                    total_loss >= hp and candidate_loss < hp
                ),
            })
    return opportunities


def _relic_token(value):
    """Match the predictor's display/compact relic identifier normalization."""

    return str(value or "").replace("_", " ").replace("-", " ").lower()


def _record_relic_coverage(record):
    """Reclassify persisted relic ids with the current coverage registry.

    Traces intentionally store the raw relic ids as well as the coverage
    result.  Recomputing from the raw ids lets a later audit understand an old
    trace after a harmless alias/registry correction (for example
    ``PreservedInsect`` or ``Potion Belt``) without pretending the relic's
    combat effect was missing during the run.
    """

    raw_ids = record.get("relic_ids_before")
    if not isinstance(raw_ids, list):
        value = record.get("relic_model_coverage")
        if not isinstance(value, dict):
            return {}
        # Old traces may have persisted a partial relic as ``modeled``.  The
        # audit must not repeat that stale exactness claim merely because the
        # raw-id field predates trace schema v4.  Recover the set of present
        # ids from every old/new category and reclassify it below.
        raw_ids = []
        for key in (
            "exact_branch_relic_ids", "modeled_relic_ids",
            "state_reflected_relic_ids", "heuristic_relic_ids",
            "unsupported_relic_ids", "noncombat_relic_ids",
            "unclassified_relic_ids",
        ):
            items = value.get(key)
            if isinstance(items, list):
                raw_ids.extend(items)
    present = sorted({_relic_token(item) for item in raw_ids if item})
    exact_registry = {
        _relic_token(item)
        for item in _combat_predictor.EXACT_BRANCH_COMBAT_RELIC_IDS
    }
    reflected_registry = {
        _relic_token(item)
        for item in _combat_predictor.STATE_REFLECTED_RELIC_IDS
    }
    noncombat_registry = {
        _relic_token(item)
        for item in _combat_predictor.NONCOMBAT_RELIC_IDS
    }
    heuristic_registry = {
        _relic_token(item)
        for item in _combat_predictor.HEURISTIC_COMBAT_RELIC_IDS
    }
    unsupported_registry = {
        _relic_token(item)
        for item in _combat_predictor.UNSUPPORTED_COMBAT_RELIC_IDS
    }
    exact_branch = [item for item in present if item in exact_registry]
    state_reflected = [item for item in present if item in reflected_registry]
    heuristic = [item for item in present if item in heuristic_registry]
    unsupported = [item for item in present if item in unsupported_registry]
    noncombat = [item for item in present if item in noncombat_registry]
    unclassified = [
        item for item in present
        if item not in exact_registry
        and item not in reflected_registry
        and item not in heuristic_registry
        and item not in unsupported_registry
        and item not in noncombat_registry
    ]
    return {
        "coverage_contract_version": 2,
        "exact_branch_relic_ids": exact_branch,
        # Backward-compatible alias.  It is deliberately exact-only.
        "modeled_relic_ids": exact_branch,
        "state_reflected_relic_ids": state_reflected,
        "heuristic_relic_ids": heuristic,
        "unsupported_relic_ids": unsupported,
        "noncombat_relic_ids": noncombat,
        "unclassified_relic_ids": unclassified,
        "exact_branch_count": len(exact_branch),
        "modeled_count": len(exact_branch),
        "state_reflected_count": len(state_reflected),
        "heuristic_count": len(heuristic),
        "unsupported_count": len(unsupported),
        "noncombat_count": len(noncombat),
        "unclassified_count": len(unclassified),
    }


def _record_relic_strategy_coverage(record):
    """Recompute per-relic decision coverage from authoritative raw ids."""

    raw_ids = record.get("relic_ids_before")
    if not isinstance(raw_ids, list):
        mechanics = _record_relic_coverage(record)
        raw_ids = []
        for key in (
            "exact_branch_relic_ids", "state_reflected_relic_ids",
            "heuristic_relic_ids", "unsupported_relic_ids",
            "noncombat_relic_ids", "unclassified_relic_ids",
        ):
            raw_ids.extend(mechanics.get(key) or [])
    return _combat_predictor.relic_strategy_coverage({
        "relics": [{"id": relic_id} for relic_id in raw_ids if relic_id]
    })


def _relic_strategy_audit_report(records, issues):
    """Build handler-level strategy evidence for every observed relic.

    Mechanics coverage alone cannot prove that the policy used a relic well.
    This report separates a modeled decision boundary from passive state,
    heuristic/unsupported behavior, and entirely new ids.  Violations are
    joined back to the exact action record rather than inferred from aggregate
    issue counts, so Pocketwatch and Calipers can identify the responsible
    turn in a historical trace.
    """

    per_relic = {}
    record_by_key = {}
    centennial_triggered_combats = set()
    discard_card_ids = {
        "acrobatics", "alloutattack", "calculatedgamble", "concentrate",
        "daggerthrow", "prepared", "stormofsteel", "survivor", "unload",
    }

    def record_key(record):
        return (
            record.get("attempt_id"), record.get("run_id"),
            _int_or_none(record.get("before_seq")),
        )

    def entry(relic_id, category, handler=None):
        value = per_relic.setdefault(relic_id, {
            "category": category,
            "handler": handler,
            "occurrences": 0,
            "eligible": 0,
            "evaluated": 0,
            "violations": 0,
            "issue_kinds": Counter(),
        })
        # A raw alias must never move between categories in one report.  If
        # it does, preserve the stricter declaration for diagnostics.
        if value["category"] != category:
            value["category"] = "unclassified"
            value["handler"] = None
        return value

    for record in records:
        key = record_key(record)
        record_by_key[key] = record
        strategy = _record_relic_strategy_coverage(record)
        categories = (
            ("decision_modeled", "decision_modeled_relic_ids"),
            ("passive", "passive_relic_ids"),
            ("heuristic", "heuristic_relic_ids"),
            ("unsupported", "unsupported_relic_ids"),
            ("noncombat", "noncombat_relic_ids"),
            ("unclassified", "unclassified_relic_ids"),
        )
        action = record.get("action")
        combat_action = action in {"play", "potion", "end"} and (
            str(record.get("phase") or "").startswith("COMBAT_TURN_")
            or "projected_hp_loss_before" in record
        )
        for category, field in categories:
            for relic_id in strategy.get(field) or []:
                handler = (strategy.get("decision_handlers") or {}).get(
                    relic_id
                )
                value = entry(relic_id, category, handler)
                value["occurrences"] += 1
                if category in {"heuristic", "unsupported", "unclassified"}:
                    if combat_action:
                        value["eligible"] += 1
                    continue
                if category != "decision_modeled" or not combat_action:
                    continue

                eligible = False
                evaluated = False
                if handler == "three_card_turn_budget":
                    eligible = action in {"play", "end"}
                    evaluated = eligible and _int_or_none(
                        record.get("turn")
                    ) is not None
                elif handler == "retained_block_decay":
                    eligible = action == "end" or (
                        action == "play"
                        and any(
                            isinstance(card, dict)
                            and card.get("card_instance_id")
                            == record.get("card_instance_id")
                            and _safe_nonnegative_int(card.get("block")) > 0
                            for card in record.get("hand_before") or []
                        )
                    )
                    evaluated = eligible and _int_or_none(
                        record.get("player_block_before")
                    ) is not None
                elif handler == "retained_energy":
                    eligible = action == "end"
                    evaluated = eligible and _int_or_none(
                        record.get("energy_before")
                    ) is not None
                elif handler == "first_discard_energy":
                    eligible = (
                        action == "play"
                        and _normalized_id(record.get("card_id"))
                        in discard_card_ids
                    )
                    search = (record.get("decision") or {}).get("search")
                    evaluated = bool(
                        eligible
                        and isinstance(search, dict)
                        and _int_or_none(
                            search.get("hovering_kite_energy_gained")
                        ) is not None
                    )
                elif handler == "shuffle_counter_energy":
                    search = (record.get("decision") or {}).get("search")
                    required_fields = (
                        "sundial_draw_pile_size",
                        "sundial_discard_pile_size",
                        "sundial_counter",
                        "sundial_shuffle_count",
                        "sundial_energy_gained",
                    )
                    has_sundial_search_evidence = bool(
                        isinstance(search, dict)
                        and any(
                            search.get(field) is not None
                            for field in required_fields
                        )
                    )
                    eligible = (
                        action == "play"
                        and (
                            _normalized_id(record.get("card_id"))
                            in _SUNDIAL_ACTION_CARD_IDS
                            or has_sundial_search_evidence
                        )
                    )
                    evaluated = bool(
                        eligible
                        and isinstance(search, dict)
                        and all(
                            _int_or_none(search.get(field)) is not None
                            for field in required_fields
                        )
                    )
                elif handler == "symmetric_strength_growth":
                    eligible = action in {"play", "end"}
                    player = record.get("player_before") or {}
                    monsters = record.get("monsters_before") or []
                    evaluated = bool(
                        eligible
                        and isinstance(player.get("powers"), list)
                        and isinstance(monsters, list)
                        and monsters
                        and all(
                            isinstance(monster, dict)
                            and isinstance(monster.get("powers"), list)
                            for monster in monsters
                        )
                        and _int_or_none(
                            record.get("projected_hp_loss_before")
                        ) is not None
                    )
                elif handler == "state_reflected_half_hp_strength":
                    # The live frame already includes Red Skull's active
                    # Strength in resolved card damage.  The policy treats
                    # the exact half-HP boundary as action-sensitive: healing
                    # potions which cross it are forced through an
                    # authoritative re-plan, while card self-damage can only
                    # make the existing bounded damage estimate conservative.
                    # Require the complete state needed to establish that
                    # boundary instead of accepting the relic id alone.
                    eligible = action in {"play", "potion", "end"}
                    player = record.get("player_before") or {}
                    hp = _int_or_none(record.get("hp_before"))
                    max_hp = _int_or_none(player.get("max_hp"))
                    evaluated = bool(
                        eligible
                        and hp is not None
                        and max_hp is not None
                        and max_hp > 0
                        and 0 <= hp <= max_hp
                        and isinstance(player.get("powers"), list)
                        and isinstance(record.get("relic_ids_before"), list)
                        and isinstance(record.get("hand_before"), list)
                        and isinstance(record.get("monsters_before"), list)
                        and _int_or_none(
                            record.get("projected_hp_loss_before")
                        ) is not None
                    )
                elif handler == "hp_loss_future_block_value":
                    decision = record.get("decision") or {}
                    search = decision.get("search") or {}
                    raw_events = search.get(
                        "self_forming_clay_hp_loss_events"
                    )
                    observed_events = (
                        raw_events
                        if type(raw_events) is int and raw_events >= 0
                        else None
                    )
                    legacy_self_cost = max(
                        0,
                        _int_or_none(
                            search.get("voluntary_self_hp_cost")
                        ) or 0,
                        _int_or_none(search.get("reactive_hp_cost")) or 0,
                        _int_or_none(decision.get("card_self_hp_cost")) or 0,
                    )
                    eligible = bool(
                        action == "play"
                        and (
                            (observed_events or 0) > 0
                            or legacy_self_cost > 0
                        )
                    )
                    raw_future_block = search.get(
                        "self_forming_clay_future_block"
                    )
                    future_block = (
                        raw_future_block
                        if type(raw_future_block) is int
                        and raw_future_block >= 0
                        else None
                    )
                    credit = _finite_number(
                        search.get("self_forming_clay_credit")
                    )
                    applied_credit = _finite_number(
                        search.get("self_forming_clay_applied_credit")
                    )
                    current_mitigation = _finite_number(
                        search.get(
                            "self_forming_clay_current_turn_mitigation"
                        )
                    )
                    authority = search.get(
                        "self_forming_clay_credit_authority"
                    )
                    candidate_search = search.get("candidate")
                    candidate_search = (
                        candidate_search
                        if isinstance(candidate_search, dict)
                        else {}
                    )
                    true_combat_end = bool(
                        search.get("true_combat_end") is True
                        or candidate_search.get("true_combat_end") is True
                    )
                    evaluated = bool(
                        eligible
                        and observed_events is not None
                        and future_block == 3 * observed_events
                        and credit is not None
                        and math.isclose(
                            credit,
                            min(9, future_block) * 0.45,
                            rel_tol=1e-9,
                            abs_tol=1e-9,
                        )
                        and applied_credit is not None
                        and math.isclose(
                            applied_credit,
                            (
                                0.0
                                if true_combat_end
                                else credit
                            ),
                            rel_tol=1e-9,
                            abs_tol=1e-9,
                        )
                        and current_mitigation == 0.0
                        and authority in {
                            "planner_dynamic_hp_loss_events",
                            "offering_prefix_hp_loss_events",
                        }
                    )
                elif handler == "first_hp_loss_draw_value":
                    combat_key = (
                        record.get("attempt_id"), record.get("run_id"),
                        record.get("combat_id"),
                    )
                    outcome = record.get("decision_outcome") or {}
                    damage_model = record.get("damage_model") or {}
                    hp_before = _int_or_none(record.get("hp_before"))
                    hp_after = _int_or_none(record.get("hp_after"))
                    observed_loss = max(
                        0,
                        _int_or_none(outcome.get("player_hp_loss")) or 0,
                        -(_int_or_none(outcome.get("hp_delta")) or 0),
                        (
                            hp_before - hp_after
                            if hp_before is not None and hp_after is not None
                            else 0
                        ),
                        (
                            _int_or_none(
                                damage_model.get("monsters_to_hero_actual")
                            ) or 0
                            if action == "end" else 0
                        ),
                    )
                    eligible = bool(
                        record.get("combat_id")
                        and combat_key not in centennial_triggered_combats
                        and observed_loss > 0
                    )
                    if eligible and action in {"play", "potion"}:
                        search = (record.get("decision") or {}).get(
                            "search"
                        ) or {}
                        voluntary_cost = max(
                            0,
                            _int_or_none(
                                search.get("voluntary_self_hp_cost")
                            ) or 0,
                        )
                        comparison = search.get(
                            "voluntary_self_damage_comparison"
                        )
                        evaluated = bool(
                            hp_before is not None
                            and hp_after is not None
                            and isinstance(record.get("hand_before"), list)
                            and isinstance(
                                record.get("monsters_before"), list
                            )
                            and (
                                voluntary_cost == 0
                                or (
                                    isinstance(comparison, dict)
                                    and comparison.get("evaluated") is True
                                )
                            )
                        )
                    elif eligible and action == "end":
                        predicted = _int_or_none(
                            damage_model.get("monsters_to_hero_predicted")
                        )
                        actual = _int_or_none(
                            damage_model.get("monsters_to_hero_actual")
                        )
                        evaluated = bool(
                            hp_before is not None
                            and (
                                predicted is not None and actual is not None
                                or (
                                    _int_or_none(
                                        record.get(
                                            "projected_hp_loss_before"
                                        )
                                    ) is not None
                                    and _int_or_none(
                                        outcome.get("hp_delta")
                                    ) is not None
                                )
                            )
                        )
                    if observed_loss > 0 and record.get("combat_id"):
                        centennial_triggered_combats.add(combat_key)
                else:
                    # Exact branch handlers are evaluated by the complete
                    # combat snapshot and the mechanics consistency checks.
                    eligible = True
                    evaluated = bool(
                        isinstance(record.get("relic_ids_before"), list)
                        and isinstance(record.get("hand_before"), list)
                        and isinstance(record.get("monsters_before"), list)
                    )
                value["eligible"] += int(eligible)
                value["evaluated"] += int(evaluated)

    handler_issue_kinds = {
        "retained_block_decay": {"end_turn_with_resources"},
        "three_card_turn_budget": {"redundant_covered_block_play"},
        "per_turn_card_limit": {
            "offering_at_choker_limit", "choker_slots_without_attack_progress",
        },
        "symmetric_strength_growth": {
            "card_damage_overprediction", "card_damage_underprediction",
            "end_turn_damage_overprediction",
            "end_turn_damage_underprediction",
        },
        "first_hp_loss_draw_value": {
            "avoidable_voluntary_self_damage",
            "avoidable_loss_end_turn_candidate",
        },
        "hp_loss_future_block_value": {
            "avoidable_voluntary_self_damage",
        },
    }
    for issue in issues:
        record = record_by_key.get(record_key(issue))
        if record is None:
            continue
        strategy = _record_relic_strategy_coverage(record)
        handlers = strategy.get("decision_handlers") or {}
        for relic_id, handler in handlers.items():
            allowed = handler_issue_kinds.get(handler, set())
            if issue.get("kind") not in allowed:
                continue
            if (
                handler == "three_card_turn_budget"
                and _safe_nonnegative_int(issue.get("turn_card_count")) != 4
            ):
                continue
            value = per_relic.get(relic_id)
            if value is None:
                continue
            value["violations"] += 1
            value["issue_kinds"][str(issue.get("kind"))] += 1

    totals = Counter()
    normalized = {}
    for relic_id, value in sorted(per_relic.items()):
        eligible = int(value["eligible"])
        evaluated = int(value["evaluated"])
        unknown = max(0, eligible - evaluated)
        violations = int(value["violations"])
        if violations:
            status = "issues"
        elif unknown:
            status = "inconclusive"
        elif eligible:
            status = "clear"
        else:
            status = "not_applicable"
        totals.update({
            "eligible": eligible,
            "evaluated": evaluated,
            "unknown": unknown,
            "violations": violations,
        })
        normalized[relic_id] = {
            **{
                key: item for key, item in value.items()
                if key != "issue_kinds"
            },
            "unknown": unknown,
            "status": status,
            "issue_kinds": dict(value["issue_kinds"]),
        }
    contract = (
        _combat_predictor.relic_strategy_coverage_contract_violations()
    )
    contract_broken = _recursive_has_values(contract)
    status = (
        "issues"
        if totals["violations"] or contract_broken
        else "inconclusive"
        if totals["unknown"]
        else "clear"
        if totals["evaluated"]
        else "not_applicable"
    )
    return {
        "coverage_contract_version": (
            _combat_predictor.RELIC_STRATEGY_COVERAGE_CONTRACT_VERSION
        ),
        "status": status,
        "contract_violations": contract,
        "eligible": int(totals["eligible"]),
        "evaluated": int(totals["evaluated"]),
        "unknown": int(totals["unknown"]),
        "violations": int(totals["violations"]),
        "per_relic": normalized,
    }


def _record_power_coverage(record):
    """Reclassify active powers from raw trace state with today's registry.

    Power coverage is intentionally not trusted as a persisted verdict.  An
    old run should immediately expose (or clear) a modeling gap after the
    registry changes, without replaying the game.  Schema-v2+ decisions keep
    raw player/monster powers, so prefer those fields over any stored summary.
    """

    player = record.get("player_before")
    monsters = record.get("monsters_before")
    if isinstance(player, dict) or isinstance(monsters, list):
        return _combat_predictor.power_model_coverage({
            "combat_state": {
                "player": player if isinstance(player, dict) else {},
                "monsters": monsters if isinstance(monsters, list) else [],
            },
        })

    value = record.get("power_model_coverage")
    if not isinstance(value, dict):
        return {}
    active = value.get("active_powers")
    if not isinstance(active, list):
        # There is no safe role-aware reconstruction from category-only ids.
        # Returning the old object would preserve a potentially false exact
        # claim, which is worse than marking this legacy trace unavailable.
        return {}

    reconstructed_player = {"powers": []}
    reconstructed_monsters = {}
    for entry in active:
        if not isinstance(entry, dict):
            continue
        power = {
            "id": entry.get("raw_id") or entry.get("normalized_id"),
            "name": entry.get("raw_name") or entry.get("raw_id"),
            "amount": entry.get("amount"),
        }
        if entry.get("owner_role") == "monster":
            owner_id = entry.get("owner_instance_id") or "legacy:monster"
            monster = reconstructed_monsters.setdefault(owner_id, {
                "enemy_instance_id": owner_id,
                "monster_index": entry.get("monster_index"),
                "powers": [],
            })
            monster["powers"].append(power)
        else:
            reconstructed_player["powers"].append(power)
    return _combat_predictor.power_model_coverage({
        "combat_state": {
            "player": reconstructed_player,
            "monsters": list(reconstructed_monsters.values()),
        },
    })


def _safe_nonnegative_int(value):
    """Coerce untrusted trace metrics without making the audit fail."""

    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _safe_nonnegative_float(value):
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _int_or_none(value):
    """Return an exact integer for an observable trace value, else ``None``."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def _coverage_summary(counter, violations=0):
    eligible = int(counter.get("eligible", 0))
    evaluated = int(counter.get("evaluated", 0))
    unknown = max(0, eligible - evaluated)
    if violations:
        status = "issues"
    elif unknown:
        status = "inconclusive"
    elif not eligible:
        status = "not_applicable"
    else:
        status = "clear"
    summary = {
        "eligible": eligible,
        "evaluated": evaluated,
        "unknown": unknown,
        "violations": int(violations),
        "status": status,
    }
    for key, value in counter.items():
        if key not in {"eligible", "evaluated", "violations"}:
            summary[key] = int(value)
    return summary


def _model_error(item):
    value = item.get("error_class")
    return str(value) if value is not None and value != "" else None


def _is_remote_consultation(item):
    """Identify an actual provider round trip, excluding cache and skips."""

    if item.get("local_cache_hit"):
        return False
    if _safe_nonnegative_int(item.get("latency_ms")) > 0:
        return True
    if str(item.get("transport") or "").strip():
        return True
    usage = item.get("usage")
    return isinstance(usage, dict) and any(
        _safe_nonnegative_int(usage.get(key)) > 0
        for key in _REMOTE_USAGE_TOKEN_KEYS
    )


def _is_valid_model_recommendation(item):
    return (
        not _model_error(item)
        and isinstance(item.get("model_choice_id"), str)
        and bool(item["model_choice_id"].strip())
    )


def _consultation_outcome(item):
    """Classify consultation value without counting menu closes as adoption.

    New producer records carry an explicit label.  The conservative fallback
    keeps older traces auditable while treating action:* navigation as a
    protocol confirmation rather than a semantic strategy adoption.
    """

    explicit = str(item.get("consultation_outcome") or "").strip()
    if explicit:
        return explicit
    status = str(item.get("status") or "").casefold()
    rule = str(item.get("rule_choice_id") or "")
    model = str(item.get("model_choice_id") or "")
    applied = item.get("applied") is True

    def protocol(value):
        token = value.casefold()
        return token.startswith("action:") or token in {
            "return", "leave", "cancel", "proceed", "confirm",
        }

    if status in {"skipped", "fallback", "shadow_queued", "not_consulted"}:
        return "consultation_suppressed"
    if applied and model and model != rule:
        return (
            "protocol_transition_change"
            if protocol(rule) and protocol(model)
            else "choice_change"
        )
    if status == "agreed" and model == rule:
        return "protocol_confirmation" if protocol(rule) else "local_confirmation"
    if model or item.get("advice_id"):
        return "review_no_choice_change"
    return "not_classified"


def _is_model_contract_error(error):
    if not error:
        return False
    token = str(error).lower()
    return (
        token.startswith("invalid_")
        or token.startswith("unexpected_")
        or "fingerprint" in token
        or token in {"model_drift", "schema_drift"}
    )


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _choice_aliases(value):
    """Return conservative exact/token aliases for one bound choice id."""

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return set()
    raw = str(value).strip().casefold()
    if not raw:
        return set()
    token = "".join(character for character in raw if character.isalnum())
    aliases = {raw}
    if token:
        aliases.add(token)
    if ":" in raw:
        parts = raw.split(":")
        suffix = parts[-1]
        # Numeric suffixes in shop candidate ids are inventory slots, not the
        # dynamically reindexed protocol choice.  UUID/coordinate suffixes
        # are stable bindings and are safe aliases.
        if suffix and not suffix.isdigit():
            aliases.add(suffix)
            suffix_token = "".join(
                character for character in suffix if character.isalnum()
            )
            if suffix_token:
                aliases.add(suffix_token)
        if parts[0] == "shop" and len(parts) >= 3:
            shop_item = parts[-2]
            aliases.add(shop_item)
            shop_token = "".join(
                character for character in shop_item
                if character.isalnum()
            )
            if shop_token:
                aliases.add(shop_token)
    if "@" in raw:
        coordinate = raw.rsplit("@", 1)[-1]
        aliases.add(coordinate)
        coordinate_token = "".join(
            character for character in coordinate if character.isalnum()
        )
        if coordinate_token:
            aliases.add(coordinate_token)
    return aliases


def _same_choice(left, right):
    return bool(_choice_aliases(left) & _choice_aliases(right))


def _actual_choice_aliases(record):
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    aliases = set()
    for key in (
        "candidate_id", "chosen", "chosen_id", "chosen_index",
        "selected", "selected_id", "selected_index",
    ):
        aliases.update(_choice_aliases(decision.get(key)))
    for key in (
        "requested_target_id", "resolved_target_id",
    ):
        aliases.update(_choice_aliases(record.get(key)))
    for key in ("selected_choice_ids", "final_choice_ids"):
        values = record.get(key)
        if isinstance(values, list):
            for value in values:
                aliases.update(_choice_aliases(value))

    chosen = record.get("chosen_option_before")
    chosen = chosen if isinstance(chosen, dict) else {}
    for key in ("option_id", "choice_index", "label"):
        aliases.update(_choice_aliases(chosen.get(key)))
    target = chosen.get("target")
    target = target if isinstance(target, dict) else {}
    for key in (
        "id", "label", "x", "y", "symbol", "card_instance_id",
        "relic_id",
    ):
        aliases.update(_choice_aliases(target.get(key)))
    card = target.get("card")
    if isinstance(card, dict):
        for key in ("id", "name", "card_instance_id"):
            aliases.update(_choice_aliases(card.get(key)))
    relic = target.get("relic")
    if isinstance(relic, dict):
        for key in ("id", "name"):
            aliases.update(_choice_aliases(relic.get(key)))
    item = target.get("item")
    if isinstance(item, dict):
        for key in (
            "id", "name", "label", "item_id", "card_instance_id",
            "potion_instance_id", "relic_id",
        ):
            aliases.update(_choice_aliases(item.get(key)))
    reward = target.get("reward")
    if isinstance(reward, dict):
        for key in ("id", "label", "reward_type"):
            aliases.update(_choice_aliases(reward.get(key)))
        reward_relic = reward.get("relic")
        if isinstance(reward_relic, dict):
            for key in ("id", "name"):
                aliases.update(_choice_aliases(reward_relic.get(key)))
    if all(target.get(key) is not None for key in ("symbol", "x", "y")):
        aliases.update(_choice_aliases(
            f"{target['symbol']}@{target['x']},{target['y']}"
        ))
    return aliases


def _candidate_rows(record):
    """Return unmerged candidate evidence from each producer channel.

    Keeping rows separate here is important: merging by a producer-provided
    id before looking at the raw protocol options would hide duplicate and
    many-to-one candidates.  Cross-channel aliases are merged only after the
    auditor has independently proved that both bind one same visible option.
    """

    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    rows = []

    def add(row, source, source_index):
        if not isinstance(row, dict):
            return
        candidate_id = row.get(
            "choice_id", row.get("candidate_id", row.get("id"))
        )
        if candidate_id is None:
            return
        candidate_id = str(candidate_id)
        current = {
            "candidate_id": candidate_id,
            "score": None,
            "facts": None,
            "consequences": None,
            "sources": [source],
            "source": source,
            "source_index": source_index,
        }
        score = (
            row.get("decision_surface_score")
            if source == "model_replay"
            and row.get("decision_surface_score") is not None
            else row.get("local_score", row.get("score"))
        )
        score = _finite_number(score)
        if score is not None:
            current["score"] = score
        facts = row.get("facts")
        if isinstance(facts, dict):
            current["facts"] = facts
            if isinstance(facts.get("consequences"), dict):
                current["consequences"] = facts["consequences"]
        if isinstance(row.get("consequences"), dict):
            current["consequences"] = row["consequences"]
        for key in (
            "choice_id", "choice_index", "semantic_id", "label",
            "action", "operation", "selection_eligible", "veto_reason",
            "local_reason", "reason_codes", "score_rule_id",
            "score_formula", "score_inputs", "score_components",
            "decision_surface_score",
        ):
            if key in row:
                current[key] = row.get(key)
        rows.append(current)

    for index, row in enumerate(decision.get("candidates") or []):
        add(row, "decision", index)
    advice = decision.get("model_advice")
    advice = advice if isinstance(advice, dict) else {}
    replay = advice.get("replay")
    replay = replay if isinstance(replay, dict) else {}
    for index, row in enumerate(replay.get("candidates") or []):
        add(row, "model_replay", index)

    explicit = decision.get("candidate_consequences")
    if isinstance(explicit, dict):
        for index, (candidate_id, consequences) in enumerate(
            explicit.items()
        ):
            matches = [
                row for row in rows
                if _same_choice(candidate_id, row.get("candidate_id"))
            ]
            if matches:
                for current in matches:
                    current["sources"].append("decision_consequences")
                    if isinstance(consequences, dict):
                        current["consequences"] = consequences
            else:
                add({
                    "candidate_id": candidate_id,
                    "consequences": consequences,
                }, "decision_consequences", index)
    return rows


def _neow_lament_elite_depths(row):
    if not isinstance(row, dict):
        return None
    consequences = row.get("consequences")
    consequences = consequences if isinstance(consequences, dict) else {}
    summary = consequences.get("route_summary")
    if not isinstance(summary, dict):
        facts = row.get("facts")
        facts = facts if isinstance(facts, dict) else {}
        summary = facts.get("route_summary")
    if not isinstance(summary, dict):
        return None
    paths = summary.get("legal_path_options")
    if not isinstance(paths, list):
        return None
    depths = []
    for path in paths:
        if not isinstance(path, dict):
            return None
        values = path.get("neow_lament_one_hp_elite_depths")
        if not isinstance(values, list) or any(
            not isinstance(value, int) or isinstance(value, bool)
            or value < 0
            for value in values
        ):
            return None
        depths.extend(values)
    return sorted(set(depths))


def _score_semantic_terms(row):
    """Return normalized concepts explicitly represented in a score contract.

    Numeric recomputation alone cannot prove that a score is complete.  Keep
    the input and component names so event-specific semantic audits can check
    that advertised irreversible rewards and costs were actually priced.
    """

    if not isinstance(row, dict):
        return set()
    terms = set()
    inputs = row.get("score_inputs")
    if isinstance(inputs, dict):
        terms.update(_normalized_id(key) for key in inputs)
    components = row.get("score_components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            terms.add(_normalized_id(component.get("name")))
            terms.add(_normalized_id(component.get("input")))
    return {term for term in terms if term}


def _missing_score_concepts(row, requirements):
    terms = _score_semantic_terms(row)
    missing = []
    for concept, fragments in requirements:
        if not any(
            all(_normalized_id(fragment) in term for fragment in fragments)
            for term in terms
        ):
            missing.append(concept)
    return missing


def _option_aliases(option):
    aliases = set()
    if not isinstance(option, dict):
        return aliases
    for key in (
        "option_id", "choice_id", "choice_index", "semantic_id", "label",
    ):
        aliases.update(_choice_aliases(option.get(key)))
    candidate_ids = option.get("candidate_ids")
    if isinstance(candidate_ids, list):
        for candidate_id in candidate_ids:
            aliases.update(_choice_aliases(candidate_id))
    index = option.get("choice_index")
    if index is not None:
        aliases.update(_choice_aliases(f"event:{index}"))
        aliases.update(_choice_aliases(f"option:{index}"))
    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    for key in ("id", "label", "card_instance_id", "relic_id"):
        aliases.update(_choice_aliases(target.get(key)))
    card = target.get("card")
    if isinstance(card, dict):
        for key in ("id", "name", "card_instance_id"):
            aliases.update(_choice_aliases(card.get(key)))
    relic = target.get("relic")
    if isinstance(relic, dict):
        for key in ("id", "name"):
            aliases.update(_choice_aliases(relic.get(key)))
    item = target.get("item")
    if isinstance(item, dict):
        for key in (
            "id", "name", "label", "item_id", "card_instance_id",
            "potion_instance_id", "relic_id",
        ):
            aliases.update(_choice_aliases(item.get(key)))
    if all(target.get(key) is not None for key in ("symbol", "x", "y")):
        aliases.update(_choice_aliases(
            f"{target['symbol']}@{target['x']},{target['y']}"
        ))
    return aliases


def _option_text(option):
    if not isinstance(option, dict):
        return ""
    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    return " ".join(str(value or "") for value in (
        option.get("label"), target.get("label"), target.get("text"),
    )).strip()


def _strategic_noncombat_multichoice(record):
    options = record.get("legal_choices_before")
    if not isinstance(options, list) or not options:
        options = record.get("available_options_before")
    if not isinstance(options, list) or not options:
        # Current v2 decision frames use the non-suffixed names for the raw
        # protocol surface.  Keep this recognition narrow to an explicit
        # multi-choice screen; combat turns never satisfy the phase gate.
        options = record.get("canonical_choices")
    if not isinstance(options, list) or not options:
        options = record.get("available_options")
    return bool(
        record.get("action") == "choose"
        and str(record.get("phase") or "") in _STRATEGIC_NONCOMBAT_PHASES
        and isinstance(options, list)
        and len(options) >= 2
    )


def _typed_choice_key(row):
    if not isinstance(row, dict):
        return None
    choice_index = row.get("choice_index")
    if choice_index is not None and (
        not isinstance(choice_index, int) or isinstance(choice_index, bool)
    ):
        return None
    action = row.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    if "operation" not in row:
        return None
    operation = row.get("operation")
    if operation is not None and (
        not isinstance(operation, str) or not operation.strip()
    ):
        return None
    return choice_index, action.strip().casefold(), (
        operation.strip().casefold() if isinstance(operation, str) else None
    )


def _command_allows_canonical_action(
    action, commands, *, phase=None, raw_command=None,
):
    normalized = {
        str(command).strip().casefold() for command in commands
        if isinstance(command, str) and command.strip()
    }
    action = str(action or "").strip().casefold()
    phase = str(phase or "").strip().upper()
    raw_command = str(raw_command or "").strip().casefold()
    if phase == "HAND_SELECT" and action == "proceed":
        # The structured controller calls this action ``proceed``, while the
        # HAND_SELECT protocol exposes the concrete command ``confirm``.
        # Require both sides of that exact mapping; do not accept a generic
        # alias that could make an unrelated synthetic command look legal.
        return raw_command == "confirm" and "confirm" in normalized
    if action in normalized:
        return True
    aliases = {
        "return": {"return", "skip", "leave"},
        "proceed": {"proceed"},
        "potion": {"potion"},
    }
    return bool(aliases.get(action, set()) & normalized)


def _canonical_noncombat_surface(record):
    """Validate the recorded canonical surface before using it for audit.

    Current traces include protocol options plus synthetic legal commands such
    as card-reward ``return`` and reward-screen ``proceed``.  Auditing only the
    raw option list therefore creates false extra-candidate findings.  This
    verifier accepts the richer surface only after independently proving its
    exact typed join to the producer rows and its coverage of raw options or
    protocol commands.
    """

    choices = record.get("legal_choices_before")
    if not isinstance(choices, list) or not choices:
        choices = record.get("canonical_choices")
    if not isinstance(choices, list) or not choices:
        return None, []
    decision_value = record.get("decision")
    decision_value = decision_value if isinstance(decision_value, dict) else {}
    producers = decision_value.get("candidates")
    producers = producers if isinstance(producers, list) else []
    if not producers:
        producers = record.get("candidates")
        producers = producers if isinstance(producers, list) else []
    if not producers:
        producers = record.get("producer_candidates")
        producers = producers if isinstance(producers, list) else []
    raw_options = record.get("available_options_before")
    raw_options = raw_options if isinstance(raw_options, list) else []
    if not raw_options:
        raw_options = record.get("available_options")
        raw_options = raw_options if isinstance(raw_options, list) else []
    commands = record.get("available_commands_before")
    commands = commands if isinstance(commands, list) else []
    if not commands:
        commands = record.get("available_commands")
        commands = commands if isinstance(commands, list) else []
    violations = []

    choice_ids = [
        choice.get("choice_id") if isinstance(choice, dict) else None
        for choice in choices
    ]
    if any(
        not isinstance(choice_id, str) or not choice_id.strip()
        for choice_id in choice_ids
    ):
        violations.append("canonical_choice_id_missing")
    if len({choice_id for choice_id in choice_ids if choice_id}) != len(choices):
        violations.append("canonical_choice_id_duplicate")

    producer_matches = [[] for _producer in producers]
    for choice_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            violations.append(f"canonical_choice_not_object:{choice_index}")
            continue
        key = _typed_choice_key(choice)
        matches = [
            producer_index
            for producer_index, producer in enumerate(producers)
            if _typed_choice_key(producer) == key and key is not None
        ]
        if len(matches) != 1:
            violations.append(
                f"canonical_producer_typed_join:{choice_index}:{len(matches)}"
            )
            continue
        producer_index = matches[0]
        producer_matches[producer_index].append(choice_index)
        producer = producers[producer_index]
        producer_id = producer.get(
            "choice_id", producer.get("candidate_id", producer.get("id"))
        )
        candidate_ids = choice.get("candidate_ids")
        if (
            choice.get("candidate_binding") != "unique"
            or not isinstance(candidate_ids, list)
            or candidate_ids != [producer_id]
        ):
            violations.append(f"canonical_candidate_binding:{choice_index}")
        if choice.get("producer_candidate_raw") != producer:
            violations.append(f"canonical_producer_raw_binding:{choice_index}")
        if producer.get("semantic_id") != choice.get("semantic_id"):
            violations.append(f"canonical_semantic_binding:{choice_index}")

    if any(len(matches) != 1 for matches in producer_matches):
        violations.append("canonical_producer_coverage")

    raw_option_ids = []
    for option_index, option in enumerate(raw_options):
        if not isinstance(option, dict):
            violations.append(f"raw_option_not_object:{option_index}")
            continue
        option_id = option.get("option_id")
        raw_option_ids.append(option_id)
        matches = [
            choice for choice in choices
            if isinstance(choice, dict)
            and choice.get("choice_id") == option_id
        ]
        if len(matches) != 1:
            violations.append(
                f"canonical_raw_option_join:{option_index}:{len(matches)}"
            )
            continue
        choice = matches[0]
        if (
            choice.get("choice_index") != option.get("choice_index")
            or choice.get("target") != option.get("target")
        ):
            violations.append(f"canonical_raw_option_binding:{option_index}")

    raw_option_ids = set(raw_option_ids)
    for choice_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        if choice.get("choice_id") in raw_option_ids:
            continue
        if not _command_allows_canonical_action(
            choice.get("action"), commands,
            phase=record.get("phase"), raw_command=choice.get("raw_text"),
        ):
            violations.append(f"canonical_command_binding:{choice_index}")
    return choices, sorted(set(violations))


def _candidate_matches_options(row, options):
    candidate_id = str(row.get("candidate_id") or "").strip().casefold()
    command_aliases = {
        "skip": "action:return",
        "return": "action:return",
        "leave": "action:return",
        "action:return": "action:return",
        "proceed": "action:proceed",
        "action:proceed": "action:proceed",
    }
    candidate_token = command_aliases.get(candidate_id, candidate_id)
    canonical_matches = []
    canonical_surface_seen = False
    for option in options:
        candidate_ids = option.get("candidate_ids") if isinstance(option, dict) else None
        if not isinstance(candidate_ids, list):
            continue
        canonical_surface_seen = True
        expected = {
            command_aliases.get(str(value).strip().casefold(), str(value).strip().casefold())
            for value in candidate_ids
            if value is not None
        }
        if candidate_token and candidate_token in expected:
            canonical_matches.append(option)
    if canonical_surface_seen:
        return canonical_matches
    aliases = _row_choice_aliases(row)
    return [
        option for option in options
        if aliases & _option_aliases(option)
    ]


def _evidence_values_compatible(left, right):
    """Allow one evidence channel to be a strict subset, never a conflict."""

    if isinstance(left, dict) and isinstance(right, dict):
        return all(
            _evidence_values_compatible(left[key], right[key])
            for key in set(left) & set(right)
        )
    if (
        isinstance(left, (int, float)) and not isinstance(left, bool)
        and isinstance(right, (int, float)) and not isinstance(right, bool)
    ):
        return math.isclose(
            float(left), float(right), rel_tol=0.0, abs_tol=1e-9
        )
    return left == right


def _option_identity(option):
    if not isinstance(option, dict):
        return None
    option_id = option.get("option_id", option.get("choice_id"))
    if isinstance(option_id, str) and option_id.strip():
        return f"option_id:{option_id.strip().casefold()}"
    index = option.get("choice_index")
    if isinstance(index, int) and not isinstance(index, bool):
        return f"choice_index:{index}"
    return None


def _candidate_option_bijection(rows, options):
    """Independently prove one semantic candidate per raw legal option."""

    option_identities = [_option_identity(option) for option in options]
    duplicate_option_ids = sorted({
        identity for identity in option_identities
        if identity is not None and option_identities.count(identity) > 1
    })
    missing_option_identity_indexes = [
        index for index, identity in enumerate(option_identities)
        if identity is None
    ]
    groups = {index: [] for index in range(len(options))}
    extra_candidate_ids = []
    ambiguous_candidate_ids = []
    for row in rows:
        matches = [
            index for index, option in enumerate(options)
            if _candidate_matches_options(row, [option])
        ]
        if not matches:
            extra_candidate_ids.append(row.get("candidate_id"))
        elif len(matches) > 1:
            ambiguous_candidate_ids.append(row.get("candidate_id"))
        else:
            groups[matches[0]].append(row)

    missing_option_ids = [
        options[index].get("option_id")
        for index, group in groups.items() if not group
    ]
    duplicate_candidate_ids = []
    score_conflict_candidate_ids = []
    evidence_conflict_candidate_ids = []
    merged = []
    for option_index, group in groups.items():
        if not group:
            continue
        source_counts = Counter(
            row.get("source") for row in group
            if row.get("source") != "decision_consequences"
        )
        if any(count > 1 for count in source_counts.values()):
            duplicate_candidate_ids.extend(
                row.get("candidate_id") for row in group
            )

        numeric_scores = [
            row.get("score") for row in group
            if row.get("score") is not None
        ]
        if numeric_scores and any(
            not math.isclose(
                numeric_scores[0], score, rel_tol=0.0, abs_tol=1e-3
            )
            for score in numeric_scores[1:]
        ):
            score_conflict_candidate_ids.extend(
                row.get("candidate_id") for row in group
            )

        for field in ("facts", "consequences"):
            values = [
                row.get(field) for row in group
                if isinstance(row.get(field), dict)
            ]
            if values and any(
                not _evidence_values_compatible(values[0], value)
                for value in values[1:]
            ):
                evidence_conflict_candidate_ids.extend(
                    row.get("candidate_id") for row in group
                )
        eligibility = [
            row.get("selection_eligible") for row in group
            if isinstance(row.get("selection_eligible"), bool)
        ]
        if eligibility and any(value != eligibility[0] for value in eligibility[1:]):
            evidence_conflict_candidate_ids.extend(
                row.get("candidate_id") for row in group
            )

        preferred = next(
            (row for row in group if row.get("source") == "decision"),
            group[0],
        )
        canonical = dict(preferred)
        replay_score = next(
            (
                row.get("score") for row in group
                if row.get("source") == "model_replay"
                and row.get("score") is not None
            ),
            None,
        )
        if replay_score is not None:
            canonical["score"] = replay_score
        if not isinstance(canonical.get("selection_eligible"), bool):
            canonical["selection_eligible"] = next(
                (
                    row.get("selection_eligible") for row in group
                    if isinstance(row.get("selection_eligible"), bool)
                ),
                True,
            )
        for field in ("facts", "consequences"):
            if canonical.get(field) is None:
                canonical[field] = next(
                    (
                        row.get(field) for row in group
                        if isinstance(row.get(field), dict)
                    ),
                    None,
                )
        canonical["sources"] = sorted({
            source
            for row in group for source in (row.get("sources") or [])
        })
        canonical["aliases"] = sorted({
            str(row.get("candidate_id")) for row in group
            if row.get("candidate_id") is not None
        })
        canonical["option_identity"] = option_identities[option_index]
        canonical["option_id"] = options[option_index].get(
            "option_id", options[option_index].get("choice_id")
        )
        # Keep the independently observed protocol target beside the merged
        # producer row.  Reward screens are sequential: gold/potions can be
        # collected independently while Sapphire Key replaces exactly one
        # linked relic.  The selection audit must recover that topology from
        # the protocol surface instead of treating every visible button as a
        # mutually exclusive score contest.
        target = options[option_index].get("target")
        canonical["target"] = target if isinstance(target, dict) else {}
        merged.append(canonical)

    invalid = bool(
        duplicate_option_ids
        or missing_option_identity_indexes
        or missing_option_ids
        or extra_candidate_ids
        or ambiguous_candidate_ids
        or duplicate_candidate_ids
        or score_conflict_candidate_ids
        or evidence_conflict_candidate_ids
    )
    merged.sort(key=lambda row: str(row.get("option_identity") or ""))
    evidence = {
        "complete": bool(options) and not invalid,
        "order_invariant": bool(options) and not invalid,
        "missing_option_ids": missing_option_ids,
        "missing_option_identity_indexes": missing_option_identity_indexes,
        "duplicate_option_ids": duplicate_option_ids,
        "extra_candidate_ids": sorted(str(item) for item in extra_candidate_ids),
        "ambiguous_candidate_ids": sorted(
            str(item) for item in ambiguous_candidate_ids
        ),
        "duplicate_candidate_ids": sorted(set(
            str(item) for item in duplicate_candidate_ids
        )),
        "score_conflict_candidate_ids": sorted(set(
            str(item) for item in score_conflict_candidate_ids
        )),
        "evidence_conflict_candidate_ids": sorted(set(
            str(item) for item in evidence_conflict_candidate_ids
        )),
        "raw_candidate_count": len(rows),
        "canonical_candidate_count": len(merged),
        "visible_option_count": len(options),
    }
    return merged, evidence


def _reward_target(row):
    target = row.get("target") if isinstance(row, dict) else None
    return target if isinstance(target, dict) else {}


def _reward_type(row):
    reward = _reward_target(row).get("reward")
    reward = reward if isinstance(reward, dict) else {}
    return str(reward.get("reward_type") or "").strip().casefold()


def _reward_relic_id(row):
    target = _reward_target(row)
    reward = target.get("reward")
    reward = reward if isinstance(reward, dict) else {}
    relic = reward.get("relic")
    relic = relic if isinstance(relic, dict) else target.get("relic")
    relic = relic if isinstance(relic, dict) else {}
    value = relic.get("id", relic.get("relic_id"))
    return str(value).strip() if value is not None and str(value).strip() else None


def _sapphire_linked_relic_id(row):
    target = _reward_target(row)
    reward = target.get("reward")
    reward = reward if isinstance(reward, dict) else {}
    link = target.get("link")
    link = link if isinstance(link, dict) else reward.get("link")
    link = link if isinstance(link, dict) else {}
    value = link.get("id", link.get("relic_id"))
    return str(value).strip() if value is not None and str(value).strip() else None


def _snapshot_sapphire_key(record, field):
    snapshot = record.get(field)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    game = snapshot.get("game_state")
    game = game if isinstance(game, dict) else snapshot
    keys = game.get("keys")
    keys = keys if isinstance(keys, dict) else {}
    value = keys.get("sapphire", game.get("has_sapphire_key"))
    return value if isinstance(value, bool) else None


def _reward_selection_competitors(record, selected_rows, eligible_rows):
    """Return the mutually-exclusive cohort for a sequential reward click.

    ``COMBAT_REWARD`` and ``SAPPHIRE_KEY`` screens expose several buttons,
    but ordinary rewards are independently collectible.  Only a Sapphire Key
    and the relic linked to that key are a real either/or decision.  Keeping
    this distinction local to the audit prevents a later key click from being
    falsely compared with uncollected gold, while preserving an exact score
    comparison against the linked relic.
    """

    phase = str(record.get("phase") or "").upper()
    if phase not in {"COMBAT_REWARD", "SAPPHIRE_KEY"} or len(selected_rows) != 1:
        return eligible_rows, "mutually_exclusive_surface"
    selected = selected_rows[0]
    target = _reward_target(selected)
    selected_kind = str(target.get("kind") or "").strip().casefold()
    selected_type = _reward_type(selected)

    sapphire_rows = [
        row for row in eligible_rows
        if str(_reward_target(row).get("kind") or "").strip().casefold()
        == "sapphire_key"
        or _reward_type(row) == "sapphire_key"
    ]
    linked_pairs = []
    for sapphire in sapphire_rows:
        linked_id = _sapphire_linked_relic_id(sapphire)
        if not linked_id:
            continue
        matches = [
            row for row in eligible_rows
            if _reward_type(row) == "relic"
            and _reward_relic_id(row) == linked_id
        ]
        if len(matches) == 1:
            linked_pairs.append((sapphire, matches[0]))

    for sapphire, relic in linked_pairs:
        if selected is not sapphire and selected is not relic:
            continue
        if str(record.get("goal_mode") or "").upper() != "HEART":
            break
        before_key = _snapshot_sapphire_key(
            record, "authoritative_state_before"
        )
        after_key = _snapshot_sapphire_key(
            record, "authoritative_state_after"
        )
        if before_key is not False:
            break
        if selected is sapphire and after_key is not True:
            break
        if selected is relic and after_key is not False:
            break
        return [sapphire, relic], "sapphire_key_linked_relic"

    # An independently collectible reward (including a reward-screen
    # confirmation) has no competing score at this click.  Its protocol
    # effect remains covered by the consequence/settlement auditors.
    if selected_kind in {"reward", "sapphire_key", "protocol_action"} or selected_type:
        return [selected], "independent_reward_click"
    return eligible_rows, "mutually_exclusive_surface"


def _coalesce_candidate_rows_by_options(rows, options):
    """Backward-compatible wrapper around the independent bijection."""

    return _candidate_option_bijection(rows, options)[0]


def _row_choice_aliases(row):
    """Return every identity retained while one visible option was merged."""

    if not isinstance(row, dict):
        return set()
    aliases = _choice_aliases(row.get("candidate_id"))
    for key in (
        "choice_id", "option_id", "choice_index", "semantic_id", "label",
    ):
        aliases.update(_choice_aliases(row.get(key)))
    for value in row.get("aliases") or []:
        aliases.update(_choice_aliases(value))
    facts = row.get("facts")
    if isinstance(facts, dict):
        for key in (
            "card_id", "item_id", "relic_id", "potion_id",
            "card_instance_id", "choice_index",
        ):
            aliases.update(_choice_aliases(facts.get(key)))
    return aliases


def _candidate_row_for_aliases(aliases, rows):
    matches = [
        row for row in rows if aliases & _row_choice_aliases(row)
    ]
    return matches[0] if len(matches) == 1 else None


def _selected_candidate(record, rows):
    exact_ids = []
    for key in (
        "selected_choice_ids", "final_choice_ids",
    ):
        values = record.get(key)
        if isinstance(values, list):
            exact_ids.extend(values)
    exact_ids.extend([
        record.get("requested_target_id"), record.get("resolved_target_id"),
    ])
    chosen = record.get("chosen_option_before")
    if isinstance(chosen, dict):
        exact_ids.append(chosen.get("option_id"))
    exact_tokens = {
        str(value).strip().casefold() for value in exact_ids
        if value is not None and str(value).strip()
    }
    exact_matches = [
        row for row in rows
        if exact_tokens & {
            str(row.get(field)).strip().casefold()
            for field in ("option_id", "choice_id", "candidate_id")
            if row.get(field) is not None
        }
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    return _candidate_row_for_aliases(_actual_choice_aliases(record), rows)


def _selection_count(record):
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    advice = decision.get("model_advice")
    advice = advice if isinstance(advice, dict) else {}
    replay = advice.get("replay")
    replay = replay if isinstance(replay, dict) else {}
    for value in (
        replay.get("selection_count"), decision.get("selection_count"),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    final_ids = advice.get("final_choice_ids")
    if isinstance(final_ids, list) and len(final_ids) > 1:
        return len(final_ids)
    chosen = decision.get("chosen")
    if isinstance(chosen, list) and len(chosen) > 1:
        return len(chosen)
    return 1


def _selected_candidate_rows(record, rows, previous_record=None):
    """Bind a complete single- or multi-select set, or fail closed."""

    count = _selection_count(record)
    observed = _selected_candidate(record, rows)
    if count == 1:
        return [observed] if observed is not None else None

    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    advice = decision.get("model_advice")
    advice = advice if isinstance(advice, dict) else {}
    sources = []
    final_ids = advice.get("final_choice_ids")
    if isinstance(final_ids, list):
        sources.append(final_ids)
    chosen = decision.get("chosen")
    if isinstance(chosen, list):
        sources.append(chosen)
    if (
        isinstance(previous_record, dict)
        and str(record.get("phase") or "").upper() in {"GRID", "HAND_SELECT"}
        and str(previous_record.get("phase") or "").upper()
        == str(record.get("phase") or "").upper()
        and previous_record.get("after_seq") == record.get("before_seq")
        and _selection_count(previous_record) == count
    ):
        previous_decision = previous_record.get("decision")
        previous_decision = (
            previous_decision if isinstance(previous_decision, dict) else {}
        )
        previous_advice = previous_decision.get("model_advice")
        previous_advice = (
            previous_advice if isinstance(previous_advice, dict) else {}
        )
        previous_final_ids = previous_advice.get("final_choice_ids")
        if isinstance(previous_final_ids, list):
            # Multi-card GRID/HAND_SELECT operations are emitted as one
            # decision record per click.  Only the first record necessarily
            # retains the planner's UUID-complete set; later records carry the
            # one protocol option selected in that frame.  Inherit the set
            # only across an exact adjacent screen transition.
            sources.append(previous_final_ids)
    for identifiers in sources:
        if len(identifiers) != count:
            continue
        selected = []
        for identifier in identifiers:
            exact = [
                row for row in rows
                if str(identifier).strip().casefold() in {
                    str(row.get(field)).strip().casefold()
                    for field in (
                        "option_id", "choice_id", "candidate_id",
                    )
                    if row.get(field) is not None
                }
            ]
            row = (
                exact[0] if len(exact) == 1
                else _candidate_row_for_aliases(
                    _choice_aliases(identifier), rows
                )
            )
            if row is None or any(row is item for item in selected):
                selected = []
                break
            selected.append(row)
        if (
            len(selected) == count
            and observed is not None
            and any(observed is item for item in selected)
        ):
            return selected
    return None


def _hand_select_card_instance_id(row):
    """Return one exact protocol card UUID from a candidate/option row."""

    if not isinstance(row, dict):
        return None
    target = row.get("target")
    target = target if isinstance(target, dict) else {}
    card = target.get("card")
    card = card if isinstance(card, dict) else {}
    value = target.get("card_instance_id", card.get("card_instance_id"))
    value = str(value or "").strip()
    return value or None


def _hand_select_plan_sequence(value):
    """Normalize the planner sequence that can justify a temporary veto."""

    if not isinstance(value, list) or not value:
        return None
    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        card_uuid = str(
            entry.get("card_instance_id", entry.get("card_uuid")) or ""
        ).strip()
        card_id = str(entry.get("card_id") or "").strip()
        if not card_uuid or not card_id:
            return None
        normalized.append({
            "card_instance_id": card_uuid,
            "card_id": card_id,
        })
    return normalized


def _hand_select_plan_protection_validation(
    record, rows, selected_rows, previous_record,
):
    """Independently validate a HAND_SELECT combat-plan preservation veto.

    A producer may exclude a card only when the immediately preceding, bound
    combat action actually consumed the first card of an exact current-turn
    planner line and the excluded UUIDs are the visible *remaining* cards of
    that line.  This prevents a convenient low local score from being hidden
    behind an unbound ``selection_eligible=false`` declaration.
    """

    if not isinstance(record, dict) or not isinstance(rows, list):
        return None
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    evidence = decision.get("combat_plan_protection")
    plan_veto_rows = [
        row for row in rows
        if row.get("veto_reason") == _HAND_SELECT_PLAN_PROTECTION_VETO
    ]
    if not plan_veto_rows and not evidence:
        return None

    violations = []
    if (
        str(record.get("phase") or "").upper() != "HAND_SELECT"
        or record.get("action") != "choose"
    ):
        violations.append("hand_select_action_binding")
    if not isinstance(evidence, dict):
        violations.append("plan_protection_evidence_missing")
        return {
            "status": "issues",
            "violations": sorted(set(violations)),
        }
    required = {
        "schema_version", "kind", "source_combat_context",
        "source_planned_sequence", "visible_card_instance_ids",
        "protected_card_instance_ids", "selection_action",
        "selection_semantics", "required_selection_count",
    }
    if set(evidence) != required:
        violations.append("plan_protection_schema")
    if evidence.get("schema_version") != 1:
        violations.append("plan_protection_schema_version")
    if evidence.get("kind") != _HAND_SELECT_PLAN_PROTECTION_VETO:
        violations.append("plan_protection_kind")
    action_token = _normalized_id(evidence.get("selection_action"))
    if (
        evidence.get("selection_semantics") != "remove"
        or not any(marker in action_token for marker in (
            "discard", "exhaust", "putonbottom", "recycle", "gamblingchip",
        ))
        or decision.get("selection_action") != evidence.get("selection_action")
        or decision.get("selection_semantics") != "remove"
    ):
        violations.append("plan_protection_selection_semantics")

    context = evidence.get("source_combat_context")
    if (
        not isinstance(context, list)
        or len(context) != 4
        or any(type(value) is not int for value in context)
    ):
        violations.append("plan_protection_context_shape")
        context = None
    source_sequence = _hand_select_plan_sequence(
        evidence.get("source_planned_sequence")
    )
    if source_sequence is None:
        violations.append("plan_protection_sequence_shape")
        source_sequence = []
    protected = evidence.get("protected_card_instance_ids")
    visible = evidence.get("visible_card_instance_ids")
    if (
        not isinstance(protected, list)
        or any(not isinstance(item, str) or not item for item in protected)
        or len(set(protected)) != len(protected)
        or protected != sorted(protected)
    ):
        violations.append("plan_protection_protected_ids")
        protected = []
    if (
        not isinstance(visible, list)
        or any(not isinstance(item, str) or not item for item in visible)
        or len(set(visible)) != len(visible)
        or visible != sorted(visible)
    ):
        violations.append("plan_protection_visible_ids")
        visible = []
    required_count = evidence.get("required_selection_count")
    if type(required_count) is not int or required_count < 1:
        violations.append("plan_protection_selection_count")
        required_count = None

    row_card_ids = [_hand_select_card_instance_id(row) for row in rows]
    if (
        any(card_uuid is None for card_uuid in row_card_ids)
        or len(set(row_card_ids)) != len(row_card_ids)
        or sorted(row_card_ids) != visible
    ):
        violations.append("plan_protection_visible_surface_binding")
    actual_protected = sorted(
        card_uuid for row, card_uuid in zip(rows, row_card_ids)
        if row.get("selection_eligible") is False
        and row.get("veto_reason") == _HAND_SELECT_PLAN_PROTECTION_VETO
    )
    other_ineligible = [
        card_uuid for row, card_uuid in zip(rows, row_card_ids)
        if row.get("selection_eligible") is False
        and row.get("veto_reason") != _HAND_SELECT_PLAN_PROTECTION_VETO
    ]
    if actual_protected != protected or other_ineligible:
        violations.append("plan_protection_candidate_veto_binding")
    selected_ids = {
        _hand_select_card_instance_id(row) for row in selected_rows or []
    }
    if None in selected_ids or selected_ids & set(protected):
        violations.append("plan_protection_selected_card_binding")
    if (
        required_count is None
        or not isinstance(selected_rows, list)
        or len(selected_rows) != required_count
        or len([card_uuid for card_uuid in row_card_ids if card_uuid not in protected])
        < required_count
    ):
        violations.append("plan_protection_selection_capacity")

    previous = previous_record if isinstance(previous_record, dict) else {}
    source_decision = previous.get("decision")
    source_decision = source_decision if isinstance(source_decision, dict) else {}
    source_context = source_decision.get("_combat_context")
    if (
        not previous
        or previous.get("record_type") != "decision"
        or str(previous.get("phase") or "").upper().startswith("COMBAT_TURN_")
        is False
        or previous.get("action") != "play"
        or previous.get("after_seq") != record.get("before_seq")
        or any(
            previous.get(key) is None
            or record.get(key) is None
            or previous.get(key) != record.get(key)
            for key in ("attempt_id", "run_id", "act", "floor")
        )
        or context is None
        or source_context != context
        or _hand_select_plan_sequence(source_decision.get("planned_sequence"))
        != source_sequence
        or _int_or_none(previous.get("turn")) != context[3]
        or _int_or_none(record.get("act")) != context[1]
        or _int_or_none(record.get("floor")) != context[2]
    ):
        violations.append("plan_protection_source_record_binding")
    elif (
        previous.get("requested_target_id") != source_sequence[0]["card_instance_id"]
        or previous.get("resolved_target_id") != source_sequence[0]["card_instance_id"]
    ):
        violations.append("plan_protection_source_action_binding")

    expected_protected = sorted({
        entry["card_instance_id"]
        for entry in source_sequence[1:]
        if entry["card_instance_id"] in set(visible)
    })
    if protected != expected_protected:
        violations.append("plan_protection_remaining_line_binding")
    row_card_ids_by_uuid = {
        _hand_select_card_instance_id(row): _normalized_id(
            (((row.get("target") or {}).get("card") or {}).get("id"))
        )
        for row in rows
    }
    selected_unique_cores = [
        card_id
        for row in selected_rows or []
        for card_id in [
            row_card_ids_by_uuid.get(_hand_select_card_instance_id(row))
        ]
        if (
            card_id in _HAND_SELECT_IRREVERSIBLE_SCALING_IDS
            and _deck_card_count(record, (card_id,)) == 1
        )
    ]
    protected_noncore_ids = [
        card_uuid for card_uuid in protected
        if row_card_ids_by_uuid.get(card_uuid)
        not in _HAND_SELECT_IRREVERSIBLE_SCALING_IDS
    ]
    eligible_noncore_capacity = sum(
        row.get("selection_eligible") is not False
        and row_card_ids_by_uuid.get(_hand_select_card_instance_id(row))
        not in _HAND_SELECT_IRREVERSIBLE_SCALING_IDS
        for row in rows
    )
    if (
        required_count is not None
        and selected_unique_cores
        and protected_noncore_ids
        and eligible_noncore_capacity < required_count
    ):
        violations.append(
            "plan_protection_displaces_irreversible_scaling_core"
        )
    return {
        "status": "issues" if violations else "clear",
        "violations": sorted(set(violations)),
        "protected_card_instance_ids": protected,
        "source_before_seq": previous.get("before_seq"),
    }


def _candidate_scores_auditable(record, rows, bijection=None):
    """Validate numeric evidence without trusting a producer declaration."""

    if not isinstance(bijection, dict) or not bijection.get("complete"):
        return False, "auditor_candidate_bijection_incomplete"
    scores = [row.get("score") for row in rows]
    if not scores or any(score is None for score in scores):
        return False, "auditor_numeric_scores_incomplete"
    binary_template = bool(
        len(scores) >= 2
        and all(score in {0.0, 1.0} for score in scores)
        and sum(score == 1.0 for score in scores) == 1
    )
    if binary_template:
        if all(_candidate_score_evidence_recomputes(row) for row in rows):
            return True, "auditor_recomputed_binary_scores"
        return False, "legacy_chosen_one_hot_template"
    return True, "auditor_bijection_numeric_scores"


def _candidate_score_evidence_recomputes(row):
    if not isinstance(row, dict):
        return False
    score = _finite_number(row.get("score"))
    formula = row.get("score_formula")
    inputs = row.get("score_inputs")
    components = row.get("score_components")
    if (
        score is None
        or not isinstance(formula, dict)
        or formula.get("kind") != "sum_components_v1"
        or not isinstance(inputs, dict)
        or not isinstance(components, list)
        or not components
        or not isinstance(row.get("score_rule_id"), str)
        or not row["score_rule_id"].strip()
        or row["score_rule_id"] == "unclassified"
        or not isinstance(row.get("local_reason"), str)
        or not row["local_reason"].strip()
        or not isinstance(row.get("reason_codes"), list)
        or not row["reason_codes"]
    ):
        return False
    total = 0.0
    for component in components:
        if not isinstance(component, dict):
            return False
        input_name = component.get("input")
        coefficient = _finite_number(component.get("coefficient"))
        value = _finite_number(component.get("value"))
        input_value = _finite_number(inputs.get(input_name))
        if (
            not isinstance(input_name, str)
            or coefficient is None
            or value is None
            or input_value is None
            or not math.isclose(
                value,
                input_value * coefficient,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            return False
        total += value
    return math.isclose(total, score, rel_tol=0.0, abs_tol=1e-9)


def _valid_applied_override(advice, selected_row, rows, local_best_rows):
    """Prove that a below-local-argmax choice was a bound model override."""

    if not isinstance(advice, dict):
        return False, "missing_model_advice"
    if advice.get("applied") is not True or advice.get("status") != "applied":
        return False, "override_not_applied"
    if _model_error(advice):
        return False, "override_has_error"
    model_choice = advice.get("model_choice_id")
    selected_aliases = _row_choice_aliases(selected_row)
    if (
        not isinstance(model_choice, str)
        or not (_choice_aliases(model_choice) & selected_aliases)
    ):
        return False, "model_choice_not_selected"
    final_ids = advice.get("final_choice_ids")
    if not (
        isinstance(final_ids, list)
        and len(final_ids) == 1
        and (_choice_aliases(final_ids[0]) & selected_aliases)
    ):
        return False, "final_choice_binding_mismatch"
    if not any(
        _choice_aliases(model_choice) & _row_choice_aliases(row)
        for row in rows
    ):
        return False, "model_choice_not_candidate"
    rule_choice = advice.get("rule_choice_id")
    if not isinstance(rule_choice, str):
        replay = advice.get("replay")
        replay = replay if isinstance(replay, dict) else {}
        rule_ids = replay.get("rule_choice_ids")
        if isinstance(rule_ids, list) and rule_ids:
            rule_choice = rule_ids[0]
    if not isinstance(rule_choice, str) or not any(
        _choice_aliases(rule_choice) & _row_choice_aliases(row)
        for row in local_best_rows
    ):
        return False, "rule_choice_not_local_argmax"
    replay = advice.get("replay")
    replay = replay if isinstance(replay, dict) else {}
    replay_ids = [
        row.get("candidate_id") for row in replay.get("candidates") or []
        if isinstance(row, dict)
    ]
    if replay_ids and not all(
        any(_same_choice(required, item) for item in replay_ids)
        for required in (rule_choice, model_choice)
    ):
        return False, "override_replay_candidate_mismatch"
    return True, "applied_model_override"


def _model_conflict_summary(advice_records):
    """Explain every model-top/local-rule disagreement and its resolution."""

    conflicts = []
    outcome_counts = Counter()
    by_type = {}
    by_status = {}

    def bucket(container, key):
        return container.setdefault(key, {
            "records": 0,
            "conflicts": 0,
            "status_counts": Counter(),
            "conflict_outcome_counts": Counter(),
        })

    for index, advice in enumerate(advice_records):
        if not isinstance(advice, dict):
            continue
        decision_type = str(advice.get("decision_type") or "unknown")
        status = str(advice.get("status") or "unknown")
        type_bucket = bucket(by_type, decision_type)
        status_bucket = bucket(by_status, status)
        for item in (type_bucket, status_bucket):
            item["records"] += 1
            item["status_counts"][status] += 1

        replay = advice.get("replay")
        replay = replay if isinstance(replay, dict) else {}
        rule_ids = replay.get("rule_choice_ids")
        if not isinstance(rule_ids, list) or not rule_ids:
            rule = advice.get("rule_choice_id")
            rule_ids = [rule] if isinstance(rule, str) and rule else []
        rule_ids = [str(item) for item in rule_ids if item is not None]
        rule_top = rule_ids[0] if rule_ids else None
        model_top = advice.get("model_choice_id")
        if not isinstance(model_top, str) or not model_top.strip():
            continue
        if rule_top is None or _same_choice(model_top, rule_top):
            continue

        final_ids = advice.get("final_choice_ids")
        if not isinstance(final_ids, list):
            final_ids = []
        final_ids = [str(item) for item in final_ids if item is not None]
        if any(_same_choice(model_top, item) for item in final_ids):
            adopted = "model"
        elif final_ids and all(
            any(_same_choice(final, rule) for rule in rule_ids)
            for final in final_ids
        ):
            adopted = "local"
        elif final_ids:
            adopted = "third"
        else:
            adopted = "unknown"

        outcome_counts[adopted] += 1
        for item in (type_bucket, status_bucket):
            item["conflicts"] += 1
            item["conflict_outcome_counts"][adopted] += 1
        fusion = advice.get("fusion")
        fusion = fusion if isinstance(fusion, dict) else {}
        conflicts.append({
            "record_index": index,
            "decision_type": decision_type,
            "status": status,
            "rule_choice_id": rule_top,
            "model_choice_id": model_top,
            "final_choice_ids": final_ids,
            "adopted": adopted,
            "applied": advice.get("applied") is True,
            "error_class": _model_error(advice),
            "confidence": advice.get("confidence"),
            "local_regret": fusion.get("local_regret"),
        })

    def serialize(container):
        return {
            key: {
                "records": value["records"],
                "conflicts": value["conflicts"],
                "status_counts": dict(value["status_counts"]),
                "conflict_outcome_counts": dict(
                    value["conflict_outcome_counts"]
                ),
            }
            for key, value in sorted(container.items())
        }

    return {
        "conflicts": len(conflicts),
        "conflict_outcome_counts": {
            key: outcome_counts.get(key, 0)
            for key in ("model", "local", "third", "unknown")
        },
        "details": conflicts,
        "by_decision_type": serialize(by_type),
        "by_status": serialize(by_status),
    }


def _default_or_unknown_reason(record):
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    reason = str(decision.get("reason") or "").casefold()
    if not reason:
        return "missing"
    normalized = "".join(
        character for character in reason if character.isalnum() or character == "_"
    )
    contract = decision.get("candidate_contract")
    candidates = decision.get("candidates")
    auditable_match_uncertainty = bool(
        normalized.startswith("match_game_")
        and isinstance(contract, dict)
        and contract.get("strategy_quality_auditable") is True
        and contract.get("all_visible_options_scored") is True
        and isinstance(candidates, list)
        and candidates
        and all(
            isinstance(candidate, dict)
            and candidate.get("score_rule_id")
            == "match_board_semantic_priority_v1"
            and _finite_number(candidate.get("score")) is not None
            and isinstance(candidate.get("consequences"), dict)
            for candidate in candidates
        )
    )
    if any(marker in normalized for marker in _DEFAULT_DECISION_MARKERS):
        # Hidden Match and Keep positions are genuinely unknown, but the
        # memory policy still scores every visible position with a complete,
        # deterministic contract.  Do not confuse named uncertainty with an
        # unscored default.  Any incomplete/forged contract remains blocking.
        if auditable_match_uncertainty:
            return None
        return reason
    signals = decision.get("chosen_signals")
    if isinstance(signals, list) and any(
        "unknown" in str(signal).casefold() for signal in signals
    ):
        return "unknown_semantic_signal"
    return None


def _curse_consequence_problem(consequences):
    """Return a reason when probabilistic curse/Omamori math is not exact."""

    if not isinstance(consequences, dict):
        return None
    curse_keys = {
        "curse_delta", "raw_curse_delta", "effective_curse_delta",
        "curse_probability", "omamori_prevented_curse_delta",
        "expected_effective_curse_delta", "expected_omamori_charge_use",
        "omamori_charges_before", "omamori_charges_after_if_triggered",
        "expected_omamori_charges_after",
    }
    if not curse_keys.intersection(consequences):
        return None
    raw = _finite_number(consequences.get("raw_curse_delta"))
    effective = _finite_number(consequences.get("effective_curse_delta"))
    probability = _finite_number(consequences.get("curse_probability"))
    prevented = _finite_number(
        consequences.get("omamori_prevented_curse_delta")
    )
    charges = _finite_number(consequences.get("omamori_charges_before"))
    expected_effective = _finite_number(
        consequences.get("expected_effective_curse_delta")
    )
    expected_use = _finite_number(
        consequences.get("expected_omamori_charge_use")
    )
    after_trigger = _finite_number(
        consequences.get("omamori_charges_after_if_triggered")
    )
    expected_after = _finite_number(
        consequences.get("expected_omamori_charges_after")
    )
    values = (
        raw, effective, probability, prevented, charges,
        expected_effective, expected_use, after_trigger, expected_after,
    )
    if any(value is None for value in values):
        return "incomplete_probability_or_omamori_fields"
    if raw < 0 or effective < 0 or prevented < 0 or charges < 0:
        return "negative_curse_or_omamori_value"
    if probability < 0 or probability > 1:
        return "probability_out_of_range"
    expected_prevented = min(raw, charges)
    checks = (
        (prevented, expected_prevented, "prevented_delta_mismatch"),
        (effective, raw - expected_prevented, "effective_delta_mismatch"),
        (
            _finite_number(consequences.get("curse_delta")),
            raw - expected_prevented,
            "legacy_curse_delta_mismatch",
        ),
        (
            expected_effective,
            (raw - expected_prevented) * probability,
            "expected_effective_delta_mismatch",
        ),
        (
            expected_use, expected_prevented * probability,
            "expected_charge_use_mismatch",
        ),
        (
            after_trigger, charges - expected_prevented,
            "post_trigger_charge_mismatch",
        ),
        (
            expected_after, charges - expected_prevented * probability,
            "expected_remaining_charge_mismatch",
        ),
    )
    for actual, expected, reason in checks:
        if actual is None or not math.isclose(
            actual, expected, rel_tol=0.0, abs_tol=1e-3
        ):
            return reason
    return None


def _mausoleum_trade_evidence(record):
    """Return independent Mausoleum evidence for an applicable event row.

    Applicability is detected from the raw protocol surface, not from the
    producer's score/consequence claims.  Once applicable, a missing or broken
    oracle interface remains explicit inconclusive evidence so omitting a
    producer curse field cannot bypass high-value review.
    """

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    options = [
        option for option in record.get("available_options_before") or []
        if isinstance(option, dict)
    ]
    is_mausoleum = any(
        _normalized_id((option.get("target") or {}).get("event_id"))
        == "themausoleum"
        for option in options
        if isinstance(option.get("target"), dict)
    )
    if not is_mausoleum:
        return None
    fallback = {
        "schema_version": 1,
        "mechanism": "mausoleum_expected_curse_for_random_relic",
        "status": "inconclusive",
        "reason": "independent_mausoleum_oracle_unavailable",
        "expected_trade_contract": None,
        "realized_settlement": None,
    }
    if (
        _independent_oracle is None
        or not hasattr(_independent_oracle, "mausoleum_trade_evidence")
    ):
        return fallback
    try:
        evidence = _independent_oracle.mausoleum_trade_evidence(record)
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        return {
            **fallback,
            "reason": "independent_mausoleum_oracle_failed",
            "error": repr(exc),
        }
    if not isinstance(evidence, dict) or evidence.get("mechanism") != (
        "mausoleum_expected_curse_for_random_relic"
    ):
        return {
            **fallback,
            "reason": "independent_mausoleum_evidence_malformed",
        }
    return evidence


def _authoritative_resource_deltas(record):
    """Recompute immediate HP/max-HP/gold deltas from bound state frames."""

    if _independent_oracle is None:
        return None
    binding_fields = tuple(getattr(
        _independent_oracle, "ATTEMPT_BINDING_FIELDS", ()
    ))
    if not binding_fields:
        return None
    before = record.get("authoritative_state_before")
    after = record.get("authoritative_state_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before_seq = record.get("before_seq")
    after_seq = record.get("after_seq")
    if not (
        type(before_seq) is int
        and type(after_seq) is int
        and after_seq > before_seq
        and before.get("protocol_version") == 2
        and after.get("protocol_version") == 2
        and before.get("state_seq") == before_seq
        and after.get("state_seq") == after_seq
    ):
        return None
    for field in binding_fields:
        values = (record.get(field), before.get(field), after.get(field))
        if (
            any(value is None for value in values)
            or type(values[0]) is not type(values[1])
            or type(values[0]) is not type(values[2])
            or values[0] != values[1]
            or values[0] != values[2]
        ):
            return None
    before_game = before.get("game_state")
    after_game = after.get("game_state")
    if not isinstance(before_game, dict) or not isinstance(after_game, dict):
        return None
    deltas = {}
    for field in ("current_hp", "max_hp", "gold"):
        left = _finite_number(before_game.get(field))
        right = _finite_number(after_game.get(field))
        if left is None or right is None:
            return None
        deltas[field] = right - left
    return deltas


def _authoritative_fairy_revive_healing(record):
    """Return the revive packet proven by bound state frames, if any.

    The producer's projected revive amount is the value under audit, so it
    cannot also serve as the observation used to reconstruct gross damage.
    A Fairy disappearing across an authoritative END transition proves the
    death hook fired.  Recompute its packet from max HP and relics; automatic
    Fairy activation does not trigger Toy Ornithopter.
    """

    if _authoritative_resource_deltas(record) is None:
        return None
    before = record["authoritative_state_before"]["game_state"]
    after = record["authoritative_state_after"]["game_state"]
    before_potions = before.get("potions")
    after_potions = after.get("potions")
    if not isinstance(before_potions, list) or not isinstance(after_potions, list):
        return None

    fairy_ids = {"fairypotion", "fairyinabottle", "fairy"}

    def fairy_count(potions):
        return sum(
            1
            for potion in potions
            if isinstance(potion, dict)
            and _normalized_id(potion.get("id") or potion.get("name"))
            in fairy_ids
        )

    if fairy_count(before_potions) - fairy_count(after_potions) != 1:
        return None

    max_hp = _int_or_none(before.get("max_hp"))
    if max_hp is None or max_hp <= 0:
        combat_player = ((before.get("combat_state") or {}).get("player") or {})
        max_hp = _int_or_none(combat_player.get("max_hp"))
    if max_hp is None or max_hp <= 0:
        return None

    relics = before.get("relics")
    if not isinstance(relics, list):
        return None
    relic_ids = {
        _normalized_id(relic.get("id") or relic.get("name"))
        for relic in relics
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return 0
    potency = 60 if "sacredbark" in relic_ids else 30
    healing = max(1, (max_hp * potency) // 100)
    if "magicflower" in relic_ids:
        healing = (healing * 3 + 1) // 2
    return min(max_hp, healing)


def _high_value_resource_losses(
    record, consequences, *, mausoleum_evidence=None
):
    """Return review-worthy irreversible losses, not a claim of bad EV."""

    losses = []
    decision_context = record.get("decision_context")
    decision_context = (
        decision_context if isinstance(decision_context, dict) else {}
    )
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    authoritative = _authoritative_resource_deltas(record)
    gold_delta = (
        authoritative["gold"] if isinstance(authoritative, dict)
        else _finite_number(outcome.get("gold_delta"))
    )
    gold_before = _finite_number(decision_context.get("gold"))
    if gold_delta is not None and gold_delta <= -75:
        losses.append({
            "resource": "gold", "amount": -gold_delta,
            "authority": (
                "authoritative_protocol_delta"
                if isinstance(authoritative, dict) else "decision_outcome"
            ),
        })
    elif authoritative is None and gold_before is not None and gold_before >= 75:
        selected = record.get("chosen_option_before") or {}
        text = _option_text(selected).casefold().replace(" ", "")
        if "loseallgold" in text or "失去所有金币" in text:
            losses.append({"resource": "gold", "amount": gold_before})

    if not isinstance(consequences, dict):
        if (
            not isinstance(authoritative, dict)
            and not isinstance(mausoleum_evidence, dict)
        ):
            return losses
        consequences = {}
    hp_delta = (
        authoritative["current_hp"] if isinstance(authoritative, dict)
        else _finite_number(consequences.get("hp_delta"))
    )
    if hp_delta is not None and hp_delta <= -15:
        losses.append({
            "resource": "hp", "amount": -hp_delta,
            "authority": (
                "authoritative_protocol_delta"
                if isinstance(authoritative, dict) else "producer_consequence"
            ),
        })
    max_hp_delta = (
        authoritative["max_hp"] if isinstance(authoritative, dict)
        else _finite_number(consequences.get("max_hp_delta"))
    )
    if max_hp_delta is not None and max_hp_delta <= -5:
        losses.append({
            "resource": "max_hp", "amount": -max_hp_delta,
            "authority": (
                "authoritative_protocol_delta"
                if isinstance(authoritative, dict) else "producer_consequence"
            ),
        })
    if isinstance(mausoleum_evidence, dict):
        contract = mausoleum_evidence.get("expected_trade_contract")
        contract = contract if isinstance(contract, dict) else {}
        risk = contract.get("risk")
        risk = risk if isinstance(risk, dict) else {}
        if mausoleum_evidence.get("status") != "clear":
            losses.append({
                "resource": "mausoleum_trade_contract",
                "amount": None,
                "evidence_status": mausoleum_evidence.get("status"),
                "evidence_reason": mausoleum_evidence.get("reason"),
            })
        elif contract.get("operation") == "mausoleum_open_coffin":
            expected_curse = _finite_number(
                risk.get("effective_curse_probability")
            )
            charge_risk = _finite_number(
                risk.get("omamori_charge_loss_probability")
            )
            if expected_curse is None or charge_risk is None:
                losses.append({
                    "resource": "mausoleum_trade_contract",
                    "amount": None,
                    "evidence_status": "inconclusive",
                    "evidence_reason": "mausoleum_risk_contract_incomplete",
                })
            else:
                if expected_curse >= 0.5:
                    losses.append({
                        "resource": "expected_curse",
                        "amount": expected_curse,
                        "authority": "independent_event_mechanics_v1",
                    })
                if charge_risk >= 0.5:
                    losses.append({
                        "resource": "omamori_charge_risk",
                        "amount": charge_risk,
                        "authority": "independent_event_mechanics_v1",
                    })
    else:
        expected_curse = _finite_number(
            consequences.get("expected_effective_curse_delta")
        )
        if expected_curse is not None and expected_curse >= 0.5:
            losses.append({
                "resource": "expected_curse", "amount": expected_curse
            })
    if consequences.get("healing_locked") is True:
        losses.append({"resource": "healing", "amount": 1.0})
    lost_card = consequences.get("lost_card")
    if isinstance(lost_card, dict):
        opportunity = _finite_number(lost_card.get("opportunity_cost"))
        if opportunity is not None and opportunity >= 18:
            losses.append({"resource": "card_value", "amount": opportunity})
    lost_potion = consequences.get("lost_potion")
    if isinstance(lost_potion, dict):
        opportunity = _finite_number(lost_potion.get("opportunity_cost"))
        if opportunity is not None and opportunity >= 12:
            losses.append({"resource": "potion_value", "amount": opportunity})
    return losses


def _golden_idol_cost_resolution(cohort, record_index, record):
    """Link Golden Idol's second-stage max-HP cost to its relic gain."""

    if record_index <= 0 or str(record.get("phase") or "").upper() != "EVENT":
        return None
    chosen = record.get("chosen_option_before")
    chosen = chosen if isinstance(chosen, dict) else {}
    target = chosen.get("target")
    target = target if isinstance(target, dict) else {}
    current_index = chosen.get(
        "original_button_index", target.get("original_button_index")
    )
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    if not (
        _normalized_id(target.get("event_id")) == "goldenidol"
        and current_index == 2
        and _finite_number(outcome.get("max_hp_delta")) == -6
    ):
        return None
    previous = cohort[record_index - 1]
    if not (
        isinstance(previous, dict)
        and previous.get("after_seq") == record.get("before_seq")
        and str(previous.get("phase") or "").upper() == "EVENT"
    ):
        return None
    previous_chosen = previous.get("chosen_option_before")
    previous_chosen = (
        previous_chosen if isinstance(previous_chosen, dict) else {}
    )
    previous_target = previous_chosen.get("target")
    previous_target = (
        previous_target if isinstance(previous_target, dict) else {}
    )
    previous_index = previous_chosen.get(
        "original_button_index",
        previous_target.get("original_button_index"),
    )
    added = (
        ((previous.get("decision_outcome") or {}).get("relics") or {})
        .get("added")
    )
    if not (
        _normalized_id(previous_target.get("event_id")) == "goldenidol"
        and previous_index == 0
        and isinstance(added, list)
        and len(added) == 1
        and isinstance(added[0], dict)
        and _normalized_id(
            added[0].get("id") or added[0].get("relic_id")
        ) == "goldenidol"
    ):
        return None
    benefit = {
        "kind": "relic_gain",
        "id": "Golden Idol",
        "relic_id": "Golden Idol",
        "authority": "adjacent_authoritative_event_settlement",
    }
    return {
        "resolution_kind": "golden_idol_cost",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": {
            "benefit_before_seq": previous.get("before_seq"),
            "cost_before_seq": record.get("before_seq"),
        },
    }


def _selected_event_identity(record):
    """Return the selected event target/index across trace schema versions."""

    chosen = record.get("chosen_option_before")
    chosen = chosen if isinstance(chosen, dict) else {}
    target = chosen.get("target")
    target = target if isinstance(target, dict) else {}
    original_index = chosen.get(
        "original_button_index", target.get("original_button_index")
    )
    if target:
        return target, original_index

    selected_ids = {
        str(choice_id) for choice_id in record.get("selected_choice_ids") or []
        if choice_id is not None
    }
    choices = record.get("canonical_choices")
    choices = choices if isinstance(choices, list) else []
    selected = [
        choice for choice in choices
        if isinstance(choice, dict)
        and (
            choice.get("selected") is True
            or str(choice.get("choice_id")) in selected_ids
        )
    ]
    if len(selected) == 1:
        selected_target = selected[0].get("target")
        selected_target = (
            selected_target if isinstance(selected_target, dict) else {}
        )
        return selected_target, selected[0].get(
            "choice_index", selected_target.get("original_button_index")
        )

    case_chosen = record.get("chosen")
    case_chosen = case_chosen if isinstance(case_chosen, dict) else {}
    chosen_index = case_chosen.get("choice_index")
    options = record.get("available_options")
    options = options if isinstance(options, list) else []
    matching = [
        option for option in options
        if isinstance(option, dict)
        and option.get("choice_index") == chosen_index
    ]
    if len(matching) == 1:
        selected_target = matching[0].get("target")
        selected_target = (
            selected_target if isinstance(selected_target, dict) else {}
        )
        return selected_target, selected_target.get(
            "original_button_index", chosen_index
        )
    return {}, None


def _mind_bloom_upgrade_package_resolution(record):
    """Bind Awake's healing lock to its authoritative deck upgrade package."""

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    target, original_index = _selected_event_identity(record)
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    changed = ((outcome.get("deck") or {}).get("changed"))
    added_relics = ((outcome.get("relics") or {}).get("added"))
    if not (
        _normalized_id(target.get("event_id")) == "mindbloom"
        and original_index == 1
        and isinstance(changed, list)
        and changed
        and isinstance(added_relics, list)
        and any(
            isinstance(relic, dict)
            and _normalized_id(relic.get("id") or relic.get("relic_id"))
            == "markofthebloom"
            for relic in added_relics
        )
    ):
        return None

    upgraded_ids = []
    for change in changed:
        if not isinstance(change, dict):
            return None
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return None
        before_id = before.get("card_instance_id")
        after_id = after.get("card_instance_id")
        before_upgrades = before.get("upgrades")
        after_upgrades = after.get("upgrades")
        if not (
            before_id and before_id == after_id
            and type(before_upgrades) is int
            and type(after_upgrades) is int
            and after_upgrades == before_upgrades + 1
        ):
            return None
        upgraded_ids.append(str(before_id))

    benefit = {
        "kind": "deck_upgrade_package",
        "count": len(upgraded_ids),
        "card_instance_ids": sorted(upgraded_ids),
        "authority": "authoritative_event_deck_delta",
    }
    return {
        "resolution_kind": "mind_bloom_upgrade_package",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": {
            **benefit,
            "relic_gained": "Mark of the Bloom",
        },
    }


def _shining_light_upgrade_package_resolution(record):
    """Bind Shining Light's HP cost to its authoritative upgrade package."""

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    target, original_index = _selected_event_identity(record)
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    changed = ((outcome.get("deck") or {}).get("changed"))
    if not (
        _normalized_id(target.get("event_id")) == "shininglight"
        and str(target.get("event_stage") or "").upper() == "INTRO"
        and original_index == 0
        and isinstance(changed, list)
        and changed
    ):
        return None

    upgraded_ids = []
    for change in changed:
        if not isinstance(change, dict):
            return None
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return None
        before_id = before.get("card_instance_id")
        after_id = after.get("card_instance_id")
        before_upgrades = before.get("upgrades")
        after_upgrades = after.get("upgrades")
        if not (
            before_id and before_id == after_id
            and type(before_upgrades) is int
            and type(after_upgrades) is int
            and after_upgrades == before_upgrades + 1
        ):
            return None
        upgraded_ids.append(str(before_id))

    benefit = {
        "kind": "deck_upgrade_package",
        "count": len(upgraded_ids),
        "card_instance_ids": sorted(upgraded_ids),
        "authority": "authoritative_event_deck_delta",
    }
    return {
        "resolution_kind": "shining_light_upgrade_package",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
    }


def _ghosts_card_package_resolution(record):
    """Bind Ghosts' max-HP trade to the authoritative Apparition package."""

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    target, original_index = _selected_event_identity(record)
    card = target.get("card")
    card = card if isinstance(card, dict) else {}
    card_id = card.get("id") or card.get("card_id")
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    added = ((outcome.get("deck") or {}).get("added"))
    ascension = int(record.get("ascension_level") or 0)
    expected_count = 3 if ascension >= 15 else 5
    if not (
        _normalized_id(target.get("event_id")) == "ghosts"
        and original_index == 0
        and card_id
        and isinstance(added, list)
        and len(added) == expected_count
        and all(
            isinstance(item, dict)
            and _normalized_id(item.get("id")) == _normalized_id(card_id)
            for item in added
        )
    ):
        return None
    benefit = {
        "kind": "card_package", "id": card_id,
        "card_id": card_id, "count": expected_count,
        "authority": "authoritative_event_deck_delta",
    }
    return {
        "resolution_kind": "ghosts_card_package",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
    }


def _beggar_remove_deferred_resolution(cohort, record_index, record):
    """Bind Beggar's 75-gold payment to its later UUID-bound purge."""

    if _independent_oracle is None:
        return None
    required = (
        "ATTEMPT_BINDING_FIELDS",
        "_one_grid_confirmation_effect",
        "_grid_deferred_settlement",
    )
    if any(not hasattr(_independent_oracle, name) for name in required):
        return None
    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    chosen = record.get("chosen_option_before")
    chosen = chosen if isinstance(chosen, dict) else {}
    target = chosen.get("target")
    target = target if isinstance(target, dict) else {}
    original_index = chosen.get(
        "original_button_index", target.get("original_button_index")
    )
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    if not (
        _normalized_id(target.get("event_id")) == "beggar"
        and original_index == 0
        and _finite_number(outcome.get("gold_delta")) == -75
    ):
        return None
    grid_index = None
    grid_record = None
    prior_after_seq = record.get("after_seq")
    for offset in range(record_index + 1, min(len(cohort), record_index + 7)):
        following = cohort[offset]
        if not isinstance(following, dict):
            return None
        if following.get("before_seq") != prior_after_seq:
            return None
        phase = str(following.get("phase") or "").upper()
        if phase == "GRID":
            grid_index, grid_record = offset, following
            break
        if phase != "EVENT" or _event_token(following) != "beggar":
            return None
        prior_after_seq = following.get("after_seq")
    if grid_record is None:
        return None
    effect = _independent_oracle._one_grid_confirmation_effect(
        grid_record, "grid_purge"
    ) or _independent_oracle._one_grid_confirmation_effect(
        grid_record, "grid_remove"
    )
    if not isinstance(effect, dict):
        return None
    expected_binding = {
        field: record.get(field)
        for field in _independent_oracle.ATTEMPT_BINDING_FIELDS
    }
    status, details = _independent_oracle._grid_deferred_settlement(
        cohort, grid_index, grid_record, effect, expected_binding
    )
    if status != "clear" or not isinstance(details, dict):
        return None
    removed_ids = details.get("selected_card_instance_ids")
    if (
        not isinstance(removed_ids, list)
        or len(removed_ids) != 1
        or not isinstance(removed_ids[0], str)
        or not removed_ids[0]
    ):
        return None
    benefit = {
        "kind": "deck_removal",
        "removed_card_instance_ids": list(removed_ids),
        "authority": "independent_grid_transaction_v1",
    }
    return {
        "resolution_kind": "beggar_deck_removal",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": details,
    }


def _designer_full_service_deferred_resolution(cohort, record_index, record):
    """Return independently verified benefits for Designer's Full Service.

    The 90-gold event decision only opens a GRID; its purge and random
    upgrade settle on later authoritative frames.  Reuse the independent
    oracle's exact GRID transaction verifier instead of trusting the event
    candidate's score or a producer-authored benefit claim.
    """

    if _independent_oracle is None:
        return None
    required = (
        "ATTEMPT_BINDING_FIELDS",
        "_one_grid_confirmation_effect",
        "_grid_deferred_settlement",
    )
    if any(not hasattr(_independent_oracle, name) for name in required):
        return None
    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    selected_ids = record.get("selected_choice_ids")
    if not isinstance(selected_ids, list) or len(selected_ids) != 1:
        return None
    available = record.get("available_options_before")
    if not isinstance(available, list):
        available = record.get("available_options")
    selected = [
        option for option in available or []
        if isinstance(option, dict)
        and str(option.get("option_id")) == str(selected_ids[0])
    ]
    if len(selected) != 1:
        return None
    target = selected[0].get("target")
    target = target if isinstance(target, dict) else {}
    contract = target.get("event_contract")
    contract = contract if isinstance(contract, dict) else {}
    expected_mechanism_id = "event-mechanism:" + hashlib.sha256(
        json.dumps(
            contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:20]
    original_index = selected[0].get(
        "original_button_index", target.get("original_button_index")
    )
    if not (
        original_index == 2
        and str(target.get("kind") or "").casefold() == "event_option"
        and _normalized_id(target.get("event_id")) == "designer"
        and contract.get("contract_version") == 1
        and contract.get("contract_kind") == "BASE_GAME_EVENT_OPTION"
        and _normalized_id(contract.get("event_id")) == "designer"
        and contract.get("event_stage") == "MAIN"
        and contract.get("original_button_index") == 2
        and contract.get("option_kind") == "FULL_SERVICE"
        and isinstance(contract.get("instance_parameters"), dict)
        and isinstance(contract.get("parameters"), dict)
        and contract["parameters"].get("purge_select_count") == 1
        and contract["parameters"].get("random_upgrade_max_count") == 1
        and contract["parameters"].get("selection_mode")
        == "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
        and target.get("mechanism_id") == expected_mechanism_id
    ):
        return None
    if record_index + 1 >= len(cohort):
        return None
    grid_record = cohort[record_index + 1]
    if not (
        str(grid_record.get("phase") or "").upper() == "GRID"
        and grid_record.get("before_seq") == record.get("after_seq")
    ):
        return None
    effect = _independent_oracle._one_grid_confirmation_effect(
        grid_record, "grid_purge"
    )
    if not isinstance(effect, dict):
        return None
    expected = {
        field: record.get(field)
        for field in _independent_oracle.ATTEMPT_BINDING_FIELDS
    }
    status, details = _independent_oracle._grid_deferred_settlement(
        cohort, record_index + 1, grid_record, effect, expected
    )
    designer = (
        details.get("designer_full_service")
        if status == "clear" and isinstance(details, dict) else None
    )
    if not isinstance(designer, dict):
        return None
    upgrade = designer.get("random_upgrade")
    upgrade = upgrade if isinstance(upgrade, dict) else {}
    before_upgrade = upgrade.get("before")
    before_upgrade = before_upgrade if isinstance(before_upgrade, dict) else {}
    removed_ids = details.get("selected_card_instance_ids")
    if (
        not isinstance(removed_ids, list)
        or not removed_ids
        or any(not isinstance(value, str) or not value for value in removed_ids)
        or not isinstance(before_upgrade.get("card_instance_id"), str)
        or not before_upgrade.get("card_instance_id")
    ):
        return None
    benefit = {
        "kind": "designer_full_service",
        "id": "Designer:FullService",
        "removed_card_instance_ids": list(removed_ids),
        "upgraded_card_instance_id": before_upgrade["card_instance_id"],
        "authority": "independent_grid_transaction_v1",
    }
    return {
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": details,
    }


def _neow_deck_change_deferred_resolution(cohort, record_index, record):
    """Resolve a typed Neow remove/transform reward through its GRID chain.

    Neow's gold/HP transition is immediate, while remove and transform rewards
    commit the actual deck change on one or more subsequent GRID records.  A
    one-frame resource audit therefore cannot prove the benefit.  Reuse the
    strict UUID-bound GRID transaction verifier and only synthesize a benefit
    after the complete chain and final deck delta are authoritative.
    """

    if _independent_oracle is None:
        return None
    required = (
        "ATTEMPT_BINDING_FIELDS",
        "_one_grid_confirmation_effect",
        "_grid_deferred_settlement",
    )
    if any(not hasattr(_independent_oracle, name) for name in required):
        return None
    if str(record.get("phase") or "").upper() != "NEOW":
        return None
    selected_ids = record.get("selected_choice_ids")
    if not isinstance(selected_ids, list) or len(selected_ids) != 1:
        return None
    selected = [
        option for option in record.get("available_options_before") or []
        if isinstance(option, dict)
        and str(option.get("option_id")) == str(selected_ids[0])
    ]
    if len(selected) != 1:
        return None
    target = selected[0].get("target")
    target = target if isinstance(target, dict) else {}
    contract = target.get("neow_contract")
    contract = contract if isinstance(contract, dict) else {}
    reward_kind = contract.get("reward_kind")
    future = getattr(
        _independent_oracle, "_ORACLE_NEOW_FUTURE_REWARDS", {}
    ).get(reward_kind)
    expected_mechanism_id = (
        _independent_oracle._oracle_neow_mechanism_id(contract)
        if contract else None
    )
    operation_by_reward = {
        "REMOVE_CARD": "remove",
        "REMOVE_TWO": "remove",
        "TRANSFORM_CARD": "transform",
        "TRANSFORM_TWO_CARDS": "transform",
    }
    expected_operation = operation_by_reward.get(reward_kind)
    if not (
        str(target.get("kind") or "").casefold() == "event_option"
        and contract.get("contract_kind") == "NEOW_REWARD"
        and expected_operation is not None
        and isinstance(future, dict)
        and future.get("kind") == "neow_grid_selection"
        and future.get("operation") == expected_operation
        and target.get("mechanism_id") == expected_mechanism_id
    ):
        return None
    settlement = record.get("authoritative_choice_settlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    observed = settlement.get("observed_outcome")
    observed = observed if isinstance(observed, dict) else {}
    if settlement.get("status") != "observed":
        return None
    expected_gold = _finite_number(observed.get("gold_delta"))
    if expected_gold is None:
        return None
    if record_index + 1 >= len(cohort):
        return None
    grid_record = cohort[record_index + 1]
    if not (
        str(grid_record.get("phase") or "").upper() == "GRID"
        and grid_record.get("before_seq") == record.get("after_seq")
    ):
        return None
    effect = None
    for operation in (
        ("grid_purge", "grid_remove")
        if expected_operation == "remove" else ("grid_transform",)
    ):
        effect = _independent_oracle._one_grid_confirmation_effect(
            grid_record, operation
        )
        if isinstance(effect, dict):
            break
    if not isinstance(effect, dict):
        return None
    expected_binding = {
        field: record.get(field)
        for field in _independent_oracle.ATTEMPT_BINDING_FIELDS
    }
    status, details = _independent_oracle._grid_deferred_settlement(
        cohort, record_index + 1, grid_record, effect, expected_binding
    )
    if status != "clear" or not isinstance(details, dict):
        return None
    removed_ids = details.get("selected_card_instance_ids")
    select_count = future.get("select_count")
    if (
        not isinstance(removed_ids, list)
        or type(select_count) is not int
        or len(removed_ids) != select_count
        or any(not isinstance(value, str) or not value for value in removed_ids)
    ):
        return None
    benefit_kind = (
        "neow_deck_removal"
        if expected_operation == "remove" else "neow_deck_transform"
    )
    benefit = {
        "kind": benefit_kind,
        "reward_kind": reward_kind,
        "selected_card_instance_ids": list(removed_ids),
        "gold_delta": expected_gold,
        "authority": "independent_grid_transaction_v1",
    }
    if expected_operation == "remove":
        benefit["removed_card_instance_ids"] = list(removed_ids)
    return {
        "resolution_kind": benefit_kind,
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": details,
    }


def _neow_card_reward_deferred_resolution(cohort, record_index, record):
    """Bind a costly typed Neow choice to its realized card-reward result.

    A Neow option can charge HP before opening a skippable CARD_REWARD screen.
    The parent frame therefore has no immediate card delta.  Resolve a genuine
    pick from the next contiguous, attempt-bound protocol settlement, and keep
    an explicit ``realized_no_benefit`` result when the player returns without
    a card.  The latter is evidence of a bad ex-ante paid option, not missing
    audit data.
    """

    if _independent_oracle is None or not hasattr(
        _independent_oracle, "ATTEMPT_BINDING_FIELDS"
    ):
        return None
    if str(record.get("phase") or "").upper() != "NEOW":
        return None
    selected_ids = record.get("selected_choice_ids")
    if not isinstance(selected_ids, list) or len(selected_ids) != 1:
        return None
    selected = [
        option for option in record.get("available_options_before") or []
        if isinstance(option, dict)
        and str(option.get("option_id")) == str(selected_ids[0])
    ]
    if len(selected) != 1:
        return None
    target = selected[0].get("target")
    target = target if isinstance(target, dict) else {}
    contract = target.get("neow_contract")
    contract = contract if isinstance(contract, dict) else {}
    reward_kind = contract.get("reward_kind")
    future = getattr(
        _independent_oracle, "_ORACLE_NEOW_FUTURE_REWARDS", {}
    ).get(reward_kind)
    expected_mechanism_id = (
        _independent_oracle._oracle_neow_mechanism_id(contract)
        if contract and hasattr(
            _independent_oracle, "_oracle_neow_mechanism_id"
        ) else None
    )
    if not (
        str(target.get("kind") or "").casefold() == "event_option"
        and contract.get("contract_kind") == "NEOW_REWARD"
        and isinstance(future, dict)
        and future.get("kind") == "neow_card_reward"
        and future.get("choose_count") == 1
        and target.get("mechanism_id") == expected_mechanism_id
        and record_index + 1 < len(cohort)
    ):
        return None

    reward_record = cohort[record_index + 1]
    if not isinstance(reward_record, dict):
        return None
    if not (
        str(reward_record.get("phase") or "").upper() == "CARD_REWARD"
        and reward_record.get("before_seq") == record.get("after_seq")
        and all(
            reward_record.get(field) == record.get(field)
            and record.get(field) is not None
            for field in _independent_oracle.ATTEMPT_BINDING_FIELDS
        )
    ):
        return None
    settlement = reward_record.get("authoritative_choice_settlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    observed = settlement.get("observed_outcome")
    observed = observed if isinstance(observed, dict) else {}
    deck_delta = observed.get("deck")
    deck_delta = deck_delta if isinstance(deck_delta, dict) else {}
    added = deck_delta.get("added")
    if not (
        settlement.get("status") == "observed"
        and settlement.get("fully_observable") is True
        and settlement.get("before_seq") == reward_record.get("before_seq")
        and settlement.get("after_seq") == reward_record.get("after_seq")
        and isinstance(added, list)
    ):
        return None
    evidence = {
        "before_seq": reward_record.get("before_seq"),
        "after_seq": reward_record.get("after_seq"),
        "choice_id": settlement.get("choice_id"),
        "reward_kind": reward_kind,
        "deck_added": added,
        "authority": "contiguous_authoritative_protocol_delta",
    }
    selected_followup = reward_record.get("selected_choice_ids")
    selected_followup = (
        selected_followup if isinstance(selected_followup, list) else []
    )
    returned = (
        str(reward_record.get("action") or "").casefold() == "return"
        or selected_followup == ["action:return"]
    )
    if returned and not added:
        return {
            "resolution_kind": "neow_card_reward",
            "status": "realized_no_benefit",
            "acquired_benefit": None,
            "observable_delta": None,
            "deferred_evidence": evidence,
        }
    if len(added) != 1 or not isinstance(added[0], dict):
        return None
    card = added[0]
    card_id = card.get("id") or card.get("card_id")
    if not isinstance(card_id, str) or not card_id:
        return None
    benefit = {
        "kind": "card",
        "id": card_id,
        "card_id": card_id,
        "card_instance_id": card.get("card_instance_id") or card.get("uuid"),
        "reward_kind": reward_kind,
        "authority": "contiguous_authoritative_protocol_delta",
    }
    return {
        "resolution_kind": "neow_card_reward",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": evidence,
    }


def _neow_relic_reward_resolution(record):
    """Bind an immediate random-relic Neow reward to its settlement delta.

    Neow's random relic options do not open a follow-up screen: the relic is
    granted in the same authoritative choice settlement.  The generic
    high-value-loss audit used to look only for a numeric positive delta and
    therefore treated the real relic as missing evidence.  Accept only the
    typed Neow random-relic contract and one fully observable, contiguous
    relic addition; never infer a benefit from the producer candidate alone.
    """

    if str(record.get("phase") or "").upper() != "NEOW":
        return None
    selected_ids = record.get("selected_choice_ids")
    if not isinstance(selected_ids, list) or len(selected_ids) != 1:
        return None
    selected = [
        option for option in record.get("available_options_before") or []
        if isinstance(option, dict)
        and str(option.get("option_id")) == str(selected_ids[0])
    ]
    if len(selected) != 1:
        return None
    target = selected[0].get("target")
    target = target if isinstance(target, dict) else {}
    contract = target.get("neow_contract")
    contract = contract if isinstance(contract, dict) else {}
    reward_kind = contract.get("reward_kind")
    random_rewards = getattr(
        _independent_oracle, "_ORACLE_NEOW_RANDOM_REWARDS", {}
    ) if _independent_oracle is not None else {}
    expected = random_rewards.get(reward_kind)
    if not (
        str(target.get("kind") or "").casefold() == "event_option"
        and contract.get("contract_kind") == "NEOW_REWARD"
        and isinstance(expected, dict)
        and expected.get("kind") == "relic_gain"
        and expected.get("count") == 1
        and expected.get("timing") == "immediate"
    ):
        return None
    settlement = record.get("authoritative_choice_settlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    observed = settlement.get("observed_outcome")
    observed = observed if isinstance(observed, dict) else {}
    added = (observed.get("relics") or {}).get("added")
    if not (
        settlement.get("status") == "observed"
        and settlement.get("fully_observable") is True
        and settlement.get("before_seq") == record.get("before_seq")
        and settlement.get("after_seq") == record.get("after_seq")
        and isinstance(added, list)
        and len(added) == 1
        and isinstance(added[0], dict)
    ):
        return None
    relic_id = added[0].get("id") or added[0].get("relic_id")
    if not isinstance(relic_id, str) or not relic_id:
        return None
    benefit = {
        "kind": "relic_gain",
        "id": relic_id,
        "relic_id": relic_id,
        "reward_kind": reward_kind,
        "authority": "authoritative_protocol_delta",
    }
    return {
        "resolution_kind": "neow_relic_reward",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "deferred_evidence": {
            "choice_id": settlement.get("choice_id"),
            "before_seq": settlement.get("before_seq"),
            "after_seq": settlement.get("after_seq"),
            "relic_id": relic_id,
            "reward_domain": expected.get("domain"),
        },
    }


def _positive_resource_gain_resolution(
    cohort, record_index, record, selected_row
):
    """Verify an ex-ante numeric benefit through bound protocol deltas.

    Some events acknowledge the selected option before applying its benefit on
    a following EVENT frame (for example Liars Game's gold-and-curse trade).
    Other trades, such as Forgotten Altar, settle a max-HP gain immediately.
    The producer's candidate identifies the promised benefit, but only exact,
    contiguous authoritative state deltas are accepted as its realization.
    """

    consequences = (
        selected_row.get("consequences")
        if isinstance(selected_row, dict) else None
    )
    if not isinstance(consequences, dict):
        return None
    expected = {}
    for consequence_key, state_key in (
        ("gold_delta", "gold"),
        ("max_hp_delta", "max_hp"),
        ("hp_delta", "current_hp"),
    ):
        value = _finite_number(consequences.get(consequence_key))
        if value is not None and value > 0:
            expected[state_key] = value
    if not expected:
        return None

    phase = str(record.get("phase") or "").upper()
    if phase not in {"EVENT", "NEOW"}:
        return None
    event_token = _event_token(record)
    observed = {key: 0.0 for key in expected}
    evidence = []
    prior_after_seq = record.get("before_seq")
    for offset in range(record_index, min(len(cohort), record_index + 7)):
        following = cohort[offset]
        if not isinstance(following, dict):
            return None
        if following.get("before_seq") != prior_after_seq:
            return None
        if (
            following.get("attempt_id") != record.get("attempt_id")
            or following.get("run_id") != record.get("run_id")
            or str(following.get("phase") or "").upper() != phase
            or _event_token(following) != event_token
        ):
            break
        deltas = _authoritative_resource_deltas(following)
        if not isinstance(deltas, dict):
            return None
        for key in observed:
            observed[key] += deltas[key]
        evidence.append({
            "before_seq": following.get("before_seq"),
            "after_seq": following.get("after_seq"),
            "resource_deltas": {
                key: deltas[key] for key in observed
            },
        })
        if all(observed[key] == value for key, value in expected.items()):
            benefit = {
                "kind": "resource_gain",
                **{
                    f"{key}_delta": value
                    for key, value in expected.items()
                },
                "authority": "contiguous_authoritative_protocol_delta",
            }
            return {
                "resolution_kind": "positive_resource_gain",
                "status": "clear",
                "acquired_benefit": benefit,
                "observable_delta": dict(benefit),
                "deferred_evidence": evidence,
            }
        prior_after_seq = following.get("after_seq")
    return None


def _addict_trade_resolution(record):
    """Bind Addict's gold payment to the relic granted in the same event.

    Addict is a two-stage event in the game UI, but some bridge versions emit
    the relic in the authoritative settlement while marking the transition
    ``unresolved`` because the animation spans several state sequences.  The
    old audit trusted only the producer's ``relic_delta`` and therefore
    reported a false missing-benefit finding.  Resolve only the exact typed
    Addict contract and require the observed/outcome relic identity and gold
    delta to agree; no generic unresolved event is waived.
    """

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    if _event_token(record) != "addict":
        return None
    outcome = record.get("decision_outcome") or {}
    if _finite_number(outcome.get("gold_delta")) != -85:
        return None
    settlement = record.get("authoritative_choice_settlement") or {}
    observed = settlement.get("observed_outcome") or {}
    if _finite_number(observed.get("gold_delta")) != -85:
        return None
    observed_relics = (observed.get("relics") or {}).get("added")
    outcome_relics = (outcome.get("relics") or {}).get("added")
    if not isinstance(observed_relics, list) or len(observed_relics) != 1:
        return None
    if not isinstance(outcome_relics, list) or len(outcome_relics) != 1:
        return None
    observed_relic = observed_relics[0]
    outcome_relic = outcome_relics[0]
    if not isinstance(observed_relic, dict) or not isinstance(outcome_relic, dict):
        return None
    observed_id = observed_relic.get("id") or observed_relic.get("relic_id")
    outcome_id = outcome_relic.get("id") or outcome_relic.get("relic_id")
    if not observed_id or not outcome_id or _normalized_id(observed_id) != _normalized_id(outcome_id):
        return None
    benefit = {
        "kind": "relic_gain",
        "id": str(observed_id),
        "relic_id": str(observed_id),
        "source": "Addict",
    }
    return {
        "resolution_kind": "addict_trade",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": {
            "gold_delta": -85,
            "relic_id": str(observed_id),
        },
        "authoritative_observable_delta": {
            "gold_delta": -85,
            "relics_added": [str(observed_id)],
        },
        "deferred_evidence": {
            "settlement_status": settlement.get("status"),
            "before_seq": settlement.get("before_seq"),
            "after_seq": settlement.get("after_seq"),
        },
    }


def _vampires_bite_package_resolution(record):
    """Prove the complete Vampires trade from authoritative state deltas.

    Five Bites are a package benefit, while every starter Strike is removed.
    The cost is either ceil(30% max HP) or one Blood Vial.  Treating only the
    HP loss as visible made the high-value gate report an unresolved sacrifice
    even when the exact package had settled successfully.
    """

    if (
        str(record.get("phase") or "").upper() != "EVENT"
        or _event_token(record) != "vampires"
        or _authoritative_resource_deltas(record) is None
    ):
        return None
    chosen = record.get("chosen_option_before")
    chosen = chosen if isinstance(chosen, dict) else {}
    target = chosen.get("target")
    target = target if isinstance(target, dict) else {}
    original_index = target.get("original_button_index")
    if type(original_index) is not int:
        return None
    before_state = record.get("authoritative_state_before") or {}
    after_state = record.get("authoritative_state_after") or {}
    before = before_state.get("game_state")
    after = after_state.get("game_state")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before_deck = before.get("deck")
    after_deck = after.get("deck")
    before_relics = before.get("relics")
    after_relics = after.get("relics")
    if not all(isinstance(value, list) for value in (
        before_deck, after_deck, before_relics, after_relics,
    )):
        return None

    def card_uuid(card):
        return (
            card.get("card_instance_id") or card.get("uuid")
            if isinstance(card, dict) else None
        )

    before_cards = {
        card_uuid(card): card for card in before_deck
        if isinstance(card, dict) and card_uuid(card)
    }
    after_cards = {
        card_uuid(card): card for card in after_deck
        if isinstance(card, dict) and card_uuid(card)
    }
    if len(before_cards) != len(before_deck) or len(after_cards) != len(after_deck):
        return None
    starter_ids = {"striker", "strikeg", "strikeb", "strikep"}
    removed_ids = set(before_cards) - set(after_cards)
    added_ids = set(after_cards) - set(before_cards)
    removed = [before_cards[uuid] for uuid in sorted(removed_ids)]
    added = [after_cards[uuid] for uuid in sorted(added_ids)]
    if (
        any(_normalized_id(card.get("id")) not in starter_ids for card in removed)
        or {
            uuid for uuid, card in before_cards.items()
            if _normalized_id(card.get("id")) in starter_ids
        } != removed_ids
        or len(added) != 5
        or any(_normalized_id(card.get("id")) != "bite" for card in added)
        or any(
            before_cards[uuid] != after_cards[uuid]
            for uuid in set(before_cards) & set(after_cards)
        )
    ):
        return None

    def relic_map(items):
        mapped = {}
        for relic in items:
            if not isinstance(relic, dict):
                return None
            relic_id = _normalized_id(relic.get("id") or relic.get("name"))
            if not relic_id or relic_id in mapped:
                return None
            mapped[relic_id] = relic
        return mapped

    left_relics = relic_map(before_relics)
    right_relics = relic_map(after_relics)
    if left_relics is None or right_relics is None:
        return None
    removed_relic_ids = set(left_relics) - set(right_relics)
    added_relic_ids = set(right_relics) - set(left_relics)
    if added_relic_ids or any(
        left_relics[key] != right_relics[key]
        for key in set(left_relics) & set(right_relics)
    ):
        return None

    before_hp = _finite_number(before.get("current_hp"))
    after_hp = _finite_number(after.get("current_hp"))
    before_max = _finite_number(before.get("max_hp"))
    after_max = _finite_number(after.get("max_hp"))
    before_gold = _finite_number(before.get("gold"))
    after_gold = _finite_number(after.get("gold"))
    if any(value is None for value in (
        before_hp, after_hp, before_max, after_max, before_gold, after_gold,
    )) or after_gold != before_gold:
        return None
    trade_vial = bool(original_index == 1 and "bloodvial" in left_relics)
    expected_max_loss = 0 if trade_vial else math.ceil(before_max * 0.30)
    expected_hp_after = min(before_hp, before_max - expected_max_loss)
    expected_removed_relics = {"bloodvial"} if trade_vial else set()
    if (
        original_index not in ({0, 1} if "bloodvial" in left_relics else {0})
        or before_max - after_max != expected_max_loss
        or after_hp != expected_hp_after
        or removed_relic_ids != expected_removed_relics
    ):
        return None

    benefit = {
        "kind": "card_package", "id": "Bite", "card_id": "Bite",
        "count": 5, "removed_starter_strike_count": len(removed),
    }
    return {
        "resolution_kind": "vampires_bite_package",
        "status": "clear",
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "authoritative_observable_delta": {
            "hp_delta": after_hp - before_hp,
            "max_hp_delta": after_max - before_max,
            "gold_delta": after_gold - before_gold,
            "cards_added": added,
            "cards_removed": removed,
            "relics_removed": [left_relics[key] for key in removed_relic_ids],
        },
    }


def _mausoleum_trade_resolution(record, evidence):
    """Adapt strict oracle evidence to the generic resource-loss gate."""

    if not isinstance(evidence, dict):
        return None
    result = {
        "resolution_kind": "mausoleum_trade",
        "status": evidence.get("status"),
        "expected_trade_contract": evidence.get("expected_trade_contract"),
        "realized_settlement": evidence.get("realized_settlement"),
        "deferred_evidence": evidence,
    }
    expected = result["expected_trade_contract"]
    realized = result["realized_settlement"]
    if not (
        evidence.get("status") == "clear"
        and isinstance(expected, dict)
        and expected.get("operation") == "mausoleum_open_coffin"
        and isinstance(expected.get("guaranteed_benefit"), dict)
        and expected["guaranteed_benefit"].get("kind")
        == "random_relic_gain"
        and expected["guaranteed_benefit"].get("count") == 1
        and isinstance(expected.get("risk"), dict)
        and isinstance(realized, dict)
        and realized.get("operation") == "mausoleum_open_coffin"
        and realized.get("authority") == "authoritative_protocol_delta"
        and isinstance(realized.get("acquired_benefit"), dict)
        and isinstance(realized.get("observable_delta"), dict)
    ):
        return result
    benefit = dict(realized["acquired_benefit"])
    result.update({
        # Both generic fields originate in the independently reconstructed
        # realized settlement.  The complete raw delta remains separate below
        # so a post-hoc relic identity can never replace the ex-ante contract.
        "acquired_benefit": benefit,
        "observable_delta": dict(benefit),
        "authoritative_observable_delta": realized["observable_delta"],
    })
    return result


def _resource_loss_resolution_evidence(
    record,
    selected_row,
    *,
    candidate_evidence_complete,
    selected_is_argmax,
    rationale_complete,
    deferred_resolution=None,
):
    """Classify whether an irreversible spend has auditable consideration."""

    consequences = (
        selected_row.get("consequences")
        if isinstance(selected_row, dict) else None
    )
    consequences = consequences if isinstance(consequences, dict) else {}
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    acquired_benefit = consequences.get("acquired_benefit")
    observable_delta = outcome.get("observable_delta")
    if isinstance(deferred_resolution, dict):
        if deferred_resolution.get("resolution_kind") == "mausoleum_trade":
            # A random relic's realized identity is unknowable at decision
            # time.  Never let a producer-authored post-hoc identity override
            # the independently reconstructed authoritative settlement.
            acquired_benefit = deferred_resolution.get("acquired_benefit")
            observable_delta = deferred_resolution.get("observable_delta")
        else:
            if acquired_benefit is None:
                acquired_benefit = deferred_resolution.get("acquired_benefit")
            if observable_delta is None:
                observable_delta = deferred_resolution.get("observable_delta")
    benefit_complete = bool(
        isinstance(acquired_benefit, dict)
        and acquired_benefit
        and _recursive_has_values(acquired_benefit)
    )
    delta_complete = bool(
        isinstance(observable_delta, dict)
        and observable_delta
        and _recursive_has_values(observable_delta)
    )
    identifiers_match = True
    if benefit_complete and delta_complete:
        for key in ("kind", "id", "card_id", "relic_id", "potion_id"):
            expected = acquired_benefit.get(key)
            observed = observable_delta.get(key)
            if (
                expected is not None
                and observed is not None
                and str(expected).casefold() != str(observed).casefold()
            ):
                identifiers_match = False
                break
    missing = []
    if not benefit_complete:
        missing.append("acquired_benefit")
    if not delta_complete or not identifiers_match:
        missing.append("observable_delta")
    if not candidate_evidence_complete:
        missing.append("complete_candidate_evidence")
    if not selected_is_argmax:
        missing.append("selected_argmax")
    if not rationale_complete:
        missing.append("decision_rationale")
    if (
        isinstance(deferred_resolution, dict)
        and deferred_resolution.get("resolution_kind") == "mausoleum_trade"
        and not (
            deferred_resolution.get("status") == "clear"
            and isinstance(
                deferred_resolution.get("expected_trade_contract"), dict
            )
            and isinstance(
                deferred_resolution.get("realized_settlement"), dict
            )
            and isinstance(
                deferred_resolution.get("authoritative_observable_delta"),
                dict,
            )
        )
    ):
        missing.append("independent_mausoleum_trade_evidence")
    return {
        "resolved": not missing,
        "missing_evidence": missing,
        "acquired_benefit": acquired_benefit,
        "observable_delta": observable_delta,
        "benefit_delta_identifiers_match": identifiers_match,
        "deferred_resolution": deferred_resolution,
    }


def _recursive_has_values(value):
    if isinstance(value, dict):
        return any(_recursive_has_values(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_recursive_has_values(item) for item in value)
    return bool(value)


def _anomaly_severity(kind):
    if kind in _CRITICAL_ANOMALY_KINDS:
        return "critical"
    if kind in _HIGH_ANOMALY_KINDS:
        return "high"
    if "unsupported" in kind or "unclassified" in kind:
        return "high"
    if "incomplete" in kind or "inconclusive" in kind:
        return "medium"
    return "medium"


def _rank_anomalies(issues, review_findings, mechanics, audit_coverage):
    anomalies = []
    for source, items in (("issue", issues), ("review", review_findings)):
        for item in items:
            kind = str(item.get("kind") or "unknown")
            severity = _anomaly_severity(kind)
            impact = _ANOMALY_BASE_IMPACT[severity]
            losses = item.get("losses")
            if isinstance(losses, list):
                impact += min(20.0, sum(
                    max(0.0, _finite_number(loss.get("amount")) or 0.0)
                    for loss in losses if isinstance(loss, dict)
                ) / 10.0)
            gap = _finite_number(item.get("score_gap"))
            if gap is not None:
                impact += min(15.0, max(0.0, gap))
            anomalies.append({
                **item,
                "source": source,
                "severity": severity,
                "impact_score": round(impact, 3),
            })

    for kind, ids, occurrences in (
        (
            "active_unclassified_mechanics",
            mechanics.get("unclassified_ids") or [],
            mechanics.get("unclassified_occurrences") or 0,
        ),
        (
            "active_unsupported_mechanics",
            mechanics.get("unsupported_ids") or [],
            mechanics.get("unsupported_occurrences") or 0,
        ),
    ):
        if ids or occurrences:
            severity = _anomaly_severity(kind)
            anomalies.append({
                "kind": kind,
                "source": "coverage",
                "severity": severity,
                "impact_score": round(
                    _ANOMALY_BASE_IMPACT[severity]
                    + min(20.0, float(occurrences) / 10.0),
                    3,
                ),
                "ids": sorted(str(item) for item in ids),
                "occurrences": int(occurrences),
            })
    inconclusive = {
        name: summary.get("unknown", 0)
        for name, summary in audit_coverage.items()
        if summary.get("status") == "inconclusive"
    }
    if inconclusive:
        anomalies.append({
            "kind": "audit_coverage_inconclusive",
            "source": "coverage",
            "severity": "medium",
            "impact_score": _ANOMALY_BASE_IMPACT["medium"],
            "unknown_by_check": inconclusive,
        })
    anomalies.sort(key=lambda item: (
        -float(item.get("impact_score") or 0.0),
        str(item.get("kind") or ""),
        _safe_nonnegative_int(item.get("before_seq")),
    ))
    for rank, item in enumerate(anomalies, 1):
        item["rank"] = rank
    return anomalies


def _deck_card_count(record, card_ids):
    counts = (record.get("decision_context") or {}).get("deck_counts")
    if not isinstance(counts, dict):
        return None
    wanted = {_normalized_id(card_id) for card_id in card_ids}
    try:
        return sum(
            max(0, int(count or 0))
            for card_id, count in counts.items()
            if _normalized_id(card_id) in wanted
        )
    except (TypeError, ValueError):
        return None


def _selected_card_from_record(record):
    """Return the exact card target from an authoritative choice frame."""

    chosen = record.get("chosen_option_before") or {}
    target = chosen.get("target") or {}
    card = target.get("card")
    if isinstance(card, dict):
        return card
    decision = record.get("decision") or {}
    for row in decision.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        if row.get("selected") is True or row.get("chosen") is True:
            consequence = row.get("consequences") or {}
            selected = consequence.get("selected_card")
            if isinstance(selected, dict):
                return selected

    # Schema-v2 live frames do not always materialize ``chosen_option_before``
    # or the legacy ``decision.candidates`` mirror.  The authoritative choice
    # is then represented by selected_choice_ids plus the canonical choice
    # surface.  Resolve that surface by stable choice id first, and by the
    # authoritative choice index as a compatibility fallback.  Do not infer
    # a card from an unselected row merely because it is visible.
    selected_ids = {
        str(choice_id)
        for choice_id in (
            record.get("selected_choice_ids")
            or record.get("final_choice_ids")
            or []
        )
        if choice_id is not None
    }
    chosen = record.get("chosen") or {}
    chosen_index = chosen.get("choice_index") if isinstance(chosen, dict) else None
    surfaces = []
    for key in ("canonical_choices", "available_options"):
        value = record.get(key)
        if isinstance(value, list):
            surfaces.extend(value)
    for row in surfaces:
        if not isinstance(row, dict):
            continue
        row_ids = {
            str(row.get(key))
            for key in ("choice_id", "option_id", "id")
            if row.get(key) is not None
        }
        selected = (
            row.get("selected") is True
            or bool(selected_ids & row_ids)
            or (
                chosen_index is not None
                and row.get("choice_index") == chosen_index
            )
        )
        if not selected:
            continue
        target = row.get("target") or {}
        card = target.get("card") if isinstance(target, dict) else None
        if isinstance(card, dict):
            return card
        consequences = row.get("consequences") or {}
        selected = consequences.get("selected_card")
        if isinstance(selected, dict):
            return selected
    return None


def _authoritative_removed_cards(record):
    outcome = record.get("decision_outcome") or {}
    settlement = record.get("authoritative_choice_settlement") or {}
    observed = settlement.get("observed_outcome") or {}
    for value in (
        observed.get("deck"), outcome.get("deck"),
    ):
        if isinstance(value, dict) and isinstance(value.get("removed"), list):
            return [card for card in value["removed"] if isinstance(card, dict)]
    return []


def _verified_gambling_chip_settlement(cohort, record_index, record):
    """Recognize the optional Gambling Chip removal transition only.

    HAND_SELECT is not generally exempt from argmax auditing.  Gambling Chip
    is a protocol action whose ``proceed`` button commits the accumulated
    optional removals; comparing a selected card against a synthetic score-0
    proceed row is a category error.  Require the typed before/after
    transition and a nearby return to COMBAT before suppressing that one
    comparison.
    """

    if (
        str(record.get("phase") or "").upper() != "HAND_SELECT"
        or record.get("action") != "choose"
    ):
        return False
    decision = record.get("decision") or {}
    if "gambl" not in _normalized_id(decision.get("selection_action")):
        return False
    settlement = record.get("authoritative_choice_settlement") or {}
    transition = (settlement.get("observed_outcome") or {}).get(
        "combat_choice_transition"
    )
    if not isinstance(transition, dict):
        return False
    before = transition.get("before") or {}
    after = transition.get("after") or {}
    before_selection = before.get("screen_selection")
    after_selection = after.get("screen_selection")
    before_selection = (
        before_selection if isinstance(before_selection, dict) else {}
    )
    after_selection = (
        after_selection if isinstance(after_selection, dict) else {}
    )
    if not (
        before.get("current_action") == "GamblingChipAction"
        and after.get("current_action") == "GamblingChipAction"
        and before.get("phase") == "HAND_SELECT"
        and after.get("phase") == "HAND_SELECT"
        and before_selection.get("source_present") is True
        and after_selection.get("source_present") is True
    ):
        return False
    before_ids = list(before_selection.get("card_instance_ids") or [])
    after_ids = list(after_selection.get("card_instance_ids") or [])
    if (
        not after_ids
        or len(after_ids) <= len(before_ids)
        or len(after_ids) != len(set(after_ids))
        or not set(before_ids).issubset(after_ids)
    ):
        return False
    for following in list(cohort[record_index + 1:record_index + 5]):
        if not isinstance(following, dict):
            continue
        if (
            str(following.get("phase") or "").upper().startswith("COMBAT_TURN_")
            and following.get("action") in {"play", "end", "potion"}
        ):
            return True
    return False


def _event_token(record):
    chosen = record.get("chosen_option_before") or {}
    target = chosen.get("target") or {}
    value = (
        target.get("event_id")
        or (record.get("decision") or {}).get("event_id")
        or record.get("event_id")
        or (record.get("outcome_facts") or {}).get("event_id")
    )
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _expected_event_card_gain(record):
    """Return a lower bound for cards promised by the chosen event option.

    This is intentionally conservative.  It is used only to catch leaving an
    event while an already-accepted, deterministic card grant is still in the
    game's visual-effect queue.
    """

    chosen = record.get("chosen_option_before") or {}
    target = chosen.get("target") or {}
    text = str(target.get("text") or "")
    token = _event_token(record)
    if token == "ghosts" and not any(word in text.lower() for word in ("refuse", "leave")) and "拒绝" not in text:
        return 5
    match = re.search(
        r"(?:obtain|receive|gain|add|获得|得到)\s*(\d+)\s*(?:张)?[^。.!]*?(?:cards?|牌)",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else 0


def _expected_event_card_grant(record):
    """Describe deterministic event mutations by card, not net deck size."""

    chosen = record.get("chosen_option_before") or {}
    target = chosen.get("target") or {}
    choice_index = chosen.get("choice_index")
    if choice_index is None:
        choice_index = target.get("choice_index")
    token = _event_token(record)
    if token == "ghosts" and choice_index in {None, 0}:
        expected = 3 if int(record.get("ascension_level") or 0) >= 15 else 5
        return {
            "card_ids": ("Ghostly", "Apparition"),
            "expected_gain": expected,
            "expected_net_size": expected,
        }
    target_card = target.get("card") or {}
    target_card_id = _normalized_id(target_card.get("id"))
    # Vampires can expose the second accept variant when Blood Vial is owned,
    # but index 1 is the refusal button in the ordinary two-option layout.
    # Bind the promised reward semantically instead of guessing from index.
    if token == "vampires" and target_card_id == "bite":
        strike_count = _deck_card_count(
            record, ("Strike_R", "Strike_G", "Strike_B")
        )
        if strike_count is None:
            return None
        return {
            "card_ids": ("Bite",),
            "expected_gain": 5,
            "expected_net_size": 5 - strike_count,
        }
    return None


def _is_event_exit(record):
    chosen = record.get("chosen_option_before") or {}
    target = chosen.get("target") or {}
    label = str(target.get("label") or chosen.get("label") or "").lower()
    text = str(target.get("text") or "").lower()
    return any(word in f"{label} {text}" for word in ("leave", "depart", "proceed", "离开"))


def _end_turn_overprediction_reason(record, predicted, actual):
    """Classify observable reasons why net HP loss is below the trace plan."""

    outcome = record.get("decision_outcome") or {}
    hp_delta = _int_or_none(outcome.get("hp_delta"))
    if hp_delta is None:
        hp_delta = _int_or_none(outcome.get("player_hp_delta"))
    if hp_delta is not None and hp_delta > 0:
        relic_ids = {
            _normalized_id(value)
            for value in record.get("relic_ids_before") or []
        }
        if "lizardtail" in relic_ids:
            return "lizard_tail_survival_trigger_or_healing_relic"
        return "observed_positive_hp_delta_or_unmodeled_healing"

    hp_before = _int_or_none(record.get("hp_before"))
    if (
        hp_before is not None
        and predicted > hp_before
        and actual == hp_before
    ):
        return "lethal_hp_cap"
    living = [
        monster for monster in record.get("monsters_before") or []
        if isinstance(monster, dict)
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
        and not monster.get("is_gone")
        and not monster.get("half_dead")
    ]
    for monster in living:
        monster_id = _normalized_id(monster.get("id"))
        powers = {
            _normalized_id(power.get("id") or power.get("name")):
            _safe_nonnegative_int(power.get("amount"))
            for power in monster.get("powers") or []
            if isinstance(power, dict)
        }
        if monster_id == "transient":
            player = record.get("player_before") or {}
            if any(
                _normalized_id(orb.get("id")) == "lightning"
                and _safe_nonnegative_int(orb.get("passive_amount")) > 0
                for orb in player.get("orbs") or []
                if isinstance(orb, dict)
            ):
                return "passive_shifting_strength_loss"
        if monster_id in _SPLIT_MONSTER_IDS:
            current_hp = _int_or_none(monster.get("current_hp"))
            max_hp = _int_or_none(monster.get("max_hp"))
            player = record.get("player_before") or {}
            passive_lightning = sum(
                _safe_nonnegative_int(orb.get("passive_amount"))
                for orb in player.get("orbs") or []
                if isinstance(orb, dict)
                and _normalized_id(orb.get("id")) == "lightning"
            )
            if (
                current_hp is not None
                and max_hp is not None
                and current_hp > max_hp // 2
                and current_hp - passive_lightning <= max_hp // 2
            ):
                return "passive_split_transition"

    def monster_power_amount(monster, *names):
        wanted = {_normalized_id(name) for name in names}
        return max(
            (
                _safe_nonnegative_int(power.get("amount"))
                for power in monster.get("powers") or []
                if isinstance(power, dict)
                and _normalized_id(power.get("id") or power.get("name"))
                in wanted
            ),
            default=0,
        )

    # Re-evaluate the producer defect fixed in the current predictor: poison
    # kills are ordered by monster action, and Corpse Explosion from an
    # earlier death can deterministically remove every later attacker.
    projected_hp = [
        _safe_nonnegative_int(monster.get("current_hp"))
        for monster in living
    ]
    projected_block = [
        _safe_nonnegative_int(monster.get("block"))
        for monster in living
    ]
    suppressed_indexes = set()
    exploded_indexes = set()
    for index, monster in enumerate(living):
        if index in suppressed_indexes:
            continue
        poison = monster_power_amount(monster, "Poison", "PoisonPower")
        intangible = monster_power_amount(
            monster, "Intangible", "IntangiblePower"
        ) > 0
        poison_damage = 1 if intangible and poison > 0 else poison
        projected_hp[index] -= poison_damage
        if projected_hp[index] > 0:
            continue
        suppressed_indexes.add(index)
        queue = [index]
        while queue:
            source_index = queue.pop(0)
            if source_index in exploded_indexes:
                continue
            source = living[source_index]
            stacks = monster_power_amount(
                source,
                "Corpse Explosion",
                "CorpseExplosion",
                "CorpseExplosionPower",
            )
            if stacks <= 0:
                continue
            exploded_indexes.add(source_index)
            raw = (
                _safe_nonnegative_int(source.get("max_hp")) * stacks
            )
            for target_index in range(index + 1, len(living)):
                if target_index in suppressed_indexes:
                    continue
                target = living[target_index]
                if monster_power_amount(
                    target, "Invincible", "InvinciblePower"
                ) > 0:
                    continue
                damage = raw
                if monster_power_amount(
                    target, "Intangible", "IntangiblePower"
                ) > 0 and damage > 0:
                    damage = 1
                blocked = min(projected_block[target_index], damage)
                projected_block[target_index] -= blocked
                projected_hp[target_index] -= damage - blocked
                if projected_hp[target_index] <= 0:
                    suppressed_indexes.add(target_index)
                    queue.append(target_index)
    attacking_indexes = {
        index
        for index, monster in enumerate(living)
        if str(monster.get("intent") or "").upper().startswith("ATTACK")
    }
    if (
        attacking_indexes
        and attacking_indexes <= suppressed_indexes
        and exploded_indexes
    ):
        return "deterministic_poison_corpse_explosion"

    player = record.get("player_before") or {}
    player_powers = {
        _normalized_id(power.get("id") or power.get("name"))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    passive_lightning = sum(
        _safe_nonnegative_int(orb.get("passive_amount"))
        for orb in player.get("orbs") or []
        if isinstance(orb, dict)
        and _normalized_id(orb.get("id")) == "lightning"
    )
    if (
        len(living) > 1
        and passive_lightning > 0
        and "electrodynamics" not in player_powers
        and any(
            str(monster.get("intent") or "").upper().startswith("ATTACK")
            and (
                _safe_nonnegative_int(monster.get("current_hp"))
                + _safe_nonnegative_int(monster.get("block"))
                <= passive_lightning
            )
            for monster in living
        )
    ):
        # In a multi-enemy room ordinary Lightning chooses its target
        # randomly.  The survival predictor must retain the branch where it
        # misses a low-HP attacker; if the live roll kills that attacker the
        # observed loss can legitimately be lower than the conservative
        # forecast.
        return "stochastic_lightning_removed_attacker"
    return "unclassified"


def _deferred_end_turn_damage(record, following_records, hp_delta):
    """Recover enemy damage that is serialized after an END frame.

    CommunicationMod acknowledges ``END`` before it applies the enemy turn.
    The next state therefore commonly contains a ``choose``/``proceed`` pair:
    the END decision still has the old HP, while the HAND_SELECT ``proceed``
    frame contains the authoritative post-attack HP.  Treat only that tightly
    bound transition as deferred damage; never search arbitrarily far ahead or
    across attempts/runs/floors.
    """

    if hp_delta is not None and hp_delta < 0:
        # The END frame already observed damage.  Do not double count it.
        return None
    end_hp = _int_or_none(record.get("hp_after"))
    if end_hp is None:
        hp_before = _int_or_none(record.get("hp_before"))
        if hp_before is not None and hp_delta is not None:
            end_hp = hp_before + hp_delta
    if end_hp is None:
        return None
    identity_keys = ("attempt_id", "run_id", "act", "floor")
    for candidate in list(following_records or [])[:4]:
        if not isinstance(candidate, dict):
            continue
        if any(
            record.get(key) is not None
            and candidate.get(key) is not None
            and record.get(key) != candidate.get(key)
            for key in identity_keys
        ):
            break
        phase = str(candidate.get("phase") or "")
        action = str(candidate.get("action") or "")
        if phase != "HAND_SELECT" or action not in {"choose", "proceed"}:
            break
        if action != "proceed":
            continue
        before = _int_or_none(candidate.get("hp_before"))
        after = _int_or_none(candidate.get("hp_after"))
        if before != end_hp or after is None or after >= before:
            return None
        return before - after
    return None


def _combat_end_healing_obscures_enemy_turn(record):
    """Whether the final snapshot hides damage behind combat-end healing."""

    model = record.get("damage_model") or {}
    if (
        model.get("monsters_to_hero_basis")
        in {
            "end_turn_total_hp_loss_before_postcombat_healing",
            "end_turn_total_hp_loss_before_fairy_revival",
        }
        and _int_or_none(model.get("monsters_to_hero_predicted")) is not None
        and _int_or_none(model.get("monsters_to_hero_actual")) is not None
    ):
        return False

    changes = (record.get("decision_outcome") or {}).get(
        "enemy_hp_changes"
    )
    if not isinstance(changes, list) or not changes:
        return False
    # A non-combat post-action state cannot expose the killed monster's final
    # HP. This is the stable trace signature of a transition to rewards.
    if any(
        not isinstance(change, dict)
        or change.get("hp_after") is not None
        for change in changes
    ):
        return False
    relics = {
        _normalized_id(relic_id)
        for relic_id in record.get("relic_ids_before") or []
    }
    powers = {
        _normalized_id(power.get("id") or power.get("name"))
        for power in (record.get("player_before") or {}).get("powers") or []
        if isinstance(power, dict)
    }
    return bool(
        relics & {"burningblood", "blackblood", "meatonthebone"}
        or powers & {"selfrepair", "selfrepairpower"}
    )


def _automatic_card_play_obscures_enemy_turn(record):
    """Whether the END receipt also contains an unsupported automatic play.

    Mayhem resolves at the start of the next player turn, before the bridge's
    next command-ready snapshot.  Its card can deal self damage, gain Block, or
    attack a reactive enemy, so the observed END-to-ready HP delta is not a
    pure enemy-turn settlement.  Until the predictor models the random top
    card, fail closed as unknown instead of reporting a strategy mismatch.
    """

    if record.get("action") != "end":
        return False
    phase_after = str(
        (record.get("decision_outcome") or {}).get("phase_after") or ""
    ).upper()
    if not phase_after.startswith("COMBAT_TURN_"):
        return False
    return any(
        _normalized_id(power.get("id") or power.get("name")) == "mayhem"
        and _safe_nonnegative_int(power.get("amount")) > 0
        for power in (record.get("player_before") or {}).get("powers") or []
        if isinstance(power, dict)
    )


def _turn_damage_summaries(cohort):
    """Compare one-turn hero/enemy damage forecasts with final HP deltas.

    ``enemy_hp_loss`` is computed from each monster's before/after HP rather
    than the serialized card damage, so poison, block, Vulnerable, relic
    packets and split/death transitions are all represented by the observed
    value.  Enemy damage is anchored to END and its tightly-bound deferred
    HAND_SELECT/PROCEED frame, matching the authoritative end-of-turn HP.
    """

    groups = {}
    for index, record in enumerate(cohort):
        if record.get("action") not in {"play", "potion", "end"}:
            continue
        combat_id = record.get("combat_id")
        turn = _int_or_none(record.get("turn"))
        if not combat_id or turn is None:
            continue
        key = (
            record.get("attempt_id"), record.get("run_id"), combat_id, turn
        )
        groups.setdefault(key, []).append((index, record))

    summaries = []
    for key, rows in groups.items():
        # A card-level ``first_action_enemy_hp_loss`` is deliberately not
        # added once per action here.  That value describes only the next
        # action in a search branch; adding it across a turn double-counts
        # replans and misses damage from the rest of the selected sequence.
        # When available, anchor the turn forecast to the first search's
        # final monster HP, which is the same final-HP basis used by the
        # observed outcome below.
        ordered_rows = sorted(rows, key=lambda item: item[0])

        def planned_turn_damage(record):
            search = (record.get("decision") or {}).get("search") or {}
            final_hp = search.get("final_enemy_hp")
            before_monsters = record.get("monsters_before")
            if not isinstance(final_hp, list) or not isinstance(
                before_monsters, list
            ) or len(final_hp) != len(before_monsters):
                return None
            before_hp = []
            for monster in before_monsters:
                if not isinstance(monster, dict):
                    return None
                value = _int_or_none(monster.get("current_hp"))
                if value is None:
                    return None
                before_hp.append(max(0, value))
            final_values = []
            for value in final_hp:
                value = _int_or_none(value)
                if value is None:
                    return None
                final_values.append(max(0, value))
            return sum(
                max(0, before - after)
                for before, after in zip(before_hp, final_values)
            )

        # The first search is a forecast for one concrete line.  The
        # controller intentionally re-plans after every authoritative frame,
        # so retain the line and realized card ids next to the numeric error;
        # otherwise a large positive delta is easily mistaken for a damage
        # formula bug when the later cards were simply re-planned.
        initial_planned_card_ids = []
        for _, candidate_record in ordered_rows:
            sequence = (candidate_record.get("decision") or {}).get(
                "planned_sequence"
            )
            if not isinstance(sequence, list) or not sequence:
                continue
            initial_planned_card_ids = [
                item.get("card_id")
                for item in sequence
                if isinstance(item, dict) and item.get("card_id")
            ]
            if initial_planned_card_ids:
                break
        realized_card_ids = [
            record.get("card_id")
            for _, record in ordered_rows
            if record.get("action") == "play" and record.get("card_id")
        ]
        if initial_planned_card_ids:
            matching_prefix = 0
            for planned_id, realized_id in zip(
                initial_planned_card_ids, realized_card_ids
            ):
                if planned_id != realized_id:
                    break
                matching_prefix += 1
            if matching_prefix == len(realized_card_ids):
                plan_adherence_status = "exact_realized_prefix"
            else:
                plan_adherence_status = "replanned_after_authoritative_frame"
        else:
            matching_prefix = None
            plan_adherence_status = "no_initial_card_sequence"

        hero_pred = 0
        hero_actual = 0
        hero_pred_known = False
        hero_actual_known = False
        hero_pred_basis = None
        # ``planned`` is a whole-turn search forecast captured on the first
        # action.  Older/special fast paths (for example guaranteed lethal
        # combos) expose only per-action forecasts; in that case every action
        # in the turn must be accumulated instead of freezing the value at
        # the first card.
        hero_pred_mode = None
        enemy_pred = None
        enemy_actual = None
        enemy_actual_source = None
        for index, record in ordered_rows:
            action = record.get("action")
            model = record.get("damage_model") or {}
            if action in {"play", "potion"}:
                if hero_pred_mode is None:
                    planned = planned_turn_damage(record)
                    if planned is not None:
                        hero_pred = planned
                        hero_pred_known = True
                        hero_pred_mode = "planned"
                        hero_pred_basis = (
                            "planned_search_final_monster_hp_delta"
                        )
                actual = model.get("hero_to_monsters_actual")
                if actual is None:
                    actual = (record.get("decision_outcome") or {}).get(
                        "enemy_hp_loss"
                    )
                if actual is not None:
                    hero_actual += max(0, _int_or_none(actual) or 0)
                    hero_actual_known = True
                # If the first search did not expose final HP, retain the
                # older card-level fallback so older traces remain useful.
                if hero_pred_mode != "planned":
                    predicted = model.get("hero_to_monsters_predicted")
                    if predicted is None:
                        search = (record.get("decision") or {}).get("search") or {}
                        predicted = search.get("first_action_enemy_hp_loss")
                    if predicted is None:
                        predicted = (record.get("decision") or {}).get(
                            "first_action_enemy_hp_loss"
                        )
                    if predicted is not None:
                        hero_pred += max(0, _int_or_none(predicted) or 0)
                        hero_pred_known = True
                        hero_pred_mode = "card"
                        hero_pred_basis = "sum_card_first_action_predictions"
            elif action == "end":
                # END can still be the action that observes poison/orb/
                # passive damage or a lethal combat transition.  Include its
                # observed enemy HP delta in the turn's final-HP total.
                planned = planned_turn_damage(record)
                if planned is not None:
                    if hero_pred_mode is None:
                        hero_pred = planned
                        hero_pred_known = True
                        hero_pred_mode = "planned_end"
                        hero_pred_basis = (
                            "end_search_final_monster_hp_delta"
                        )
                    elif hero_pred_mode == "card":
                        # Card-level fallback forecasts cover the immediate
                        # plays, while the END search covers deterministic
                        # Lightning/Poison/Combust and similar packets that
                        # resolve after the last card.  Add that residual once.
                        hero_pred += planned
                        hero_pred_basis = (
                            "sum_card_predictions_plus_end_search_final_hp"
                        )
                actual = model.get("hero_to_monsters_actual")
                if actual is None:
                    actual = (record.get("decision_outcome") or {}).get(
                        "enemy_hp_loss"
                    )
                if actual is not None:
                    hero_actual += max(0, _int_or_none(actual) or 0)
                    hero_actual_known = True
            if action != "end":
                continue
            attack = _int_or_none(
                record.get("projected_attack_hp_loss_before")
            )
            end_turn = _int_or_none(
                record.get("projected_end_turn_hp_loss_before")
            )
            if attack is not None or end_turn is not None:
                enemy_pred = max(0, attack or 0) + max(0, end_turn or 0)
            outcome = record.get("decision_outcome") or {}
            hp_delta = _int_or_none(outcome.get("hp_delta"))
            exact_damage_model = record.get("damage_model") or {}
            if exact_damage_model.get("monsters_to_hero_basis") == (
                "end_turn_total_hp_loss_before_postcombat_healing"
            ):
                exact_gross_loss = _int_or_none(
                    exact_damage_model.get("monsters_to_hero_actual")
                )
                if exact_gross_loss is not None:
                    hp_delta = -max(0, exact_gross_loss)
            deferred = _deferred_end_turn_damage(
                record, cohort[index + 1:index + 5], hp_delta
            )
            if deferred is not None:
                enemy_actual = deferred
                enemy_actual_source = "deferred_end_turn_hp_delta"
            elif hp_delta is not None and not _combat_end_healing_obscures_enemy_turn(record):
                enemy_actual = max(0, -hp_delta)
                enemy_actual_source = "end_action_hp_delta"
            elif hp_delta is not None:
                enemy_actual_source = "combat_end_healing_obscured"

        summaries.append({
            "attempt_id": key[0],
            "run_id": key[1],
            "combat_id": key[2],
            "turn": key[3],
            "hero_to_monsters": {
                "predicted": hero_pred if hero_pred_known else None,
                "actual": hero_actual if hero_actual_known else None,
                "delta": (
                    hero_actual - hero_pred
                    if hero_pred_known and hero_actual_known else None
                ),
                "basis": "final_monster_hp_delta",
                "predicted_basis": hero_pred_basis,
            },
            "monsters_to_hero": {
                "predicted": enemy_pred,
                "actual": enemy_actual,
                "delta": (
                    enemy_actual - enemy_pred
                    if enemy_actual is not None and enemy_pred is not None
                    else None
                ),
                "basis": "end_of_turn_hp_delta",
                "actual_source": enemy_actual_source,
            },
            "forecast_scope": (
                "initial_search_line"
                if hero_pred_mode in {"planned", "planned_end"}
                else "per_action_fallback"
                if hero_pred_mode == "card"
                else "unknown"
            ),
            "initial_planned_card_ids": initial_planned_card_ids,
            "realized_card_ids": realized_card_ids,
            "plan_adherence": {
                "status": plan_adherence_status,
                "matching_prefix": matching_prefix,
                "planned_count": len(initial_planned_card_ids),
                "realized_count": len(realized_card_ids),
            },
        })

    def metrics(direction):
        def aggregate(items):
            evaluated = [
                item[direction]
                for item in items
                if item[direction]["predicted"] is not None
                and item[direction]["actual"] is not None
            ]
            errors = [
                item["actual"] - item["predicted"] for item in evaluated
            ]
            return {
                "turns": len(items),
                "evaluated": len(evaluated),
                "unknown": max(0, len(items) - len(evaluated)),
                "exact": sum(error == 0 for error in errors),
                "mae": round(
                    sum(abs(error) for error in errors)
                    / max(1, len(errors)),
                    3,
                ),
                "mean_bias_actual_minus_predicted": round(
                    sum(errors) / max(1, len(errors)), 3
                ),
            }

        result = aggregate(summaries)
        by_adherence = {}
        for status in {
            (item.get("plan_adherence") or {}).get("status")
            for item in summaries
        }:
            if status is None:
                continue
            subset = [
                item for item in summaries
                if (item.get("plan_adherence") or {}).get("status")
                == status
            ]
            by_adherence[status] = aggregate(subset)
        result["by_plan_adherence"] = by_adherence
        return result

    return {
        "summaries": summaries,
        "metrics": {
            "hero_to_monsters": metrics("hero_to_monsters"),
            "monsters_to_hero": metrics("monsters_to_hero"),
        },
    }


# This table is deliberately independent of FastCombatPlanner's encounter
# profiles. If production forgets an encounter, the auditor must still be
# able to disagree instead of importing the same omission and declaring the
# trace clear. Temporary Weak/Frail turns are intentionally excluded.
_INDEPENDENT_PERSISTENT_ATTRITION_ENCOUNTERS = {
    "lagavulin": {
        "effect": "recurring_strength_and_dexterity_loss",
        "cycle_turns": 3,
    },
}
_INDEPENDENT_RECURRING_GROWTH_POWER_MARKERS = {
    "growth", "ritual", "strengthup",
}


def _independent_persistent_attrition_profile(monster):
    if not isinstance(monster, dict):
        return None
    enemy_id = _normalized_id(monster.get("id"))
    profile = _INDEPENDENT_PERSISTENT_ATTRITION_ENCOUNTERS.get(enemy_id)
    if profile is not None:
        return profile
    for power in monster.get("powers") or []:
        if not isinstance(power, dict):
            continue
        power_id = _normalized_id(power.get("id") or power.get("name"))
        if any(
            marker in power_id
            for marker in _INDEPENDENT_RECURRING_GROWTH_POWER_MARKERS
        ):
            return {
                "effect": f"recurring_enemy_growth:{power_id}",
                "cycle_turns": 1,
            }
    return None


def _snapshot_combat_state(record, field):
    snapshot = record.get(field)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    game_state = snapshot.get("game_state")
    game_state = game_state if isinstance(game_state, dict) else {}
    combat_state = game_state.get("combat_state")
    return combat_state if isinstance(combat_state, dict) else None


def _negative_player_stat_debt(record):
    powers = (record.get("player_before") or {}).get("powers") or []
    amounts = Counter()
    for power in powers:
        if not isinstance(power, dict):
            continue
        power_id = _normalized_id(power.get("id") or power.get("name"))
        if power_id in {"strength", "dexterity"}:
            amount = _int_or_none(power.get("amount"))
            if amount is not None:
                amounts[power_id] += max(0, -amount)
    return amounts["strength"] + amounts["dexterity"]


def _persistent_attrition_turn_opportunity(turn_records):
    """Return a conservative direct Attack/Block tempo counterfactual.

    Only the first authoritative hand is used, and only serialized damage,
    Block, Energy, and incoming damage are counted. Vulnerable setup, powers,
    poison, relic procs, draw, generated cards, and future turns are ignored,
    making the result a lower bound rather than a replay of production logic.
    """

    if not turn_records:
        return None, "missing_turn_records"
    first = turn_records[0]
    end = next(
        (
            record for record in reversed(turn_records)
            if record.get("action") == "end"
        ),
        None,
    )
    if end is None:
        # A lethal card action ends combat without a following explicit END
        # command.  Prove that boundary from the last authoritative after
        # state instead of turning successful kill turns into audit unknowns.
        state_after = turn_records[-1].get("authoritative_state_after")
        game_after = (
            state_after.get("game_state")
            if isinstance(state_after, dict) else None
        )
        phase_after = (
            str(state_after.get("phase") or "").upper()
            if isinstance(state_after, dict) else ""
        )
        room_phase_after = (
            str(game_after.get("room_phase") or "").upper()
            if isinstance(game_after, dict) else ""
        )
        if (
            phase_after == "COMBAT_REWARD"
            and room_phase_after == "COMPLETE"
        ):
            return None, "combat_ended_before_turn_end"
        after = _snapshot_combat_state(
            turn_records[-1], "authoritative_state_after"
        )
        living_after = [
            monster for monster in (after or {}).get("monsters") or []
            if isinstance(monster, dict)
            and not monster.get("is_gone")
            and not monster.get("half_dead")
            and _safe_nonnegative_int(monster.get("current_hp")) > 0
        ]
        if after is None or living_after:
            return None, "turn_end_missing"
        return None, "combat_ended_before_turn_end"
    living = [
        monster for monster in first.get("monsters_before") or []
        if isinstance(monster, dict)
        and not monster.get("is_gone")
        and not monster.get("half_dead")
        and _safe_nonnegative_int(monster.get("current_hp")) > 0
    ]
    if len(living) != 1:
        return None, "requires_single_living_enemy"
    monster = living[0]
    enemy_id = _normalized_id(monster.get("id"))
    enemy_instance_id = monster.get("enemy_instance_id")
    profile = _independent_persistent_attrition_profile(monster)
    if profile is None:
        return None, "not_persistent_attrition_encounter"
    intent = str(monster.get("intent") or "").upper()
    if intent in {"NONE", "SLEEP", "STUN", "UNKNOWN"}:
        return None, "setup_or_unknown_enemy_turn"

    after = _snapshot_combat_state(end, "authoritative_state_after")
    if after is None:
        # A normal END action that resolves the final enemy transitions
        # directly to COMBAT_REWARD. That authoritative reward snapshot
        # intentionally has no combat_state, so it is a completed turn—not
        # an unavailable arithmetic observation.
        state_after = end.get("authoritative_state_after")
        game_after = (
            state_after.get("game_state")
            if isinstance(state_after, dict) else None
        )
        phase_after = (
            str(state_after.get("phase") or "").upper()
            if isinstance(state_after, dict) else ""
        )
        room_phase_after = (
            str(game_after.get("room_phase") or "").upper()
            if isinstance(game_after, dict) else ""
        )
        if (
            phase_after == "COMBAT_REWARD"
            and room_phase_after == "COMPLETE"
        ):
            return None, "combat_ended_before_turn_end"
        return None, "authoritative_turn_end_missing"
    serialized_after_instances = any(
        isinstance(value, dict) and value.get("enemy_instance_id")
        for value in after.get("monsters") or []
    )
    after_monsters = [
        value for value in after.get("monsters") or []
        if isinstance(value, dict)
        and (
            str(value.get("enemy_instance_id") or "")
            == str(enemy_instance_id)
            if enemy_instance_id and serialized_after_instances
            else _normalized_id(value.get("id")) == enemy_id
        )
    ]
    after_player = after.get("player")
    if len(after_monsters) != 1 or not isinstance(after_player, dict):
        return None, "authoritative_turn_end_ambiguous"

    player = first.get("player_before") or {}
    start_hp = _int_or_none(player.get("current_hp"))
    max_hp = _int_or_none(player.get("max_hp"))
    start_block = _int_or_none(player.get("block"))
    energy = _int_or_none(first.get("energy_before"))
    incoming = _int_or_none(first.get("projected_incoming_before"))
    end_hp = _int_or_none(after_player.get("current_hp"))
    start_enemy_hp = _int_or_none(monster.get("current_hp"))
    end_enemy_hp = _int_or_none(after_monsters[0].get("current_hp"))
    enemy_block = _int_or_none(monster.get("block"))
    values = (
        start_hp, max_hp, start_block, energy, incoming, end_hp,
        start_enemy_hp, end_enemy_hp, enemy_block,
    )
    if any(value is None for value in values):
        return None, "turn_arithmetic_fields_missing"

    simple_cards = []
    for card in first.get("hand_before") or []:
        if not isinstance(card, dict) or not card.get("is_playable"):
            continue
        raw_cost = _int_or_none(card.get("cost"))
        if raw_cost is None or raw_cost < 0:
            continue
        card_type = str(card.get("type") or "").upper()
        damage = (
            max(
                0,
                _int_or_none(card.get("damage"))
                or _int_or_none(card.get("base_damage"))
                or 0,
            )
            if card_type == "ATTACK" else 0
        )
        base_block = _int_or_none(card.get("base_block"))
        block = (
            max(
                0,
                _int_or_none(card.get("block"))
                or base_block
                or 0,
            )
            if base_block is not None and base_block > 0 else 0
        )
        if damage <= 0 and block <= 0:
            continue
        simple_cards.append({
            "id": card.get("id"),
            "card_instance_id": card.get("card_instance_id"),
            "cost": raw_cost,
            "damage": damage,
            "block": block,
        })
    if not simple_cards:
        return None, "no_simple_direct_cards"

    actual_progress = max(0, start_enemy_hp - max(0, end_enemy_hp))
    actual_loss = max(0, start_hp - max(0, end_hp))
    reserve = max(6, int(math.ceil(max_hp * 0.12)))
    added_loss_cap = max(6, min(12, start_hp // 8))
    alternatives = []
    for size in range(1, len(simple_cards) + 1):
        for selected in itertools.combinations(simple_cards, size):
            if sum(card["cost"] for card in selected) > energy:
                continue
            damage = max(
                0,
                sum(card["damage"] for card in selected) - enemy_block,
            )
            projected_loss = max(
                0,
                incoming
                - start_block
                - sum(card["block"] for card in selected),
            )
            added_loss = max(0, projected_loss - actual_loss)
            progress_gain = max(0, damage - actual_progress)
            if (
                start_hp - projected_loss < reserve
                or added_loss > added_loss_cap
                or progress_gain < 8
                or progress_gain < added_loss + 4
            ):
                continue
            alternatives.append({
                "cards": [card["id"] for card in selected],
                "projected_hp_loss": projected_loss,
                "enemy_hp_progress": damage,
                "added_hp_loss": added_loss,
                "progress_gain": progress_gain,
            })
    if not alternatives:
        return None, "no_safe_efficient_tempo_alternative"
    best = max(
        alternatives,
        key=lambda item: (
            item["progress_gain"] - item["added_hp_loss"],
            item["progress_gain"],
            -item["projected_hp_loss"],
        ),
    )
    search = (first.get("decision") or {}).get("search") or {}
    persistent_pressure = _finite_number(
        search.get("persistent_debuff_pressure")
    )
    scaling_pressure = _finite_number(search.get("scaling_pressure"))
    return {
        "source_record": first,
        "turn": _int_or_none(first.get("turn")),
        "enemy_id": monster.get("id"),
        "effect": profile["effect"],
        "actual": {
            "cards": [
                record.get("card_id")
                for record in turn_records
                if record.get("action") == "play"
            ],
            "hp_loss": actual_loss,
            "enemy_hp_progress": actual_progress,
        },
        "counterfactual": best,
        "planner_persistent_debuff_pressure": persistent_pressure,
        "planner_scaling_pressure": scaling_pressure,
        "pressure_modeled": bool(
            (persistent_pressure is not None and persistent_pressure > 0)
            or (scaling_pressure is not None and scaling_pressure > 0)
        ),
    }, None


def _persistent_attrition_race_audit(cohort):
    grouped = {}
    combat_order = []
    for record in cohort:
        combat_id = record.get("combat_id")
        turn = _int_or_none(record.get("turn"))
        if not combat_id or turn is None:
            continue
        combat_key = (
            record.get("attempt_id"), record.get("run_id"), combat_id,
        )
        turn_key = (*combat_key, turn)
        if combat_key not in combat_order:
            combat_order.append(combat_key)
        grouped.setdefault(turn_key, []).append(record)

    coverage = Counter()
    findings = []
    for combat_key in combat_order:
        turn_keys = sorted(
            (key for key in grouped if key[:3] == combat_key),
            key=lambda key: key[3],
        )
        opportunities = []
        strong_debuff_turns = []
        recurring_growth_turns = []
        maximum_stat_debt = 0
        final_end_record = None
        for turn_key in turn_keys:
            turn_records = grouped[turn_key]
            first = turn_records[0]
            living_monsters = [
                monster
                for monster in first.get("monsters_before") or []
                if isinstance(monster, dict)
                and _safe_nonnegative_int(monster.get("current_hp")) > 0
                and not monster.get("is_gone")
            ]
            if not any(
                _independent_persistent_attrition_profile(monster)
                is not None
                for monster in living_monsters
            ):
                continue
            coverage["eligible"] += 1
            opportunity, unknown_reason = (
                _persistent_attrition_turn_opportunity(turn_records)
            )
            if unknown_reason in {
                "turn_end_missing", "authoritative_turn_end_missing",
                "authoritative_turn_end_ambiguous",
                "turn_arithmetic_fields_missing",
            }:
                coverage["unknown"] += 1
            else:
                coverage["evaluated"] += 1
            if opportunity is not None:
                opportunities.append(opportunity)
            for record in turn_records:
                maximum_stat_debt = max(
                    maximum_stat_debt,
                    _negative_player_stat_debt(record),
                )
                if any(
                    str(monster.get("intent") or "").upper()
                    == "STRONG_DEBUFF"
                    for monster in record.get("monsters_before") or []
                    if isinstance(monster, dict)
                ):
                    strong_debuff_turns.append(turn_key[3])
                if any(
                    _independent_persistent_attrition_profile(monster)
                    is not None
                    and _normalized_id(monster.get("id"))
                    not in _INDEPENDENT_PERSISTENT_ATTRITION_ENCOUNTERS
                    for monster in record.get("monsters_before") or []
                    if isinstance(monster, dict)
                ):
                    recurring_growth_turns.append(turn_key[3])
            end = next(
                (
                    record for record in reversed(turn_records)
                    if record.get("action") == "end"
                ),
                None,
            )
            if end is not None:
                final_end_record = end

        if not opportunities or not (
            strong_debuff_turns or recurring_growth_turns
        ):
            continue
        after = (
            _snapshot_combat_state(
                final_end_record, "authoritative_state_after"
            )
            if final_end_record is not None else None
        )
        final_player = (after or {}).get("player") or {}
        died = _int_or_none(final_player.get("current_hp")) == 0
        if not died and maximum_stat_debt < 2:
            continue
        source = opportunities[0]["source_record"]
        serialized_opportunities = [
            {
                key: value for key, value in opportunity.items()
                if key != "source_record"
            }
            for opportunity in opportunities
        ]
        findings.append(_issue(
            source,
            "persistent_attrition_race_missed",
            enemy_id=opportunities[0]["enemy_id"],
            recurring_effect=opportunities[0]["effect"],
            strong_debuff_turns=sorted(set(strong_debuff_turns)),
            recurring_growth_turns=sorted(set(recurring_growth_turns)),
            maximum_negative_strength_dexterity=maximum_stat_debt,
            combat_ended_in_death=died,
            planner_pressure_modeled=all(
                opportunity["pressure_modeled"]
                for opportunity in opportunities
            ),
            safe_tempo_opportunities=serialized_opportunities,
            evidence_basis=(
                "independent_serialized_direct_attack_block_lower_bound"
            ),
        ))
    coverage["violations"] += len(findings)
    return findings, coverage


def _apparition_realized_value_audit(cohort):
    """Compare Ghosts' signed estimate with later live card utilization.

    The event settlement proves that the five cards arrived, but that alone
    cannot prove the estimated value.  Unupgraded Apparitions are Ethereal, so
    every accepted copy later played or left in hand at end of turn is an
    observable timing opportunity.  Also compare any immediate remove/
    transform choice against starter alternatives to keep the event and grid
    value surfaces internally consistent.
    """

    def value(record, key, default=None):
        payload = record.get("decision")
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
        return record.get(key, default)

    def decision_outcome(record):
        outcome = value(record, "decision_outcome", {})
        return outcome if isinstance(outcome, dict) else {}

    def candidates(record):
        rows = []
        for key in ("producer_candidates", "candidates"):
            current = value(record, key, [])
            if isinstance(current, list):
                rows.extend(row for row in current if isinstance(row, dict))
        return rows

    def selected_score(rows, selected_card):
        selected_uuid = selected_card.get("card_instance_id")
        for row in rows:
            consequence = row.get("consequences")
            consequence = consequence if isinstance(consequence, dict) else {}
            card = consequence.get("selected_card")
            card = card if isinstance(card, dict) else {}
            if (
                selected_uuid
                and card.get("card_instance_id") == selected_uuid
            ):
                return _finite_number(row.get("local_score", row.get("score")))
        return None

    findings = []
    coverage = {
        "apparition_realized_value": Counter(),
        "apparition_cross_decision_consistency": Counter(),
    }
    for event_index, event in enumerate(cohort):
        if (
            str(value(event, "phase", "") or "").upper() != "EVENT"
            or str(value(event, "reason", "") or "")
            != "ghosts_signed_post_state"
        ):
            continue
        outcome = decision_outcome(event)
        added = ((outcome.get("deck") or {}).get("added"))
        added = added if isinstance(added, list) else []
        package_cards = [
            card for card in added
            if isinstance(card, dict)
            and _normalized_id(card.get("id") or card.get("card_id"))
            in {"apparition", "ghostly"}
        ]
        if not package_cards:
            continue
        package_ids = {
            str(card.get("card_instance_id"))
            for card in package_cards if card.get("card_instance_id")
        }
        outcome_facts = value(event, "outcome_facts", {})
        outcome_facts = (
            outcome_facts if isinstance(outcome_facts, dict) else {}
        )
        valuation = None
        for fact in outcome_facts.values():
            fact = fact if isinstance(fact, dict) else {}
            post_state = fact.get("post_state")
            post_state = post_state if isinstance(post_state, dict) else {}
            current = post_state.get("apparition_valuation")
            if isinstance(current, dict):
                valuation = current
                break
        valuation = valuation or {}
        predicted_playability = _finite_number(
            valuation.get("playability_fraction")
        )
        upgraded = bool(valuation.get("upgraded"))
        event_score = None
        for row in candidates(event):
            if str(row.get("score_rule_id") or "").startswith(
                "ghosts_contextual_apparition_value_"
            ):
                event_score = _finite_number(
                    row.get("local_score", row.get("score"))
                )
                break

        realized = coverage["apparition_realized_value"]
        realized["eligible"] += 1
        played = set()
        expired = set()
        projected_prevention = 0.0
        for record_index in range(event_index + 1, len(cohort)):
            record = cohort[record_index]
            action = str(value(record, "action", "") or "").lower()
            combat_id = value(record, "combat_id")
            turn = _int_or_none(value(record, "turn"))
            card_id = _normalized_id(value(record, "card_id"))
            card_uuid = value(record, "card_instance_id")
            package_match = (
                str(card_uuid) in package_ids
                if package_ids and card_uuid
                else card_id in {"apparition", "ghostly"}
            )
            if action == "play" and package_match:
                opportunity = (
                    combat_id, str(card_uuid or value(record, "before_seq"))
                )
                played.add(opportunity)
                before_loss = _finite_number(
                    value(record, "projected_hp_loss_before")
                )
                if before_loss is not None:
                    for following in cohort[record_index + 1:]:
                        if (
                            value(following, "combat_id") != combat_id
                            or _int_or_none(value(following, "turn")) != turn
                        ):
                            break
                        after_loss = _finite_number(
                            value(following, "projected_hp_loss_before")
                        )
                        if after_loss is not None:
                            projected_prevention += max(
                                0.0, before_loss - after_loss
                            )
                            break
            if action == "end" and not upgraded:
                hand = value(record, "hand_before", [])
                hand = hand if isinstance(hand, list) else []
                for card in hand:
                    if not isinstance(card, dict):
                        continue
                    hand_id = _normalized_id(
                        card.get("id") or card.get("card_id")
                    )
                    hand_uuid = card.get("card_instance_id")
                    hand_match = (
                        str(hand_uuid) in package_ids
                        if package_ids and hand_uuid
                        else hand_id in {"apparition", "ghostly"}
                    )
                    if hand_match:
                        expired.add((combat_id, str(
                            hand_uuid or f"{value(record, 'before_seq')}:{len(expired)}"
                        )))

            if str(value(record, "phase", "") or "").upper() != "GRID":
                continue
            rows = candidates(record)
            package_rows = []
            starter_scores = []
            for row in rows:
                consequence = row.get("consequences")
                consequence = (
                    consequence if isinstance(consequence, dict) else {}
                )
                candidate_card = consequence.get("selected_card")
                candidate_card = (
                    candidate_card
                    if isinstance(candidate_card, dict) else {}
                )
                candidate_id = _normalized_id(
                    candidate_card.get("id")
                    or candidate_card.get("card_id")
                )
                candidate_uuid = candidate_card.get("card_instance_id")
                score = _finite_number(
                    row.get("local_score", row.get("score"))
                )
                if (
                    candidate_id in {"apparition", "ghostly"}
                    and (
                        not package_ids
                        or not candidate_uuid
                        or str(candidate_uuid) in package_ids
                    )
                ):
                    package_rows.append(row)
                if (
                    score is not None
                    and candidate_id.startswith(("strike", "defend"))
                ):
                    starter_scores.append(score)
            if not package_rows or not starter_scores:
                continue
            cross = coverage["apparition_cross_decision_consistency"]
            cross["eligible"] += 1
            cross["evaluated"] += 1
            selected = _selected_card_from_record(record)
            selected = selected if isinstance(selected, dict) else {}
            if _normalized_id(
                selected.get("id") or selected.get("card_id")
            ) not in {"apparition", "ghostly"}:
                continue
            chosen_score = selected_score(rows, selected)
            best_starter_score = max(starter_scores)
            if (
                chosen_score is not None
                and chosen_score > best_starter_score + 1e-9
                and (event_score is None or event_score > 0.0)
            ):
                findings.append(_issue(
                    record,
                    "apparition_cross_decision_value_conflict",
                    event_before_seq=value(event, "before_seq"),
                    event_score=event_score,
                    selected_apparition_score=chosen_score,
                    best_starter_score=best_starter_score,
                    selected_card_instance_id=selected.get(
                        "card_instance_id"
                    ),
                    evidence_basis=(
                        "accepted_package_then_grid_marginal_value"
                    ),
                ))
                cross["violations"] += 1

        opportunities = len(played) + len(expired - played)
        if predicted_playability is None or opportunities < 3:
            realized["unknown"] += 1
            continue
        realized["evaluated"] += 1
        observed_playability = len(played) / max(1, opportunities)
        if observed_playability + 0.20 < predicted_playability:
            findings.append(_issue(
                event,
                "apparition_playability_overestimated",
                predicted_playability=round(predicted_playability, 4),
                observed_playability=round(observed_playability, 4),
                played_opportunities=len(played),
                expired_unused_opportunities=len(expired - played),
                total_observed_opportunities=opportunities,
                projected_hp_prevention_lower_bound=round(
                    projected_prevention, 3
                ),
                evidence_basis="accepted_package_later_combat_actions",
            ))
            realized["violations"] += 1
    return findings, coverage


def audit_records(records, decision_hash, attempt_id=None):
    records = list(records)
    bound_records = [
        record for record in records
        if (
            record.get("attempt_id") == attempt_id
            if attempt_id is not None
            else record.get("decision_hash") == decision_hash
        )
    ]
    record_type_counts = Counter(
        str(record.get("record_type") or "missing")
        for record in bound_records
    )
    known_record_types = sorted(
        record_type for record_type in record_type_counts
        if record_type in _KNOWN_RECORD_TYPES
    )
    unknown_record_types = sorted(
        record_type for record_type in record_type_counts
        if record_type not in _KNOWN_RECORD_TYPES
    )
    unknown_record_type_records = [
        record for record in bound_records
        if str(record.get("record_type") or "missing")
        not in _KNOWN_RECORD_TYPES
    ]
    hash_mismatch_records = [
        record for record in records
        if attempt_id is not None
        and record.get("attempt_id") == attempt_id
        and record.get("record_type") in {
            "protocol_event", "model_advice",
        }
        and record.get("decision_hash") != decision_hash
    ]
    matching = [
        record for record in records
        if record.get("decision_hash") == decision_hash
        and (attempt_id is None or record.get("attempt_id") == attempt_id)
        and (
            record.get("decision_schema_version") == 2
            or record.get("schema_version") == 2
        )
    ]
    cohort = [
        record for record in matching
        if record.get("record_type") == "decision"
        and record.get("decision_schema_version") == 2
    ]
    protocol_event_records = [
        record for record in matching
        if record.get("record_type") == "protocol_event"
    ]
    protocol_event_counts = Counter(
        str(record.get("event") or "unknown")
        for record in protocol_event_records
    )
    benign_resync_indexes = _benign_resync_record_indexes(protocol_event_records)
    fatal_protocol_records = [
        record for index, record in enumerate(protocol_event_records)
        if index not in benign_resync_indexes
        and record.get("event") in _FATAL_PROTOCOL_EVENTS
    ]
    model_advice_records = []
    cache_warmup_records = []
    for record in matching:
        if record.get("record_type") == "cache_warmup":
            advisor = record.get("advisor")
            if isinstance(advisor, dict):
                cache_warmup_records.append(advisor)
        if record.get("record_type") == "model_advice":
            advisor = record.get("advisor")
            if isinstance(advisor, dict):
                model_advice_records.append(advisor)
        if record.get("record_type") == "decision":
            advisor = (record.get("decision") or {}).get("model_advice")
            if isinstance(advisor, dict):
                model_advice_records.append(advisor)
    model_status_counts = Counter(
        str(item.get("status") or "unknown")
        for item in model_advice_records
    )
    model_usage = [
        item.get("usage") or {}
        for item in model_advice_records
        if isinstance(item.get("usage"), dict)
    ]
    warmup_usage = [
        item.get("usage") or {}
        for item in cache_warmup_records
        if isinstance(item.get("usage"), dict)
    ]
    remote_model_advice_records = [
        item for item in model_advice_records
        if _is_remote_consultation(item)
    ]
    valid_model_advice_records = [
        item for item in model_advice_records
        if _is_valid_model_recommendation(item)
    ]
    remote_valid_model_advice_records = [
        item for item in remote_model_advice_records
        if _is_valid_model_recommendation(item)
    ]
    model_error_counts = Counter(
        error for item in model_advice_records
        if (error := _model_error(item)) is not None
    )
    model_agreements = sum(
        item.get("model_choice_id") == item.get("rule_choice_id")
        for item in valid_model_advice_records
    )
    effective_overrides = sum(
        bool(item.get("applied"))
        and item.get("model_choice_id") != item.get("rule_choice_id")
        for item in valid_model_advice_records
    )
    circuit_suppressions = sum(
        model_error_counts[error] for error in _CIRCUIT_SUPPRESSION_ERRORS
    )
    budget_suppressions = sum(
        model_error_counts[error] for error in _BUDGET_SUPPRESSION_ERRORS
    )
    advisor_disabled_suppressions = sum(
        model_error_counts[error] for error in _ADVISOR_DISABLED_ERRORS
    )
    all_suppression_errors = (
        _CIRCUIT_SUPPRESSION_ERRORS
        | _BUDGET_SUPPRESSION_ERRORS
        | _ADVISOR_DISABLED_ERRORS
    )
    suppressed_consultations = sum(
        model_error_counts[error] for error in all_suppression_errors
    )
    remote_latencies = [
        _safe_nonnegative_int(item.get("latency_ms"))
        for item in remote_model_advice_records
    ]
    all_model_latencies = [
        _safe_nonnegative_int(item.get("latency_ms"))
        for item in model_advice_records
    ]
    cache_hit_tokens = sum(
        _safe_nonnegative_int(item.get("prompt_cache_hit_tokens"))
        for item in model_usage
    )
    cache_miss_tokens = sum(
        _safe_nonnegative_int(item.get("prompt_cache_miss_tokens"))
        for item in model_usage
    )
    model_conflicts = _model_conflict_summary(model_advice_records)
    consultation_outcomes = Counter(
        _consultation_outcome(item) for item in model_advice_records
    )
    semantic_choice_changes = consultation_outcomes["choice_change"]
    local_confirmations = consultation_outcomes["local_confirmation"]
    protocol_confirmations = consultation_outcomes["protocol_confirmation"]
    review_no_choice_change = consultation_outcomes[
        "review_no_choice_change"
    ]
    semantic_adoption_denominator = (
        semantic_choice_changes
        + local_confirmations
        + review_no_choice_change
    )
    # Latest terminal outcomes remain useful for progress reporting, but an
    # earlier bound operational error permanently taints comparability.  Keep
    # that evidence explicit instead of allowing a later GAME_OVER to hide it.
    latest_termination_by_attempt = {}
    operational_errors_by_attempt = {}
    latest_combat_decision_by_attempt = {}
    combat_decisions_by_attempt = {}
    for record in matching:
        identity = record.get("attempt_id")
        if (
            record.get("record_type") == "decision"
            and isinstance(identity, str)
            and identity.strip()
            and isinstance(record.get("monsters_before"), list)
        ):
            latest_combat_decision_by_attempt[identity] = record
            combat_decisions_by_attempt.setdefault(identity, []).append(
                record
            )
        if record.get("termination_kind") not in {"game_over", "operational_error"}:
            continue
        if not isinstance(identity, str) or not identity.strip():
            # Preflight and validation diagnostics are intentionally traced,
            # but without a verified attempt binding they are not a terminal
            # event in the autonomous-run cohort.
            continue
        if record.get("termination_kind") == "operational_error":
            operational_errors_by_attempt.setdefault(identity, []).append(
                record
            )
        latest_termination_by_attempt[identity] = record
    termination_counts = Counter(
        record.get("termination_kind")
        for record in latest_termination_by_attempt.values()
    )
    issues = []
    review_findings = []
    review_resolutions = []
    reasons = Counter()
    review_reasons = Counter()
    for polluted in (
        record for record in matching
        if record.get("record_type") == "auxiliary_trace_pollution"
    ):
        kind = "auxiliary_trace_pollution"
        issues.append(_issue(
            polluted,
            kind,
            foreign_attempt_id=polluted.get("foreign_attempt_id"),
            foreign_record_type=polluted.get("foreign_record_type"),
            foreign_decision_hash=polluted.get("foreign_decision_hash"),
            source_trace=polluted.get("source_trace"),
        ))
        reasons[kind] += 1
    for unknown_record in unknown_record_type_records:
        kind = "unknown_record_type"
        issues.append(_issue(
            unknown_record,
            kind,
            record_type=unknown_record.get("record_type"),
        ))
        reasons[kind] += 1
    for mismatched in hash_mismatch_records:
        issues.append(_issue(
            mismatched,
            "decision_hash_mismatch",
            record_type=mismatched.get("record_type"),
            expected_decision_hash=decision_hash,
            observed_decision_hash=mismatched.get("decision_hash"),
        ))
        reasons["decision_hash_mismatch"] += 1
    for protocol_record in fatal_protocol_records:
        event = str(protocol_record.get("event") or "unknown")
        if event not in _FATAL_PROTOCOL_EVENTS:
            continue
        issues.append(_issue(
            protocol_record,
            event,
            receipt_status=protocol_record.get("receipt_status"),
            receipt_error=protocol_record.get("receipt_error"),
        ))
        reasons[event] += 1
    for identity, errors in operational_errors_by_attempt.items():
        latest_error = errors[-1]
        issues.append(_issue(
            latest_error,
            "operational_error_tainted_attempt",
            error=latest_error.get("error"),
            message=latest_error.get("message"),
            occurrences=len(errors),
        ))
        reasons["operational_error_tainted_attempt"] += 1
        # Keep the protocol taint, but also expose a combat-specific issue
        # when the bridge rejected a targeted card because its authoritative
        # enemy binding was transiently absent/ambiguous.  This is the exact
        # failure that used to terminate the F39 Writhing Mass fight without
        # appearing in the combat strategy channel.
        error_text = " ".join(
            str(latest_error.get(field) or "")
            for field in ("error", "message")
        )
        target_binding_markers = (
            "enemy_instance_id does not resolve uniquely",
            "bound enemy is not a legal target",
        )
        if any(marker in error_text for marker in target_binding_markers):
            # Terminal/operational records do not have a ``before_seq`` like
            # a normal decision.  Preserve the authoritative rejection frame
            # so the combat review can place the protocol race on the exact
            # turn instead of leaving it as an unlocated global error.
            rejection_state_seq = (
                _int_or_none(latest_error.get("result_state_seq"))
                or _int_or_none(latest_error.get("terminal_state_seq"))
                or _int_or_none(latest_error.get("state_seq"))
            )
            issues.append(_issue(
                latest_error,
                "combat_target_binding_rejection",
                error=latest_error.get("error"),
                message=latest_error.get("message"),
                occurrences=len(errors),
                result_state_seq=rejection_state_seq,
            ))
            reasons["combat_target_binding_rejection"] += 1
    for terminal in latest_termination_by_attempt.values():
        if (
            terminal.get("termination_kind") != "game_over"
            or bool(terminal.get("victory"))
            or _int_or_none(terminal.get("current_hp")) not in {None, 0}
        ):
            continue
        retained = []
        retained_ids = set()
        latest_combat = latest_combat_decision_by_attempt.get(
            terminal.get("attempt_id")
        )
        for potion in terminal.get("potions") or []:
            if not isinstance(potion, dict):
                continue
            potion_id = _normalized_id(potion.get("id"))
            if (
                potion_id in _ACTIONABLE_TERMINAL_POTION_IDS
                and not _retained_debuff_potion_blocked_by_artifact(
                    potion_id, latest_combat
                )
            ):
                retained.append({
                    "id": potion.get("id"),
                    "slot": _int_or_none(potion.get("slot")),
                })
                retained_ids.add(potion_id)
        opportunities = _terminal_retained_potion_opportunities(
            retained_ids,
            combat_decisions_by_attempt.get(terminal.get("attempt_id")),
        )
        evidenced_ids = {
            item["potion_id"] for item in opportunities
        }
        retained = [
            potion for potion in retained
            if _normalized_id(potion.get("id")) in evidenced_ids
        ]
        if retained and opportunities:
            issues.append(_issue(
                terminal,
                "death_with_actionable_potion_retained",
                act=_int_or_none(terminal.get("act")),
                floor=_int_or_none(terminal.get("floor")),
                character=terminal.get("class"),
                retained_potions=retained,
                missed_opportunities=opportunities,
                evidence_basis=(
                    "exact_serialized_critical_turn_mitigation"
                ),
            ))
            reasons["death_with_actionable_potion_retained"] += 1
    pending_event_card_grants = {}
    pending_true_combat_end = {}
    combat_turn_contexts = {}
    reported_long_setup_turns = set()
    reported_smoke_bomb_turns = set()
    barricade_liveness_trackers = {}
    # A setup card's value can depend on cards played later in this same
    # turn.  Index the turn first so Rage followed by another Attack is not
    # reported as a fully wasted late setup merely because an earlier attack
    # happened before Rage.  A setup with no downstream consumer remains
    # actionable and is still reported.
    turn_play_index = {}
    for index, item in enumerate(cohort):
        if item.get("action") != "play":
            continue
        item_combat = item.get("combat_id")
        item_turn = _int_or_none(item.get("turn"))
        if not item_combat or item_turn is None:
            continue
        key = (
            item.get("attempt_id"), item.get("run_id"),
            item_combat, item_turn,
        )
        hand = item.get("hand_before") or []
        played = next(
            (
                card for card in hand
                if isinstance(card, dict)
                and card.get("card_instance_id")
                == item.get("card_instance_id")
            ),
            None,
        )
        turn_play_index.setdefault(key, []).append({
            "index": index,
            "card_type": str((played or {}).get("type") or "").upper(),
        })
    optional_hand_selection = None
    unproven_potions_by_turn = Counter()
    coverage = {
        "combat_decision_context": Counter(),
        "card_play_damage_underprediction": Counter(),
        "end_turn_damage_underprediction": Counter(),
        "turn_loss_arithmetic": Counter(),
        "next_turn_start_damage": Counter(),
        "avoidable_loss_end_turn_candidate": Counter(),
        "time_eater_safe_reset_missed": Counter(),
        "split_interrupt_opportunity": Counter(),
        "invalid_post_unload_plan": Counter(),
        "forced_damage_source_exhaustion": Counter(),
        "impossible_post_forced_exhaust_plan": Counter(),
        "late_trigger_power_setup": Counter(),
        "missed_thousand_cuts_trigger": Counter(),
        "offering_at_choker_limit": Counter(),
        "voluntary_self_damage_dominance": Counter(),
        "smoke_bomb_escape": Counter(),
        "long_fight_scaling_setup": Counter(),
        "unconsumed_double_tap_setup": Counter(),
        "choker_slots_without_attack_progress": Counter(),
        "affordable_attack_progress_ignored": Counter(),
        "redundant_covered_block_play": Counter(),
        "passive_doom_claim_proof": Counter(),
        "card_damage_consistency": Counter(),
        "end_turn_damage_consistency": Counter(),
        "end_turn_with_resources": Counter(),
        "noncombat_candidate_selection": Counter(),
        "noncombat_candidate_consequences": Counter(),
        "high_impact_noncombat_rationale": Counter(),
        "event_curse_probability_consistency": Counter(),
        "mind_bloom_value_component_pricing": Counter(),
        "vampires_bite_package_purge": Counter(),
        "missed_shelled_parasite_progress": Counter(),
        "barricade_liveness_stall": Counter(),
        "persistent_attrition_race_missed": Counter(),
        "apparition_realized_value": Counter(),
        "apparition_cross_decision_consistency": Counter(),
        "neow_lament_elite_route": Counter(),
        "negative_relic_purchase_risk": Counter(),
    }
    attrition_findings, attrition_coverage = (
        _persistent_attrition_race_audit(cohort)
    )
    issues.extend(attrition_findings)
    reasons["persistent_attrition_race_missed"] += len(
        attrition_findings
    )
    coverage["persistent_attrition_race_missed"].update(
        attrition_coverage
    )
    apparition_findings, apparition_coverage = (
        _apparition_realized_value_audit(cohort)
    )
    issues.extend(apparition_findings)
    for finding in apparition_findings:
        reasons[str(finding.get("kind") or "unknown")] += 1
    for name, counter in apparition_coverage.items():
        coverage[name].update(counter)
    damage_consistency = {
        "player_actions": Counter(),
        "enemy_turns": Counter(),
    }
    last_combat_context = None
    vampires_package_pending = {}
    passive_doom_evidence = []
    for cohort_index, record in enumerate(cohort):
        action = record.get("action")
        if record.get("policy_version") != "fast-policy-v5":
            issues.append(_issue(record, "policy_mismatch"))
            reasons["policy_mismatch"] += 1

        decision_payload = record.get("decision") or {}
        if str(decision_payload.get("reason") or "") == "shop_global_comparison":
            selected_id = str(
                decision_payload.get("chosen_id")
                or decision_payload.get("chosen")
                or ""
            )
            selected_candidate = next(
                (
                    candidate
                    for candidate in decision_payload.get("candidates") or []
                    if isinstance(candidate, dict)
                    and (
                        str(candidate.get("choice_id") or "") == selected_id
                        or candidate.get("reason") == "selected_purchase"
                    )
                ),
                None,
            )
            acquired = (
                ((selected_candidate or {}).get("consequences") or {}).get(
                    "acquired_benefit"
                )
                or {}
            )
            selected_relic_id = _normalized_id(
                acquired.get("relic_id") or acquired.get("id")
            )
            relic_strategy = _combat_predictor.relic_strategy_coverage({
                "relics": [{
                    "id": acquired.get("relic_id") or acquired.get("id")
                }]
            })
            relic_handler = (
                relic_strategy.get("decision_handlers") or {}
            ).get(selected_relic_id)
            negative_relic_handlers = getattr(
                _combat_predictor,
                "NEGATIVE_RELIC_STRATEGY_HANDLERS",
                frozenset(),
            )
            if (
                acquired.get("kind") == "relic"
                and relic_handler in negative_relic_handlers
            ):
                risk_coverage = coverage["negative_relic_purchase_risk"]
                risk_coverage["eligible"] += 1
                risk = (selected_candidate or {}).get("strategic_risk")
                score_inputs = (selected_candidate or {}).get(
                    "score_inputs"
                ) or {}
                downside = score_inputs.get("relic_downside_risk")
                evaluated = bool(
                    isinstance(risk, dict)
                    and risk.get("evaluated") is True
                    and str(risk.get("effect") or "")
                    and str(risk.get("boss_id") or "")
                    and isinstance(downside, (int, float))
                    and not isinstance(downside, bool)
                    and float(downside) > 0.0
                )
                if evaluated:
                    risk_coverage["evaluated"] += 1
                else:
                    issues.append(_issue(
                        record,
                        "negative_relic_purchase_unpriced",
                        relic_id=acquired.get("relic_id") or acquired.get("id"),
                        relic_strategy_handler=relic_handler,
                        strategic_risk=risk,
                        score_inputs=score_inputs,
                    ))
                    reasons["negative_relic_purchase_unpriced"] += 1
                    risk_coverage["violations"] += 1

        phase = str(record.get("phase") or "")
        combat_id = record.get("combat_id")
        combat_key = (
            record.get("attempt_id"), record.get("run_id"), combat_id
        )
        attempt_run_key = (record.get("attempt_id"), record.get("run_id"))

        if phase == "MAP":
            lament_charges = _int_or_none(
                decision_payload.get("neow_lament_charges")
            )
            if lament_charges is not None and lament_charges > 0:
                lament_coverage = coverage["neow_lament_elite_route"]
                lament_coverage["eligible"] += 1
                rows = _candidate_rows(record)
                options = record.get("available_options_before")
                if isinstance(options, list) and options:
                    canonical_rows, canonical_evidence = (
                        _candidate_option_bijection(rows, options)
                    )
                    if canonical_evidence.get("complete") is True:
                        rows = canonical_rows
                eligible_rows = [
                    row for row in rows
                    if row.get("selection_eligible") is not False
                ]
                route_depths = {
                    row.get("candidate_id"): _neow_lament_elite_depths(row)
                    for row in eligible_rows
                }
                if not eligible_rows or any(
                    depths is None for depths in route_depths.values()
                ):
                    lament_coverage["evaluated"] += 1
                    lament_coverage["violations"] += 1
                    issues.append(_issue(
                        record,
                        "neow_lament_route_evidence_missing",
                        remaining_charges=lament_charges,
                        candidate_route_depths=route_depths,
                        candidate_count=len(eligible_rows),
                    ))
                    reasons["neow_lament_elite_route"] += 1
                else:
                    selected = _selected_candidate(record, rows)
                    reachable = {
                        candidate_id: depths
                        for candidate_id, depths in route_depths.items()
                        if depths
                    }
                    if selected is None:
                        lament_coverage["unknown"] += 1
                    else:
                        lament_coverage["evaluated"] += 1
                        selected_depths = _neow_lament_elite_depths(
                            selected
                        )
                        if reachable and not selected_depths:
                            lament_coverage["violations"] += 1
                            issues.append(_issue(
                                record,
                                "neow_lament_elite_route_missed",
                                remaining_charges=lament_charges,
                                selected_candidate=selected.get(
                                    "candidate_id"
                                ),
                                selected_lament_elite_depths=(
                                    selected_depths
                                ),
                                reachable_lament_elite_routes=reachable,
                                evidence_basis=(
                                    "connected_route_charge_consumption"
                                ),
                            ))
                            reasons["neow_lament_elite_route"] += 1

        # A non-combat room decision is an authoritative encounter boundary.
        # Multi-stage rooms such as Colosseum can start another fight on the
        # same act/floor/room_type, which intentionally hashes to the same
        # legacy combat_id.  Do not carry terminal claims or turn-local state
        # from the completed stage into the later combat.  GRID/HAND_SELECT are
        # excluded because they may be continuations inside a live combat.
        encounter_boundary = (
            not combat_id
            and phase.upper() in {
                "NEOW", "EVENT", "MAP", "COMBAT_REWARD", "BOSS_REWARD",
                "REST", "SHOP", "SHOP_SCREEN", "TREASURE", "CHEST",
            }
        )
        if encounter_boundary:
            pending_true_combat_end = {
                key: value for key, value in pending_true_combat_end.items()
                if key[:2] != attempt_run_key
            }
            combat_turn_contexts = {
                key: value for key, value in combat_turn_contexts.items()
                if key[:2] != attempt_run_key
            }
            barricade_liveness_trackers = {
                key: value
                for key, value in barricade_liveness_trackers.items()
                if key[:2] != attempt_run_key
            }
            if (
                last_combat_context is not None
                and last_combat_context.get("attempt_run_key")
                == attempt_run_key
            ):
                last_combat_context = None

        # Track durable-Block encounters independently of the per-turn
        # arithmetic checks below.  A normal Block packet is deliberately not
        # eligible; only a single live Barricade/Spheric Guardian can produce
        # this cross-turn liveness evidence.
        turn_value = _int_or_none(record.get("turn"))
        if (
            phase.startswith("COMBAT")
            and turn_value is not None
            and action in {"play", "end"}
        ):
            persistent_monster = _persistent_barricade_monster(record)
            matching_tracker_keys = [
                key for key in barricade_liveness_trackers
                if key[:3] == combat_key
            ]
            if persistent_monster is None:
                for key in matching_tracker_keys:
                    barricade_liveness_trackers.pop(key, None)
            else:
                enemy_key = (
                    persistent_monster.get("enemy_instance_id")
                    or persistent_monster.get("id")
                    or persistent_monster.get("monster_index")
                )
                tracker_key = (*combat_key, str(enemy_key))
                for key in matching_tracker_keys:
                    if key != tracker_key:
                        barricade_liveness_trackers.pop(key, None)
                tracker = barricade_liveness_trackers.setdefault(
                    tracker_key,
                    {
                        "first_turn": turn_value,
                        "turn": None,
                        "turn_min_hp": None,
                        "turn_min_block": None,
                        "best_hp": None,
                        "best_block": None,
                        "stalled_turns": 0,
                        "completed_turns": 0,
                        "reported": False,
                        "first_seq": record.get("before_seq"),
                        "last_seq": record.get("before_seq"),
                    },
                )
                hp_now = _safe_nonnegative_int(
                    persistent_monster.get("current_hp")
                )
                block_now = _safe_nonnegative_int(
                    persistent_monster.get("block")
                )
                if tracker.get("turn") != turn_value:
                    tracker["turn"] = turn_value
                    tracker["turn_min_hp"] = hp_now
                    tracker["turn_min_block"] = block_now
                else:
                    tracker["turn_min_hp"] = min(
                        hp_now,
                        _safe_nonnegative_int(
                            tracker.get("turn_min_hp")
                        ),
                    )
                    tracker["turn_min_block"] = min(
                        block_now,
                        _safe_nonnegative_int(
                            tracker.get("turn_min_block")
                        ),
                    )
                tracker["last_seq"] = record.get("before_seq")
                if action == "end":
                    coverage["barricade_liveness_stall"]["eligible"] += 1
                    coverage["barricade_liveness_stall"]["evaluated"] += 1
                    turn_hp = _safe_nonnegative_int(
                        tracker.get("turn_min_hp")
                    )
                    turn_block = _safe_nonnegative_int(
                        tracker.get("turn_min_block")
                    )
                    best_hp = tracker.get("best_hp")
                    best_block = tracker.get("best_block")
                    if best_hp is None or best_block is None:
                        tracker["best_hp"] = turn_hp
                        tracker["best_block"] = turn_block
                        tracker["stalled_turns"] = 1
                    else:
                        progressed = (
                            turn_hp < int(best_hp)
                            or turn_block < int(best_block)
                        )
                        if progressed:
                            tracker["best_hp"] = min(int(best_hp), turn_hp)
                            tracker["best_block"] = min(
                                int(best_block), turn_block
                            )
                            tracker["stalled_turns"] = 0
                        else:
                            tracker["stalled_turns"] = int(
                                tracker.get("stalled_turns") or 0
                            ) + 1
                    tracker["completed_turns"] = int(
                        tracker.get("completed_turns") or 0
                    ) + 1
                    # Twelve complete turns is the same deliberately narrow
                    # bound used by the live controller guard.  Requiring a
                    # positive record-low Block prevents a zero-Block fight
                    # from being mislabeled as Barricade liveness.
                    if (
                        not tracker.get("reported")
                        and int(tracker.get("stalled_turns") or 0) >= 12
                        and _safe_nonnegative_int(
                            tracker.get("best_block")
                        ) > 0
                    ):
                        issues.append(_issue(
                            record,
                            "barricade_liveness_stall",
                            enemy_id=persistent_monster.get("id"),
                            enemy_instance_id=persistent_monster.get(
                                "enemy_instance_id"
                            ),
                            first_turn=tracker.get("first_turn"),
                            last_turn=turn_value,
                            first_seq=tracker.get("first_seq"),
                            last_seq=tracker.get("last_seq"),
                            completed_turns=tracker.get(
                                "completed_turns"
                            ),
                            turns_without_progress=tracker.get(
                                "stalled_turns"
                            ),
                            best_hp=tracker.get("best_hp"),
                            best_block=tracker.get("best_block"),
                        ))
                        reasons["barricade_liveness_stall"] += 1
                        tracker["reported"] = True

        # Bind the irreversible Vampires payment to later GRID decisions even
        # when the producer omitted its candidate rows.  This is an
        # authoritative package ledger, not a heuristic that treats every
        # Bite as bad: only an accepted max-HP trade creates the obligation.
        if phase == "EVENT" and _event_token(record) == "vampires":
            event_card = _selected_card_from_record(record)
            outcome = record.get("decision_outcome") or {}
            if (
                _normalized_id((event_card or {}).get("id")) == "bite"
                and isinstance(record.get("selected_choice_ids"), list)
                and record.get("selected_choice_ids")
                and (
                    (
                        _int_or_none(outcome.get("max_hp_delta")) is not None
                        and _int_or_none(outcome.get("max_hp_delta")) < 0
                    )
                    or (
                        _int_or_none(outcome.get("hp_delta")) is not None
                        and _int_or_none(outcome.get("hp_delta")) < 0
                    )
                )
            ):
                vampires_package_pending[attempt_run_key] = {
                    "event_before_seq": record.get("before_seq"),
                    "accepted_bite": True,
                }

        if _strategic_noncombat_multichoice(record):
            canonical_options, canonical_surface_violations = (
                _canonical_noncombat_surface(record)
            )
            options = (
                canonical_options
                if canonical_options is not None
                else record.get("available_options_before") or []
            )
            raw_rows = _candidate_rows(record)
            rows, candidate_bijection = _candidate_option_bijection(
                raw_rows, options
            )
            candidate_bijection["canonical_surface_violations"] = (
                canonical_surface_violations
            )
            if canonical_surface_violations:
                candidate_bijection["complete"] = False
                candidate_bijection["order_invariant"] = False
            selection_coverage = coverage[
                "noncombat_candidate_selection"
            ]
            selection_coverage["eligible"] += 1
            scored_rows = [
                row for row in rows if row.get("score") is not None
            ]
            selected_row = _selected_candidate(record, rows)
            selected_rows = _selected_candidate_rows(
                record,
                rows,
                cohort[cohort_index - 1] if cohort_index > 0 else None,
            )
            scores_auditable, score_evidence = (
                _candidate_scores_auditable(
                    record, rows, candidate_bijection
                )
            )
            candidate_evidence_complete = bool(
                candidate_bijection["complete"]
                and len(scored_rows) == len(rows)
                and selected_rows is not None
                and scores_auditable
            )
            selected_is_argmax = False
            if (
                not rows
                or len(scored_rows) != len(rows)
                or not candidate_bijection["complete"]
                or selected_rows is None
                or not scores_auditable
            ):
                kind = "noncombat_candidate_evidence_incomplete"
                review_findings.append(_issue(
                    record,
                    kind,
                    phase=phase,
                    candidate_count=len(rows),
                    raw_candidate_count=len(raw_rows),
                    scored_candidate_count=len(scored_rows),
                    visible_option_count=len(options),
                    unmatched_option_ids=candidate_bijection[
                        "missing_option_ids"
                    ],
                    extra_candidate_ids=candidate_bijection[
                        "extra_candidate_ids"
                    ],
                    ambiguous_candidate_ids=candidate_bijection[
                        "ambiguous_candidate_ids"
                    ],
                    duplicate_candidate_ids=candidate_bijection[
                        "duplicate_candidate_ids"
                    ],
                    duplicate_option_ids=candidate_bijection[
                        "duplicate_option_ids"
                    ],
                    score_conflict_candidate_ids=candidate_bijection[
                        "score_conflict_candidate_ids"
                    ],
                    evidence_conflict_candidate_ids=candidate_bijection[
                        "evidence_conflict_candidate_ids"
                    ],
                    canonical_surface_violations=candidate_bijection[
                        "canonical_surface_violations"
                    ],
                    order_invariant=candidate_bijection[
                        "order_invariant"
                    ],
                    actual_choice_bound=selected_row is not None,
                    complete_selection_bound=selected_rows is not None,
                    expected_selection_count=_selection_count(record),
                    score_evidence=score_evidence,
                ))
                review_reasons[kind] += 1
            else:
                selection_coverage["evaluated"] += 1
                selection_count = len(selected_rows)
                plan_protection = _hand_select_plan_protection_validation(
                    record,
                    rows,
                    selected_rows,
                    cohort[cohort_index - 1] if cohort_index else None,
                )
                plan_protection_valid = not (
                    isinstance(plan_protection, dict)
                    and plan_protection.get("status") == "issues"
                )
                if not plan_protection_valid:
                    issues.append(_issue(
                        record,
                        "hand_select_plan_protection_unverified",
                        validation=plan_protection,
                    ))
                    reasons["hand_select_plan_protection_unverified"] += 1
                    selection_coverage["violations"] += 1
                eligible_rows = [
                    row for row in rows
                    if row.get("selection_eligible") is not False
                ]
                if not plan_protection_valid:
                    # An unverified preservation declaration cannot hide a
                    # locally better candidate from the normal argmax audit.
                    eligible_rows = rows
                if (
                    len(eligible_rows) < selection_count
                    or any(
                        row.get("selection_eligible") is False
                        for row in selected_rows
                    )
                ):
                    kind = "noncombat_ineligible_candidate_selected"
                    issues.append(_issue(
                        record,
                        kind,
                        phase=phase,
                        chosen_candidate_ids=[
                            row["candidate_id"] for row in selected_rows
                        ],
                        eligible_candidate_ids=[
                            row["candidate_id"] for row in eligible_rows
                        ],
                    ))
                    reasons[kind] += 1
                    selection_coverage["violations"] += 1
                    eligible_rows = rows
                comparison_rows, selection_scope = _reward_selection_competitors(
                    record, selected_rows, eligible_rows
                )
                ranked_rows = sorted(
                    comparison_rows,
                    key=lambda row: row["score"], reverse=True,
                )
                cutoff_score = ranked_rows[selection_count - 1]["score"]
                best_rows = [
                    row for row in comparison_rows
                    if row["score"] >= cutoff_score - 1e-6
                ]
                best_ids = [row["candidate_id"] for row in best_rows]
                selected_score = min(
                    row["score"] for row in selected_rows
                )
                selected_is_argmax = bool(
                    selected_score >= cutoff_score - 1e-6
                )
                if selected_score < cutoff_score - 1e-6:
                    gambling_chip_transition = _verified_gambling_chip_settlement(
                        cohort, cohort_index, record
                    )
                    if gambling_chip_transition:
                        # The comparison is not a card-vs-card decision: the
                        # chosen card is an optional removal and
                        # ``action:proceed`` is the protocol commit button.
                        # The typed transition above proves that the card is
                        # part of a valid multi-stage selection, so do not
                        # manufacture a local-policy violation from its score
                        # against synthetic proceed=0.
                        selected_is_argmax = True
                        selection_coverage["accepted_overrides"] += 1
                    decision_value = record.get("decision") or {}
                    advice = (
                        decision_value.get("model_advice")
                        if isinstance(decision_value, dict) else None
                    )
                    if gambling_chip_transition:
                        valid_override = True
                        override_reason = "verified_gambling_chip_transition"
                    elif selection_count == 1:
                        valid_override, override_reason = _valid_applied_override(
                            advice,
                            selected_rows[0],
                            comparison_rows,
                            best_rows,
                        )
                    else:
                        valid_override = False
                        override_reason = "multi_select_below_local_top_n"
                    if valid_override:
                        selection_coverage["accepted_overrides"] += 1
                    else:
                        kind = (
                            "noncombat_invalid_model_override"
                            if isinstance(advice, dict)
                            and advice.get("applied") is True
                            else "noncombat_candidate_argmax_missed"
                        )
                        issues.append(_issue(
                            record,
                            kind,
                            phase=phase,
                            chosen_candidate_ids=[
                                row["candidate_id"]
                                for row in selected_rows
                            ],
                            chosen_score=round(selected_score, 6),
                            local_best_candidate_ids=best_ids,
                            local_best_score=round(cutoff_score, 6),
                            score_gap=round(
                                cutoff_score - selected_score, 6
                            ),
                            selection_scope=selection_scope,
                            override_validation=override_reason,
                        ))
                        reasons[kind] += 1
                        selection_coverage["violations"] += 1

                # The Vampires event is a coupled trade: max HP is paid once
                # to receive a package of free Bite attacks.  The ordinary
                # GRID auditor only checks whether the selected card was the
                # local argmax, so it previously blessed each later Bite
                # purge even when Coffee Dripper made that package the run's
                # main compensation for the irreversible max-HP loss.  Keep
                # this gate narrow: require Coffee Dripper, at least two
                # Bites in the authoritative deck, a destructive GRID
                # operation, and a visible non-Bite alternative.  A forced
                # one-card surface therefore remains auditable rather than
                # being mislabeled as a policy error.
                if phase == "GRID" and selected_rows:
                    selected_resource = selected_rows[0]
                    selected_consequence = (
                        selected_resource.get("consequences")
                        if isinstance(selected_resource, dict) else None
                    )
                    selected_card = (
                        selected_consequence.get("selected_card")
                        if isinstance(selected_consequence, dict) else None
                    )
                    selected_card_id = _normalized_id(
                        selected_card.get("id")
                        if isinstance(selected_card, dict) else None
                    )
                    operation = _normalized_id(
                        selected_consequence.get("operation")
                        if isinstance(selected_consequence, dict) else None
                    )
                    context = record.get("decision_context") or {}
                    relic_ids = record.get("relic_ids_before") or context.get(
                        "relic_ids"
                    ) or []
                    has_coffee = "coffeedripper" in {
                        _normalized_id(relic_id) for relic_id in relic_ids
                    }
                    bite_count = _deck_card_count(record, ("Bite",))
                    alternative_rows = []
                    for row in rows:
                        if row is selected_resource:
                            continue
                        if row.get("selection_eligible") is False:
                            continue
                        consequence = row.get("consequences")
                        card = (
                            consequence.get("selected_card")
                            if isinstance(consequence, dict) else None
                        )
                        if _normalized_id(
                            card.get("id") if isinstance(card, dict) else None
                        ) != "bite":
                            alternative_rows.append(row)
                    bite_coverage = coverage["vampires_bite_package_purge"]
                    if (
                        selected_card_id == "bite"
                        and operation in {"gridpurge", "gridremove", "remove"}
                        and has_coffee
                        and bite_count is not None
                        and bite_count >= 2
                        and alternative_rows
                    ):
                        bite_coverage["eligible"] += 1
                        bite_coverage["evaluated"] += 1
                        issues.append(_issue(
                            record,
                            "vampires_bite_package_purged",
                            event_before_seq=(
                                vampires_package_pending.get(
                                    attempt_run_key, {}
                                ).get("event_before_seq")
                            ),
                            selected_card_instance_id=(
                                selected_card.get("card_instance_id")
                                if isinstance(selected_card, dict) else None
                            ),
                            bite_count=bite_count,
                            alternative_card_ids=[
                                (
                                    ((row.get("consequences") or {}).get(
                                        "selected_card"
                                    ) or {}).get("id")
                                )
                                for row in alternative_rows
                            ],
                        ))
                        reasons["vampires_bite_package_purged"] += 1
                        bite_coverage["violations"] += 1

            # Some historical GRID frames contain only the authoritative
            # selected option and producer settlement; ``decision.candidates``
            # is empty because the action was emitted by the macro producer.
            # Do not let that evidence gap erase the package obligation above.
            if (
                phase == "GRID"
                and vampires_package_pending.get(attempt_run_key)
                and not any(
                    item.get("before_seq") == record.get("before_seq")
                    and item.get("kind") == "vampires_bite_package_purged"
                    for item in issues
                    if isinstance(item, dict)
                )
            ):
                selected_card = _selected_card_from_record(record) or {}
                selected_card_id = _normalized_id(selected_card.get("id"))
                context = record.get("decision_context") or {}
                relic_ids = record.get("relic_ids_before") or context.get(
                    "relic_ids"
                ) or []
                has_coffee = "coffeedripper" in {
                    _normalized_id(value) for value in relic_ids
                }
                bite_count = _deck_card_count(record, ("Bite",))
                deck_counts = context.get("deck_counts")
                has_non_bite = isinstance(deck_counts, dict) and any(
                    _normalized_id(card_id) != "bite"
                    and _safe_nonnegative_int(count) > 0
                    for card_id, count in deck_counts.items()
                )
                settlement = record.get("authoritative_choice_settlement") or {}
                future = settlement.get("classified_future_costs") or []
                operation = next(
                    (
                        _normalized_id(item.get("operation"))
                        for item in future if isinstance(item, dict)
                    ),
                    "",
                )
                removed = _authoritative_removed_cards(record)
                removed_bite = any(
                    _normalized_id(card.get("id")) == "bite"
                    for card in removed
                )
                destructive = (
                    operation in {"gridpurge", "gridremove", "remove"}
                    or "remove" in str(
                        (record.get("decision") or {}).get("reason") or ""
                    ).casefold()
                    or bool(removed)
                )
                if (
                    selected_card_id == "bite"
                    and has_coffee
                    and bite_count is not None
                    and bite_count >= 2
                    and has_non_bite
                    and destructive
                    and (removed_bite or not removed)
                ):
                    coverage["vampires_bite_package_purge"]["eligible"] += 1
                    coverage["vampires_bite_package_purge"]["evaluated"] += 1
                    issues.append(_issue(
                        record,
                        "vampires_bite_package_purged",
                        selected_card_instance_id=selected_card.get(
                            "card_instance_id"
                        ),
                        bite_count=bite_count,
                        audit_source=(
                            "authoritative_producer_settlement_without_"
                            "candidate_rows"
                        ),
                        event_before_seq=vampires_package_pending[
                            attempt_run_key
                        ]["event_before_seq"],
                    ))
                    reasons["vampires_bite_package_purged"] += 1
                    coverage["vampires_bite_package_purge"]["violations"] += 1
            high_impact = phase in _HIGH_IMPACT_NONCOMBAT_PHASES
            if high_impact:
                rationale_coverage = coverage[
                    "high_impact_noncombat_rationale"
                ]
                rationale_coverage["eligible"] += 1
                unsafe_reason = _default_or_unknown_reason(record)
                if unsafe_reason is None:
                    rationale_coverage["evaluated"] += 1
                elif unsafe_reason == "missing":
                    kind = "high_impact_decision_rationale_missing"
                    review_findings.append(_issue(
                        record, kind, phase=phase,
                    ))
                    review_reasons[kind] += 1
                else:
                    rationale_coverage["evaluated"] += 1
                    rationale_coverage["violations"] += 1
                    kind = "high_impact_default_choice"
                    issues.append(_issue(
                        record,
                        kind,
                        phase=phase,
                        reason=unsafe_reason,
                        visible_option_count=len(options),
                    ))
                    reasons[kind] += 1

            if phase == "EVENT":
                consequence_coverage = coverage[
                    "noncombat_candidate_consequences"
                ]
                consequence_coverage["eligible"] += 1
                missing_consequences = [
                    row["candidate_id"] for row in rows
                    if not isinstance(row.get("consequences"), dict)
                ]
                if not rows or missing_consequences:
                    kind = "noncombat_candidate_consequences_incomplete"
                    review_findings.append(_issue(
                        record,
                        kind,
                        event_id=_event_token(record),
                        missing_candidate_ids=missing_consequences,
                        candidate_count=len(rows),
                    ))
                    review_reasons[kind] += 1
                else:
                    consequence_coverage["evaluated"] += 1

                probability_coverage = coverage[
                    "event_curse_probability_consistency"
                ]
                for row in rows:
                    consequences = row.get("consequences")
                    matched_options = _candidate_matches_options(
                        row, options
                    )
                    visible_text = " ".join(
                        _option_text(option)
                        for option in matched_options
                    ).casefold()
                    mentions_curse = any(marker in visible_text for marker in (
                        "curse", "normality", "诅咒", "凡庸",
                    ))
                    has_curse_fields = bool(
                        isinstance(consequences, dict)
                        and any(
                            "curse" in str(key).casefold()
                            or "omamori" in str(key).casefold()
                            for key in consequences
                        )
                    )
                    if not mentions_curse and not has_curse_fields:
                        continue
                    probability_coverage["eligible"] += 1
                    if not has_curse_fields:
                        kind = "event_curse_consequence_incomplete"
                        review_findings.append(_issue(
                            record,
                            kind,
                            candidate_id=row.get("candidate_id"),
                        ))
                        review_reasons[kind] += 1
                        continue
                    probability_coverage["evaluated"] += 1
                    problem = _curse_consequence_problem(consequences)
                    if problem is not None:
                        kind = "event_curse_probability_inconsistent"
                        issues.append(_issue(
                            record,
                            kind,
                            candidate_id=row.get("candidate_id"),
                            problem=problem,
                            consequences=consequences,
                        ))
                        reasons[kind] += 1
                        probability_coverage["violations"] += 1

                # Mind Bloom is a compact regression surface for a broader
                # score-completeness failure: all three options previously had
                # reproducible totals, but War's rare relic was hidden inside
                # a fixed base and low HP was charged twice.  Verify semantic
                # coverage, not just arithmetic shape, for every advertised
                # irreversible reward/cost in this event.
                if _event_token(record) == "mindbloom":
                    pricing_coverage = coverage[
                        "mind_bloom_value_component_pricing"
                    ]
                    incomplete = []
                    for row in rows:
                        consequences = row.get("consequences")
                        if not isinstance(consequences, dict):
                            continue
                        requirements = []
                        if (
                            _safe_nonnegative_int(
                                consequences.get("rare_relic_delta")
                            ) > 0
                        ):
                            requirements = [
                                ("rare_relic_reward", ("rare", "relic")),
                                ("boss_combat_gold", ("gold",)),
                                ("current_hp_risk", ("low", "hp")),
                                ("deck_strength_risk", ("weak", "deck")),
                            ]
                        elif (
                            _safe_nonnegative_int(
                                consequences.get("upgraded_card_delta")
                            ) > 0
                            or consequences.get("healing_locked") is True
                        ):
                            requirements = [
                                ("upgrade_package", ("upgrade",)),
                                ("healing_lock_cost", ("healing", "lock")),
                            ]
                        elif (
                            _safe_nonnegative_int(
                                consequences.get("gold_delta")
                            ) >= 999
                        ):
                            requirements = [
                                ("gold_reward", ("gold",)),
                                ("curse_cost", ("curse",)),
                            ]
                        elif (
                            _safe_nonnegative_int(
                                consequences.get("hp_delta")
                            ) > 0
                            and any(
                                "curse" in str(key).casefold()
                                for key in consequences
                            )
                        ):
                            requirements = [
                                ("healing_reward", ("heal",)),
                                ("curse_cost", ("curse",)),
                            ]
                        if not requirements:
                            continue
                        pricing_coverage["eligible"] += 1
                        missing = _missing_score_concepts(row, requirements)
                        if missing:
                            pricing_coverage["violations"] += 1
                            incomplete.append({
                                "candidate_id": row.get("candidate_id"),
                                "missing_score_concepts": missing,
                                "score_rule_id": row.get("score_rule_id"),
                                "score_inputs": row.get("score_inputs"),
                                "consequences": consequences,
                            })
                        else:
                            pricing_coverage["evaluated"] += 1
                    if incomplete:
                        kind = "mind_bloom_value_components_incomplete"
                        issues.append(_issue(
                            record,
                            kind,
                            candidates=incomplete,
                        ))
                        reasons[kind] += 1

            if high_impact:
                mausoleum_evidence = _mausoleum_trade_evidence(record)
                losses = _high_value_resource_losses(
                    record,
                    (
                        selected_row.get("consequences")
                        if isinstance(selected_row, dict) else None
                    ),
                    mausoleum_evidence=mausoleum_evidence,
                )
                if losses:
                    rationale_complete = (
                        _default_or_unknown_reason(record) is None
                    )
                    resolution = _resource_loss_resolution_evidence(
                        record,
                        selected_row,
                        candidate_evidence_complete=(
                            candidate_evidence_complete
                        ),
                        selected_is_argmax=selected_is_argmax,
                        rationale_complete=rationale_complete,
                        deferred_resolution=(
                            _mausoleum_trade_resolution(
                                record, mausoleum_evidence
                            )
                            or _golden_idol_cost_resolution(
                                cohort, cohort_index, record
                            )
                            or _mind_bloom_upgrade_package_resolution(record)
                            or _shining_light_upgrade_package_resolution(record)
                            or _ghosts_card_package_resolution(record)
                            or _beggar_remove_deferred_resolution(
                                cohort, cohort_index, record
                            )
                            or _addict_trade_resolution(record)
                            or _vampires_bite_package_resolution(record)
                            or _neow_deck_change_deferred_resolution(
                                cohort, cohort_index, record
                            )
                            or _neow_card_reward_deferred_resolution(
                                cohort, cohort_index, record
                            )
                            or _neow_relic_reward_resolution(record)
                            or _positive_resource_gain_resolution(
                                cohort, cohort_index, record, selected_row
                            )
                            or _designer_full_service_deferred_resolution(
                                cohort, cohort_index, record
                            )
                        ),
                    )
                    details = {
                        "phase": phase,
                        "chosen_candidate_id": (
                            selected_row.get("candidate_id")
                            if isinstance(selected_row, dict) else None
                        ),
                        "losses": losses,
                        **resolution,
                    }
                    if resolution["resolved"]:
                        review_resolutions.append(_issue(
                            record,
                            "high_value_resource_loss_resolved",
                            **details,
                        ))
                    else:
                        deferred = resolution.get("deferred_resolution")
                        kind = (
                            "costly_followup_reward_skipped"
                            if isinstance(deferred, dict)
                            and deferred.get("status")
                            == "realized_no_benefit"
                            else "high_value_resource_loss_choice"
                        )
                        review_findings.append(_issue(
                            record, kind, **details,
                        ))
                        review_reasons[kind] += 1

        if combat_id and combat_key in pending_true_combat_end:
            assertion = pending_true_combat_end[combat_key]
            living = [
                monster for monster in record.get("monsters_before") or []
                if isinstance(monster, dict)
                and _safe_nonnegative_int(monster.get("current_hp")) > 0
                and not monster.get("is_gone")
                and not monster.get("half_dead")
            ]
            assertion_turn = _int_or_none(assertion.get("turn"))
            current_turn = _int_or_none(record.get("turn"))
            # ``true_combat_end`` describes the complete searched line,
            # including deterministic end-turn damage such as Combust,
            # poison and Lightning.  The controller must still issue END to
            # resolve those effects, so a living monster on that same END
            # frame is expected rather than contradictory.  A later combat
            # turn with a living monster is the first authoritative proof
            # that the predicted terminal line failed.
            contradicted = (
                assertion_turn is not None
                and current_turn is not None
                and current_turn > assertion_turn
            )
            if living and contradicted:
                issues.append(_issue(
                    assertion,
                    "false_combat_end_prediction",
                    contradicted_before_seq=record.get("before_seq"),
                    living_enemy_ids=[
                        monster.get("id") for monster in living
                    ],
                ))
                reasons["false_combat_end_prediction"] += 1
                pending_true_combat_end.pop(combat_key, None)
            elif not living:
                pending_true_combat_end.pop(combat_key, None)

        if phase != "HAND_SELECT" and optional_hand_selection is not None:
            initial = optional_hand_selection["initial"]
            selected = optional_hand_selection["selected"]
            protected = optional_hand_selection["protected"]
            if initial and initial.issubset(selected) and protected:
                source = optional_hand_selection["last_record"]
                issues.append(_issue(
                    source,
                    "optional_hand_overselection",
                    selected_count=len(selected),
                    initial_count=len(initial),
                    protected_cards=sorted(protected),
                ))
                reasons["optional_hand_overselection"] += 1
            optional_hand_selection = None

        if phase == "HAND_SELECT":
            hand_decision = record.get("decision") or {}
            selection_action = _normalized_id(
                hand_decision.get("selection_action")
            )
            optional_gambling = (
                "gambl" in selection_action
                and str(hand_decision.get("selection_semantics") or "")
                in {"remove", "remove_up_to_count"}
            )
            if optional_gambling:
                available = record.get("available_options_before") or []
                cards = [
                    (option.get("target") or {}).get("card") or {}
                    for option in available if isinstance(option, dict)
                ]
                available_ids = {
                    card.get("card_instance_id")
                    for card in cards if card.get("card_instance_id")
                }
                if optional_hand_selection is None:
                    optional_hand_selection = {
                        "initial": set(available_ids),
                        "selected": set(),
                        "protected": {
                            str(card.get("id"))
                            for card in cards
                            if (
                                _normalized_id(card.get("id"))
                                in _DIRECT_DRAW_CARD_IDS
                                or str(card.get("type") or "").upper()
                                == "POWER"
                                or _safe_nonnegative_int(card.get("block")) >= 8
                            )
                        },
                        "last_record": record,
                    }
                chosen = (record.get("chosen_option_before") or {}).get(
                    "target"
                ) or {}
                chosen_id = (chosen.get("card") or {}).get(
                    "card_instance_id"
                )
                if chosen_id:
                    optional_hand_selection["selected"].add(chosen_id)
                optional_hand_selection["last_record"] = record

            forced_exhaust_coverage = coverage[
                "forced_damage_source_exhaustion"
            ]
            chosen_target = (
                (record.get("chosen_option_before") or {}).get("target")
                or {}
            )
            chosen_card = chosen_target.get("card") or {}
            available_cards = [
                ((option.get("target") or {}).get("card") or {})
                for option in (record.get("available_options_before") or [])
                if isinstance(option, dict)
            ]
            state_before = record.get("authoritative_state_before") or {}
            game_state = state_before.get("game_state") or {}
            combat_state = game_state.get("combat_state") or {}
            current_action = _normalized_id(game_state.get("current_action"))
            if (
                action == "choose"
                and current_action == "exhaustaction"
                and str(hand_decision.get("selection_semantics") or "")
                == "remove"
                and str(chosen_card.get("type") or "").upper() == "ATTACK"
                and available_cards
                and all(
                    str(card.get("type") or "").upper() == "ATTACK"
                    for card in available_cards
                )
            ):
                forced_exhaust_coverage["eligible"] += 1
                attack_sources = {}
                for pile_name in (
                    "hand", "draw_pile", "discard_pile", "limbo",
                ):
                    for card in combat_state.get(pile_name) or []:
                        if not isinstance(card, dict) or str(
                            card.get("type") or ""
                        ).upper() != "ATTACK":
                            continue
                        identity = str(
                            card.get("card_instance_id") or ""
                        ).strip()
                        if identity:
                            attack_sources[identity] = card
                monsters = [
                    monster
                    for monster in combat_state.get("monsters") or []
                    if isinstance(monster, dict)
                    and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    and not monster.get("is_gone")
                    and not monster.get("half_dead")
                ]
                persistent_barriers = []
                for monster in monsters:
                    powers = monster.get("powers") or []
                    power_amounts = {
                        _normalized_id(power.get("id") or power.get("name")):
                        _safe_nonnegative_int(power.get("amount"))
                        for power in powers if isinstance(power, dict)
                    }
                    plated = max(
                        power_amounts.get("platedarmor", 0),
                        power_amounts.get("platedarmorpower", 0),
                    )
                    regenerate = max(
                        power_amounts.get("regenerate", 0),
                        power_amounts.get("regeneration", 0),
                        power_amounts.get("regeneratepower", 0),
                    )
                    barricade = any(
                        power_amounts.get(power_id, 0) > 0
                        for power_id in {"barricade", "barricadepower"}
                    )
                    if plated > 0 or regenerate > 0 or barricade:
                        persistent_barriers.append({
                            "enemy_instance_id": monster.get(
                                "enemy_instance_id"
                            ),
                            "id": monster.get("id"),
                            "block": _safe_nonnegative_int(
                                monster.get("block")
                            ),
                            "plated_armor": plated,
                            "regenerate": regenerate,
                            "barricade": barricade,
                        })
                forced_exhaust_coverage["evaluated"] += 1
                previous = (
                    cohort[cohort_index - 1] if cohort_index > 0 else {}
                )
                previous_loss = _int_or_none(
                    previous.get("projected_hp_loss_before")
                )
                previous_hp = _int_or_none(previous.get("hp_before"))
                selected_uuid = str(
                    chosen_card.get("card_instance_id") or ""
                ).strip()
                remaining_attacks = [
                    card for identity, card in attack_sources.items()
                    if identity != selected_uuid
                ]
                remaining_damage = max(
                    (
                        _safe_nonnegative_int(
                            card.get("damage", card.get("base_damage"))
                        )
                        for card in remaining_attacks
                    ),
                    default=0,
                )
                barrier = max(
                    (
                        max(
                            item["block"], item["plated_armor"]
                        )
                        for item in persistent_barriers
                    ),
                    default=0,
                )
                immediate_survival_required = (
                    previous_loss is not None
                    and previous_hp is not None
                    and previous_loss >= previous_hp
                )
                if (
                    len(attack_sources) <= 2
                    and len(remaining_attacks) <= 1
                    and persistent_barriers
                    and remaining_damage <= barrier
                    and not immediate_survival_required
                ):
                    issues.append(_issue(
                        record,
                        "forced_damage_source_exhaustion",
                        selected_card_id=chosen_card.get("id"),
                        selected_card_instance_id=selected_uuid,
                        attack_source_count=len(attack_sources),
                        remaining_attack_count=len(remaining_attacks),
                        remaining_max_attack_damage=remaining_damage,
                        persistent_barrier=barrier,
                        persistent_defenders=persistent_barriers,
                    ))
                    reasons["forced_damage_source_exhaustion"] += 1
                    forced_exhaust_coverage["violations"] += 1

        combat_turn_phase = phase.startswith("COMBAT_TURN_")
        combat_action = action in {"play", "potion", "end"} and (
            combat_turn_phase
            or "projected_hp_loss_before" in record
        )
        if combat_action:
            context_coverage = coverage["combat_decision_context"]
            context_coverage["eligible"] += 1
            trace_schema = _int_or_none(record.get("trace_schema_version"))
            turn = _int_or_none(record.get("turn"))
            combat_id = record.get("combat_id")
            hand = record.get("hand_before")
            hand_complete = isinstance(hand, list) and all(
                isinstance(card, dict)
                and {
                    "card_instance_id", "id", "type", "cost",
                    "is_playable",
                }.issubset(card)
                for card in hand
            )
            action_complete = (
                action == "end"
                or action == "play"
                and isinstance(record.get("card_instance_id"), str)
                and bool(record.get("card_instance_id"))
                and isinstance(record.get("card_id"), str)
                and bool(record.get("card_id"))
                or action == "potion"
                and isinstance(record.get("potion_instance_id"), str)
                and bool(record.get("potion_instance_id"))
                and record.get("potion_operation") in {"use", "discard"}
            )
            capabilities = set(record.get("trace_capabilities") or [])
            player_state = record.get("player_before")
            player_complete = (
                isinstance(player_state, dict)
                and all(
                    _int_or_none(player_state.get(field)) is not None
                    for field in ("current_hp", "max_hp", "block", "energy")
                )
                and isinstance(player_state.get("powers"), list)
                and isinstance(player_state.get("orbs"), list)
            )
            monsters_state = record.get("monsters_before")
            monsters_complete = (
                isinstance(monsters_state, list)
                and bool(monsters_state)
                and all(
                    isinstance(monster, dict)
                    and {
                        "enemy_instance_id", "id", "monster_index",
                        "current_hp", "max_hp", "block", "intent",
                        "move_adjusted_damage", "move_hits", "is_gone",
                        "half_dead", "powers",
                    }.issubset(monster)
                    and isinstance(monster.get("powers"), list)
                    for monster in monsters_state
                )
            )
            potions_state = record.get("potions_before")
            potions_complete = (
                isinstance(potions_state, list)
                and all(
                    isinstance(potion, dict)
                    and {
                        "potion_instance_id", "id", "can_use",
                        "requires_target",
                    }.issubset(potion)
                    for potion in potions_state
                )
            )
            expected_oracle_version = (
                getattr(_independent_oracle, "ORACLE_VERSION", None)
                if _independent_oracle is not None else None
            )
            oracle_version_valid = bool(
                isinstance(expected_oracle_version, str)
                and expected_oracle_version
                and record.get("audit_oracle_version")
                == expected_oracle_version
            )
            if not oracle_version_valid:
                issues.append(_issue(
                    record,
                    "combat_audit_oracle_version_mismatch",
                    expected=expected_oracle_version,
                    observed=record.get("audit_oracle_version"),
                ))
                reasons["combat_audit_oracle_version_mismatch"] += 1
                context_coverage["violations"] += 1
            context_complete = (
                trace_schema is not None
                and trace_schema >= 3
                and turn is not None
                and isinstance(combat_id, str)
                and bool(combat_id.strip())
                and _int_or_none(record.get("before_seq")) is not None
                and _int_or_none(record.get("hp_before")) is not None
                and _int_or_none(record.get("energy_before")) is not None
                and _int_or_none(
                    record.get("projected_hp_loss_before")
                ) is not None
                and _int_or_none(
                    record.get("projected_attack_hp_loss_before")
                ) is not None
                and _int_or_none(
                    record.get("projected_end_turn_hp_loss_before")
                ) is not None
                and (
                    "next_turn_start_hp_loss" not in capabilities
                    or _int_or_none(
                        record.get(
                            "projected_next_turn_start_hp_loss_before"
                        )
                    ) is not None
                )
                and _int_or_none(
                    record.get("projected_block_before")
                ) is not None
                and hand_complete
                and action_complete
                and player_complete
                and monsters_complete
                and potions_complete
                and isinstance(record.get("relic_ids_before"), list)
                and oracle_version_valid
                and {
                    "ordered_hand", "player_state", "monster_state",
                    "potions",
                }.issubset(capabilities)
            )
            if context_complete:
                context_coverage["evaluated"] += 1
                scope = (
                    record.get("attempt_id"), record.get("run_id"),
                    record.get("act"), record.get("floor"),
                )
                if (
                    last_combat_context is not None
                    and last_combat_context["scope"] == scope
                ):
                    if last_combat_context["combat_id"] != combat_id:
                        issues.append(_issue(
                            record,
                            "combat_trace_identity_changed",
                            previous_combat_id=last_combat_context[
                                "combat_id"
                            ],
                            combat_id=combat_id,
                        ))
                        reasons["combat_trace_identity_changed"] += 1
                        context_coverage["violations"] += 1
                    # Some combat frames can transiently reset the serialized
                    # turn counter while the same player turn is still being
                    # executed.  A consecutive non-END action with no energy
                    # refill is authoritative evidence of that continuity;
                    # treating it as a new turn creates a false trace error.
                    energy_before = _int_or_none(record.get("energy_before"))
                    same_player_turn = (
                        last_combat_context.get("action") != "end"
                        and energy_before is not None
                        and last_combat_context.get("energy_before") is not None
                        and energy_before
                        <= last_combat_context["energy_before"]
                    )
                    if (
                        turn < last_combat_context["turn"]
                        and not same_player_turn
                    ):
                        issues.append(_issue(
                            record,
                            "combat_trace_turn_regressed",
                            previous_turn=last_combat_context["turn"],
                            turn=turn,
                        ))
                        reasons["combat_trace_turn_regressed"] += 1
                        context_coverage["violations"] += 1
                last_combat_context = {
                    "attempt_run_key": attempt_run_key,
                    "scope": scope,
                    "combat_id": combat_id,
                    "turn": turn,
                    "action": action,
                    "energy_before": _int_or_none(
                        record.get("energy_before")
                    ),
                }

            decision_search = decision_payload.get("search") or {}
            # Smoke Bomb is a zero-card escape from ordinary and Elite
            # encounters.  Audit the selected line, not just raw incoming
            # damage: a legal bottle is only a missed survival out when the
            # planner's own complete line still loses at least current HP.
            # Boss and unknown room types stay fail-closed.
            room_type = str(record.get("room_type_before") or "")
            if not room_type:
                authoritative = record.get(
                    "authoritative_state_before"
                ) or {}
                room_type = str(
                    ((authoritative.get("game_state") or {}).get(
                        "room_type"
                    ))
                    or ""
                )
            smoke_potions = [
                potion for potion in record.get("potions_before") or []
                if isinstance(potion, dict)
                and _normalized_id(potion.get("id")) == "smokebomb"
                and potion.get("can_use") is True
            ]
            exact_plan_loss = _int_or_none(
                decision_search.get("actual_loss")
            )
            if exact_plan_loss is None:
                exact_plan_loss = _int_or_none(
                    decision_search.get("projected_loss")
                )
            planned_loss = max(
                0,
                exact_plan_loss
                if exact_plan_loss is not None
                else _int_or_none(
                    record.get("projected_hp_loss_before")
                ) or 0,
            )
            hp_before = max(
                0,
                _int_or_none(record.get("hp_before"))
                or _int_or_none(
                    (record.get("player_before") or {}).get("current_hp")
                )
                or 0,
            )
            selected_smoke = bool(
                action == "potion"
                and record.get("potion_operation") == "use"
                and any(
                    potion.get("potion_instance_id")
                    == record.get("potion_instance_id")
                    for potion in smoke_potions
                )
            )
            smoke_turn_key = (
                record.get("attempt_id"), record.get("run_id"), combat_id,
                turn,
            )
            if (
                smoke_potions
                and _normalized_id(room_type)
                in {"monsterroom", "monsterroomelite"}
                and hp_before > 0
                and planned_loss >= hp_before
                and not decision_search.get("true_combat_end")
            ):
                smoke_coverage = coverage["smoke_bomb_escape"]
                smoke_coverage["eligible"] += 1
                smoke_coverage["evaluated"] += 1
                if (
                    not selected_smoke
                    and action != "potion"
                    and smoke_turn_key not in reported_smoke_bomb_turns
                ):
                    issues.append(_issue(
                        record,
                        "avoidable_smoke_bomb_omission",
                        room_type=room_type,
                        hp_before=hp_before,
                        planned_loss=planned_loss,
                        potion_instance_ids=[
                            potion.get("potion_instance_id")
                            for potion in smoke_potions
                        ],
                    ))
                    reasons["avoidable_smoke_bomb_omission"] += 1
                    smoke_coverage["violations"] += 1
                    reported_smoke_bomb_turns.add(smoke_turn_key)
            # A present zero is authoritative.  ``card_self_hp_cost`` also
            # includes damage caused by reactive enemy powers (for example
            # Guardian's Sharp Hide), so falling back through ``A or B``
            # incorrectly classified a modeled zero voluntary cost as a
            # missing field and demanded an Offering/Hemokinesis dominance
            # comparison for an ordinary attack.  Only legacy traces which
            # genuinely lack the separated search field use the aggregate.
            if "voluntary_self_hp_cost" in decision_search:
                voluntary_self_cost = max(
                    0,
                    _int_or_none(
                        decision_search.get("voluntary_self_hp_cost")
                    )
                    or 0,
                )
            else:
                voluntary_self_cost = max(
                    0,
                    _int_or_none(
                        decision_payload.get("card_self_hp_cost")
                    )
                    or 0,
                )
            if voluntary_self_cost > 0:
                self_damage_coverage = coverage[
                    "voluntary_self_damage_dominance"
                ]
                self_damage_coverage["eligible"] += 1
                comparison = decision_search.get(
                    "voluntary_self_damage_comparison"
                )
                if (
                    isinstance(comparison, dict)
                    and comparison.get("evaluated") is True
                    and comparison.get("status")
                    in {
                        "selected_with_material_advantage",
                        "rejected_dominated_plan",
                        "selected_dominated_plan",
                    }
                ):
                    self_damage_coverage["evaluated"] += 1
                    # ``rejected_dominated_plan`` is evidence that the
                    # planner found the bad self-damage line and did *not*
                    # execute it.  Only a dominated line that remained the
                    # selected plan is a strategy violation.
                    if (
                        comparison.get("dominated") is True
                        and comparison.get("status")
                        == "selected_dominated_plan"
                    ):
                        issues.append(_issue(
                            record,
                            "avoidable_voluntary_self_damage",
                            voluntary_self_hp_cost=voluntary_self_cost,
                            comparison=comparison,
                        ))
                        reasons[
                            "avoidable_voluntary_self_damage"
                        ] += 1
                        self_damage_coverage["violations"] += 1

            no_positive_end = (
                action == "end"
                and str(decision_payload.get("reason") or "")
                == "no_positive_marginal_action"
                and max(
                    0,
                    _int_or_none(record.get("projected_hp_loss_before"))
                    or 0,
                ) == 0
            )
            if no_positive_end:
                long_setup_ids = {
                    "demonform", "echoform", "noxiousfumes", "footwork",
                }
                energy = max(
                    0, _int_or_none(record.get("energy_before")) or 0
                )
                setup_cards = [
                    card for card in record.get("hand_before") or []
                    if isinstance(card, dict)
                    and str(card.get("type") or "").upper() == "POWER"
                    and _normalized_id(card.get("id")) in long_setup_ids
                    and card.get("is_playable") is True
                    and (
                        (_int_or_none(card.get("cost")) or 0) < 0
                        or max(0, _int_or_none(card.get("cost")) or 0)
                        <= energy
                    )
                ]
                living = [
                    monster for monster in record.get("monsters_before") or []
                    if isinstance(monster, dict)
                    and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    and not monster.get("is_gone")
                    and not monster.get("half_dead")
                ]
                effective_enemy_hp = sum(
                    _safe_nonnegative_int(monster.get("current_hp"))
                    + _safe_nonnegative_int(monster.get("block"))
                    for monster in living
                )
                awakened = any(
                    _normalized_id(monster.get("id")) == "awakenedone"
                    for monster in living
                )
                time_warp_near_limit = any(
                    _normalized_id(monster.get("id")) == "timeeater"
                    and any(
                        _normalized_id(power.get("id") or power.get("name"))
                        in {"timewarp", "timewarppower"}
                        and _safe_nonnegative_int(power.get("amount")) >= 11
                        for power in monster.get("powers") or []
                        if isinstance(power, dict)
                    )
                    for monster in living
                )
                if (
                    setup_cards
                    and effective_enemy_hp >= 80
                    and not awakened
                    and not time_warp_near_limit
                ):
                    setup_coverage = coverage["long_fight_scaling_setup"]
                    setup_coverage["eligible"] += 1
                    zones = []
                    for zone_name in (
                        "hand_before", "draw_pile_before",
                        "discard_pile_before",
                    ):
                        zone = record.get(zone_name)
                        if isinstance(zone, list):
                            zones.extend(
                                card for card in zone
                                if isinstance(card, dict)
                            )
                    observable = bool(zones and living)
                    if observable:
                        setup_coverage["evaluated"] += 1
                        actionable = []
                        for setup in setup_cards:
                            setup_id = _normalized_id(setup.get("id"))
                            if setup_id == "demonform":
                                has_consumer = any(
                                    str(card.get("type") or "").upper()
                                    == "ATTACK"
                                    and max(
                                        _safe_nonnegative_int(
                                            card.get("damage")
                                        ),
                                        _safe_nonnegative_int(
                                            card.get("base_damage")
                                        ),
                                    ) > 0
                                    for card in zones
                                )
                                if not has_consumer:
                                    continue
                            actionable.append(setup.get("id"))
                        if actionable:
                            issues.append(_issue(
                                record,
                                "missed_long_fight_scaling_setup",
                                setup_cards=actionable,
                                energy=energy,
                                effective_enemy_hp=effective_enemy_hp,
                                rejected_lifecycle=decision_payload.get(
                                    "rejected_lifecycle"
                                ),
                            ))
                            reasons[
                                "missed_long_fight_scaling_setup"
                            ] += 1
                            setup_coverage["violations"] += 1
                            reported_long_setup_turns.add((
                                record.get("attempt_id"),
                                record.get("run_id"),
                                combat_id,
                                _int_or_none(record.get("turn")),
                            ))

            turn_key = (
                record.get("attempt_id"), record.get("run_id"), combat_id,
                _int_or_none(record.get("turn")),
            )
            turn_context = combat_turn_contexts.setdefault(turn_key, {
                "initial_hand": list(record.get("hand_before") or []),
                "initial_draw_pile": list(
                    record.get("draw_pile_before") or []
                ),
                "initial_discard_pile": list(
                    record.get("discard_pile_before") or []
                ),
                "initial_monsters": list(
                    record.get("monsters_before") or []
                ),
                "initial_before_seq": record.get("before_seq"),
                "initial_energy": _int_or_none(record.get("energy_before")),
                "initial_projected_loss": _int_or_none(
                    record.get("projected_hp_loss_before")
                ),
                "played": [],
                "seen_affordable_attacks": [],
                "attack_hp_loss": 0,
            })
            # Keep a turn-level opportunity ledger.  The old audit inspected
            # only the current END frame; that loses attacks which were
            # playable before Velvet Choker exhausted the six-resolution
            # budget.  We retain only the small, typed lower-bound evidence
            # needed by the Choker/setup checks below.
            seen_attacks = _record_affordable_attack_candidates(record)
            if seen_attacks:
                known_attack_keys = {
                    (item.get("card_instance_id"), item.get("id"))
                    for item in turn_context["seen_affordable_attacks"]
                    if isinstance(item, dict)
                }
                for candidate in seen_attacks:
                    key = (
                        candidate.get("card_instance_id"),
                        candidate.get("id"),
                    )
                    active_blocks = [
                        _safe_nonnegative_int(monster.get("block"))
                        for monster in record.get("monsters_before") or []
                        if isinstance(monster, dict)
                        and not monster.get("is_gone")
                        and not monster.get("half_dead")
                        and _safe_nonnegative_int(
                            monster.get("current_hp")
                        ) > 0
                    ]
                    current_block = min(active_blocks) if active_blocks else 0
                    existing = next(
                        (
                            item for item in turn_context[
                                "seen_affordable_attacks"
                            ]
                            if isinstance(item, dict)
                            and (
                                item.get("card_instance_id"),
                                item.get("id"),
                            ) == key
                        ),
                        None,
                    )
                    if existing is not None:
                        # A card can become a real HP-progress option after
                        # another action strips enemy Block.  Do not freeze
                        # the first (higher) Block observation forever.
                        if current_block < int(
                            existing.get("enemy_block_before") or 0
                        ):
                            existing["enemy_block_before"] = current_block
                            existing["before_seq"] = record.get(
                                "before_seq"
                            )
                        existing["damage"] = max(
                            int(existing.get("damage") or 0),
                            int(candidate.get("damage") or 0),
                        )
                        continue
                    if key not in known_attack_keys:
                        turn_context["seen_affordable_attacks"].append({
                            **candidate,
                            "before_seq": record.get("before_seq"),
                            "enemy_block_before": current_block,
                        })
                        known_attack_keys.add(key)
            if action == "play":
                played_card = next((
                    card for card in record.get("hand_before") or []
                    if isinstance(card, dict)
                    and card.get("card_instance_id")
                    == record.get("card_instance_id")
                ), None)
                played_id = _normalized_id(record.get("card_id"))
                if played_id == "doubletap":
                    double_tap_coverage = coverage[
                        "unconsumed_double_tap_setup"
                    ]
                    double_tap_coverage["eligible"] += 1
                    turn_index = turn_play_index.get(turn_key, [])
                    downstream_attack = any(
                        entry.get("index", cohort_index) > cohort_index
                        and entry.get("card_type") == "ATTACK"
                        for entry in turn_index
                    )
                    if turn_key in turn_play_index:
                        double_tap_coverage["evaluated"] += 1
                    played_cost = (
                        _int_or_none(played_card.get("cost"))
                        if isinstance(played_card, dict) else None
                    )
                    remaining_energy = max(
                        0,
                        (_int_or_none(record.get("energy_before")) or 0)
                        - max(0, played_cost or 0),
                    )
                    follow_up_attacks = _record_affordable_attack_candidates(
                        record, remaining_energy
                    )
                    if not downstream_attack:
                        if (
                            follow_up_attacks
                            and _record_attack_can_cross_current_block(
                                record, follow_up_attacks
                            )
                            and max(
                                0,
                                _int_or_none(
                                    record.get("projected_hp_loss_before")
                                ) or 0,
                            ) == 0
                            and not _record_enemy_reaction_hazard(record)
                        ):
                            issues.append(_issue(
                                record,
                                "unconsumed_double_tap_setup",
                                card_id=record.get("card_id"),
                                follow_up_attacks=follow_up_attacks,
                                remaining_energy=remaining_energy,
                            ))
                            reasons["unconsumed_double_tap_setup"] += 1
                choker_coverage = coverage["offering_at_choker_limit"]
                if (
                    played_id == "offering"
                    and "velvetchoker" in {
                        _normalized_id(relic_id)
                        for relic_id in (record.get("relic_ids_before") or [])
                    }
                    and len(turn_context["played"]) >= 5
                ):
                    choker_coverage["eligible"] += 1
                    turn_index = turn_play_index.get(turn_key, [])
                    later_card_play = any(
                        entry.get("index", cohort_index) > cohort_index
                        for entry in turn_index
                    )
                    if not later_card_play:
                        choker_coverage["evaluated"] += 1
                        search = (record.get("decision") or {}).get("search") or {}
                        voluntary_cost = max(
                            0,
                            _int_or_none(
                                search.get("voluntary_self_hp_cost")
                            ) or _int_or_none(
                                (record.get("decision") or {}).get(
                                    "card_self_hp_cost"
                                )
                            ) or 0,
                        )
                        issues.append(_issue(
                            record,
                            "offering_at_choker_limit",
                            card_id=record.get("card_id"),
                            prior_card_count=len(turn_context["played"]),
                            voluntary_self_hp_cost=voluntary_cost,
                        ))
                        reasons["offering_at_choker_limit"] += 1
                if (
                    played_id in _DIRECT_DRAW_CARD_IDS
                    and len(turn_context["played"]) >= 2
                    and any(
                        isinstance(card, dict)
                        and _normalized_id(card.get("id")) == "normality"
                        for card in record.get("hand_before") or []
                    )
                    and _safe_nonnegative_int(
                        played_card.get("block")
                        if isinstance(played_card, dict) else 0
                    ) == 0
                    and _safe_nonnegative_int(
                        (record.get("decision_outcome") or {}).get(
                            "enemy_hp_loss"
                        )
                    ) == 0
                ):
                    issues.append(_issue(
                        record,
                        "normality_draw_at_card_limit",
                        card_id=record.get("card_id"),
                        card_number=len(turn_context["played"]) + 1,
                        prior_cards=list(turn_context["played"]),
                    ))
                    reasons["normality_draw_at_card_limit"] += 1
                unload_coverage = coverage["invalid_post_unload_plan"]
                if played_id == "unload":
                    unload_coverage["eligible"] += 1
                    planned = (record.get("decision") or {}).get(
                        "planned_sequence"
                    )
                    hand_types = {
                        _normalized_id(card.get("id")): str(
                            card.get("type") or ""
                        ).upper()
                        for card in (record.get("hand_before") or [])
                        if isinstance(card, dict)
                    }
                    if isinstance(planned, list):
                        unload_coverage["evaluated"] += 1
                        invalid_suffix = [
                            step.get("card_id")
                            for step in planned[1:]
                            if isinstance(step, dict)
                            and hand_types.get(
                                _normalized_id(step.get("card_id"))
                            ) not in {None, "ATTACK"}
                        ]
                        if invalid_suffix:
                            issues.append(_issue(
                                record,
                                "invalid_post_unload_plan",
                                cards=invalid_suffix,
                            ))
                            reasons["invalid_post_unload_plan"] += 1

                forced_plan_coverage = coverage[
                    "impossible_post_forced_exhaust_plan"
                ]
                if played_id == "truegrit":
                    hand_before = [
                        card for card in (record.get("hand_before") or [])
                        if isinstance(card, dict)
                    ]
                    other_cards = [
                        card for card in hand_before
                        if card.get("card_instance_id")
                        != record.get("card_instance_id")
                    ]
                    planned = (record.get("decision") or {}).get(
                        "planned_sequence"
                    )
                    if len(other_cards) == 1 and isinstance(planned, list):
                        forced_plan_coverage["eligible"] += 1
                        forced_plan_coverage["evaluated"] += 1
                        exhausted_uuid = other_cards[0].get(
                            "card_instance_id"
                        )
                        invalid_suffix = [
                            step for step in planned[1:]
                            if isinstance(step, dict)
                            and step.get("card_uuid") == exhausted_uuid
                        ]
                        if invalid_suffix:
                            issues.append(_issue(
                                record,
                                "impossible_post_forced_exhaust_plan",
                                exhausted_card_id=other_cards[0].get("id"),
                                exhausted_card_instance_id=exhausted_uuid,
                                planned_suffix=invalid_suffix,
                            ))
                            reasons[
                                "impossible_post_forced_exhaust_plan"
                            ] += 1
                            forced_plan_coverage["violations"] += 1

                setup_coverage = coverage["late_trigger_power_setup"]
                setup_prior = []
                if played_id == "athousandcuts":
                    setup_prior = [
                        card_id
                        for card_id, card_type in turn_context.get(
                            "played_with_type", []
                        )
                        if card_type == "ATTACK"
                    ]
                elif played_id == "accuracy":
                    setup_prior = [
                        card_id for card_id in turn_context["played"]
                        if card_id == "shiv"
                    ]
                elif (
                    played_id == "afterimage"
                    and max(
                        0, turn_context.get("initial_projected_loss") or 0
                    ) > 0
                ):
                    setup_prior = [
                        card_id
                        for card_id, card_type in turn_context.get(
                            "played_with_type", []
                        )
                        if card_type == "ATTACK"
                    ]
                if setup_prior:
                    setup_coverage["eligible"] += 1
                    initial_ids = {
                        card.get("card_instance_id")
                        for card in turn_context["initial_hand"]
                        if isinstance(card, dict)
                    }
                    prior_cost = sum(
                        cost
                        for _card_id, cost in turn_context.get(
                            "played_with_cost", []
                        )
                    )
                    current_cost = max(
                        0,
                        _int_or_none(
                            played_card.get("cost")
                            if isinstance(played_card, dict) else 0
                        ) or 0,
                    )
                    initially_affordable = (
                        record.get("card_instance_id") in initial_ids
                        and turn_context.get("initial_energy") is not None
                        and prior_cost + current_cost
                        <= turn_context["initial_energy"]
                    )
                    setup_coverage["evaluated"] += 1
                    if initially_affordable:
                        issues.append(_issue(
                            record,
                            "late_trigger_power_setup",
                            card_id=record.get("card_id"),
                            prior_cards=setup_prior,
                        ))
                        reasons["late_trigger_power_setup"] += 1
                if played_id == "battletrance":
                    initial_ids = {
                        card.get("card_instance_id")
                        for card in turn_context["initial_hand"]
                        if isinstance(card, dict)
                    }
                    earlier_draw = any(
                        card_id in _DIRECT_DRAW_CARD_IDS
                        for card_id in turn_context["played"]
                    )
                    earlier_energy_play = any(
                        cost > 0
                        for _card_id, cost in turn_context.get(
                            "played_with_cost", []
                        )
                    )
                    if (
                        record.get("card_instance_id") in initial_ids
                        and len(turn_context["initial_hand"]) <= 8
                        and (turn_context["initial_energy"] or 0) > 0
                        and (_int_or_none(record.get("energy_before")) or 0)
                        == 0
                        and earlier_energy_play
                        and not earlier_draw
                    ):
                        issues.append(_issue(
                            record,
                            "late_zero_cost_draw",
                            card_id=record.get("card_id"),
                            initial_energy=turn_context["initial_energy"],
                            prior_cards=list(turn_context["played"]),
                        ))
                        reasons["late_zero_cost_draw"] += 1
                if played_id == "rage":
                    initial_ids = {
                        card.get("card_instance_id")
                        for card in turn_context["initial_hand"]
                        if isinstance(card, dict)
                    }
                    earlier_attacks = [
                        card_id
                        for card_id, card_type in turn_context.get(
                            "played_with_type", []
                        )
                        if card_type == "ATTACK"
                    ]
                    turn_index = turn_play_index.get(turn_key, [])
                    downstream_attacks = any(
                        entry.get("index", cohort_index) > cohort_index
                        and entry.get("card_type") == "ATTACK"
                        for entry in turn_index
                    )
                    if (
                        record.get("card_instance_id") in initial_ids
                        and earlier_attacks
                        and not downstream_attacks
                        and max(
                            0,
                            _int_or_none(record.get(
                                "projected_attack_hp_loss_before"
                            )) or 0,
                        ) > 0
                    ):
                        issues.append(_issue(
                            record,
                            "late_attack_setup",
                            card_id=record.get("card_id"),
                            prior_attacks=earlier_attacks,
                        ))
                        reasons["late_attack_setup"] += 1
                if isinstance(played_card, dict):
                    # A basic block card is not a free action.  Against
                    # Chosen, playing a Skill also consumes a Hex stack and
                    # adds Dazed.  If the authoritative frame already has
                    # enough Block to absorb the full attack, an additional
                    # Defend/Deflect is a concrete strategy defect rather
                    # than a merely legal action.  The old audit only checked
                    # END with unused resources, so it missed this redundant
                    # play entirely.
                    pure_block_ids = {
                        "defend", "defendg", "defendb", "defendr", "deflect",
                        # Survivor is pure block when it is played as the
                        # last defensive action.  It still costs energy and
                        # (against Chosen) consumes Hex, so it belongs in the
                        # same covered-attack guard as Defend/Deflect.
                        "survivor",
                    }
                    chosen_alive = any(
                        isinstance(monster, dict)
                        and _normalized_id(monster.get("id")) == "chosen"
                        and _safe_nonnegative_int(monster.get("current_hp")) > 0
                        and not monster.get("is_gone")
                        for monster in record.get("monsters_before") or []
                    )
                    attacking_alive = any(
                        isinstance(monster, dict)
                        and "ATTACK" in str(
                            monster.get("intent") or ""
                        ).upper()
                        and _safe_nonnegative_int(
                            monster.get("current_hp")
                        ) > 0
                        and not monster.get("is_gone")
                        and not monster.get("half_dead")
                        for monster in record.get("monsters_before") or []
                    )
                    hex_active = any(
                        isinstance(power, dict)
                        and _normalized_id(power.get("id") or power.get("name"))
                        in {"hex", "hexpower"}
                        and _safe_nonnegative_int(power.get("amount")) > 0
                        for power in (
                            (record.get("player_before") or {}).get("powers")
                            or []
                        )
                    )
                    incoming_before = sum(
                        max(0, _int_or_none(monster.get("move_adjusted_damage")) or 0)
                        * max(1, _int_or_none(monster.get("move_hits")) or 1)
                        for monster in record.get("monsters_before") or []
                        if isinstance(monster, dict)
                        and "ATTACK" in str(monster.get("intent") or "").upper()
                        and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    )
                    projected_attack_before = max(
                        0,
                        _int_or_none(
                            record.get("projected_attack_hp_loss_before")
                        ) or 0,
                    )
                    projected_total_before = max(
                        0,
                        _int_or_none(record.get("projected_hp_loss_before"))
                        or 0,
                    )
                    projected_block_before = max(
                        0,
                        _int_or_none(record.get("projected_block_before"))
                        or 0,
                    )
                    turn_index = turn_play_index.get(turn_key, [])
                    downstream_attack = any(
                        entry.get("index", cohort_index) > cohort_index
                        and entry.get("card_type") == "ATTACK"
                        for entry in turn_index
                    )
                    slow_setup_value = bool(
                        downstream_attack
                        and any(
                            isinstance(power, dict)
                            and _normalized_id(
                                power.get("id") or power.get("name")
                            ) in {"slow", "slowpower"}
                            and _safe_nonnegative_int(power.get("amount")) > 0
                            for monster in record.get("monsters_before") or []
                            if isinstance(monster, dict)
                            for power in monster.get("powers") or []
                        )
                    )
                    reactive_attack_setup_value = bool(
                        downstream_attack
                        and any(
                            isinstance(power, dict)
                            and _normalized_id(
                                power.get("id") or power.get("name")
                            ) in {"sharphide", "thorns"}
                            and _safe_nonnegative_int(power.get("amount")) > 0
                            for monster in record.get("monsters_before") or []
                            if isinstance(monster, dict)
                            and _safe_nonnegative_int(
                                monster.get("current_hp")
                            ) > 0
                            and not monster.get("is_gone")
                            for power in monster.get("powers") or []
                        )
                    )
                    player_power_ids = {
                        _normalized_id(power.get("id") or power.get("name"))
                        for power in (
                            (record.get("player_before") or {}).get("powers")
                            or []
                        )
                        if isinstance(power, dict)
                        and _int_or_none(power.get("amount")) != 0
                    }
                    live_enemy_ids = {
                        _normalized_id(monster.get("id"))
                        for monster in record.get("monsters_before") or []
                        if isinstance(monster, dict)
                        and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    }
                    choker_at_limit = bool(
                        "velvetchoker" in {
                            _normalized_id(value)
                            for value in record.get("relic_ids_before") or []
                        }
                        and len(turn_context["played"]) >= 5
                    )
                    corruption_cleanup_value = bool(
                        {"corruption", "corruptionpower"} & player_power_ids
                        and str(played_card.get("type") or "").upper()
                        == "SKILL"
                        and _int_or_none(played_card.get("cost")) == 0
                        and not hex_active
                        and not choker_at_limit
                        and not (
                            live_enemy_ids
                            & {"timeeater", "gremlinnob", "corruptheart"}
                        )
                    )
                    letter_opener_trigger_value = bool(
                        str(played_card.get("type") or "").upper() == "SKILL"
                        and any(
                            isinstance(relic, dict)
                            and _normalized_id(
                                relic.get("id") or relic.get("name")
                            ) == "letteropener"
                            and _int_or_none(relic.get("counter")) == 2
                            for relic in record.get("relics_before") or []
                        )
                        and live_enemy_ids
                    )
                    redundant_block_coverage = coverage[
                        "redundant_covered_block_play"
                    ]
                    redundant_eligible = (
                        played_id in pure_block_ids
                        and attacking_alive
                        and not slow_setup_value
                        and not reactive_attack_setup_value
                        and not corruption_cleanup_value
                        and not letter_opener_trigger_value
                        and (
                            incoming_before > 0
                            or (
                                _safe_nonnegative_int(
                                    record.get("player_block_before")
                                ) > 0
                                and projected_attack_before == 0
                            )
                        )
                    )
                    if redundant_eligible:
                        redundant_block_coverage["eligible"] += 1
                        redundant_block_coverage["evaluated"] += 1
                    calipers_gain = 0
                    if "calipers" in {
                        _normalized_id(value)
                        for value in record.get("relic_ids_before") or []
                    }:
                        current_block = _safe_nonnegative_int(
                            record.get(
                                "player_block_before",
                                (record.get("player_before") or {}).get(
                                    "block"
                                ),
                            )
                        )
                        card_block = _safe_nonnegative_int(
                            played_card.get("block")
                        )
                        calipers_gain = max(
                            0,
                            max(0, current_block + card_block - 15)
                            - max(0, current_block - 15),
                        )
                    if (
                        redundant_eligible
                        and projected_attack_before == 0
                        and projected_total_before == 0
                        and projected_block_before >= incoming_before
                        and calipers_gain <= 0
                    ):
                        issues.append(_issue(
                            record,
                            "redundant_covered_block_play",
                            card_id=record.get("card_id"),
                            card_number=len(turn_context["played"]) + 1,
                            turn_card_count=len(
                                turn_play_index.get(turn_key, [])
                            ),
                            incoming_damage=incoming_before,
                            projected_block=projected_block_before,
                            chosen_hex=hex_active,
                            calipers_retained_block_gain=calipers_gain,
                            slow_setup_value=slow_setup_value,
                            reactive_attack_setup_value=(
                                reactive_attack_setup_value
                            ),
                            corruption_cleanup_value=(
                                corruption_cleanup_value
                            ),
                            letter_opener_trigger_value=(
                                letter_opener_trigger_value
                            ),
                        ))
                        reasons["redundant_covered_block_play"] += 1
                    energy = max(
                        0,
                        _int_or_none(record.get("energy_before")) or 0,
                    )
                    search = (
                        (record.get("decision") or {}).get("search") or {}
                    )
                    if (
                        _int_or_none(played_card.get("cost")) == -1
                        and energy == 0
                        and _int_or_none(
                            search.get("first_action_enemy_hp_loss")
                        ) == 0
                        and _safe_nonnegative_int(
                            (record.get("decision_outcome") or {}).get(
                                "enemy_hp_loss"
                            )
                        ) == 0
                    ):
                        issues.append(_issue(
                            record,
                            "zero_energy_x_attack_no_effect",
                            card_id=record.get("card_id"),
                        ))
                        reasons["zero_energy_x_attack_no_effect"] += 1

                    active_nob = next((
                        monster
                        for monster in record.get("monsters_before") or []
                        if isinstance(monster, dict)
                        and _normalized_id(monster.get("id"))
                        == "gremlinnob"
                        and _safe_nonnegative_int(
                            monster.get("current_hp")
                        ) > 0
                        and not monster.get("is_gone")
                    ), None)
                    if (
                        active_nob is not None
                        and str(played_card.get("type") or "").upper()
                        == "SKILL"
                        and _safe_nonnegative_int(played_card.get("block"))
                        > 0
                    ):
                        power_amounts = {
                            _normalized_id(
                                power.get("id") or power.get("name")
                            ): _safe_nonnegative_int(power.get("amount"))
                            for power in active_nob.get("powers") or []
                            if isinstance(power, dict)
                        }
                        enrage = max(
                            power_amounts.get("anger", 0),
                            power_amounts.get("enrage", 0),
                            power_amounts.get("enragepower", 0),
                        )
                        intent = str(active_nob.get("intent") or "").upper()
                        hits = (
                            max(
                                1,
                                _safe_nonnegative_int(
                                    active_nob.get("move_hits")
                                ),
                            )
                            if "ATTACK" in intent else 0
                        )
                        affordable_attack_costs = [
                            (
                                energy
                                if _int_or_none(card.get("cost")) == -1
                                else max(
                                    0,
                                    _int_or_none(card.get("cost")) or 0,
                                )
                            )
                            for card in record.get("hand_before") or []
                            if (
                            isinstance(card, dict)
                            and card.get("card_instance_id")
                            != record.get("card_instance_id")
                            and card.get("is_playable")
                            and str(card.get("type") or "").upper()
                            == "ATTACK"
                            and _safe_nonnegative_int(card.get("damage")) > 0
                            )
                        ]
                        affordable_attack = any(
                            cost <= energy for cost in affordable_attack_costs
                        )
                        hp_before = _int_or_none(record.get("hp_before"))
                        projected = _int_or_none(
                            record.get("projected_hp_loss_before")
                        )
                        base_block = _int_or_none(
                            played_card.get("base_block")
                        )
                        real_block = (
                            _safe_nonnegative_int(played_card.get("block"))
                            if base_block is not None and base_block >= 0
                            else 0
                        )
                        net_immediate_block = real_block - enrage * hits
                        skill_cost = max(
                            0,
                            _int_or_none(played_card.get("cost")) or 0,
                        )
                        crowds_out_attack = bool(
                            affordable_attack
                            and not any(
                                cost <= max(0, energy - skill_cost)
                                for cost in affordable_attack_costs
                            )
                        )
                        if (
                            enrage > 0
                            and affordable_attack
                            and hp_before is not None
                            and projected is not None
                            and projected < hp_before
                            and projected <= hp_before * 0.4
                            and net_immediate_block <= 3
                            and (
                                net_immediate_block <= 0
                                or crowds_out_attack
                            )
                        ):
                            issues.append(_issue(
                                record,
                                "low_value_nob_enrage_skill",
                                card_id=record.get("card_id"),
                                block=real_block,
                                enrage=enrage,
                                hits=hits,
                                net_immediate_block=net_immediate_block,
                            ))
                            reasons["low_value_nob_enrage_skill"] += 1
                if played_card is not None and str(
                    (played_card or {}).get("type") or ""
                ).upper() == "ATTACK":
                    outcome = record.get("decision_outcome") or {}
                    turn_context["attack_hp_loss"] += max(
                        0, _int_or_none(outcome.get("enemy_hp_loss")) or 0
                    )
                cost = (
                    _int_or_none(played_card.get("cost"))
                    if isinstance(played_card, dict) else None
                )
                turn_context["played"].append(played_id)
                turn_context.setdefault("played_with_cost", []).append(
                    (played_id, max(0, cost or 0))
                )
                turn_context.setdefault("played_with_type", []).append((
                    played_id,
                    str(
                        played_card.get("type")
                        if isinstance(played_card, dict) else ""
                    ).upper(),
                ))

            if action == "end":
                # Audit the opportunity at the start of the whole turn.  The
                # former END-only check lost Demon Form after cheaper cards
                # consumed its Energy, because the card was no longer
                # playable in the terminal frame.  Persistent enemy defense
                # is direct long-fight evidence; otherwise retain the older
                # high-durability bound for generic encounters.
                initial_hand = [
                    card for card in turn_context.get("initial_hand") or []
                    if isinstance(card, dict)
                ]
                initial_energy = max(
                    0, turn_context.get("initial_energy") or 0
                )
                initial_setup_cards = [
                    card for card in initial_hand
                    if str(card.get("type") or "").upper() == "POWER"
                    and _normalized_id(card.get("id"))
                    in {"demonform", "echoform", "noxiousfumes", "footwork"}
                    and card.get("is_playable") is True
                    and (
                        (_int_or_none(card.get("cost")) or 0) < 0
                        or max(0, _int_or_none(card.get("cost")) or 0)
                        <= initial_energy
                    )
                    and _normalized_id(card.get("id"))
                    not in set(turn_context.get("played") or [])
                ]
                initial_living = [
                    monster
                    for monster in turn_context.get("initial_monsters") or []
                    if isinstance(monster, dict)
                    and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    and not monster.get("is_gone")
                    and not monster.get("half_dead")
                ]
                initial_effective_hp = sum(
                    _safe_nonnegative_int(monster.get("current_hp"))
                    + _safe_nonnegative_int(monster.get("block"))
                    for monster in initial_living
                )
                persistent_defense = any(
                    any(
                        isinstance(power, dict)
                        and (
                            _normalized_id(
                                power.get("id") or power.get("name")
                            ) in {"barricade", "barricadepower"}
                            or (
                                _normalized_id(
                                    power.get("id") or power.get("name")
                                ) in {"platedarmor", "platedarmorpower"}
                                and _safe_nonnegative_int(
                                    power.get("amount")
                                ) > 0
                            )
                        )
                        for power in monster.get("powers") or []
                    )
                    for monster in initial_living
                )
                awakened = any(
                    _normalized_id(monster.get("id")) == "awakenedone"
                    for monster in initial_living
                )
                time_warp_near_limit = any(
                    _normalized_id(monster.get("id")) == "timeeater"
                    and any(
                        isinstance(power, dict)
                        and _normalized_id(
                            power.get("id") or power.get("name")
                        ) in {"timewarp", "timewarppower"}
                        and _safe_nonnegative_int(power.get("amount")) >= 11
                        for power in monster.get("powers") or []
                    )
                    for monster in initial_living
                )
                setup_turn_eligible = bool(
                    initial_setup_cards
                    and turn_context.get("initial_projected_loss") is not None
                    and max(
                        0,
                        turn_context.get("initial_projected_loss") or 0,
                    ) == 0
                    and (persistent_defense or initial_effective_hp >= 80)
                    and not awakened
                    and not time_warp_near_limit
                    and int(turn_context.get("attack_hp_loss") or 0) == 0
                )
                if setup_turn_eligible and turn_key not in reported_long_setup_turns:
                    setup_coverage = coverage["long_fight_scaling_setup"]
                    setup_coverage["eligible"] += 1
                    zones = [
                        *initial_hand,
                        *[
                            card for card in turn_context.get(
                                "initial_draw_pile"
                            ) or []
                            if isinstance(card, dict)
                        ],
                        *[
                            card for card in turn_context.get(
                                "initial_discard_pile"
                            ) or []
                            if isinstance(card, dict)
                        ],
                    ]
                    observable = bool(zones and initial_living)
                    if observable:
                        setup_coverage["evaluated"] += 1
                        actionable = []
                        for setup in initial_setup_cards:
                            if _normalized_id(setup.get("id")) == "demonform":
                                has_consumer = any(
                                    str(card.get("type") or "").upper()
                                    == "ATTACK"
                                    and max(
                                        _safe_nonnegative_int(
                                            card.get("damage")
                                        ),
                                        _safe_nonnegative_int(
                                            card.get("base_damage")
                                        ),
                                    ) > 0
                                    for card in zones
                                )
                                if not has_consumer:
                                    continue
                            actionable.append(setup.get("id"))
                        if actionable:
                            issues.append(_issue(
                                record,
                                "missed_long_fight_scaling_setup",
                                setup_cards=actionable,
                                initial_energy=initial_energy,
                                final_energy=max(
                                    0,
                                    _int_or_none(record.get("energy_before"))
                                    or 0,
                                ),
                                initial_effective_enemy_hp=(
                                    initial_effective_hp
                                ),
                                persistent_enemy_defense=(
                                    persistent_defense
                                ),
                                played_cards=list(
                                    turn_context.get("played") or []
                                ),
                                opportunity_before_seq=turn_context.get(
                                    "initial_before_seq"
                                ),
                            ))
                            reasons[
                                "missed_long_fight_scaling_setup"
                            ] += 1
                            setup_coverage["violations"] += 1
                            reported_long_setup_turns.add(turn_key)
                # Plated Armor/Shelled Parasite is persistent across turns;
                # an END with affordable direct damage is therefore not the
                # same as an ordinary zero-value attack into temporary Block.
                # The old END audit only looked for cards that reduce current
                # hero HP loss, so it silently accepted a no-positive-action
                # decision while the enemy's permanent armor remained.  Keep
                # this as a narrow, reviewable invariant: require the named
                # enemy, a positive Plated Armor stack, an affordable
                # playable attack, and the planner's explicit no-positive END
                # reason.  Other enemies and ordinary Block remain untouched.
                shelled_coverage = coverage[
                    "missed_shelled_parasite_progress"
                ]
                shelled = any(
                    isinstance(monster, dict)
                    and _normalized_id(monster.get("id"))
                    in {"shelledparasite", "shelledparasiteelite"}
                    and _safe_nonnegative_int(monster.get("current_hp")) > 0
                    and any(
                        isinstance(power, dict)
                        and _normalized_id(
                            power.get("id") or power.get("name")
                        ) in {"platedarmor", "platedarmorpower"}
                        and _safe_nonnegative_int(power.get("amount")) > 0
                        for power in monster.get("powers") or []
                    )
                    for monster in record.get("monsters_before") or []
                )
                player_energy = max(
                    0,
                    _int_or_none(record.get("energy_before"))
                    or _int_or_none(
                        (record.get("player_before") or {}).get("energy")
                    )
                    or 0,
                )
                decision_reason = str(
                    (record.get("decision") or {}).get("reason") or ""
                )
                affordable_attacks = []
                for card in record.get("hand_before") or []:
                    if not isinstance(card, dict):
                        continue
                    if not card.get("is_playable"):
                        continue
                    if str(card.get("type") or "").upper() != "ATTACK":
                        continue
                    raw_damage = max(
                        0,
                        _int_or_none(
                            card.get("damage", card.get("base_damage"))
                        ) or 0,
                    )
                    if raw_damage <= 0:
                        continue
                    raw_cost = _int_or_none(card.get("cost"))
                    if raw_cost == -1 and player_energy <= 0:
                        # X-cost attacks are not executable with zero energy;
                        # treating X=0 as a free positive attack recreates the
                        # very audit false positive this rule is meant to
                        # avoid.
                        continue
                    cost = (
                        player_energy if raw_cost == -1
                        else max(0, raw_cost or 0)
                    )
                    if cost <= player_energy:
                        affordable_attacks.append({
                            "id": card.get("id"),
                            "card_instance_id": card.get(
                                "card_instance_id"
                            ),
                            "cost": raw_cost,
                            "damage": raw_damage,
                        })
                # Separate offensive progress from survival mitigation.  A
                # no-positive END is not auditable merely because the next
                # enemy hit is already covered: if a serialized attack can
                # cross current Block and no attack in this turn dealt HP
                # damage, the choice left a concrete progress opportunity
                # unexplained.  Reaction hazards and Plated Armor remain
                # fail-closed/specialized paths rather than being folded into
                # this lower-bound check.
                attack_progress_coverage = coverage[
                    "affordable_attack_progress_ignored"
                ]
                if (
                    decision_reason == "no_positive_marginal_action"
                    and affordable_attacks
                ):
                    attack_progress_coverage["eligible"] += 1
                    attack_progress_coverage["evaluated"] += 1
                    if (
                        not _record_enemy_reaction_hazard(record)
                        and _record_attack_can_cross_current_block(
                            record, affordable_attacks
                        )
                        and max(
                            0,
                            _int_or_none(
                                record.get("projected_attack_hp_loss_before")
                            ) or 0,
                        ) == 0
                        and not shelled
                        and int(turn_context.get("attack_hp_loss") or 0) == 0
                    ):
                        issues.append(_issue(
                            record,
                            "affordable_attack_progress_ignored",
                            attack_candidates=affordable_attacks,
                            energy=player_energy,
                            enemy_block=[
                                _safe_nonnegative_int(monster.get("block"))
                                for monster in record.get(
                                    "monsters_before"
                                ) or []
                                if isinstance(monster, dict)
                            ],
                            attack_hp_loss_this_turn=int(
                                turn_context.get("attack_hp_loss") or 0
                            ),
                        ))
                        reasons["affordable_attack_progress_ignored"] += 1

                choker_progress_coverage = coverage[
                    "choker_slots_without_attack_progress"
                ]
                choker_present = "velvetchoker" in {
                    _normalized_id(relic_id)
                    for relic_id in (record.get("relic_ids_before") or [])
                }
                if (
                    choker_present
                    and decision_reason == "play_phase_exhausted"
                    and len(turn_context.get("played") or []) >= 6
                    and any(
                        int(item.get("damage") or 0)
                        > int(item.get("enemy_block_before") or 0)
                        for item in turn_context.get(
                            "seen_affordable_attacks"
                        ) or []
                        if isinstance(item, dict)
                    )
                ):
                    choker_progress_coverage["eligible"] += 1
                    choker_progress_coverage["evaluated"] += 1
                    if (
                        not _record_enemy_reaction_hazard(record)
                        and int(turn_context.get("attack_hp_loss") or 0) == 0
                        and any(
                            _safe_nonnegative_int(
                                monster.get("current_hp")
                            ) > 0
                            for monster in record.get("monsters_before") or []
                            if isinstance(monster, dict)
                        )
                    ):
                        issues.append(_issue(
                            record,
                            "choker_slots_without_attack_progress",
                            cards_played=list(
                                turn_context.get("played") or []
                            ),
                            attack_opportunities=list(
                                turn_context.get(
                                    "seen_affordable_attacks"
                                ) or []
                            ),
                            attack_hp_loss_this_turn=int(
                                turn_context.get("attack_hp_loss") or 0
                            ),
                        ))
                        reasons[
                            "choker_slots_without_attack_progress"
                        ] += 1
                if (
                    shelled
                    and decision_reason == "no_positive_marginal_action"
                    and affordable_attacks
                ):
                    shelled_coverage["eligible"] += 1
                    shelled_coverage["evaluated"] += 1
                    issues.append(_issue(
                        record,
                        "missed_shelled_parasite_progress",
                        attack_candidates=affordable_attacks,
                        energy=player_energy,
                    ))
                    reasons["missed_shelled_parasite_progress"] += 1
                passive_doom_coverage = coverage[
                    "passive_doom_claim_proof"
                ]
                if decision_reason == "all_enemies_passively_doomed":
                    passive_doom_coverage["eligible"] += 1
                    passive_doom_coverage["evaluated"] += 1
                    proof = _record_passive_doom_proof(record)
                    passive_doom_evidence.append({
                        "before_seq": record.get("before_seq"),
                        "floor": record.get("floor"),
                        "turn": record.get("turn"),
                        **proof,
                    })
                    if proof.get("status") != "proven":
                        issues.append(_issue(
                            record,
                            "passive_doom_claim_unproven",
                            proof=proof,
                        ))
                        reasons["passive_doom_claim_unproven"] += 1
                        passive_doom_coverage["violations"] += 1
                trigger_coverage = coverage["missed_thousand_cuts_trigger"]
                powers = (
                    (record.get("player_before") or {}).get("powers") or []
                )
                thousand_cuts = max(
                    (
                        _safe_nonnegative_int(power.get("amount"))
                        for power in powers
                        if isinstance(power, dict)
                        and _normalized_id(power.get("id"))
                        in {"thousandcuts", "thousandcutspower"}
                    ),
                    default=0,
                )
                if thousand_cuts > 0:
                    trigger_coverage["eligible"] += 1
                    energy = _int_or_none(record.get("energy_before"))
                    hand = record.get("hand_before")
                    monsters = record.get("monsters_before") or []
                    decision_reason = str(
                        (record.get("decision") or {}).get("reason") or ""
                    )
                    observable = energy is not None and isinstance(hand, list)
                    if observable:
                        trigger_coverage["evaluated"] += 1
                        # A passive poison/Combust terminal line is already
                        # proven safe by the live planner.  Spending a card
                        # solely for one Thousand Cuts tick would invalidate
                        # the same doomed-target invariant the combat policy
                        # is protecting.
                        if decision_reason == "all_enemies_passively_doomed":
                            safe_trigger_cards = []
                        else:
                            safe_trigger_cards = []
                            projected_loss = max(
                                0,
                                _int_or_none(
                                    record.get("projected_hp_loss_before")
                                ) or 0,
                            )
                            hp_before = _int_or_none(
                                record.get("hp_before")
                            )
                            for card in hand:
                                if not (
                                    isinstance(card, dict)
                                    and card.get("is_playable")
                                    and str(card.get("type") or "").upper()
                                    in {"SKILL", "POWER"}
                                    and max(
                                        0,
                                        _int_or_none(card.get("cost")) or 0,
                                    )
                                    <= energy
                                ):
                                    continue
                                if (
                                    _normalized_id(card.get("id"))
                                    == "panicbutton"
                                    and hp_before is not None
                                    and not _panic_button_is_emergency(
                                        hp_before, projected_loss
                                    )
                                ):
                                    continue
                                safe_trigger_cards.append(card.get("id"))
                        hazardous = any(
                            isinstance(monster, dict)
                            and (
                                _normalized_id(monster.get("id"))
                                in {"timeeater", "gremlinnob", "corruptheart"}
                                or any(
                                    isinstance(power, dict)
                                    and _normalized_id(power.get("id"))
                                    in {"beatofdeath", "beatofdeathpower"}
                                    for power in monster.get("powers") or []
                                )
                            )
                            for monster in monsters
                            if _safe_nonnegative_int(
                                monster.get("current_hp")
                                if isinstance(monster, dict) else 0
                            ) > 0
                        )
                        if (
                            safe_trigger_cards
                            and not hazardous
                            and max(
                                0,
                                _int_or_none(
                                    record.get("projected_hp_loss_before")
                                ) or 0,
                            ) == 0
                        ):
                            issues.append(_issue(
                                record,
                                "missed_thousand_cuts_trigger",
                                cards=safe_trigger_cards,
                                damage_per_enemy=thousand_cuts,
                            ))
                            reasons["missed_thousand_cuts_trigger"] += 1

            play_damage_coverage = coverage[
                "card_play_damage_underprediction"
            ]
            if action == "play":
                played_instance = record.get("card_instance_id")
                played_card = next(
                    (
                        card for card in (record.get("hand_before") or [])
                        if isinstance(card, dict)
                        and card.get("card_instance_id") == played_instance
                    ),
                    None,
                )
                if (
                    isinstance(played_card, dict)
                    and str(played_card.get("type") or "").upper()
                    == "ATTACK"
                ):
                    play_damage_coverage["eligible"] += 1
                    hp_delta = _int_or_none(
                        (record.get("decision_outcome") or {}).get(
                            "hp_delta"
                        )
                    )
                    decision = record.get("decision") or {}
                    search = decision.get("search")
                    reported_card_loss = _int_or_none(
                        decision.get("card_self_hp_cost")
                    )
                    reported_plan_loss = (
                        _int_or_none(search.get("actual_loss"))
                        if isinstance(search, dict)
                        else None
                    )
                    forced_end = bool(
                        isinstance(search, dict)
                        and search.get("forced_end") is True
                    )
                    # A card which consumes Time Eater's twelfth-card slot
                    # immediately advances through the forced enemy turn.
                    # ``card_self_hp_cost`` still describes only the card's
                    # own packet, while the observed post-action HP delta and
                    # ``search.actual_loss`` include that forced turn.  Do
                    # not let a present zero self-cost hide the plan loss.
                    if forced_end and reported_plan_loss is not None:
                        reported_loss = max(
                            reported_card_loss or 0,
                            reported_plan_loss,
                        )
                    else:
                        reported_loss = (
                            reported_card_loss
                            if reported_card_loss is not None
                            else reported_plan_loss
                        )
                    # A plain attack may omit both projected self-loss fields
                    # when it caused no HP loss.  The authoritative outcome
                    # explicitly records zero player loss; use that fact to
                    # keep coverage conclusive while preserving unknowns for
                    # missing/negative telemetry.
                    observed_player_loss = _int_or_none(
                        (record.get("decision_outcome") or {}).get(
                            "player_hp_loss"
                        )
                    )
                    if (
                        reported_loss is None
                        and hp_delta is not None
                        and hp_delta >= 0
                        and observed_player_loss == 0
                    ):
                        reported_loss = 0
                    if hp_delta is not None and reported_loss is not None:
                        play_damage_coverage["evaluated"] += 1
                        immediate_loss = max(0, -hp_delta)
                        reported_loss = max(0, reported_loss)
                        if immediate_loss > reported_loss:
                            issues.append(_issue(
                                record,
                                "card_play_damage_underprediction",
                                card_id=record.get("card_id"),
                                immediate_loss=immediate_loss,
                                reported_card_loss=reported_loss,
                                reported_plan_loss=reported_plan_loss,
                            ))
                            reasons[
                                "card_play_damage_underprediction"
                            ] += 1

                card_consistency = coverage["card_damage_consistency"]
                decision_payload = record.get("decision") or {}
                search_payload = decision_payload.get("search") or {}
                # ``first_action_enemy_hp_loss`` is a plan-level shadow
                # value.  A bound terminal plan reuses the original search
                # snapshot for every continuation card; after the first
                # card, that value may also include a later card or a
                # passive end-turn packet.  Comparing it with the immediate
                # post-action HP delta produces false overprediction reports
                # (for example Strike -> Zap followed by Lightning at END).
                # Keep the planner's per-card *HP-loss* shadow value for
                # ordinary searches, and skip terminal continuations that
                # only carry the stale plan-level field.  ``card_damage`` is
                # raw damage before target Block/Vulnerable resolution, so it
                # must not be compared with observed HP loss here.
                expected_card_damage = None
                search_card_damage = _int_or_none(
                    search_payload.get("first_action_enemy_hp_loss")
                )
                if (
                    decision_payload.get("reason")
                    == "velvet_choker_progress_fallback"
                ):
                    # Older producer traces could retain the winning beam's
                    # first-action shadow after this fallback selected another
                    # target.  Its target-bound candidate damage is the
                    # authoritative immediate HP-loss projection.
                    expected_card_damage = _int_or_none(
                        decision_payload.get("card_damage")
                    )
                elif decision_payload.get("reason") != "terminal_plan_continuation":
                    expected_card_damage = search_card_damage
                capabilities = set(record.get("trace_capabilities") or [])
                if (
                    isinstance(played_card, dict)
                    and str(played_card.get("type") or "").upper()
                    == "ATTACK"
                    and expected_card_damage is not None
                    and "post_action_damage" in capabilities
                ):
                    card_consistency["eligible"] += 1
                    outcome = record.get("decision_outcome") or {}
                    changes = outcome.get("enemy_hp_changes")
                    deferred_resolution_required = bool(
                        str(outcome.get("phase_after") or "").upper()
                        in {"GRID", "HAND_SELECT"}
                        and isinstance(
                            search_payload.get(
                                "first_action_resolution_count"
                            ),
                            int,
                        )
                        and not isinstance(
                            search_payload.get(
                                "first_action_resolution_count"
                            ),
                            bool,
                        )
                        and search_payload.get(
                            "first_action_resolution_count"
                        ) > 1
                    )
                    deferred_observed_loss = (
                        _deferred_attack_enemy_hp_loss(
                            cohort, cohort_index, record
                        )
                        if deferred_resolution_required else None
                    )
                    before_by_id = {
                        monster.get("enemy_instance_id"): monster
                        for monster in (record.get("monsters_before") or [])
                        if isinstance(monster, dict)
                        and monster.get("enemy_instance_id") is not None
                    }
                    observed_losses = []
                    unresolved_changes = []
                    ignored_predead_changes = []
                    if deferred_resolution_required:
                        if deferred_observed_loss is None:
                            unresolved_changes.append({
                                "reason": (
                                    "deferred_attack_settlement_missing"
                                ),
                            })
                        else:
                            observed_losses.append(deferred_observed_loss)
                    elif isinstance(changes, list):
                        for change in changes:
                            if not isinstance(change, dict):
                                unresolved_changes.append({
                                    "reason": "malformed_enemy_change",
                                })
                                continue
                            loss = _int_or_none(change.get("hp_loss"))
                            if loss is not None:
                                observed_losses.append(loss)
                                continue
                            before = before_by_id.get(
                                change.get("enemy_instance_id")
                            ) or {}
                            hp_before = _int_or_none(
                                change.get("hp_before")
                            )
                            if hp_before is None:
                                hp_before = _int_or_none(
                                    before.get("current_hp")
                                )
                            provably_predead = bool(
                                hp_before is not None and hp_before <= 0
                            ) or before.get("is_gone") is True or (
                                change.get("is_gone_before") is True
                            )
                            if provably_predead:
                                ignored_predead_changes.append(
                                    change.get("enemy_instance_id")
                                )
                            else:
                                unresolved_changes.append({
                                    "enemy_instance_id": change.get(
                                        "enemy_instance_id"
                                    ),
                                    "id": change.get("id"),
                                    "hp_before": hp_before,
                                    "reason": "live_enemy_hp_loss_missing",
                                })
                    else:
                        unresolved_changes.append({
                            "reason": "enemy_hp_changes_missing",
                        })
                    if not observed_losses and not unresolved_changes:
                        unresolved_changes.append({
                            "reason": "no_observed_live_enemy_damage",
                        })
                    if observed_losses and not unresolved_changes:
                        card_consistency["evaluated"] += 1
                        actual_card_damage = sum(
                            max(0, value) for value in observed_losses
                        )
                        expected_card_damage = max(0, expected_card_damage)
                        if actual_card_damage == expected_card_damage:
                            damage_consistency["player_actions"]["exact"] += 1
                        elif actual_card_damage > expected_card_damage:
                            damage_consistency["player_actions"][
                                "actual_above_prediction"
                            ] += 1
                        else:
                            damage_consistency["player_actions"][
                                "actual_below_prediction"
                            ] += 1
                            issues.append(_issue(
                                record,
                                "card_damage_overprediction",
                                card_id=record.get("card_id"),
                                predicted=expected_card_damage,
                                actual=actual_card_damage,
                            ))
                            reasons["card_damage_overprediction"] += 1
                    else:
                        damage_consistency["player_actions"]["unknown"] += 1
                        kind = "card_damage_observation_incomplete"
                        review_findings.append(_issue(
                            record,
                            kind,
                            card_id=record.get("card_id"),
                            predicted=expected_card_damage,
                            unresolved_changes=unresolved_changes,
                            ignored_predead_enemy_instance_ids=(
                                ignored_predead_changes
                            ),
                        ))
                        review_reasons[kind] += 1

            split_coverage = coverage["split_interrupt_opportunity"]
            active_monsters = [
                monster
                for monster in (record.get("monsters_before") or [])
                if isinstance(monster, dict)
                and not monster.get("is_gone")
                and not monster.get("half_dead")
                and (_int_or_none(monster.get("current_hp")) or 0) > 0
            ]
            if action == "play" and len(active_monsters) == 1:
                split_target = active_monsters[0]
                enemy_id = _normalized_id(split_target.get("id"))
                current_hp = _int_or_none(split_target.get("current_hp"))
                max_hp = _int_or_none(split_target.get("max_hp"))
                enemy_block = _int_or_none(split_target.get("block"))
                intent = str(split_target.get("intent") or "").upper()
                energy = _int_or_none(record.get("energy_before"))
                hand = record.get("hand_before")
                max_damage = (
                    _max_affordable_direct_damage(hand, energy)
                    if isinstance(hand, list) and energy is not None
                    else None
                )
                if (
                    enemy_id in _SPLIT_MONSTER_IDS
                    and current_hp is not None
                    and max_hp is not None
                    and enemy_block is not None
                    and current_hp > max_hp // 2
                    and intent.startswith("ATTACK")
                    and max_damage is not None
                    and max_damage
                    >= current_hp - max_hp // 2 + max(0, enemy_block)
                ):
                    split_coverage["eligible"] += 1
                    search = (record.get("decision") or {}).get("search")
                    final_hp = (
                        search.get("final_enemy_hp")
                        if isinstance(search, dict)
                        else None
                    )
                    suppressed = (
                        search.get("action_suppressed_enemy_indexes")
                        if isinstance(search, dict)
                        else None
                    )
                    if (
                        isinstance(final_hp, list)
                        and len(final_hp) == 1
                        and _int_or_none(final_hp[0]) is not None
                        and isinstance(suppressed, list)
                    ):
                        split_coverage["evaluated"] += 1
                        crossed = int(final_hp[0]) <= max_hp // 2
                        killed_before_split = int(final_hp[0]) <= 0
                        action_suppressed = 0 in suppressed
                        if (
                            not killed_before_split
                            and (not crossed or not action_suppressed)
                        ):
                            issues.append(_issue(
                                record,
                                "dominated_split_interrupt_missed",
                                enemy_id=split_target.get("id"),
                                enemy_hp=current_hp,
                                split_threshold=max_hp // 2,
                                affordable_direct_damage=max_damage,
                                planned_final_hp=final_hp[0],
                                action_suppressed=action_suppressed,
                            ))
                            reasons["dominated_split_interrupt_missed"] += 1
                            split_coverage["violations"] += 1
        elif (
            record.get("record_type") == "decision"
        ):
            # COMBAT_REWARD shares the broad COMBAT_ prefix but is a screen
            # between encounters.  A potion discard there has no combat
            # lifecycle snapshot and must also reset combat identity before
            # the next encounter (which may share the same act/floor).
            last_combat_context = None

        if action == "end":
            resource_coverage = coverage["end_turn_with_resources"]
            resources = record.get("end_turn_resources")
            if isinstance(resources, dict):
                resource_coverage["eligible"] += 1
                resource_coverage["evaluated"] += 1
                playable_count = _int_or_none(
                    resources.get("playable_card_count")
                ) or 0
                energy = _int_or_none(resources.get("energy_before")) or 0
                reason = str(resources.get("reason") or "")
                safety_class = str(resources.get("safety_class") or "")
                projected = max(
                    0,
                    _int_or_none(record.get("projected_hp_loss_before")) or 0,
                )
                projected_attack = _int_or_none(
                    record.get("projected_attack_hp_loss_before")
                )
                playable_ids = set(resources.get("playable_card_ids") or [])
                hp_before = _int_or_none(record.get("hp_before"))
                if hp_before is None:
                    hp_before = _int_or_none(
                        (record.get("player_before") or {}).get("current_hp")
                    )
                positive_cards = any(
                    isinstance(card, dict)
                    and card.get("id") in playable_ids
                    and not (
                        _normalized_id(card.get("id")) == "panicbutton"
                        and hp_before is not None
                        and not _panic_button_is_emergency(
                            hp_before, projected
                        )
                    )
                    and _resource_card_has_survival_gain(
                        card,
                        record.get("monsters_before") or [],
                        hand=record.get("hand_before") or [],
                        incoming_attack_hp_loss=projected_attack,
                        player_block_before=record.get(
                            "player_block_before",
                            (record.get("player_before") or {}).get("block"),
                        ),
                        passive_end_block=_passive_end_block(record),
                        calipers_active=(
                            "calipers" in {
                                _normalized_id(value)
                                for value in record.get(
                                    "relic_ids_before"
                                ) or []
                            }
                        ),
                    )
                    for card in record.get("hand_before") or []
                )
                unexplained = (
                    playable_count > 0
                    and energy > 0
                    and positive_cards
                    and (
                        safety_class in {
                            "resources_unexplained",
                            "inconsistent_no_playable_reason",
                        }
                        or (
                            safety_class
                            == "evaluated_no_positive_marginal_action"
                        )
                    )
                )
                affordable_resource_block = max(
                    (
                        block
                        for block, _cards_played in _affordable_block_options(
                            [
                                card for card in record.get("hand_before") or []
                                if isinstance(card, dict)
                                and card.get("id") in playable_ids
                            ],
                            max(0, energy),
                        )
                    ),
                    default=0,
                )
                if unexplained and _fairy_revival_proves_block_neutral(
                    record, affordable_resource_block
                ):
                    unexplained = False
                ice_cream_retains_energy = bool(
                    projected == 0
                    and "icecream" in {
                        _normalized_id(value)
                        for value in record.get("relic_ids_before") or []
                    }
                )
                if ice_cream_retains_energy:
                    # Saving Energy is the relic's explicit payoff.  Without
                    # current projected loss, an affordable card is not proof
                    # that END discarded the resource; the per-relic report
                    # records the retained-energy boundary instead.
                    unexplained = False
                if unexplained and _record_passive_doom_proof(
                    record
                ).get("status") == "proven":
                    # A deterministic passive kill is a terminal action, not
                    # discarded energy.  Older traces could carry the generic
                    # no-playable reason after the live planner filtered out
                    # attacks against the doomed target, so recompute the
                    # proof instead of keying this exception on the reason.
                    unexplained = False
                if unexplained:
                    issues.append(_issue(
                        record,
                        "end_turn_with_resources",
                        reason=reason,
                        safety_class=safety_class,
                        energy_before=energy,
                        playable_card_count=playable_count,
                        playable_card_ids=resources.get("playable_card_ids") or [],
                        projected_hp_loss=projected,
                    ))
                    reasons["end_turn_with_resources"] += 1
                    resource_coverage["violations"] += 1
            damage_coverage = coverage["end_turn_damage_underprediction"]
            damage_coverage["eligible"] += 1
            outcome = record.get("decision_outcome") or {}
            hp_delta = _int_or_none(outcome.get("hp_delta"))
            exact_damage_model = record.get("damage_model") or {}
            if exact_damage_model.get("monsters_to_hero_basis") == (
                "end_turn_total_hp_loss_before_postcombat_healing"
            ):
                exact_gross_loss = _int_or_none(
                    exact_damage_model.get("monsters_to_hero_actual")
                )
                if exact_gross_loss is not None:
                    hp_delta = -max(0, exact_gross_loss)
            deferred_damage = _deferred_end_turn_damage(
                record,
                cohort[cohort_index + 1:cohort_index + 5],
                hp_delta,
            )
            if deferred_damage is not None:
                # Keep all downstream arithmetic on one authoritative value.
                # The END frame is acknowledged before the enemy turn, while
                # the following HAND_SELECT/proceed frame carries its damage.
                hp_delta = -deferred_damage
            predicted_value = _int_or_none(
                record.get("projected_hp_loss_before")
            )
            attack_value = _int_or_none(
                record.get("projected_attack_hp_loss_before")
            )
            end_turn_value = _int_or_none(
                record.get("projected_end_turn_hp_loss_before")
            )
            start_value = _int_or_none(
                record.get("projected_next_turn_start_hp_loss_before")
            )
            capabilities = set(record.get("trace_capabilities") or [])
            lifecycle_trace = "next_turn_start_hp_loss" in capabilities

            arithmetic_coverage = coverage["turn_loss_arithmetic"]
            if lifecycle_trace:
                arithmetic_coverage["eligible"] += 1
                if all(
                    value is not None
                    for value in (
                        predicted_value, attack_value, end_turn_value,
                        start_value,
                    )
                ):
                    arithmetic_coverage["evaluated"] += 1
                    component_total = (
                        max(0, attack_value)
                        + max(0, end_turn_value)
                        + max(0, start_value)
                    )
                    if max(0, predicted_value) != component_total:
                        issues.append(_issue(
                            record,
                            "turn_loss_component_mismatch",
                            projected=max(0, predicted_value),
                            components=component_total,
                        ))
                        reasons["turn_loss_component_mismatch"] += 1
                        arithmetic_coverage["violations"] += 1

            player_state = record.get("player_before") or {}
            player_powers = player_state.get("powers") or []
            brutality_amount = max(
                (
                    _safe_nonnegative_int(power.get("amount"))
                    for power in player_powers
                    if isinstance(power, dict)
                    and _normalized_id(
                        power.get("id") or power.get("name")
                    ) in {"brutality", "brutalitypower"}
                ),
                default=0,
            )
            has_brutality = brutality_amount > 0
            has_buffer = any(
                _normalized_id(power.get("id") or power.get("name"))
                in {"buffer", "bufferpower"}
                and _safe_nonnegative_int(power.get("amount")) > 0
                for power in player_powers
                if isinstance(power, dict)
            )
            has_tungsten = any(
                _normalized_id(relic_id) == "tungstenrod"
                for relic_id in record.get("relic_ids_before") or []
            )
            living_enemy_ids = {
                monster.get("enemy_instance_id")
                for monster in record.get("monsters_before") or []
                if isinstance(monster, dict)
                and _safe_nonnegative_int(monster.get("current_hp")) > 0
                and not monster.get("is_gone")
                and not monster.get("half_dead")
            }
            doomed_ids = set(record.get("doomed_enemy_ids_before") or [])
            decision_search = (record.get("decision") or {}).get("search") or {}
            combat_ends = bool(decision_search.get("true_combat_end")) or (
                bool(living_enemy_ids)
                and living_enemy_ids.issubset(doomed_ids)
            )
            hp_before_start = _int_or_none(record.get("hp_before"))
            pre_start_loss = (
                max(0, attack_value or 0) + max(0, end_turn_value or 0)
            )
            brutality_should_resolve = (
                lifecycle_trace
                and has_brutality
                and not has_buffer
                and not combat_ends
                and hp_before_start is not None
                and hp_before_start > pre_start_loss
            )
            expected_brutality_loss = max(
                0, brutality_amount - (1 if has_tungsten else 0)
            )
            start_coverage = coverage["next_turn_start_damage"]
            if lifecycle_trace and has_brutality:
                start_coverage["eligible"] += 1
                if start_value is not None:
                    start_coverage["evaluated"] += 1
                    if (
                        brutality_should_resolve
                        and start_value < expected_brutality_loss
                    ):
                        issues.append(_issue(
                            record,
                            "next_turn_start_damage_underprediction",
                            predicted=max(0, start_value),
                            expected=expected_brutality_loss,
                        ))
                        reasons[
                            "next_turn_start_damage_underprediction"
                        ] += 1
                        start_coverage["violations"] += 1
            if (
                hp_delta is not None
                and predicted_value is not None
                and (
                    _combat_end_healing_obscures_enemy_turn(record)
                    or _automatic_card_play_obscures_enemy_turn(record)
                )
            ):
                # The action may include an enemy attack plus combat-end
                # healing or a next-turn automatic card play in one final
                # delta. The intermediate attack loss is not observable.
                damage_coverage["unknown"] += 1
                consistency_coverage = coverage[
                    "end_turn_damage_consistency"
                ]
                consistency_coverage["eligible"] += 1
                consistency_coverage["unknown"] += 1
                damage_consistency["enemy_turns"]["unknown"] += 1
                hp_delta = None
            fairy_trace = "fairy_revival" in capabilities
            fairy_consumed = bool(
                record.get("projected_fairy_revive_consumed_before")
            )
            predicted_fairy_healing = _int_or_none(
                record.get("projected_fairy_revive_healing_before")
            )
            authoritative_fairy_healing = (
                _authoritative_fairy_revive_healing(record)
            )
            fairy_healing = (
                authoritative_fairy_healing
                if authoritative_fairy_healing is not None
                else predicted_fairy_healing
            )
            fairy_consumed = bool(
                fairy_consumed or authoritative_fairy_healing is not None
            )
            projected_hp_delta = _int_or_none(
                record.get("projected_player_hp_delta_before")
            )
            fairy_comparison_handled = False
            if fairy_trace and fairy_consumed:
                fairy_comparison_handled = True
                consistency_coverage = coverage[
                    "end_turn_damage_consistency"
                ]
                consistency_coverage["eligible"] += 1
                if any(
                    value is None
                    for value in (
                        hp_delta, predicted_value, fairy_healing,
                        projected_hp_delta,
                    )
                ):
                    damage_coverage["unknown"] += 1
                    consistency_coverage["unknown"] += 1
                    damage_consistency["enemy_turns"]["unknown"] += 1
                else:
                    damage_coverage["evaluated"] += 1
                    consistency_coverage["evaluated"] += 1
                    gross_actual = max(0, fairy_healing - hp_delta)
                    prediction_matches = (
                        projected_hp_delta == hp_delta
                        and max(0, predicted_value) == gross_actual
                        and (
                            authoritative_fairy_healing is None
                            or predicted_fairy_healing
                            == authoritative_fairy_healing
                        )
                    )
                    if prediction_matches:
                        damage_consistency["enemy_turns"]["exact"] += 1
                    else:
                        damage_consistency["enemy_turns"][
                            "actual_above_prediction"
                            if gross_actual > max(0, predicted_value)
                            else "actual_below_prediction"
                        ] += 1
                        issues.append(_issue(
                            record,
                            "fairy_revival_prediction_mismatch",
                            predicted_gross_damage=max(
                                0, predicted_value
                            ),
                            actual_gross_damage=gross_actual,
                            predicted_hp_delta=projected_hp_delta,
                            actual_hp_delta=hp_delta,
                            revive_healing=fairy_healing,
                            predicted_revive_healing=(
                                predicted_fairy_healing
                            ),
                            revive_healing_authority=(
                                "authoritative_state_transition"
                                if authoritative_fairy_healing is not None
                                else "producer_projection_fallback"
                            ),
                        ))
                        reasons[
                            "fairy_revival_prediction_mismatch"
                        ] += 1
                        consistency_coverage["violations"] += 1
            if (
                not fairy_comparison_handled
                and hp_delta is not None
                and predicted_value is not None
            ):
                damage_coverage["evaluated"] += 1
                actual_loss = max(0, -hp_delta)
                predicted = max(0, predicted_value)
                projected_healing = _projected_end_turn_healing(
                    record, max(0, end_turn_value or 0)
                )
                net_prediction = max(0, predicted - projected_healing)
                consistency_coverage = coverage[
                    "end_turn_damage_consistency"
                ]
                consistency_coverage["eligible"] += 1
                hp_before = _int_or_none(record.get("hp_before"))
                if hp_before is None:
                    damage_consistency["enemy_turns"]["unknown"] += 1
                else:
                    consistency_coverage["evaluated"] += 1
                    comparable_prediction = min(
                        net_prediction, max(0, hp_before)
                    )
                    if actual_loss == comparable_prediction:
                        damage_consistency["enemy_turns"]["exact"] += 1
                    elif actual_loss > comparable_prediction:
                        damage_consistency["enemy_turns"][
                            "actual_above_prediction"
                        ] += 1
                    else:
                        damage_consistency["enemy_turns"][
                            "actual_below_prediction"
                        ] += 1
                        overprediction_reason = (
                            _end_turn_overprediction_reason(
                                record, predicted, actual_loss
                            )
                        )
                        if overprediction_reason not in {
                            "stochastic_lightning_removed_attacker",
                            "deterministic_poison_corpse_explosion",
                        }:
                            issues.append(_issue(
                                record,
                                "end_turn_damage_overprediction",
                                predicted=predicted,
                                projected_healing=projected_healing,
                                comparable_prediction=comparable_prediction,
                                actual=actual_loss,
                                reason=overprediction_reason,
                            ))
                            reasons["end_turn_damage_overprediction"] += 1
                tolerance = 0 if (
                    lifecycle_trace
                    and (
                        max(0, start_value or 0) > 0
                        or brutality_should_resolve
                        and expected_brutality_loss > 0
                    )
                ) else 2
                if actual_loss > net_prediction + tolerance:
                    issues.append(_issue(
                        record,
                        "end_turn_damage_underprediction",
                        predicted=predicted,
                        projected_healing=projected_healing,
                        comparable_prediction=net_prediction,
                        actual=actual_loss,
                    ))
                    reasons["end_turn_damage_underprediction"] += 1

            mitigation_coverage = coverage[
                "avoidable_loss_end_turn_candidate"
            ]
            mitigation_coverage["eligible"] += 1
            hp_value = record.get("hp_before")
            if hp_value is None:
                # Compatibility with hand-authored/legacy audit fixtures.
                hp_value = record.get("player_hp_before")
            hp_before = _int_or_none(hp_value)
            energy = _int_or_none(record.get("energy_before"))
            block_value = _int_or_none(record.get("projected_block_before"))
            hand = record.get("hand_before")
            mitigation_observable = all(
                value is not None
                for value in (
                    hp_before,
                    energy,
                    predicted_value,
                    attack_value,
                    block_value,
                    end_turn_value,
                    start_value if lifecycle_trace else 0,
                )
            ) and isinstance(hand, list)
            if mitigation_observable:
                mitigation_coverage["evaluated"] += 1
                predicted = max(0, predicted_value)
                attack_loss = max(0, attack_value)
                baseline_block = max(0, block_value)
                playable_mitigation = []
                for card in hand:
                    if not (
                        isinstance(card, dict)
                        and card.get("is_playable")
                        and card.get("base_block") is not None
                        and int(card.get("base_block") or 0) >= 0
                        and int(card.get("block") or 0) > 0
                        and int(card.get("cost") or 0) <= energy
                    ):
                        continue
                    if (
                        _normalized_id(card.get("id")) == "panicbutton"
                        and hp_before is not None
                        and not _panic_button_is_emergency(
                            hp_before, predicted
                        )
                    ):
                        # The live policy reserves this card because its
                        # two-turn No Block drawback can cost more than a
                        # small current hit.  Do not report the deliberate
                        # reserve as an avoidable Defend.
                        continue
                    candidate = card
                    if _normalized_id(card.get("id")) == "secondwind":
                        # CommunicationMod serializes Second Wind's block per
                        # exhausted card. It grants no fixed block by itself.
                        # Convert it to the exact hand-local total before the
                        # generic mitigation knapsack sees it.
                        exhaustible = sum(
                            1
                            for other in hand
                            if other is not card
                            and isinstance(other, dict)
                            and str(other.get("type") or "").upper()
                            != "ATTACK"
                        )
                        effective_block = (
                            max(0, int(card.get("block") or 0))
                            * exhaustible
                        )
                        if effective_block <= 0:
                            continue
                        candidate = dict(card)
                        candidate["block"] = effective_block
                    playable_mitigation.append(candidate)
                time_warp_remaining, forced_end_strength_damage = (
                    _time_warp_context(record)
                )
                block_options = _affordable_block_options(
                    playable_mitigation, energy,
                    max_cards=time_warp_remaining,
                )
                # A sequence that consumes Time Eater's final card is still
                # legal, but its new Strength applies before the monster's
                # current multi-hit attack.  Only block beyond that certain
                # added damage is audited as avoided loss.  This is deliberately
                # conservative around Buffer, Torii and other ordered effects.
                affordable_block, extra_effective_block = max(
                    (
                        (
                            block,
                            max(
                                0,
                                block - (
                                    forced_end_strength_damage
                                    if time_warp_remaining is not None
                                    and cards_played >= time_warp_remaining
                                    else 0
                                ),
                            ),
                        )
                        for block, cards_played in block_options
                    ),
                    default=(0, 0),
                    key=lambda item: (item[1], item[0]),
                )
                if "player_block_before" in record:
                    if record.get("orichalcum_active_before"):
                        # Immediate block disables Orichalcum's six-block
                        # fallback.  Compare the candidate against the
                        # baseline *net* block: a six-block card merely
                        # replaces the six block Orichalcum would have
                        # supplied at end of turn and must not be reported as
                        # avoidable mitigation.  (Live frames expose
                        # projected_block_before as the real block before
                        # passive end-turn sources, which is usually zero.)
                        real_block_before = max(
                            0,
                            _int_or_none(record.get("player_block_before"))
                            or 0,
                        )
                        orichalcum_fallback = (
                            6 if real_block_before <= 0 else 0
                        )
                        effective_after = baseline_block + max(
                            0,
                            extra_effective_block - orichalcum_fallback,
                        )
                    else:
                        effective_after = baseline_block + extra_effective_block
                else:
                    # Old traces did not distinguish real block from
                    # Orichalcum. Requiring the candidate to beat the entire
                    # projection is a conservative historical fallback.
                    effective_after = extra_effective_block
                extra_effective_block = max(
                    0, effective_after - baseline_block
                )
                effective_total_loss = max(
                    0, attack_loss - extra_effective_block
                ) + max(0, end_turn_value) + max(0, start_value or 0)
                effective_total_loss = max(
                    0,
                    effective_total_loss
                    - _projected_end_turn_healing(
                        record, max(0, end_turn_value)
                    ),
                )
                still_lethal = (
                    hp_before > 0
                    and predicted >= hp_before
                    and effective_total_loss >= hp_before
                )
                observed_actual_loss = (
                    max(0, -hp_delta) if hp_delta is not None else None
                )
                overprediction_reason = (
                    _end_turn_overprediction_reason(
                        record, predicted, observed_actual_loss
                    )
                    if observed_actual_loss is not None
                    and observed_actual_loss < predicted
                    else None
                )
                known_non_attack = overprediction_reason in {
                    "passive_shifting_strength_loss",
                    "passive_split_transition",
                }
                if (
                    predicted > 0
                    and attack_loss > 0
                    and playable_mitigation
                    and effective_after > baseline_block
                    and not still_lethal
                    and not known_non_attack
                    and not _fairy_revival_proves_block_neutral(
                        record, extra_effective_block
                    )
                ):
                    issues.append(_issue(
                        record,
                        "avoidable_loss_end_turn_candidate",
                        cards=[
                            card.get("id") for card in playable_mitigation
                        ],
                        projected_block=baseline_block,
                        affordable_block=affordable_block,
                        effective_block_after=effective_after,
                        time_warp_remaining=time_warp_remaining,
                        forced_end_strength_damage=forced_end_strength_damage,
                    ))
                    reasons["avoidable_loss_end_turn_candidate"] += 1

            reset_coverage = coverage["time_eater_safe_reset_missed"]
            time_warp_remaining, forced_end_strength_damage = (
                _time_warp_context(record)
            )
            if time_warp_remaining == 1:
                reset_coverage["eligible"] += 1
                hp_before = _int_or_none(record.get("hp_before"))
                energy = _int_or_none(record.get("energy_before"))
                predicted = _int_or_none(
                    record.get("projected_hp_loss_before")
                )
                hand = record.get("hand_before")
                eater = next((
                    monster
                    for monster in record.get("monsters_before") or []
                    if _normalized_id(monster.get("id")) == "timeeater"
                ), None)
                observable = (
                    hp_before is not None
                    and energy is not None
                    and predicted is not None
                    and isinstance(hand, list)
                    and isinstance(eater, dict)
                )
                if observable:
                    reset_coverage["evaluated"] += 1
                    eater_hp = _int_or_none(eater.get("current_hp"))
                    eater_max_hp = _int_or_none(eater.get("max_hp"))
                    haste_pending = (
                        eater_hp is not None
                        and eater_max_hp is not None
                        and eater_hp <= max(1, eater_max_hp // 2)
                    )
                    enemy_block = max(
                        0, _int_or_none(eater.get("block")) or 0
                    )
                    useful_cards = []
                    for card in hand:
                        if not isinstance(card, dict) or not card.get(
                            "is_playable"
                        ):
                            continue
                        cost = _int_or_none(card.get("cost"))
                        if cost is None or cost < 0 or cost > energy:
                            continue
                        block = max(
                            0, _int_or_none(card.get("block")) or 0
                        )
                        damage = max(
                            0, _int_or_none(card.get("damage")) or 0
                        )
                        if block > 0 or (
                            str(card.get("type") or "").upper() == "ATTACK"
                            and damage > enemy_block
                        ):
                            useful_cards.append(card.get("id"))
                    conservative_forced_loss = (
                        max(0, predicted)
                        + max(0, forced_end_strength_damage)
                    )
                    if (
                        useful_cards
                        and not haste_pending
                        and conservative_forced_loss < hp_before
                    ):
                        issues.append(_issue(
                            record,
                            "time_eater_safe_reset_missed",
                            cards=useful_cards,
                            projected_loss=max(0, predicted),
                            conservative_forced_loss=(
                                conservative_forced_loss
                            ),
                        ))
                        reasons["time_eater_safe_reset_missed"] += 1
                else:
                    reset_coverage["unknown"] += 1

        target = record.get("enemy_instance_id")
        if (
            action == "play"
            and target
            and target in (record.get("doomed_enemy_ids_before") or [])
            and record.get("card_id") not in ALLOWED_DOOMED_TARGET_CARDS
            and not _doomed_target_has_material_attack(record)
        ):
            issues.append(_issue(
                record,
                "redundant_doomed_target",
                card_id=record.get("card_id"),
                target=target,
            ))
            reasons["redundant_doomed_target"] += 1

        if action == "potion":
            decision = record.get("decision") or {}
            potion_id = "".join(
                character
                for character in str(
                    record.get("potion_instance_id")
                    and (record.get("potion") or {}).get("id")
                    or decision.get("potion_id")
                    or ""
                ).lower()
                if character.isalnum()
            )
            if (
                potion_id == "liquidmemories"
                and decision.get("planned_turn_hp_loss") == 0
                and int(decision.get("liquid_memories_marginal_kills") or 0) <= 0
                and not decision.get("liquid_memories_critical_setup")
            ):
                issues.append(_issue(
                    record,
                    "liquid_memories_zero_loss_use",
                    potion_id=decision.get("potion_id"),
                ))
                reasons["liquid_memories_zero_loss_use"] += 1
            baseline_loss = _int_or_none(
                decision.get("planned_turn_hp_loss")
            )
            candidate_loss = None
            for key in (
                "potion_candidate_hp_loss", "potion_protected_hp_loss",
            ):
                if key in decision:
                    candidate_loss = _int_or_none(decision.get(key))
                    break
            if (
                potion_id == "energypotion"
                and baseline_loss is not None
                and candidate_loss is not None
                and candidate_loss >= baseline_loss
                and not decision.get("potion_candidate_combat_end")
                and int(decision.get("potion_marginal_damage") or 0) < 20
                and int(decision.get("potion_marginal_kills") or 0) <= 0
                and not decision.get("potion_candidate_critical_setup")
            ):
                issues.append(_issue(
                    record,
                    "potion_without_marginal_gain",
                    potion_id=potion_id,
                    baseline_loss=baseline_loss,
                    candidate_loss=candidate_loss,
                ))
                reasons["potion_without_marginal_gain"] += 1
            if potion_id in _UNPROVEN_RANDOM_POTION_IDS:
                potion_turn_key = (
                    record.get("attempt_id"), record.get("run_id"),
                    record.get("combat_id"), record.get("turn"),
                )
                unproven_potions_by_turn[potion_turn_key] += 1
                if unproven_potions_by_turn[potion_turn_key] > 1:
                    issues.append(_issue(
                        record,
                        "repeated_unproven_random_potion",
                        potion_id=potion_id,
                        uses=unproven_potions_by_turn[potion_turn_key],
                    ))
                    reasons["repeated_unproven_random_potion"] += 1

        search = (record.get("decision") or {}).get("search")
        if (
            action == "play"
            and isinstance(search, dict)
            and search.get("true_combat_end")
            and _normalized_id(record.get("card_id"))
            in _ORB_REQUIRED_CARD_IDS
        ):
            player_orbs = (record.get("player_before") or {}).get("orbs")
            occupied_orbs = [
                orb for orb in player_orbs or []
                if isinstance(orb, dict)
                and _normalized_id(orb.get("id") or orb.get("orb_id"))
                not in {"", "empty"}
            ] if isinstance(player_orbs, list) else None
            if occupied_orbs == []:
                issues.append(_issue(
                    record,
                    "terminal_plan_missing_orb_resource",
                    card_id=record.get("card_id"),
                    action_true_combat_end_predicted=(
                        search.get("action_true_combat_end_predicted")
                    ),
                    planned_sequence=search.get("planned_sequence"),
                ))
                reasons["terminal_plan_missing_orb_resource"] += 1
        if (
            combat_id
            and isinstance(search, dict)
            and search.get("true_combat_end")
        ):
            pending_true_combat_end[combat_key] = record

        grant_key = (record.get("attempt_id"), _event_token(record))
        deck_size = _deck_size(record)
        pending = pending_event_card_grants.get(grant_key)
        if pending is not None and deck_size is not None:
            card_count = _deck_card_count(record, pending["card_ids"])
            observed_card_gain = (
                None
                if card_count is None
                else card_count - pending["card_count"]
            )
            observed_net_size = deck_size - pending["deck_size"]
            if (
                observed_card_gain is not None
                and observed_card_gain >= pending["expected_gain"]
                and observed_net_size >= pending["expected_net_size"]
            ):
                pending_event_card_grants.pop(grant_key, None)
            elif record.get("phase") == "EVENT" and _is_event_exit(record):
                issues.append(_issue(
                    record,
                    "event_reward_left_unsettled",
                    event_id=grant_key[1],
                    expected_gain=pending["expected_gain"],
                    observed_gain=max(0, observed_card_gain or 0),
                    observed_net_size=observed_net_size,
                    accepted_before_seq=pending["before_seq"],
                ))
                reasons["event_reward_left_unsettled"] += 1
                pending_event_card_grants.pop(grant_key, None)

        if record.get("phase") == "EVENT" and action == "choose" and not _is_event_exit(record):
            grant = _expected_event_card_grant(record)
            card_count = (
                _deck_card_count(record, grant["card_ids"])
                if grant is not None
                else None
            )
            if grant is not None and deck_size is not None and card_count is not None:
                pending_event_card_grants[grant_key] = {
                    "deck_size": deck_size,
                    "card_count": card_count,
                    **grant,
                    "before_seq": record.get("before_seq"),
                }

    if optional_hand_selection is not None:
        initial = optional_hand_selection["initial"]
        selected = optional_hand_selection["selected"]
        protected = optional_hand_selection["protected"]
        if initial and initial.issubset(selected) and protected:
            source = optional_hand_selection["last_record"]
            issues.append(_issue(
                source,
                "optional_hand_overselection",
                selected_count=len(selected),
                initial_count=len(initial),
                protected_cards=sorted(protected),
            ))
            reasons["optional_hand_overselection"] += 1

    relic_strategy_audit = _relic_strategy_audit_report(cohort, issues)
    audit_coverage = {
        name: _coverage_summary(
            counter,
            violations=(
                max(
                    int(counter.get("violations", 0)),
                    int(reasons.get(name, 0)),
                )
            ),
        )
        for name, counter in coverage.items()
    }
    audit_coverage["relic_strategy_decision"] = {
        key: relic_strategy_audit[key]
        for key in (
            "eligible", "evaluated", "unknown", "violations", "status"
        )
    }
    turn_damage = _turn_damage_summaries(cohort)
    exact_branch_relics = Counter()
    state_reflected_relics = Counter()
    heuristic_relics = Counter()
    unsupported_relics = Counter()
    noncombat_relics = Counter()
    unclassified_relics = Counter()
    power_categories = {
        category: Counter()
        for category in (
            "exact_branch", "state_reflected", "heuristic", "unsupported",
            "unclassified",
        )
    }
    power_categories_by_owner = {
        role: {
            category: Counter()
            for category in power_categories
        }
        for role in ("player", "monster")
    }
    exact_power_handlers = Counter()
    power_records_with_unsupported = 0
    power_records_with_unclassified = 0
    for record in cohort:
        coverage_value = _record_relic_coverage(record) or {}
        if isinstance(coverage_value, dict):
            exact_branch_relics.update(
                coverage_value.get("exact_branch_relic_ids")
                or coverage_value.get("modeled_relic_ids")
                or []
            )
            state_reflected_relics.update(
                coverage_value.get("state_reflected_relic_ids") or []
            )
            heuristic_relics.update(
                coverage_value.get("heuristic_relic_ids") or []
            )
            unsupported_relics.update(
                coverage_value.get("unsupported_relic_ids") or []
            )
            noncombat_relics.update(
                coverage_value.get("noncombat_relic_ids") or []
            )
            unclassified_relics.update(
                coverage_value.get("unclassified_relic_ids") or []
            )

        power_coverage = _record_power_coverage(record) or {}
        active_powers = power_coverage.get("active_powers")
        if not isinstance(active_powers, list):
            continue
        record_categories = set()
        for entry in active_powers:
            if not isinstance(entry, dict):
                continue
            category = entry.get("category")
            if category not in power_categories:
                category = "unclassified"
            power_id = entry.get("normalized_id") or entry.get("raw_id")
            if not power_id:
                continue
            role = (
                "monster"
                if entry.get("owner_role") == "monster"
                else "player"
            )
            power_categories[category][power_id] += 1
            power_categories_by_owner[role][category][power_id] += 1
            record_categories.add(category)
            if category == "exact_branch" and entry.get("handler"):
                exact_power_handlers[entry["handler"]] += 1
        power_records_with_unsupported += int(
            "unsupported" in record_categories
        )
        power_records_with_unclassified += int(
            "unclassified" in record_categories
        )

    relic_report = {
        "coverage_contract_version": 2,
        "exact_branch_relics": dict(exact_branch_relics),
        # Backward-compatible exact-only alias.
        "modeled_relics": dict(exact_branch_relics),
        "state_reflected_relics": dict(state_reflected_relics),
        "heuristic_relics": dict(heuristic_relics),
        "unsupported_relics": dict(unsupported_relics),
        "noncombat_relics": dict(noncombat_relics),
        "unclassified_relics": dict(unclassified_relics),
        "records_with_unsupported": sum(
            bool((_record_relic_coverage(record) or {}).get(
                "unsupported_relic_ids"
            ))
            for record in cohort
            if isinstance(_record_relic_coverage(record), dict)
        ),
        "records_with_unclassified": sum(
            bool((_record_relic_coverage(record) or {}).get(
                "unclassified_relic_ids"
            ))
            for record in cohort
            if isinstance(_record_relic_coverage(record), dict)
        ),
    }
    power_contract_violations = (
        _combat_predictor.power_coverage_contract_violations()
    )
    relic_contract_violations = (
        _combat_predictor.relic_coverage_contract_violations()
    )
    power_report = {
        "coverage_contract_version": (
            _combat_predictor.POWER_COVERAGE_CONTRACT_VERSION
        ),
        "contract_violations": power_contract_violations,
        "active_power_occurrences": sum(
            sum(counter.values())
            for counter in power_categories.values()
        ),
        "exact_branch_powers": dict(power_categories["exact_branch"]),
        # Compatibility vocabulary: modeled remains exact-only.
        "modeled_powers": dict(power_categories["exact_branch"]),
        "state_reflected_powers": dict(
            power_categories["state_reflected"]
        ),
        "heuristic_powers": dict(power_categories["heuristic"]),
        "unsupported_powers": dict(power_categories["unsupported"]),
        "unclassified_powers": dict(power_categories["unclassified"]),
        "exact_handler_occurrences": dict(exact_power_handlers),
        "by_owner": {
            role: {
                f"{category}_powers": dict(counter)
                for category, counter in categories.items()
            }
            for role, categories in power_categories_by_owner.items()
        },
        "records_with_unsupported": power_records_with_unsupported,
        "records_with_unclassified": power_records_with_unclassified,
    }

    unknown_protocol_events = sorted(
        event for event in protocol_event_counts
        if event not in _KNOWN_PROTOCOL_EVENTS
    )
    fatal_protocol_events = dict(Counter(
        record["event"] for record in fatal_protocol_records
    ))
    protocol_status = (
        "issues"
        if (
            operational_errors_by_attempt
            or fatal_protocol_events
            or hash_mismatch_records
            or unknown_record_type_records
            or reasons.get("auxiliary_trace_pollution", 0)
        )
        else "inconclusive"
        if not cohort or unknown_protocol_events
        else "clear"
    )
    protocol_correctness = {
        "status": protocol_status,
        "events": sum(protocol_event_counts.values()),
        "event_counts": dict(protocol_event_counts),
        "fatal_event_counts": fatal_protocol_events,
        "verified_readiness_resync_pairs": len(benign_resync_indexes) // 2,
        "unknown_event_types": unknown_protocol_events,
        "operational_error_attempts": len(operational_errors_by_attempt),
        "decision_hash_mismatch_records": len(hash_mismatch_records),
    }

    unclassified_ids = sorted(set(
        unclassified_relics
    ) | set(power_categories["unclassified"]))
    unsupported_ids = sorted(set(
        unsupported_relics
    ) | set(power_categories["unsupported"]))
    mechanics_summary = {
        "unclassified_ids": unclassified_ids,
        "unsupported_ids": unsupported_ids,
        "unclassified_occurrences": (
            sum(unclassified_relics.values())
            + sum(power_categories["unclassified"].values())
        ),
        "unsupported_occurrences": (
            sum(unsupported_relics.values())
            + sum(power_categories["unsupported"].values())
        ),
    }
    combat_context = audit_coverage["combat_decision_context"]
    mechanics_contract_violations = {
        "relics": relic_contract_violations,
        "powers": power_contract_violations,
    }
    mechanics_contract_broken = _recursive_has_values(
        mechanics_contract_violations
    )
    mechanics_status = (
        "issues"
        if mechanics_contract_broken
        else "not_applicable"
        if not combat_context["eligible"]
        else "inconclusive"
        if (
            mechanics_summary["unclassified_occurrences"]
            or mechanics_summary["unsupported_occurrences"]
            or combat_context["status"] == "inconclusive"
        )
        else "clear"
    )
    mechanics_coverage = {
        "status": mechanics_status,
        "combat_records": combat_context["eligible"],
        "context_unknown": combat_context["unknown"],
        "contract_violations": mechanics_contract_violations,
        **mechanics_summary,
    }

    protocol_issue_kinds = set(_FATAL_PROTOCOL_EVENTS) | {
        "auxiliary_trace_pollution", "decision_hash_mismatch",
        "operational_error_tainted_attempt", "unknown_record_type",
    }
    strategic_issues = [
        item for item in issues
        if item.get("kind") not in protocol_issue_kinds
    ]
    strategy_status = (
        "issues"
        if strategic_issues
        else "inconclusive"
        if (
            not cohort
            or review_findings
            or any(
                item["status"] in {"issues", "inconclusive"}
                for item in audit_coverage.values()
            )
            or not any(
                item["evaluated"] > 0
                for item in audit_coverage.values()
            )
        )
        else "clear"
    )
    strategy_quality = {
        "status": strategy_status,
        "issue_count": len(strategic_issues),
        "review_finding_count": len(review_findings),
        "checks": audit_coverage,
        "relics": relic_strategy_audit,
    }
    combat_strategy_issues = [
        item for item in strategic_issues
        if item.get("kind") in _COMBAT_STRATEGY_ISSUE_KINDS
    ]
    combat_strategy_review = {
        "status": (
            "issues" if combat_strategy_issues
            else "inconclusive"
            if combat_context["status"] == "inconclusive"
            else "not_applicable"
            if not combat_context["eligible"]
            else "clear"
        ),
        "eligible_records": combat_context["eligible"],
        "evaluated_records": combat_context["evaluated"],
        "unknown_records": combat_context["unknown"],
        "issue_count": len(combat_strategy_issues),
        "issue_kinds": dict(Counter(
            str(item.get("kind") or "unknown")
            for item in combat_strategy_issues
        )),
        "issues": combat_strategy_issues,
        "decision_case_corpus_includes_combat": False,
    }
    quality_dimensions = {
        "protocol_correctness": protocol_correctness,
        "mechanics_coverage": mechanics_coverage,
        "strategy_quality": strategy_quality,
    }
    audit_status = (
        "issues"
        if any(
            item["status"] == "issues"
            for item in quality_dimensions.values()
        )
        else "clear"
        if all(
            item["status"] == "clear"
            for item in quality_dimensions.values()
        )
        else "inconclusive"
    )
    anomalies = _rank_anomalies(
        issues, review_findings, mechanics_summary, audit_coverage
    )
    anomaly_counts = Counter(
        item.get("severity") for item in anomalies
    )
    eligible_unknown_count = sum(
        int(summary.get("unknown", 0))
        for summary in audit_coverage.values()
    )
    observed_acts = [
        value for value in (
            _int_or_none(record.get("act")) for record in matching
        )
        if value is not None and value >= 0
    ]
    report = {
        "schema_version": 2,
        "audit_engine_sha256": audit_engine_sha256(),
        "decision_hash": decision_hash,
        "attempt_id": attempt_id,
        "records": len(cohort),
        "audit_status": audit_status,
        "issue_count": len(issues),
        "review_finding_count": len(review_findings),
        "eligible_unknown_count": eligible_unknown_count,
        "observed_max_act": max(observed_acts, default=None),
        "record_type_counts": dict(record_type_counts),
        "known_record_types": known_record_types,
        "unknown_record_types": unknown_record_types,
        "audit_coverage": audit_coverage,
        "protocol_correctness": protocol_correctness,
        "mechanics_coverage": mechanics_coverage,
        "strategy_quality": strategy_quality,
        "combat_strategy_review": combat_strategy_review,
        "quality_dimensions": quality_dimensions,
        "protocol_events": sum(protocol_event_counts.values()),
        "protocol_event_counts": dict(protocol_event_counts),
        "model_advice": {
            "records": len(model_advice_records),
            "status_counts": dict(model_status_counts),
            "remote_consultations": len(remote_model_advice_records),
            "valid_recommendations": len(valid_model_advice_records),
            "remote_valid_recommendations": len(
                remote_valid_model_advice_records
            ),
            "consultation_outcome_counts": dict(consultation_outcomes),
            "semantic_choice_changes": semantic_choice_changes,
            "local_confirmations": local_confirmations,
            "protocol_confirmations": protocol_confirmations,
            "review_no_choice_change": review_no_choice_change,
            "semantic_adoption_denominator": semantic_adoption_denominator,
            "semantic_adoption_rate": round(
                semantic_choice_changes / semantic_adoption_denominator,
                6,
            ) if semantic_adoption_denominator else None,
            "model_agreements": model_agreements,
            "effective_overrides": effective_overrides,
            "conflicts": model_conflicts["conflicts"],
            "conflict_outcome_counts": model_conflicts[
                "conflict_outcome_counts"
            ],
            "conflict_details": model_conflicts["details"],
            "by_decision_type": model_conflicts["by_decision_type"],
            "by_status": model_conflicts["by_status"],
            "error_counts": dict(model_error_counts),
            "circuit_suppressions": circuit_suppressions,
            "budget_suppressions": budget_suppressions,
            "advisor_disabled_suppressions": advisor_disabled_suppressions,
            "suppressed_consultations": suppressed_consultations,
            "applied": sum(
                bool(item.get("applied")) for item in model_advice_records
            ),
            "fallbacks": sum(
                str(item.get("status")) in {
                    "fallback", "stale", "low_confidence", "regret_cap"
                }
                for item in model_advice_records
            ),
            "local_cache_hits": sum(
                bool(item.get("local_cache_hit"))
                for item in model_advice_records
            ),
            "prompt_cache_hit_tokens": cache_hit_tokens,
            "prompt_cache_miss_tokens": cache_miss_tokens,
            "prompt_cache_hit_ratio": (
                round(
                    cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens),
                    6,
                )
                if cache_hit_tokens + cache_miss_tokens else None
            ),
            "estimated_cost_usd": round(sum(
                _safe_nonnegative_float(item.get("estimated_cost_usd"))
                for item in model_usage + warmup_usage
            ), 8),
            "cache_warmups": len(cache_warmup_records),
            "cache_warmup_cost_usd": round(sum(
                _safe_nonnegative_float(item.get("estimated_cost_usd"))
                for item in warmup_usage
            ), 8),
            "latency_ms": {
                "maximum": max(all_model_latencies, default=0),
                "average": round(
                    sum(all_model_latencies)
                    / max(1, len(model_advice_records)),
                    3,
                ),
            },
            "remote_latency_ms": {
                "maximum": max(remote_latencies, default=0),
                "average": round(
                    sum(remote_latencies)
                    / max(1, len(remote_latencies)),
                    3,
                ),
            },
            "healthy": not any(
                _is_model_contract_error(error)
                or error in {
                    "network_error", "auth", "account",
                    "advisor_exception",
                }
                or error == "timeout"
                or error in _CIRCUIT_SUPPRESSION_ERRORS
                for error in model_error_counts
            ),
        },
        "terminal_attempts": len(latest_termination_by_attempt),
        "termination_counts": dict(termination_counts),
        "operational_error_attempts": len(operational_errors_by_attempt),
        "operational_error_events": sum(
            len(items) for items in operational_errors_by_attempt.values()
        ),
        "tainted_attempt_ids": sorted(operational_errors_by_attempt),
        "issues": issues,
        "issue_counts": dict(reasons),
        "review_findings": review_findings,
        "review_finding_counts": dict(review_reasons),
        "review_resolutions": review_resolutions,
        "review_resolution_count": len(review_resolutions),
        "passive_doom_evidence": passive_doom_evidence,
        "anomalies": anomalies,
        "anomaly_counts_by_severity": dict(anomaly_counts),
        "terminal_observed": bool(latest_termination_by_attempt),
        "damage_consistency": {
            name: dict(counter)
            for name, counter in damage_consistency.items()
        },
        "turn_damage_metrics": turn_damage["metrics"],
        "turn_damage_summaries": turn_damage["summaries"],
        "relic_model_coverage": relic_report,
        "relic_strategy_audit": relic_strategy_audit,
        "power_model_coverage": power_report,
    }
    report["release_gate_passed"] = _release_gate_passed(report)
    return report


def _release_gate_passed(report):
    """Return the strict machine gate used by both API and CLI callers."""

    if not isinstance(report, dict):
        return False
    counts_clear = all(
        isinstance(report.get(key), int)
        and not isinstance(report.get(key), bool)
        and report.get(key) == 0
        for key in (
            "issue_count", "review_finding_count",
            "eligible_unknown_count", "oracle_disagreement_count",
        )
    )
    oracle = report.get("independent_oracle")
    replay = report.get("death_replay")
    death_observed = report.get("death_observed")
    expected_oracle_version = _EXPECTED_INDEPENDENT_ORACLE_VERSION
    expected_coverage_version = (
        _EXPECTED_ORACLE_COVERAGE_CONTRACT_VERSION
    )
    expected_coverage_keys = set(_EXPECTED_ORACLE_COVERAGE_KEYS)
    oracle_coverage = (
        oracle.get("coverage") if isinstance(oracle, dict) else None
    )
    oracle_coverage_clear = bool(
        isinstance(oracle_coverage, dict)
        and set(oracle_coverage) == expected_coverage_keys
        and all(
            isinstance(bucket, dict)
            and bucket.get("status") in {"clear", "not_applicable"}
            and _nonnegative_audit_count(bucket.get("unknown")) == 0
            and _nonnegative_audit_count(bucket.get("issues")) == 0
            for bucket in oracle_coverage.values()
        )
    )
    mechanism_contract_clear = bool(
        isinstance(oracle, dict)
        and not _base_game_mechanism_contract_violations(oracle)
    )
    oracle_clear = bool(
        isinstance(oracle, dict)
        and isinstance(expected_oracle_version, str)
        and oracle.get("oracle_version") == expected_oracle_version
        and oracle.get("coverage_contract_version")
        == expected_coverage_version
        and oracle.get("required_coverage_keys")
        == sorted(expected_coverage_keys)
        and oracle.get("binding_fields")
        == list(_EXPECTED_ORACLE_BINDING_FIELDS)
        and oracle_coverage_clear
        and mechanism_contract_clear
        and oracle.get("status") == "clear"
        and _nonnegative_audit_count(oracle.get("issue_count")) == 0
        and _nonnegative_audit_count(
            oracle.get("eligible_unknown_count")
        ) == 0
        and _nonnegative_audit_count(
            oracle.get("disagreement_count")
        ) == 0
        and report.get("oracle_disagreement_count")
        == oracle.get("disagreement_count")
    )
    replay_status_clear = bool(
        isinstance(death_observed, bool)
        and isinstance(replay, dict)
        and _nonnegative_audit_count(replay.get("issue_count")) == 0
        and _nonnegative_audit_count(
            replay.get("eligible_unknown_count")
        ) == 0
        and replay.get("death_observed") is death_observed
        and (
            replay.get("status") == "clear"
            if death_observed
            else replay.get("status") in {"clear", "not_applicable"}
        )
    )
    # Combat turns are deliberately outside the compact DecisionCase corpus,
    # so the dedicated combat channel is part of the release contract.  A
    # legacy/hand-built report that omits it must not regain a clear gate just
    # because the non-combat dimensions happen to be empty.
    combat_review = report.get("combat_strategy_review")
    combat_review_clear = bool(
        isinstance(combat_review, dict)
        and combat_review.get("status") in {"clear", "not_applicable"}
        and _nonnegative_audit_count(combat_review.get("issue_count")) == 0
        and _nonnegative_audit_count(
            combat_review.get("unknown_records")
        ) == 0
        and isinstance(
            combat_review.get("decision_case_corpus_includes_combat"),
            bool,
        )
        and combat_review.get("decision_case_corpus_includes_combat") is False
    )
    receipt = report.get("audit_receipt")
    receipt_clear = bool(
        isinstance(receipt, dict)
        and receipt.get("receipt_schema_version") == 1
        and receipt.get("exact_attempt_suffix") is True
        and receipt.get("attempt_id") == report.get("attempt_id")
        and receipt.get("decision_hash") == report.get("decision_hash")
        and isinstance(receipt.get("trace_sha256"), str)
        and len(receipt.get("trace_sha256")) == 64
        and receipt.get("artifacts_complete") is True
        and isinstance(receipt.get("artifact_sha256"), dict)
        and set(receipt.get("artifact_sha256"))
        == {"run_context", "run_result", "state", "selection"}
        and all(
            isinstance(value, str) and len(value) == 64
            for value in receipt.get("artifact_sha256").values()
        )
    )
    return bool(
        report.get("schema_version") == 2
        and report.get("audit_status") == "clear"
        and report.get("terminal_observed") is True
        and counts_clear
        and oracle_clear
        and replay_status_clear
        and combat_review_clear
        and receipt_clear
        and all(
            (report.get(dimension) or {}).get("status") == "clear"
            for dimension in (
                "protocol_correctness", "mechanics_coverage",
                "strategy_quality",
            )
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=Path("autoplay.log"))
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument(
        "--require-clear",
        action="store_true",
        help=(
            "exit 2 unless protocol, mechanics, and strategy quality are "
            "all clear"
        ),
    )
    args = parser.parse_args(argv)
    if args.require_clear and not args.attempt_id:
        parser.error("--require-clear requires --attempt-id")
    try:
        report = (
            audit_attempt_trace(
                args.trace, args.decision_hash, args.attempt_id
            )
            if args.attempt_id
            else audit_records(
                load_jsonl(
                    args.trace,
                    decision_hash=args.decision_hash,
                ),
                args.decision_hash,
            )
        )
    except TraceAuditError as exc:
        report = {
            "schema_version": 2,
            "audit_engine_sha256": audit_engine_sha256(),
            "decision_hash": args.decision_hash,
            "attempt_id": args.attempt_id,
            "audit_status": "issues",
            "issue_count": 1,
            "review_finding_count": 0,
            "eligible_unknown_count": 0,
            "oracle_disagreement_count": 0,
            "terminal_observed": False,
            "issues": [{
                "kind": "attempt_trace_contract_violation",
                "severity": "P1",
                "error": str(exc),
            }],
            "release_gate_passed": False,
            "audit_receipt": {
                "receipt_schema_version": 1,
                "attempt_id": args.attempt_id,
                "decision_hash": args.decision_hash,
                "exact_attempt_suffix": False,
                "trace_path": str(args.trace.resolve()),
            },
        }
    report["release_gate_passed"] = _release_gate_passed(report)
    print(json.dumps(report, ensure_ascii=False))
    if report.get("audit_status") == "issues" and not args.require_clear:
        return 1
    return (
        2
        if args.require_clear and not report["release_gate_passed"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
