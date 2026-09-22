"""Offline, attempt-scoped review gate for audited Heart cohorts.

The live controller never imports this module.  Reviews run only after the
controller has exited and read the isolated artifacts for the exact attempts
named by ``cohort-report.json``.  A missing field is evidence loss, not a
reason to infer that an attempt was healthy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path

import death_replay as _death_replay
import independent_oracle as _independent_oracle


def _current_audit_engine_sha256():
    """Load the auditor digest without touching any attempt trace."""

    try:
        import strategy_audit
        return strategy_audit.audit_engine_sha256()
    except Exception:
        return None


SCHEMA_VERSION = 2
POLICY_VERSION = "fast-policy-v5"
BATCH_SIZE = 6
REVIEW_SIX_WITHOUT_HIDDEN = "six_without_hidden"
REVIEW_ACT4_FAILURE = "act4_without_heart"
REVIEW_HEART_VICTORY = "heart_victory_final"
REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP = "twelve_without_hidden_deep"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SOURCE_DIGEST_KEYS = frozenset({
    "trace",
    "context",
    "result",
    "state",
    "selection",
    "audit",
    "replay",
    "controller_exit",
})
HISTORY_AUDIT_FIELDS = (
    "schema_version", "policy_version", "attempt_id", "run_id", "seed",
    "character", "ascension_level", "run_type", "decision_hash",
    "performance_hash", "controller_hash", "selection_id", "selection_digest",
    "terminal_state_seq",
    "termination_kind", "generated_at", "audit_status",
    "release_gate_passed", "protocol_events", "operational_error_attempts",
    "operational_error_events", "protocol_correctness", "mechanics_coverage",
    "strategy_quality", "issue_count", "review_finding_count",
    "oracle_disagreement_count", "eligible_unknown",
    "model_conflict_count", "model_advice", "independent_oracle",
    "death_replay", "death_observed", "code_changed",
    "operational_error_evidence",
)
TRACE_SUPPORT_BINDING_FIELDS = (
    "policy_version", "attempt_id", "run_id", "seed", "character",
    "ascension_level", "run_type", "decision_hash", "controller_hash",
    "selection_id", "selection_digest",
)
ORACLE_REQUIRED_COVERAGE_KEYS = frozenset({
    "protocol_binding",
    "authoritative_state_envelopes",
    "canonical_choice_completeness",
    "candidate_consequences",
    "choice_selection",
    "strategy_conflict_review",
    "observable_state_deltas",
    "combat_choice_transitions",
    "combat_choice_settlements",
    "damage_consistency",
    "true_combat_end",
    "terminal_authority",
    "act4_authority",
    "heart_defeat_authority",
})

_KNOWN_TRACE_TYPES = {
    "controller_start",
    "decision",
    "model_advice",
    "cache_warmup",
    "transition_settle",
    "protocol_event",
    "terminal_result",
}


class CohortReviewError(ValueError):
    """Raised when a requested review cannot be proved from exact artifacts."""


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _attempt_directory(attempt_root, attempt_id):
    if (
        not _nonempty(attempt_id)
        or attempt_id in {".", ".."}
        or "/" in attempt_id
        or "\\" in attempt_id
    ):
        raise CohortReviewError("attempt_id is not a safe path component")
    return Path(attempt_root) / attempt_id


def _load_json(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CohortReviewError(f"missing artifact: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CohortReviewError(f"unreadable artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise CohortReviewError(f"artifact is not an object: {path.name}")
    return value


def _digest(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise CohortReviewError(f"cannot hash artifact: {Path(path).name}") from exc
    return digest.hexdigest()


def _read_bytes(path, label):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise CohortReviewError(f"missing or unreadable {label}") from exc


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_trace(path):
    records = []
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        raise CohortReviewError("isolated autoplay.log is missing") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CohortReviewError(
                    f"isolated autoplay.log line {line_number} is malformed"
                ) from exc
            if not isinstance(value, dict):
                raise CohortReviewError(
                    f"isolated autoplay.log line {line_number} is not an object"
                )
            records.append(value)
    if not records:
        raise CohortReviewError("isolated autoplay.log is empty")
    return records


def _load_history(path):
    records = []
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        raise CohortReviewError("run-history.jsonl is missing") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CohortReviewError(
                    f"run-history.jsonl line {line_number} is malformed"
                ) from exc
            if not isinstance(value, dict):
                raise CohortReviewError(
                    f"run-history.jsonl line {line_number} is not an object"
                )
            records.append(value)
    return records


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _history_chain(records, expected, result, audit, controller_exit):
    attempt_id = expected["attempt_id"]
    typed = {
        "terminal_result": [],
        "run_audit": [],
        "controller_exit": [],
    }
    for index, record in enumerate(records):
        record_type = record.get("record_type")
        if record.get("attempt_id") == attempt_id and record_type in typed:
            typed[record_type].append((index, record))
    if any(len(items) != 1 for items in typed.values()):
        raise CohortReviewError(
            "history terminal/audit/controller-exit chain is not unique"
        )
    terminal_index, terminal = typed["terminal_result"][0]
    audit_index, history_audit = typed["run_audit"][0]
    exit_index, history_exit = typed["controller_exit"][0]
    if not terminal_index < audit_index < exit_index:
        raise CohortReviewError(
            "history terminal/audit/controller-exit chain is out of order"
        )
    if any(
        terminal_index < index < exit_index
        and record.get("record_type") == "terminal_result"
        and record.get("attempt_id") != attempt_id
        for index, record in enumerate(records)
    ):
        raise CohortReviewError(
            "history controller exit landed after the next terminal"
        )
    expected_rows = {
        "terminal_result": {**result, "record_type": "terminal_result"},
        "run_audit": {
            **{field: audit.get(field) for field in HISTORY_AUDIT_FIELDS},
            "record_type": "run_audit",
        },
        "controller_exit": controller_exit,
    }
    observed_rows = {
        "terminal_result": terminal,
        "run_audit": history_audit,
        "controller_exit": history_exit,
    }
    for name, value in expected_rows.items():
        if _canonical_json(observed_rows[name]) != _canonical_json(value):
            raise CohortReviewError(f"history {name} differs from artifact")
    return {
        "terminal_index": terminal_index,
        "audit_index": audit_index,
        "controller_exit_index": exit_index,
    }


def _binding(attempt):
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": attempt.get("policy_version"),
        "attempt_id": attempt.get("attempt_id"),
        "run_id": attempt.get("run_id"),
        "seed": attempt.get("seed"),
        "character": attempt.get("character"),
        "ascension_level": attempt.get("ascension_level"),
        "run_type": attempt.get("run_type"),
        "decision_hash": attempt.get("decision_hash"),
        "controller_hash": attempt.get("controller_hash"),
        "selection_id": attempt.get("selection_id"),
        "selection_digest": attempt.get("selection_digest"),
        "terminal_state_seq": attempt.get("terminal_state_seq"),
    }


def _require_binding(artifact, expected, name, *, state=False):
    for field, wanted in expected.items():
        if state and field == "schema_version":
            if artifact.get("protocol_version") != 2:
                raise CohortReviewError(f"{name} protocol_version mismatch")
            continue
        observed = artifact.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise CohortReviewError(f"{name} {field} binding mismatch")


def _require_terminal_authority(state, result, expected):
    """Cross-check the raw terminal frame against the terminal result.

    The result is a derived artifact.  A clear review therefore needs the
    protocol frame to independently carry the same terminal facts rather than
    merely agreeing with cohort-report.json.
    """

    if state.get("phase") != "GAME_OVER":
        raise CohortReviewError("terminal-state phase is not GAME_OVER")
    game = state.get("game_state")
    if not isinstance(game, dict) or game.get("screen_type") != "GAME_OVER":
        raise CohortReviewError(
            "terminal-state is not authoritative GAME_OVER"
        )
    if result.get("schema_version") != SCHEMA_VERSION:
        raise CohortReviewError("run-result schema_version mismatch")
    if result.get("policy_version") != POLICY_VERSION:
        raise CohortReviewError("run-result policy_version mismatch")
    if result.get("run_type") != "standard":
        raise CohortReviewError("run-result run_type is not standard")
    if type(result.get("ascension_level")) is not int or result.get(
        "ascension_level"
    ) != 0:
        raise CohortReviewError("run-result ascension_level is not A0")
    if result.get("class") != result.get("character"):
        raise CohortReviewError("run-result class/character mismatch")

    facts = {
        "seed": result.get("seed"),
        "class": result.get("character"),
        "ascension_level": result.get("ascension_level"),
        "act": result.get("act"),
        "floor": result.get("floor"),
        "current_hp": result.get("current_hp"),
        "max_hp": result.get("max_hp"),
        "deck": result.get("deck"),
        "relics": result.get("relics"),
        "potions": result.get("potions"),
        "run_victory": result.get("victory"),
        "heart_defeated": result.get("heart_defeated"),
    }
    for field, wanted in facts.items():
        observed = game.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise CohortReviewError(
                f"terminal-state game_state.{field} mismatch"
            )
    keys = result.get("keys")
    if (
        not isinstance(keys, dict)
        or set(keys) != {"ruby", "emerald", "sapphire"}
        or any(type(value) is not bool for value in keys.values())
    ):
        raise CohortReviewError("run-result keys are invalid")
    for key_name, state_field in (
        ("ruby", "has_ruby_key"),
        ("emerald", "has_emerald_key"),
        ("sapphire", "has_sapphire_key"),
    ):
        if game.get(state_field) is not keys[key_name]:
            raise CohortReviewError(
                f"terminal-state game_state.{state_field} mismatch"
            )
    screen = game.get("screen_state")
    if isinstance(screen, dict) and "victory" in screen:
        if (
            type(screen.get("victory")) is not bool
            or screen.get("victory") is not result.get("victory")
        ):
            raise CohortReviewError(
                "terminal-state screen_state.victory mismatch"
            )
    expected_run_id = ":".join(str(value) for value in (
        game.get("class"), game.get("ascension_level"), game.get("seed")
    ))
    if result.get("run_id") != expected_run_id:
        raise CohortReviewError("run-result run_id is not state-derived")
    if result.get("heart_defeated") is True:
        if result.get("victory") is not True or result.get("act", 0) < 4:
            raise CohortReviewError(
                "Heart defeat is not an Act4 authoritative victory"
            )
    if result.get("seed") != expected.get("seed"):
        raise CohortReviewError("terminal seed differs from cohort binding")


def _trace_terminal_for_replay(records, expected, result, state):
    """Return the one authoritative, fully cross-bound trace terminal.

    ``run-result.json`` is a derived artifact and must never be promoted into
    a trace record by adding ``record_type``.  The replay authority is the
    original terminal_result row in the isolated trace, cross-checked against
    both the result artifact and the raw terminal protocol frame.
    """

    terminals = [
        (index, record)
        for index, record in enumerate(records)
        if isinstance(record, dict)
        and record.get("record_type") == "terminal_result"
    ]
    if len(terminals) != 1:
        raise CohortReviewError(
            "isolated trace terminal_result is missing or not unique"
        )
    terminal_index, terminal = terminals[0]
    if terminal_index != len(records) - 1:
        raise CohortReviewError(
            "isolated trace terminal_result is out of order"
        )

    _require_binding(terminal, expected, "isolated terminal trace")
    if (
        terminal.get("schema_version") != SCHEMA_VERSION
        or terminal.get("authoritative_game_over") is not True
        or terminal.get("screen_type") != "GAME_OVER"
        or terminal.get("termination_kind") != "game_over"
        or terminal.get("state_seq") != expected["terminal_state_seq"]
        or terminal.get("terminal_state_seq")
        != expected["terminal_state_seq"]
        or terminal.get("ascension_level") != 0
        or terminal.get("run_type") != "standard"
        or terminal.get("policy_version") != POLICY_VERSION
    ):
        raise CohortReviewError(
            "isolated trace terminal_result authority is incomplete"
        )

    result_fields = (
        "attempt_id", "run_id", "seed", "character",
        "ascension_level", "run_type", "decision_hash",
        "controller_hash", "policy_version", "selection_id",
        "terminal_state_seq", "state_seq", "victory",
        "heart_defeated", "act", "floor", "current_hp", "keys",
        "termination_kind", "authoritative_game_over", "screen_type",
    )
    for field in result_fields:
        observed = terminal.get(field)
        wanted = result.get(field)
        if (
            field not in terminal
            or field not in result
            or type(observed) is not type(wanted)
            or observed != wanted
        ):
            raise CohortReviewError(
                f"isolated trace terminal_result {field} differs from "
                "run-result"
            )

    game = state.get("game_state") if isinstance(state, dict) else None
    if not isinstance(game, dict):
        raise CohortReviewError("terminal-state game_state is missing")
    state_fields = {
        "seed": "seed",
        "character": "class",
        "ascension_level": "ascension_level",
        "act": "act",
        "floor": "floor",
        "current_hp": "current_hp",
        "victory": "run_victory",
        "heart_defeated": "heart_defeated",
    }
    for terminal_field, state_field in state_fields.items():
        observed = terminal.get(terminal_field)
        wanted = game.get(state_field)
        if (
            terminal_field not in terminal
            or state_field not in game
            or type(observed) is not type(wanted)
            or observed != wanted
        ):
            raise CohortReviewError(
                "isolated trace terminal_result "
                f"{terminal_field} differs from terminal-state"
            )
    keys = terminal.get("keys")
    if (
        not isinstance(keys, dict)
        or set(keys) != {"ruby", "emerald", "sapphire"}
        or any(type(value) is not bool for value in keys.values())
    ):
        raise CohortReviewError(
            "isolated trace terminal_result keys are invalid"
        )
    for key_name, state_field in (
        ("ruby", "has_ruby_key"),
        ("emerald", "has_emerald_key"),
        ("sapphire", "has_sapphire_key"),
    ):
        if (
            state_field not in game
            or type(game.get(state_field)) is not bool
            or game.get(state_field) is not keys[key_name]
        ):
            raise CohortReviewError(
                "isolated trace terminal_result keys differ from "
                "terminal-state"
            )
    if (
        state.get("phase") != "GAME_OVER"
        or game.get("screen_type") != "GAME_OVER"
        or state.get("state_seq") != terminal["state_seq"]
    ):
        raise CohortReviewError(
            "isolated trace terminal_result differs from authoritative "
            "terminal-state"
        )
    return terminal


def _validate_replay_artifact(
    replay, audit, expected, result, state, records
):
    """Rebuild, validate, and bind the persisted death replay.

    Equality between ``death-replay.json`` and the object embedded in the
    production audit is necessary but not sufficient: both are producer
    artifacts and can be corrupted together.  Recompute from the isolated
    trace and its original authoritative terminal_result record before
    accepting either copy.
    """

    if replay.get("schema_version") != 1:
        raise CohortReviewError("death-replay schema_version mismatch")
    replay_binding = {
        field: value for field, value in expected.items()
        if field != "schema_version"
    }
    _require_binding(replay, replay_binding, "death-replay")
    embedded = audit.get("death_replay")
    if not isinstance(embedded, dict):
        raise CohortReviewError("run-audit death_replay object is missing")
    if _canonical_json(replay) != _canonical_json(embedded):
        raise CohortReviewError(
            "death-replay differs from run-audit embedded object"
        )
    terminal = _trace_terminal_for_replay(
        records, expected, result, state
    )
    try:
        recomputed = _death_replay.build_death_replay(
            records, terminal=terminal
        )
    except Exception as exc:
        raise CohortReviewError(
            "death-replay independent rebuild failed"
        ) from exc
    if _canonical_json(recomputed) != _canonical_json(replay):
        raise CohortReviewError(
            "death-replay differs from independent trace rebuild"
        )
    issues = replay.get("issues")
    unknowns = replay.get("unknowns")
    if not isinstance(issues, list) or not isinstance(unknowns, list):
        raise CohortReviewError("death-replay issue lists are invalid")
    if (
        type(replay.get("issue_count")) is not int
        or replay.get("issue_count") != len(issues)
        or type(replay.get("eligible_unknown_count")) is not int
        or replay.get("eligible_unknown_count") != len(unknowns)
    ):
        raise CohortReviewError("death-replay counts are inconsistent")

    is_death = (
        result.get("victory") is False
        and isinstance(result.get("current_hp"), (int, float))
        and not isinstance(result.get("current_hp"), bool)
        and result.get("current_hp") <= 0
    )
    is_heart_victory = (
        result.get("victory") is True
        and result.get("heart_defeated") is True
    )
    if is_death:
        expected_kind = "death"
    elif is_heart_victory:
        expected_kind = "act4_terminal"
    else:
        expected_kind = "not_applicable"
    if expected_kind == "not_applicable":
        expected_status = "not_applicable"
    elif issues:
        expected_status = "issues"
    elif unknowns:
        expected_status = "inconclusive"
    else:
        expected_status = "clear"
    if (
        replay.get("replay_kind") != expected_kind
        or replay.get("status") != expected_status
        or (
            expected_kind == "not_applicable"
            and (
                replay.get("issue_count") != 0
                or replay.get("eligible_unknown_count") != 0
            )
        )
    ):
        raise CohortReviewError("death-replay terminal classification mismatch")
    if expected_status == "clear":
        turns = replay.get("turns")
        if (
            not isinstance(turns, list)
            or not turns
            or any(
                not isinstance(turn, dict) or turn.get("complete") is not True
                for turn in turns
            )
        ):
            raise CohortReviewError("death-replay complete turns are missing")
    expected_death_observed = is_death
    if audit.get("death_observed") is not expected_death_observed:
        raise CohortReviewError("run-audit death_observed mismatch")
    if replay.get("death_observed") is not expected_death_observed:
        raise CohortReviewError("death-replay death_observed mismatch")
    return _sha256_bytes(_canonical_json(replay))


def _validate_oracle_contract(oracle, expected):
    """Validate the complete oracle-v2 result contract."""

    if not isinstance(oracle, dict):
        raise CohortReviewError("independent oracle object is missing")
    issues = oracle.get("issues")
    unknowns = oracle.get("unknowns")
    disagreement_kinds = oracle.get("disagreement_kinds")
    if (
        oracle.get("oracle_version") != "independent-oracle-v2"
        or oracle.get("coverage_contract_version") != 2
        or oracle.get("attempt_id") != expected.get("attempt_id")
        or oracle.get("decision_hash") != expected.get("decision_hash")
        or not isinstance(issues, list)
        or not isinstance(unknowns, list)
        or type(oracle.get("issue_count")) is not int
        or oracle.get("issue_count") != len(issues)
        or type(oracle.get("eligible_unknown_count")) is not int
        or oracle.get("eligible_unknown_count") != len(unknowns)
        or type(oracle.get("disagreement_count")) is not int
        or not isinstance(disagreement_kinds, list)
        or set(disagreement_kinds) != set(
            _independent_oracle.DISAGREEMENT_KINDS
        )
        or len(disagreement_kinds) != len(set(disagreement_kinds))
        or oracle.get("disagreement_count") != sum(
            1 for item in issues
            if isinstance(item, dict)
            and item.get("kind") in set(disagreement_kinds)
        )
    ):
        raise CohortReviewError("independent oracle contract is invalid")
    expected_oracle_status = (
        "issues" if issues else "inconclusive" if unknowns else "clear"
    )
    if oracle.get("status") != expected_oracle_status:
        raise CohortReviewError("independent oracle status is inconsistent")
    required = oracle.get("required_coverage_keys")
    coverage = oracle.get("coverage")
    if (
        not isinstance(required, list)
        or set(required) != ORACLE_REQUIRED_COVERAGE_KEYS
        or len(required) != len(ORACLE_REQUIRED_COVERAGE_KEYS)
        or not isinstance(coverage, dict)
        or set(coverage) != ORACLE_REQUIRED_COVERAGE_KEYS
    ):
        raise CohortReviewError("independent oracle coverage contract mismatch")
    total_eligible = 0
    for name in sorted(ORACLE_REQUIRED_COVERAGE_KEYS):
        bucket = coverage.get(name)
        if not isinstance(bucket, dict):
            raise CohortReviewError(
                f"independent oracle coverage {name} is invalid"
            )
        counts = []
        for field in ("eligible", "evaluated", "unknown", "issues"):
            value = bucket.get(field)
            if type(value) is not int or value < 0:
                raise CohortReviewError(
                    f"independent oracle coverage {name}.{field} is invalid"
                )
            counts.append(value)
        eligible, evaluated, unknown, issues = counts
        if evaluated > eligible:
            raise CohortReviewError(
                f"independent oracle coverage {name} is invalid"
            )
        expected_status = (
            "issues" if issues else
            "inconclusive" if unknown else
            "clear" if eligible else "not_applicable"
        )
        if bucket.get("status") != expected_status:
            raise CohortReviewError(
                f"independent oracle coverage {name} status mismatch"
            )
        if expected_oracle_status == "clear" and evaluated != eligible:
            raise CohortReviewError(
                f"independent oracle coverage {name} is not complete"
            )
        total_eligible += eligible
    if total_eligible <= 0:
        raise CohortReviewError("independent oracle audited no eligible evidence")
    binding_fields = oracle.get("binding_fields")
    if (
        not isinstance(binding_fields, list)
        or len(binding_fields) != len(set(binding_fields))
        or not {
            "attempt_id", "run_id", "seed", "character",
            "ascension_level", "decision_hash", "controller_hash",
            "policy_version", "selection_id", "terminal_state_seq",
        } <= set(binding_fields)
    ):
        raise CohortReviewError("independent oracle binding contract mismatch")
    return _sha256_bytes(_canonical_json(oracle))


def _validate_oracle_artifact(audit, expected, records, artifacts):
    """Independently rerun the oracle and bind its canonical result.

    A structurally valid embedded ``clear`` object is still only a claim.
    Re-executing the standalone oracle over the isolated trace and immutable
    sidecars prevents a forged audit and oracle object from self-proving.
    """

    oracle = audit.get("independent_oracle")
    digest = _validate_oracle_contract(oracle, expected)
    try:
        recomputed = _independent_oracle.audit_records(
            records,
            expected.get("decision_hash"),
            expected.get("attempt_id"),
            artifacts=artifacts,
        )
    except Exception as exc:
        raise CohortReviewError(
            "independent oracle rebuild failed"
        ) from exc
    if _canonical_json(recomputed) != _canonical_json(oracle):
        raise CohortReviewError(
            "independent oracle differs from isolated trace rebuild"
        )
    return digest


def _controller_exit_evidence(
    controller_exit, expected, attempt, directory, result
):
    """Independently bind the outer receipt to its captured byte streams."""

    _require_binding(controller_exit, expected, "controller-exit")
    if controller_exit.get("record_type") != "controller_exit":
        raise CohortReviewError("controller-exit record_type mismatch")
    if controller_exit.get("controller_exit_status") != "clear":
        raise CohortReviewError("controller-exit is not clear")
    if type(controller_exit.get("exit_code")) is not int:
        raise CohortReviewError("controller-exit exit_code is not an integer")
    if controller_exit.get("exit_code") != 0:
        raise CohortReviewError("controller-exit exit_code is not zero")

    stdout = _read_bytes(
        Path(directory) / "controller.stdout.log", "controller stdout"
    )
    stderr = _read_bytes(
        Path(directory) / "controller.stderr.log", "controller stderr"
    )
    observed = {
        "stdout_size": len(stdout),
        "stdout_sha256": _sha256_bytes(stdout),
        "stderr_size": len(stderr),
        "stderr_sha256": _sha256_bytes(stderr),
    }
    for field, value in observed.items():
        actual = controller_exit.get(field)
        if type(actual) is not type(value) or actual != value:
            raise CohortReviewError(f"controller-exit {field} mismatch")
    if stderr or observed["stderr_sha256"] != EMPTY_SHA256:
        raise CohortReviewError("controller stderr is not empty")
    try:
        stdout_text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CohortReviewError("controller stdout is not UTF-8") from exc
    stdout_lines = stdout_text.splitlines()
    if len(stdout_lines) != 1 or not stdout_lines[0].strip():
        raise CohortReviewError(
            "controller stdout is not exactly one terminal JSON line"
        )
    try:
        stdout_result = json.loads(stdout_lines[0])
    except json.JSONDecodeError as exc:
        raise CohortReviewError("controller stdout terminal JSON is invalid") from exc
    stdout_canonical = json.dumps(
        stdout_result,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result_canonical = json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if stdout_canonical != result_canonical:
        raise CohortReviewError(
            "controller stdout terminal summary differs from run-result"
        )
    semantic = _sha256_bytes(stdout_canonical)
    semantic_evidence = {
        "stdout_line_count": 1,
        "stdout_semantic_sha256": semantic,
    }
    for field, value in semantic_evidence.items():
        actual = controller_exit.get(field)
        if type(actual) is not type(value) or actual != value:
            raise CohortReviewError(f"controller-exit {field} mismatch")
    for field in ("freeze_manifest_sha256", "freeze_source_digest"):
        if not _is_sha256(controller_exit.get(field)):
            raise CohortReviewError(f"controller-exit {field} is invalid")
    freeze_generated_at = controller_exit.get("freeze_generated_at")
    if (
        type(freeze_generated_at) not in {int, float}
        or freeze_generated_at <= 0
    ):
        raise CohortReviewError(
            "controller-exit freeze_generated_at is invalid"
        )

    cohort_expectations = {
        "controller_exit_clean": True,
        "controller_exit_code": 0,
        "controller_stdout_size": observed["stdout_size"],
        "controller_stderr_size": 0,
        "controller_stdout_sha256": observed["stdout_sha256"],
        "controller_stderr_sha256": observed["stderr_sha256"],
        "controller_stdout_line_count": 1,
        "controller_stdout_semantic_sha256": semantic,
        "freeze_manifest_sha256": controller_exit[
            "freeze_manifest_sha256"
        ],
        "freeze_source_digest": controller_exit["freeze_source_digest"],
        "freeze_generated_at": freeze_generated_at,
    }
    for field, value in cohort_expectations.items():
        actual = attempt.get(field)
        if type(actual) is not type(value) or actual != value:
            raise CohortReviewError(f"cohort {field} mismatch")
    return {
        "status": "clear",
        "exit_code": 0,
        **observed,
        **semantic_evidence,
        "freeze_manifest_sha256": controller_exit["freeze_manifest_sha256"],
        "freeze_source_digest": controller_exit["freeze_source_digest"],
        "freeze_generated_at": freeze_generated_at,
    }


def _audit_is_clear(audit):
    if (
        audit.get("audit_status") != "clear"
        or audit.get("release_gate_passed") is not True
        or audit.get("audit_engine_sha256")
        != _current_audit_engine_sha256()
    ):
        return False
    for field in (
        "protocol_correctness", "mechanics_coverage", "strategy_quality",
    ):
        if not isinstance(audit.get(field), dict):
            return False
        if audit[field].get("status") != "clear":
            return False
    for field in (
        "issue_count", "review_finding_count", "eligible_unknown_count",
        "oracle_disagreement_count",
    ):
        if type(audit.get(field)) is not int or audit[field] != 0:
            return False
    if audit.get("eligible_unknown", 0) != 0:
        return False
    if audit.get("code_changed") is not False:
        return False
    oracle = audit.get("independent_oracle")
    replay = audit.get("death_replay")
    if (
        not isinstance(oracle, dict)
        or oracle.get("status") != "clear"
        or oracle.get("issue_count") != 0
        or oracle.get("eligible_unknown_count") != 0
        or oracle.get("disagreement_count") != 0
    ):
        return False
    if (
        not isinstance(replay, dict)
        or replay.get("status") not in {"clear", "not_applicable"}
        or replay.get("issue_count") != 0
        or replay.get("eligible_unknown_count") != 0
    ):
        return False
    mechanics = audit.get("mechanics_coverage") or {}
    if any(mechanics.get(field) for field in (
        "unsupported_ids", "unclassified_ids", "unsupported_occurrences",
        "unclassified_occurrences",
    )):
        return False
    if audit.get("issues") or audit.get("review_findings"):
        return False
    return True


def _selected_attempts(cohort, reason):
    if not isinstance(cohort, dict) or cohort.get("schema_version") != 2:
        raise CohortReviewError("cohort report is missing schema_version=2")
    attempts = cohort.get("attempts")
    if not isinstance(attempts, list):
        raise CohortReviewError("cohort report attempts are missing")
    if reason == REVIEW_SIX_WITHOUT_HIDDEN:
        if len(attempts) < BATCH_SIZE:
            raise CohortReviewError("six-attempt review requested too early")
        selected = attempts[-BATCH_SIZE:]
        if any(item.get("hidden_entry") is not False for item in selected):
            raise CohortReviewError("six-attempt batch contains a hidden entry")
        return selected
    if reason == REVIEW_ACT4_FAILURE:
        if not attempts:
            raise CohortReviewError("Act4 review has no attempt")
        latest = attempts[-1]
        if latest.get("hidden_entry") is not True:
            raise CohortReviewError("latest attempt did not enter Act4")
        if latest.get("heart_defeated") is not False:
            raise CohortReviewError("Act4 review cannot replace Heart acceptance")
        return [latest]
    if reason == REVIEW_HEART_VICTORY:
        if not attempts:
            raise CohortReviewError("Heart review has no attempt")
        latest = attempts[-1]
        if (
            latest.get("hidden_entry") is not True
            or latest.get("heart_defeated") is not True
            or latest.get("victory") is not True
        ):
            raise CohortReviewError(
                "Heart review requires an authoritative Heart victory"
            )
        return [latest]
    if reason == REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP:
        if len(attempts) < BATCH_SIZE * 2:
            raise CohortReviewError("deep review requested before two batches")
        selected = attempts[-BATCH_SIZE * 2:]
        if any(item.get("hidden_entry") is not False for item in selected):
            raise CohortReviewError("deep review window contains a hidden entry")
        return selected
    raise CohortReviewError("unsupported cohort review reason")


def review_requirement(cohort):
    """Return the next mandatory offline review reason, or ``None``."""

    attempts = cohort.get("attempts") if isinstance(cohort, dict) else None
    if not isinstance(attempts, list) or not attempts:
        return None
    latest = attempts[-1]
    if latest.get("heart_defeated") is True:
        return REVIEW_HEART_VICTORY
    if latest.get("hidden_entry") is True and latest.get("heart_defeated") is False:
        return REVIEW_ACT4_FAILURE
    if len(attempts) % BATCH_SIZE == 0:
        batch = attempts[-BATCH_SIZE:]
        if len(batch) == BATCH_SIZE and not any(
            item.get("hidden_entry") is True for item in batch
        ):
            if len(attempts) >= BATCH_SIZE * 2 and not any(
                item.get("hidden_entry") is True
                for item in attempts[-BATCH_SIZE * 2:]
            ):
                return REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP
            return REVIEW_SIX_WITHOUT_HIDDEN
    return None


def _trace_summary(records, expected):
    record_types = Counter()
    decision_types = Counter()
    protocol_events = []
    model_conflicts = []
    route = []
    campfires = []
    shops = []
    boss_relics = []
    events = []
    potion_actions = []
    potion_changes = []
    unknown_types = []
    act_floor_counts = Counter()
    boss_distribution = Counter()
    boss_entries = []
    act4_combats = []
    key_transitions = []
    resource_curve = []
    candidate_surfaces = []
    prediction_observations = []
    combat_seen = set()
    last_resource_point = None
    for index, record in enumerate(records):
        record_type = record.get("record_type")
        record_types[str(record_type)] += 1
        if record_type not in _KNOWN_TRACE_TYPES:
            unknown_types.append({"index": index, "record_type": record_type})
        for field in ("attempt_id", "decision_hash", "controller_hash"):
            if (
                field not in record
                or type(record.get(field)) is not type(expected.get(field))
                or record.get(field) != expected.get(field)
            ):
                raise CohortReviewError(
                    f"isolated trace record {index} {field} binding mismatch"
                )
        if record_type == "terminal_result":
            _require_binding(
                record, expected, "isolated terminal trace"
            )
        if record_type == "protocol_event":
            protocol_events.append({
                "state_seq": record.get("before_seq"),
                "event": record.get("event"),
            })
        if record_type != "decision":
            continue
        decision = record.get("decision") or {}
        dtype = str(
            decision.get("decision_type")
            or record.get("screen_type")
            or record.get("phase")
            or "unknown"
        ).upper()
        decision_types[dtype] += 1
        before_authority = record.get("authoritative_state_before") or {}
        before_game = before_authority.get("game_state") or {}
        combat = before_game.get("combat_state") or {}
        monsters = record.get("monsters_before")
        if not isinstance(monsters, list):
            monsters = combat.get("monsters") or []
        player = record.get("player_before")
        if not isinstance(player, dict):
            player = combat.get("player") or {}
        act = record.get("act")
        floor = record.get("floor")
        if act is None:
            act = before_game.get("act")
        if floor is None:
            floor = before_game.get("floor")
        is_act4_support = type(act) is int and act >= 4
        if is_act4_support or any(
            "heart" in str(
                monster.get("id", monster.get("name", ""))
            ).casefold()
            for monster in monsters if isinstance(monster, dict)
        ):
            _require_binding(
                record,
                {
                    field: expected.get(field)
                    for field in TRACE_SUPPORT_BINDING_FIELDS
                },
                f"isolated Act4 trace record {index}",
            )
        act_floor_counts[f"{act}:{floor}"] += 1
        compact = {
            "state_seq": record.get("before_seq"),
            "act": record.get("act"),
            "floor": record.get("floor"),
            "action": record.get("action"),
            "selected_choice_id": decision.get("selected_choice_id"),
            "reason": decision.get("reason"),
            "selection_source": decision.get("selection_source"),
            "local_margin": decision.get("local_margin"),
            "risk": decision.get("risk") or decision.get("route_risk"),
            "candidate_count": (
                len(decision.get("candidates"))
                if isinstance(decision.get("candidates"), list) else None
            ),
        }
        decision_context = record.get("decision_context") or {}
        act_boss = decision_context.get("act_boss") or before_game.get(
            "act_boss"
        )
        if _nonempty(act_boss):
            boss_distribution[str(act_boss)] += 1

        outcome = record.get("decision_outcome") or {}
        keys_before = outcome.get("keys_before")
        keys_after = outcome.get("keys_after")
        if (
            isinstance(keys_before, dict)
            and isinstance(keys_after, dict)
            and keys_before != keys_after
        ):
            key_transitions.append({
                "state_seq": record.get("before_seq"),
                "act": act,
                "floor": floor,
                "before": keys_before,
                "after": keys_after,
            })
        potion_delta = outcome.get("potions")
        if isinstance(potion_delta, dict) and any(
            potion_delta.get(field) for field in (
                "added", "removed", "changed"
            )
        ):
            potion_changes.append({
                "state_seq": record.get("before_seq"),
                "act": act,
                "floor": floor,
                **potion_delta,
            })

        observable = record.get("observable_state_before")
        if not isinstance(observable, dict):
            observable = decision_context.get("observable_state")
        if not isinstance(observable, dict) and before_game:
            observable = {
                "current_hp": before_game.get("current_hp"),
                "max_hp": before_game.get("max_hp"),
                "gold": before_game.get("gold"),
                "block": before_game.get("block"),
                "deck": before_game.get("deck") or [],
                "relics": before_game.get("relics") or [],
                "potions": before_game.get("potions") or [],
                "keys": before_game.get("keys") or {},
            }
        if isinstance(observable, dict):
            point = {
                "state_seq": record.get("before_seq"),
                "act": act,
                "floor": floor,
                "current_hp": observable.get("current_hp"),
                "max_hp": observable.get("max_hp"),
                "gold": observable.get("gold"),
                "deck_size": len(observable.get("deck") or []),
                "relic_count": len(observable.get("relics") or []),
                "potions": [
                    item.get("id") for item in observable.get("potions") or []
                    if isinstance(item, dict)
                ],
                "keys": observable.get("keys") or {},
            }
            signature = tuple(
                json.dumps(point.get(field), sort_keys=True)
                for field in (
                    "act", "floor", "current_hp", "max_hp", "gold",
                    "deck_size", "relic_count", "potions", "keys",
                )
            )
            if signature != last_resource_point:
                resource_curve.append(point)
                last_resource_point = signature

        legal_choices = record.get("legal_choices_before")
        if isinstance(legal_choices, list):
            candidate_surfaces.append({
                "state_seq": record.get("before_seq"),
                "decision_type": dtype,
                "legal_choice_count": len(legal_choices),
                "choice_ids": [
                    row.get("choice_id") for row in legal_choices
                    if isinstance(row, dict)
                ],
                "selected_choice_ids": list(
                    record.get("selected_choice_ids") or []
                ),
                "settlement_status": (
                    record.get("authoritative_choice_settlement") or {}
                ).get("status"),
            })

        projected = record.get("projected_hp_loss_before")
        hp_delta = outcome.get("hp_delta")
        if projected is not None or hp_delta is not None:
            prediction_observations.append({
                "state_seq": record.get("before_seq"),
                "action": record.get("action"),
                "projected_hp_loss": projected,
                "observed_hp_delta": hp_delta,
                "true_combat_end": (
                    decision.get("true_combat_end")
                    if "true_combat_end" in decision else None
                ),
            })

        if monsters:
            combat_key = (
                record.get("combat_trace_id"),
                act,
                floor,
                tuple(
                    monster.get("enemy_instance_id") or monster.get("id")
                    for monster in monsters if isinstance(monster, dict)
                ),
            )
            if combat_key not in combat_seen:
                combat_seen.add(combat_key)
                monster_ids = [
                    monster.get("id") or monster.get("name")
                    for monster in monsters if isinstance(monster, dict)
                ]
                entry = {
                    "state_seq": record.get("before_seq"),
                    "act": act,
                    "floor": floor,
                    "room_type": before_game.get("room_type"),
                    "act_boss": act_boss,
                    "monsters": monster_ids,
                    "player_hp": player.get("current_hp"),
                    "player_max_hp": player.get("max_hp"),
                    "potions": [
                        potion.get("id")
                        for potion in (
                            record.get("potions_before")
                            or before_game.get("potions")
                            or []
                        )
                        if isinstance(potion, dict)
                    ],
                }
                is_boss = (
                    str(before_game.get("room_type") or "").upper() == "BOSS"
                    or any(
                        monster.get("is_boss") is True
                        or str(monster.get("type") or "").upper() == "BOSS"
                        for monster in monsters if isinstance(monster, dict)
                    )
                )
                if is_boss:
                    boss_entries.append(entry)
                    for monster_id in monster_ids:
                        if monster_id is not None:
                            boss_distribution[str(monster_id)] += 1
                if type(act) is int and act >= 4:
                    act4_combats.append({
                        **entry,
                        "monster_powers": [
                            {
                                "monster": monster.get("id"),
                                "powers": [
                                    power.get("id") or power.get("name")
                                    for power in monster.get("powers") or []
                                    if isinstance(power, dict)
                                ],
                            }
                            for monster in monsters
                            if isinstance(monster, dict)
                        ],
                    })
        if dtype == "MAP":
            route.append(compact)
        elif dtype in {"REST", "CAMPFIRE"}:
            campfires.append(compact)
        elif "SHOP" in dtype:
            shops.append(compact)
        elif dtype in {"BOSS_RELIC", "BOSS_REWARD"}:
            boss_relics.append(compact)
        elif dtype == "EVENT":
            events.append(compact)
        if str(record.get("action") or "").lower() == "potion":
            potion_actions.append(compact)
        advice = decision.get("model_advice")
        if isinstance(advice, dict) and advice.get("conflict") is True:
            model_conflicts.append({
                **compact,
                "local_choice": advice.get("local_choice"),
                "model_choice": advice.get("model_choice"),
                "final_choice": advice.get("final_choice"),
                "confidence": advice.get("confidence"),
            })
    return {
        "record_type_counts": dict(sorted(record_types.items())),
        "decision_type_counts": dict(sorted(decision_types.items())),
        "protocol_events": protocol_events,
        "model_conflicts": model_conflicts,
        "route_decisions": route,
        "campfire_choices": campfires,
        "shop_choices": shops,
        "boss_relic_choices": boss_relics,
        "event_choices": events,
        "potion_actions": potion_actions,
        "potion_changes": potion_changes,
        "unknown_record_types": unknown_types,
        "act_floor_distribution": dict(sorted(act_floor_counts.items())),
        "boss_distribution": dict(sorted(boss_distribution.items())),
        "boss_entries": boss_entries,
        "act4_combats": act4_combats,
        "key_transitions": key_transitions,
        "resource_curve": resource_curve,
        "candidate_surfaces": candidate_surfaces,
        "prediction_observations": prediction_observations,
    }


def _audit_items_matching(audit, *tokens):
    rows = []
    for source in ("issues", "review_findings"):
        for item in audit.get(source) or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").casefold()
            if any(token in kind for token in tokens):
                rows.append({"source": source, **item})
    return rows


def _audit_signal_summary(audit):
    coverage = audit.get("audit_coverage") or {}
    candidate_coverage = {
        name: value for name, value in coverage.items()
        if any(token in str(name).casefold() for token in (
            "candidate", "choice", "coverage", "noncombat",
        ))
    }
    return {
        "false_combat_end": _audit_items_matching(
            audit, "false_combat_end"
        ),
        "prediction_actual_errors": _audit_items_matching(
            audit, "prediction", "underprediction", "overprediction",
            "damage_mismatch", "turn_loss_component",
        ),
        "high_value_resource_loss": _audit_items_matching(
            audit, "high_value", "resource_loss", "actionable_potion",
            "potion_without_marginal_gain",
        ),
        "candidate_coverage_gaps": _audit_items_matching(
            audit, "candidate", "choice_coverage", "visible_choice",
            "canonical_choice",
        ),
        "unsupported_unclassified": _audit_items_matching(
            audit, "unsupported", "unclassified"
        ),
        "candidate_coverage": candidate_coverage,
        "mechanics_coverage": audit.get("mechanics_coverage"),
        "combat_strategy_review": audit.get("combat_strategy_review"),
        "review_findings": list(audit.get("review_findings") or []),
        "issues": list(audit.get("issues") or []),
        "independent_oracle": audit.get("independent_oracle"),
        "oracle_disagreement_count": audit.get(
            "oracle_disagreement_count"
        ),
        "model_advice": audit.get("model_advice"),
    }


def _repository_root(attempt_root):
    attempt_root = Path(attempt_root).resolve()
    if (
        attempt_root.name.casefold() == "attempts"
        and attempt_root.parent.name.casefold() == "logs"
    ):
        return attempt_root.parent.parent
    return attempt_root


def _valid_fixed_seed(value):
    return type(value) is int or (
        isinstance(value, str) and bool(value.strip())
    )


def _fixed_seed_evidence_is_clear(fixed, decision_hash, controller_hash):
    """Validate one complete, one-to-one fixed-seed comparison matrix."""

    if not (
        isinstance(fixed, dict)
        and fixed.get("schema_version") == 1
        and fixed.get("policy_version") == POLICY_VERSION
        and fixed.get("decision_hash") == decision_hash
        and fixed.get("controller_hash") == controller_hash
        and fixed.get("status") == "clear"
        and fixed.get("release_gate_passed") is True
        and type(fixed.get("issue_count")) is int
        and fixed.get("issue_count") == 0
        and type(fixed.get("eligible_unknown_count")) is int
        and fixed.get("eligible_unknown_count") == 0
    ):
        return False
    prescribed = fixed.get("required_seeds")
    observed = fixed.get("seeds")
    comparisons = fixed.get("comparisons")
    if not all(isinstance(value, list) for value in (
        prescribed, observed, comparisons
    )):
        return False
    if not prescribed or not observed or not comparisons:
        return False
    if not all(_valid_fixed_seed(seed) for seed in prescribed + observed):
        return False
    # JSON seed scalars are hashable; retain type in the identity so 1 and
    # "1" cannot collapse into the same prescribed seed.
    prescribed_ids = [(type(seed).__name__, seed) for seed in prescribed]
    observed_ids = [(type(seed).__name__, seed) for seed in observed]
    if (
        len(set(prescribed_ids)) != len(prescribed_ids)
        or len(set(observed_ids)) != len(observed_ids)
        or Counter(prescribed_ids) != Counter(observed_ids)
    ):
        return False
    comparison_ids = []
    for comparison in comparisons:
        if not isinstance(comparison, dict) or not comparison:
            return False
        seed = comparison.get("seed")
        if not _valid_fixed_seed(seed):
            return False
        comparison_ids.append((type(seed).__name__, seed))
        if not (
            comparison.get("status") == "clear"
            and comparison.get("release_gate_passed") is True
            and type(comparison.get("issue_count")) is int
            and comparison.get("issue_count") == 0
            and type(comparison.get("eligible_unknown_count")) is int
            and comparison.get("eligible_unknown_count") == 0
        ):
            return False
    return (
        len(set(comparison_ids)) == len(comparison_ids)
        and Counter(comparison_ids) == Counter(prescribed_ids)
    )


def _load_deep_evidence(repository_root, decision_hash, controller_hash):
    """Load independent fixed-case/seed evidence for the two-batch gate."""

    repository_root = Path(repository_root)
    findings = []
    evidence = {"source_digests": {}}
    specifications = (
        ("decision_case_replay", repository_root / "decision-case-replay.json"),
        ("fixed_seed_replay", repository_root / "fixed-seed-replay.json"),
    )
    loaded = {}
    for name, path in specifications:
        try:
            value = _load_json(path)
            evidence["source_digests"][name] = _digest(path)
            loaded[name] = value
        except CohortReviewError as exc:
            findings.append({
                "severity": "P1",
                "kind": f"deep_review_{name}_missing",
                "eligible_unknown": True,
                "reason": str(exc),
            })

    replay = loaded.get("decision_case_replay")
    if replay is not None:
        valid = False
        try:
            # Reuse the freeze gate's independent per-case accounting rather
            # than maintaining a second, shallower definition of "clear".
            import freeze_manifest
        except ImportError:
            freeze_manifest = None
        if freeze_manifest is not None:
            try:
                freeze_manifest._validate_replay_evidence(
                    replay, decision_hash
                )
                valid = True
            except freeze_manifest.FreezeManifestError:
                valid = False
        if not valid:
            findings.append({
                "severity": "P1",
                "kind": "deep_review_decision_case_replay_not_clear",
                "eligible_unknown": True,
            })
        evidence["decision_case_replay"] = replay

    fixed = loaded.get("fixed_seed_replay")
    if fixed is not None:
        valid = _fixed_seed_evidence_is_clear(
            fixed, decision_hash, controller_hash
        )
        if not valid:
            findings.append({
                "severity": "P1",
                "kind": "deep_review_fixed_seed_replay_not_clear",
                "eligible_unknown": True,
            })
        evidence["fixed_seed_replay"] = fixed
    return evidence, findings


def build_review(cohort, attempt_root, reason, *, generated_at=None):
    """Review only the exact isolated attempts selected from one cohort."""

    selected = _selected_attempts(cohort, reason)
    decision_hash = cohort.get("decision_hash")
    controller_hash = cohort.get("controller_hash")
    if not _nonempty(decision_hash) or not _nonempty(controller_hash):
        raise CohortReviewError("cohort hashes are missing")
    attempt_root = Path(attempt_root)
    repository_root = _repository_root(attempt_root)
    history_path = repository_root / "run-history.jsonl"
    history_records = _load_history(history_path)
    history_digest = _digest(history_path)
    reviewed = []
    findings = []
    repeated = Counter()
    for attempt in selected:
        expected = _binding(attempt)
        if (
            expected["decision_hash"] != decision_hash
            or expected["controller_hash"] != controller_hash
            or not _nonempty(expected["attempt_id"])
        ):
            raise CohortReviewError("cohort attempt binding is inconsistent")
        directory = _attempt_directory(attempt_root, expected["attempt_id"])
        paths = {
            "trace": directory / "autoplay.log",
            "context": directory / "run-context.json",
            "result": directory / "run-result.json",
            "state": directory / "terminal-state.json",
            "selection": directory / "selection.json",
            "audit": directory / "run-audit.json",
            "replay": directory / "death-replay.json",
            "controller_exit": directory / "controller-exit.json",
        }
        context = _load_json(paths["context"])
        result = _load_json(paths["result"])
        state = _load_json(paths["state"])
        selection = _load_json(paths["selection"])
        audit = _load_json(paths["audit"])
        replay = _load_json(paths["replay"])
        controller_exit = _load_json(paths["controller_exit"])
        records = _load_trace(paths["trace"])
        _require_binding(context, expected, "run-context")
        _require_binding(result, expected, "run-result")
        _require_binding(state, expected, "terminal-state", state=True)
        _require_binding(selection, expected, "selection")
        _require_binding(audit, expected, "run-audit")
        if state.get("state_seq") != expected["terminal_state_seq"]:
            raise CohortReviewError(
                "terminal-state state_seq binding mismatch"
            )
        if (
            result.get("termination_kind") != "game_over"
            or result.get("authoritative_game_over") is not True
            or result.get("screen_type") != "GAME_OVER"
            or result.get("state_seq") != expected["terminal_state_seq"]
            or result.get("victory") is not attempt.get("victory")
            or result.get("heart_defeated") is not attempt.get(
                "heart_defeated"
            )
            or result.get("act") != attempt.get("act")
            or result.get("floor") != attempt.get("floor")
            or result.get("keys") != attempt.get("keys")
            or result.get("actions") != attempt.get("actions")
        ):
            raise CohortReviewError("run-result terminal authority mismatch")
        observed_max_act = result.get("observed_max_act")
        if type(observed_max_act) is not int:
            raise CohortReviewError("run-result observed_max_act is invalid")
        if (observed_max_act >= 4) is not attempt.get("hidden_entry"):
            raise CohortReviewError(
                "run-result hidden-entry evidence mismatch"
            )
        _require_terminal_authority(state, result, expected)
        controller_exit_evidence = _controller_exit_evidence(
            controller_exit, expected, attempt, directory, result
        )
        history_chain = _history_chain(
            history_records, expected, result, audit, controller_exit
        )
        history_chain["history_sha256"] = history_digest
        # Validate every record binding before any derived channel can use the
        # trace as evidence, especially for Act4/Heart claims.
        trace_summary = _trace_summary(records, expected)
        replay_canonical_sha256 = _validate_replay_artifact(
            replay, audit, expected, result, state, records
        )
        oracle_canonical_sha256 = _validate_oracle_artifact(
            audit,
            expected,
            records,
            {
                "run_context": context,
                "run_result": result,
                "state": state,
                "selection": selection,
            },
        )
        current_audit_engine_sha256 = _current_audit_engine_sha256()
        if audit.get("audit_engine_sha256") != current_audit_engine_sha256:
            findings.append({
                "severity": "P1",
                "kind": "stale_audit_engine_artifact",
                "attempt_id": expected["attempt_id"],
                "reported": audit.get("audit_engine_sha256"),
                "current": current_audit_engine_sha256,
                "eligible_unknown": True,
            })
        if not _audit_is_clear(audit):
            findings.append({
                "severity": "P1",
                "kind": "attempt_audit_not_clear",
                "attempt_id": expected["attempt_id"],
            })
        terminal_count = trace_summary["record_type_counts"].get(
            "terminal_result", 0
        )
        decision_count = trace_summary["record_type_counts"].get(
            "decision", 0
        )
        for kind, observed in (
            ("terminal_result", terminal_count),
            ("decision", decision_count),
        ):
            if observed < 1:
                findings.append({
                    "severity": "P1",
                    "kind": f"cohort_trace_{kind}_missing",
                    "eligible_unknown": True,
                    "attempt_id": expected["attempt_id"],
                })
        for unknown in trace_summary["unknown_record_types"]:
            findings.append({
                "severity": "P1",
                "kind": "unknown_trace_record_type",
                "attempt_id": expected["attempt_id"],
                **unknown,
            })
        if attempt.get("hidden_entry") is True:
            heart_combat_observed = False
            if not trace_summary["act4_combats"]:
                findings.append({
                    "severity": "P1",
                    "kind": "act4_combat_evidence_missing",
                    "eligible_unknown": True,
                    "attempt_id": expected["attempt_id"],
                })
            for combat in trace_summary["act4_combats"]:
                monsters = [
                    str(value or "").casefold()
                    for value in combat.get("monsters") or []
                ]
                if any("heart" in value for value in monsters):
                    heart_combat_observed = True
                    powers = {
                        str(power or "").casefold()
                        for row in combat.get("monster_powers") or []
                        for power in row.get("powers") or []
                    }
                    for required_power in ("beatofdeath", "invincible"):
                        if not any(
                            required_power in power.replace(" ", "")
                            for power in powers
                        ):
                            findings.append({
                                "severity": "P1",
                                "kind": (
                                    "act4_heart_power_evidence_missing"
                                ),
                                "eligible_unknown": True,
                                "required_power": required_power,
                                "attempt_id": expected["attempt_id"],
                                "state_seq": combat.get("state_seq"),
                            })
            if (
                reason == REVIEW_HEART_VICTORY
                and not heart_combat_observed
            ):
                findings.append({
                    "severity": "P1",
                    "kind": "heart_victory_combat_evidence_missing",
                    "eligible_unknown": True,
                    "attempt_id": expected["attempt_id"],
                })
        for item in audit.get("issues") or []:
            repeated[str(item.get("kind") or "unknown_issue")] += 1
        for item in audit.get("review_findings") or []:
            repeated[str(item.get("kind") or "unknown_review") ] += 1
        audit_signals = _audit_signal_summary(audit)
        reviewed.append({
            **expected,
            "act": attempt.get("act"),
            "floor": attempt.get("floor"),
            "victory": attempt.get("victory"),
            "heart_defeated": attempt.get("heart_defeated"),
            "hidden_entry": attempt.get("hidden_entry"),
            "keys": attempt.get("keys"),
            "actions": attempt.get("actions"),
            "terminal_hp": result.get("current_hp"),
            "terminal_potions": result.get("potions") or [],
            "terminal_deck": result.get("deck") or [],
            "terminal_relics": result.get("relics") or [],
            "death_position": None if result.get("victory") else {
                "act": result.get("act"), "floor": result.get("floor")
            },
            "audit_status": audit.get("audit_status"),
            "issue_count": audit.get("issue_count"),
            "review_finding_count": audit.get("review_finding_count"),
            "eligible_unknown_count": audit.get("eligible_unknown_count"),
            "oracle_disagreement_count": audit.get(
                "oracle_disagreement_count"
            ),
            "mechanics": audit.get("mechanics_coverage"),
            "model_advice": audit.get("model_advice"),
            "death_replay": replay,
            "death_replay_canonical_sha256": replay_canonical_sha256,
            "independent_oracle_canonical_sha256": (
                oracle_canonical_sha256
            ),
            "audit_signals": audit_signals,
            "key_progress": {
                "transitions": trace_summary["key_transitions"],
                "terminal": attempt.get("keys"),
            },
            "boss_readiness": trace_summary["boss_entries"],
            "build_resource_curve": trace_summary["resource_curve"],
            "controller_exit": controller_exit_evidence,
            "history_chain": history_chain,
            "trace_summary": trace_summary,
            "source_digests": {
                name: _digest(path) for name, path in paths.items()
            },
        })
    deep_review = None
    if reason == REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP:
        for item in reviewed:
            if not item["trace_summary"]["route_decisions"]:
                findings.append({
                    "severity": "P1",
                    "kind": "deep_review_route_evidence_missing",
                    "eligible_unknown": True,
                    "attempt_id": item["attempt_id"],
                })
            if not item["build_resource_curve"]:
                findings.append({
                    "severity": "P1",
                    "kind": "deep_review_resource_curve_missing",
                    "eligible_unknown": True,
                    "attempt_id": item["attempt_id"],
                })
            if not isinstance(item.get("terminal_deck"), list) or not item[
                "terminal_deck"
            ]:
                findings.append({
                    "severity": "P1",
                    "kind": "deep_review_build_evidence_missing",
                    "eligible_unknown": True,
                    "attempt_id": item["attempt_id"],
                })
        deep_review, deep_findings = _load_deep_evidence(
            _repository_root(attempt_root), decision_hash, controller_hash
        )
        findings.extend(deep_findings)

    act_floor_distribution = Counter()
    boss_distribution = Counter()
    aggregate = {
        "boss_entries": [],
        "key_progress": [],
        "route_decisions": [],
        "campfire_choices": [],
        "shop_choices": [],
        "boss_relic_choices": [],
        "event_choices": [],
        "potion_actions": [],
        "potion_changes": [],
        "model_conflicts": [],
        "false_combat_end": [],
        "prediction_actual_errors": [],
        "unsupported_unclassified": [],
        "high_value_resource_loss": [],
        "candidate_coverage_gaps": [],
        "review_findings": [],
        "oracle_reports": [],
        "death_replays": [],
        "build_resource_curves": [],
        "act4_combats": [],
        "audit_coverage": [],
    }
    for item in reviewed:
        attempt_id = item["attempt_id"]
        trace = item["trace_summary"]
        act_floor_distribution.update(trace["act_floor_distribution"])
        boss_distribution.update(trace["boss_distribution"])
        for field in (
            "boss_entries", "route_decisions", "campfire_choices",
            "shop_choices", "boss_relic_choices", "event_choices",
            "potion_actions", "potion_changes", "model_conflicts",
            "act4_combats",
        ):
            aggregate[field].extend(
                {"attempt_id": attempt_id, **row}
                for row in trace[field]
            )
        aggregate["key_progress"].append({
            "attempt_id": attempt_id,
            **item["key_progress"],
        })
        aggregate["build_resource_curves"].append({
            "attempt_id": attempt_id,
            "curve": item["build_resource_curve"],
            "terminal_deck": item["terminal_deck"],
            "terminal_relics": item["terminal_relics"],
        })
        signals = item["audit_signals"]
        aggregate["audit_coverage"].append({
            "attempt_id": attempt_id,
            "candidate_coverage": signals["candidate_coverage"],
            "mechanics_coverage": signals["mechanics_coverage"],
            "candidate_surfaces": trace["candidate_surfaces"],
            "prediction_observations": trace[
                "prediction_observations"
            ],
            "model_advice": signals["model_advice"],
        })
        model_advice = signals.get("model_advice") or {}
        for conflict in model_advice.get("conflict_details") or []:
            if isinstance(conflict, dict):
                aggregate["model_conflicts"].append({
                    "attempt_id": attempt_id,
                    **conflict,
                })
        for field in (
            "false_combat_end", "prediction_actual_errors",
            "unsupported_unclassified", "high_value_resource_loss",
            "candidate_coverage_gaps", "review_findings",
        ):
            aggregate[field].extend(
                {"attempt_id": attempt_id, **row}
                for row in signals[field]
                if isinstance(row, dict)
            )
        aggregate["oracle_reports"].append({
            "attempt_id": attempt_id,
            "oracle_disagreement_count": signals[
                "oracle_disagreement_count"
            ],
            "independent_oracle": signals["independent_oracle"],
        })
        aggregate["death_replays"].append({
            "attempt_id": attempt_id,
            "replay": item["death_replay"],
        })

    for finding in findings:
        repeated[str(finding.get("kind") or "unknown_finding")] += 1
    issue_count = len(findings)
    eligible_unknown_count = sum(
        item.get("eligible_unknown") is True for item in findings
    )
    oracle_disagreement_count = sum(
        int(item.get("oracle_disagreement_count") or 0)
        for item in aggregate["oracle_reports"]
    )
    review_status = "clear" if issue_count == 0 else "issues"
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "review_kind": reason,
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "generated_at": time.time() if generated_at is None else generated_at,
        "attempt_ids": [item["attempt_id"] for item in reviewed],
        "reviewed_attempt_count": len(reviewed),
        "review_status": review_status,
        "release_gate_passed": review_status == "clear",
        "issue_count": issue_count,
        "review_finding_count": issue_count,
        "eligible_unknown_count": eligible_unknown_count,
        "oracle_disagreement_count": oracle_disagreement_count,
        "code_changed": False,
        "hidden_entry_count": sum(
            item["hidden_entry"] is True for item in reviewed
        ),
        "death_positions": [
            {"attempt_id": item["attempt_id"], **item["death_position"]}
            for item in reviewed if item["death_position"] is not None
        ],
        "repeated_failure_patterns": dict(sorted(repeated.items())),
        "act_floor_distribution": dict(sorted(act_floor_distribution.items())),
        "boss_distribution": dict(sorted(boss_distribution.items())),
        "cohort_evidence": aggregate,
        "deep_review_evidence": deep_review,
        "findings": findings,
        "attempts": reviewed,
    }


def build_blocking_review(cohort, reason, error, *, generated_at=None):
    """Persist a machine-readable P1 when source review cannot be built."""

    selected = _selected_attempts(cohort, reason)
    message = str(error).strip() or "cohort review evidence is unavailable"
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "review_kind": reason,
        "decision_hash": cohort.get("decision_hash"),
        "controller_hash": cohort.get("controller_hash"),
        "generated_at": time.time() if generated_at is None else generated_at,
        "attempt_ids": [item.get("attempt_id") for item in selected],
        "reviewed_attempt_count": len(selected),
        "review_status": "inconclusive",
        "release_gate_passed": False,
        "issue_count": 1,
        "review_finding_count": 1,
        "eligible_unknown_count": 1,
        "oracle_disagreement_count": 0,
        "code_changed": False,
        "hidden_entry_count": sum(
            item.get("hidden_entry") is True for item in selected
        ),
        "death_positions": [],
        "repeated_failure_patterns": {
            "cohort_review_evidence_unavailable": 1,
        },
        "findings": [{
            "severity": "P1",
            "kind": "cohort_review_evidence_unavailable",
            "reason": message,
        }],
        # Absence of attempt evidence is deliberate: this report cannot pass
        # validate_review_binding or the launch gate.
        "attempts": [],
    }


def _review_envelope_matches(review, cohort, reason, expected_ids):
    return (
        isinstance(review, dict)
        and type(review.get("schema_version")) is int
        and review.get("schema_version") == SCHEMA_VERSION
        and review.get("policy_version") == POLICY_VERSION
        and review.get("review_kind") == reason
        and review.get("decision_hash") == cohort.get("decision_hash")
        and review.get("controller_hash") == cohort.get("controller_hash")
        and review.get("attempt_ids") == expected_ids
        and type(review.get("reviewed_attempt_count")) is int
        and review.get("reviewed_attempt_count") == len(expected_ids)
        and review.get("code_changed") is False
    )


def _reviewed_attempt_matches(item, selected, attempt_root=None):
    if not isinstance(item, dict):
        return False
    expected = _binding(selected)
    for field, value in expected.items():
        actual = item.get(field)
        if type(actual) is not type(value) or actual != value:
            return False
    for field in (
        "act", "floor", "victory", "heart_defeated", "hidden_entry",
        "keys", "actions",
    ):
        value = selected.get(field)
        actual = item.get(field)
        if type(actual) is not type(value) or actual != value:
            return False

    digests = item.get("source_digests")
    if not isinstance(digests, dict) or set(digests) != SOURCE_DIGEST_KEYS:
        return False
    if not all(_is_sha256(value) for value in digests.values()):
        return False
    replay_semantic_digest = item.get("death_replay_canonical_sha256")
    if not _is_sha256(replay_semantic_digest):
        return False
    oracle_semantic_digest = item.get(
        "independent_oracle_canonical_sha256"
    )
    if not _is_sha256(oracle_semantic_digest):
        return False
    exit_evidence = item.get("controller_exit")
    if not isinstance(exit_evidence, dict):
        return False
    expected_exit = {
        "status": "clear",
        "exit_code": 0,
        "stdout_size": selected.get("controller_stdout_size"),
        "stdout_sha256": selected.get("controller_stdout_sha256"),
        "stdout_line_count": selected.get("controller_stdout_line_count"),
        "stdout_semantic_sha256": selected.get(
            "controller_stdout_semantic_sha256"
        ),
        "stderr_size": 0,
        "stderr_sha256": selected.get("controller_stderr_sha256"),
        "freeze_manifest_sha256": selected.get(
            "freeze_manifest_sha256"
        ),
        "freeze_source_digest": selected.get("freeze_source_digest"),
        "freeze_generated_at": selected.get("freeze_generated_at"),
    }
    for field, value in expected_exit.items():
        actual = exit_evidence.get(field)
        if type(actual) is not type(value) or actual != value:
            return False
    if not _is_sha256(exit_evidence.get("stdout_sha256")):
        return False
    if exit_evidence.get("stdout_line_count") != 1:
        return False
    if not _is_sha256(exit_evidence.get("stdout_semantic_sha256")):
        return False
    for field in ("freeze_manifest_sha256", "freeze_source_digest"):
        if not _is_sha256(exit_evidence.get(field)):
            return False
    if (
        type(exit_evidence.get("freeze_generated_at")) not in {int, float}
        or exit_evidence["freeze_generated_at"] <= 0
    ):
        return False
    if selected.get("controller_exit_clean") is not True:
        return False
    if selected.get("controller_exit_code") != 0:
        return False
    if selected.get("controller_stderr_size") != 0:
        return False
    history_chain = item.get("history_chain")
    if not isinstance(history_chain, dict):
        return False
    indices = [
        history_chain.get("terminal_index"),
        history_chain.get("audit_index"),
        history_chain.get("controller_exit_index"),
    ]
    if (
        any(type(value) is not int or value < 0 for value in indices)
        or not indices[0] < indices[1] < indices[2]
        or not _is_sha256(history_chain.get("history_sha256"))
    ):
        return False

    if attempt_root is None:
        return True
    try:
        directory = _attempt_directory(attempt_root, expected["attempt_id"])
        paths = {
            "trace": directory / "autoplay.log",
            "context": directory / "run-context.json",
            "result": directory / "run-result.json",
            "state": directory / "terminal-state.json",
            "selection": directory / "selection.json",
            "audit": directory / "run-audit.json",
            "replay": directory / "death-replay.json",
            "controller_exit": directory / "controller-exit.json",
        }
        if any(_digest(path) != digests[name] for name, path in paths.items()):
            return False
        controller_exit = _load_json(paths["controller_exit"])
        context = _load_json(paths["context"])
        result = _load_json(paths["result"])
        state = _load_json(paths["state"])
        selection = _load_json(paths["selection"])
        audit = _load_json(paths["audit"])
        replay = _load_json(paths["replay"])
        records = _load_trace(paths["trace"])
        if _sha256_bytes(_canonical_json(replay)) != replay_semantic_digest:
            return False
        if _validate_replay_artifact(
            replay, audit, expected, result, state, records
        ) != replay_semantic_digest:
            return False
        if _validate_oracle_artifact(
            audit,
            expected,
            records,
            {
                "run_context": context,
                "run_result": result,
                "state": state,
                "selection": selection,
            },
        ) != oracle_semantic_digest:
            return False
        observed_exit = _controller_exit_evidence(
            controller_exit, expected, selected, directory, result
        )
        repository_root = _repository_root(attempt_root)
        history_path = repository_root / "run-history.jsonl"
        history_records = _load_history(history_path)
        observed_chain = _history_chain(
            history_records, expected, result, audit, controller_exit
        )
        observed_chain["history_sha256"] = _digest(history_path)
    except CohortReviewError:
        return False
    return observed_exit == exit_evidence and observed_chain == history_chain


def validate_review_binding(review, cohort, attempt_root=None):
    """Return whether a review is current and exactly source-bound.

    This intentionally does not require a clear status.  The runner uses it to
    recognize an already-landed blocking review without rewriting evidence on
    every restart.  ``validate_review_gate`` adds the release requirements.
    """

    reason = review_requirement(cohort)
    if reason is None:
        return True
    try:
        selected = _selected_attempts(cohort, reason)
    except CohortReviewError:
        return False
    expected_ids = [item.get("attempt_id") for item in selected]
    if not _review_envelope_matches(review, cohort, reason, expected_ids):
        return False
    aggregate = review.get("cohort_evidence")
    required_aggregate = {
        "boss_entries", "key_progress", "route_decisions",
        "campfire_choices", "shop_choices", "boss_relic_choices",
        "event_choices", "potion_actions", "model_conflicts",
        "potion_changes",
        "false_combat_end", "prediction_actual_errors",
        "unsupported_unclassified", "high_value_resource_loss",
        "candidate_coverage_gaps", "review_findings", "oracle_reports",
        "death_replays", "build_resource_curves", "act4_combats",
        "audit_coverage",
    }
    if (
        not isinstance(aggregate, dict)
        or not required_aggregate <= set(aggregate)
        or any(not isinstance(aggregate[field], list) for field in required_aggregate)
    ):
        return False
    attempts = review.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != len(selected):
        return False
    attempts_match = all(
        _reviewed_attempt_matches(item, expected, attempt_root)
        for item, expected in zip(attempts, selected)
    )
    if not attempts_match:
        return False
    if reason != REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP:
        return True
    deep = review.get("deep_review_evidence")
    if not isinstance(deep, dict):
        return False
    source_digests = deep.get("source_digests")
    if not isinstance(source_digests, dict) or not all(
        _is_sha256(value) for value in source_digests.values()
    ):
        return False
    clear_deep_sources = set(source_digests) == {
        "decision_case_replay", "fixed_seed_replay"
    }
    if review.get("review_status") == "clear" and not clear_deep_sources:
        return False
    if attempt_root is not None:
        actual, actual_findings = _load_deep_evidence(
            _repository_root(attempt_root),
            cohort.get("decision_hash"),
            cohort.get("controller_hash"),
        )
        if actual != deep:
            return False
        if review.get("review_status") == "clear" and actual_findings:
            return False
        if review.get("review_status") != "clear":
            reported_kinds = {
                item.get("kind") for item in review.get("findings") or []
                if isinstance(item, dict)
            }
            if any(
                item.get("kind") not in reported_kinds
                for item in actual_findings
            ):
                return False
    return True


def validate_review_gate(review, cohort, attempt_root=None):
    """Return True only for a freshly re-proved, source-bound clear review.

    A serialized review is an index, not authority.  Release callers must
    supply the isolated attempt root so this function can rebuild the review
    from the audit/oracle/replay/controller/history artifacts and compare the
    complete canonical report.  Summary-only validation is never release
    evidence.
    """

    if review_requirement(cohort) is None:
        return True
    if attempt_root is None:
        return False
    if not validate_review_binding(review, cohort, attempt_root):
        return False
    if (
        review.get("review_status") != "clear"
        or review.get("release_gate_passed") is not True
    ):
        return False
    for field in (
        "issue_count", "review_finding_count", "eligible_unknown_count",
        "oracle_disagreement_count",
    ):
        if type(review.get(field)) is not int or review[field] != 0:
            return False
    generated_at = review.get("generated_at")
    if (
        type(generated_at) not in {int, float}
        or generated_at <= 0
    ):
        return False
    try:
        rebuilt = build_review(
            cohort,
            attempt_root,
            review_requirement(cohort),
            generated_at=generated_at,
        )
    except CohortReviewError:
        return False
    return _canonical_json(review) == _canonical_json(rebuilt)


def write_review(path, review):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(review, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort", type=Path, default=Path("cohort-report.json"))
    parser.add_argument("--attempt-root", type=Path, default=Path("logs/attempts"))
    parser.add_argument(
        "--reason",
        choices=(
            REVIEW_SIX_WITHOUT_HIDDEN,
            REVIEW_ACT4_FAILURE,
            REVIEW_HEART_VICTORY,
            REVIEW_TWELVE_WITHOUT_HIDDEN_DEEP,
        ),
        required=True,
    )
    parser.add_argument("--write", type=Path, default=Path("cohort-review.json"))
    args = parser.parse_args()
    try:
        cohort = _load_json(args.cohort)
        report = build_review(cohort, args.attempt_root, args.reason)
        write_review(args.write, report)
    except CohortReviewError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
