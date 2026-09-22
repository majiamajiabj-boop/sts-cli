"""Homogeneous-cohort character selection for autonomous Heart attempts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
import uuid
from collections import defaultdict
from pathlib import Path


CHARACTERS = ("IRONCLAD", "THE_SILENT", "DEFECT")
ALGORITHM = "beta-thompson-v1"
BOOTSTRAP_RUNS = 3
STARVATION_WINDOW = 6
COHORT_RUNS_PER_CHARACTER = 2
VALIDATION_BATCH_SIZE = 6
FOLLOWUP_VALIDATION_REASON = "followup_validation_batch"
FIXED_QUOTA_SEQUENCE = CHARACTERS * COHORT_RUNS_PER_CHARACTER
P0_ONLY_SUBCOHORT_SIZE = len(FIXED_QUOTA_SEQUENCE)
P0_ONLY_SUBCOHORTS = 4
P0_ONLY_MAX_ATTEMPTS = P0_ONLY_SUBCOHORT_SIZE * P0_ONLY_SUBCOHORTS
AUDIT_RECORD_TYPE = "run_audit"
TERMINAL_RECORD_TYPE = "terminal_result"
CONTROLLER_EXIT_RECORD_TYPE = "controller_exit"
POLICY_VERSION = "fast-policy-v5"
_HISTORY_PARSE_CACHE = {}


class HistoryValidationError(ValueError):
    """Raised when an append-only history cannot be parsed safely."""


def _valid_seed(seed):
    return type(seed) is int or (
        isinstance(seed, str) and bool(seed.strip())
    )


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def selection_digest(selection):
    """Digest every selector field, including diagnostic/non-core fields."""

    if not isinstance(selection, dict):
        raise ValueError("selection must be an object")
    return hashlib.sha256(_canonical(selection).encode("utf-8")).hexdigest()


def load_history(path):
    path = Path(path)
    if not path.exists():
        _HISTORY_PARSE_CACHE.pop(str(path.resolve()), None)
        return []
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except OSError:
        raise
    cache_key = str(path.resolve())
    content_sha256 = hashlib.sha256(raw).hexdigest()
    cached = _HISTORY_PARSE_CACHE.get(cache_key)
    if cached is not None and cached["sha256"] == content_sha256:
        # Return a new outer list so callers cannot append into the cache.
        # Every call still hashes the exact bytes, so same-size/same-mtime
        # rewrites cannot masquerade as the append-only history.
        return list(cached["records"])
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistoryValidationError(
                f"history line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise HistoryValidationError(
                f"history line {line_number} is not an object"
            )
        records.append(value)
    _HISTORY_PARSE_CACHE[cache_key] = {
        "sha256": content_sha256,
        "records": tuple(records),
    }
    return list(records)


def _strict_nonnegative_int(value):
    return type(value) is int and value >= 0


def _valid_sha256(value):
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _terminal_validation_error(record, decision_hash):
    """Return a fail-closed reason for an invalid terminal record."""

    if not isinstance(record, dict):
        return "terminal_not_object"
    if record.get("record_type") != TERMINAL_RECORD_TYPE:
        return "terminal_record_type"
    if record.get("termination_kind") != "game_over":
        return "termination_kind"
    if record.get("authoritative_game_over") is not True:
        return "authoritative_game_over"
    if str(record.get("screen_type") or "").upper() != "GAME_OVER":
        return "screen_type"
    if record.get("schema_version") != 2:
        return "schema_version"
    if record.get("policy_version") != POLICY_VERSION:
        return "policy_version"
    if record.get("goal_mode") != "HEART":
        return "goal_mode"
    if record.get("decision_hash") != decision_hash:
        return "decision_hash"
    if (
        "performance_hash" in record
        and not _nonempty_string(record.get("performance_hash"))
    ):
        return "performance_hash"
    if not _nonempty_string(record.get("controller_hash")):
        return "controller_hash"
    if not _nonempty_string(record.get("attempt_id")):
        return "attempt_id"
    if type(record.get("ascension_level")) is not int:
        return "ascension_level_type"
    if record.get("ascension_level") != 0:
        return "ascension_level"
    if record.get("run_type") != "standard":
        return "run_type"

    character = record.get("character")
    if character not in CHARACTERS:
        return "character"
    if "class" in record and record.get("class") != character:
        return "class_character_mismatch"
    seed = record.get("seed")
    if not _valid_seed(seed):
        return "seed"
    if record.get("run_id") != f"{character}:0:{seed}":
        return "run_id"
    if not _strict_nonnegative_int(record.get("state_seq")):
        return "state_seq"
    if not _strict_nonnegative_int(record.get("terminal_state_seq")):
        return "terminal_state_seq"
    if record.get("terminal_state_seq") != record.get("state_seq"):
        return "terminal_state_seq_binding"
    if not _strict_nonnegative_int(record.get("act")):
        return "act"
    if not _strict_nonnegative_int(record.get("floor")):
        return "floor"
    if not _strict_nonnegative_int(record.get("actions")):
        return "actions"
    observed_max_act = record.get("observed_max_act")
    if (
        not _strict_nonnegative_int(observed_max_act)
        or observed_max_act < record.get("act")
    ):
        return "observed_max_act"
    if "code_changed" in record and record.get("code_changed") is not False:
        return "code_changed"
    if type(record.get("heart_defeated")) is not bool:
        return "heart_defeated"
    if type(record.get("victory")) is not bool:
        return "victory"
    if record.get("heart_defeated") and not record.get("victory"):
        return "heart_without_victory"
    if "error" in record:
        return "terminal_error"

    keys = record.get("keys")
    if not isinstance(keys, dict) or any(
        type(keys.get(name)) is not bool
        for name in ("ruby", "emerald", "sapphire")
    ):
        return "keys"

    selection_id = record.get("selection_id")
    selection = record.get("selection")
    if not _nonempty_string(selection_id):
        return "selection_id"
    if not isinstance(selection, dict):
        return "selection"
    if selection.get("selection_id") != selection_id:
        return "selection_id_binding"
    if selection.get("algorithm") != ALGORITHM:
        return "selection_algorithm"
    if selection.get("decision_hash") != decision_hash:
        return "selection_decision_hash"
    if selection.get("controller_hash") != record.get("controller_hash"):
        return "selection_controller_hash"
    if selection.get("goal_mode") != record.get("goal_mode"):
        return "selection_goal_mode"
    if selection.get("policy_version") != record.get("policy_version"):
        return "selection_policy_version"
    if type(selection.get("ascension_level")) is not int:
        return "selection_ascension_type"
    if selection.get("ascension_level") != record.get("ascension_level"):
        return "selection_ascension"
    if selection.get("run_type") != record.get("run_type"):
        return "selection_run_type"
    if selection.get("character") != character:
        return "selection_character"
    digest = record.get("selection_digest")
    if not _valid_sha256(digest):
        return "selection_digest"
    if digest != selection_digest(selection):
        return "selection_digest_binding"
    created_at = selection.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        return "selection_created_at"
    return None


def audit_model_conflict_count(audit):
    """Return the recorded conflict count, or None when evidence is absent."""

    if not isinstance(audit, dict):
        return None
    value = audit.get("model_conflict_count")
    if value is None:
        model_advice = audit.get("model_advice")
        if isinstance(model_advice, dict):
            value = model_advice.get("conflicts")
    if not _strict_nonnegative_int(value):
        return None
    return value


def _audit_validation_error(audit, terminal):
    """Return a reason unless one audit proves this exact terminal is clear."""

    if not isinstance(audit, dict):
        return "audit_not_object"
    if audit.get("record_type") != AUDIT_RECORD_TYPE:
        return "audit_record_type"
    binding = {
        "schema_version": 2,
        "policy_version": terminal.get("policy_version"),
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character"),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get("terminal_state_seq"),
    }
    if "performance_hash" in terminal:
        binding["performance_hash"] = terminal.get("performance_hash")
    for field, expected in binding.items():
        actual = audit.get(field)
        if type(actual) is not type(expected) or actual != expected:
            return f"audit_binding_{field}"
    if audit.get("termination_kind") not in {None, "game_over"}:
        return "audit_termination_kind"
    if audit.get("audit_status") != "clear":
        return "audit_status"
    if audit.get("release_gate_passed") is not True:
        return "release_gate"
    for field in (
        "protocol_correctness", "mechanics_coverage", "strategy_quality",
    ):
        value = audit.get(field)
        if not isinstance(value, dict) or value.get("status") != "clear":
            return field
    for field in (
        "issue_count", "review_finding_count",
        "oracle_disagreement_count", "eligible_unknown",
    ):
        if type(audit.get(field)) is not int or audit.get(field) != 0:
            return field
    for field in ("issues", "review_findings", "unknown_record_types"):
        value = audit.get(field)
        if isinstance(value, list) and value:
            return field
    # Model/local disagreements may be fully resolved by a third authority;
    # they remain diagnostic evidence rather than a release failure.  Only a
    # missing or malformed count is invalid here.
    if audit_model_conflict_count(audit) is None:
        return "model_conflict_count"
    oracle = audit.get("independent_oracle")
    if (
        not isinstance(oracle, dict)
        or oracle.get("status") != "clear"
        or oracle.get("issue_count") != 0
        or oracle.get("eligible_unknown_count") != 0
        or oracle.get("disagreement_count") != 0
        or bool(oracle.get("issues"))
        or bool(oracle.get("unknowns"))
    ):
        return "independent_oracle"
    replay = audit.get("death_replay")
    death_observed = audit.get("death_observed")
    if not isinstance(replay, dict) or type(death_observed) is not bool:
        return "death_replay"
    acceptable_replay = (
        replay.get("status") == "clear"
        if death_observed
        else replay.get("status") in {"clear", "not_applicable"}
    )
    if (
        not acceptable_replay
        or replay.get("issue_count") != 0
        or replay.get("eligible_unknown_count") != 0
        or replay.get("death_observed") is not death_observed
        or bool(replay.get("issues"))
        or bool(replay.get("unknowns"))
    ):
        return "death_replay"
    if "code_changed" in audit and audit.get("code_changed") is not False:
        return "audit_code_changed"
    return None


def _controller_exit_validation_error(controller_exit, terminal):
    """Return a reason unless the outer process receipt is clean and bound."""

    if not isinstance(controller_exit, dict):
        return "controller_exit_not_object"
    if controller_exit.get("record_type") != CONTROLLER_EXIT_RECORD_TYPE:
        return "controller_exit_record_type"
    if controller_exit.get("controller_exit_status") != "clear":
        return "controller_exit_status"
    binding = {
        "schema_version": 2,
        "policy_version": terminal.get("policy_version"),
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character"),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get("terminal_state_seq"),
    }
    for field, expected in binding.items():
        actual = controller_exit.get(field)
        if type(actual) is not type(expected) or actual != expected:
            return f"controller_exit_binding_{field}"
    if type(controller_exit.get("exit_code")) is not int:
        return "controller_exit_code_type"
    if controller_exit.get("exit_code") != 0:
        return "controller_exit_code"
    for field in ("stdout_size", "stderr_size"):
        if not _strict_nonnegative_int(controller_exit.get(field)):
            return f"controller_exit_{field}"
    if controller_exit.get("stdout_size") == 0:
        return "controller_exit_stdout_empty"
    if controller_exit.get("stderr_size") != 0:
        return "controller_exit_stderr"
    for field in ("stdout_sha256", "stderr_sha256"):
        value = controller_exit.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            return f"controller_exit_{field}"
    if controller_exit.get("stdout_line_count") != 1:
        return "controller_exit_stdout_line_count"
    for field in (
        "stdout_semantic_sha256", "freeze_manifest_sha256",
        "freeze_source_digest",
    ):
        value = controller_exit.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            return f"controller_exit_{field}"
    freeze_generated_at = controller_exit.get("freeze_generated_at")
    if (
        type(freeze_generated_at) not in {int, float}
        or freeze_generated_at <= 0
    ):
        return "controller_exit_freeze_generated_at"
    return None


def _assessment_requires_hard_stop(assessment):
    """Stop on every incomplete, inconclusive, or issue-bearing attempt.

    A fixed-size validation cohort is useful only when every preceding run
    passed its release gate.  Downgrading P1 findings or eligible unknowns to
    sampling-only evidence made a red audit look like permission to launch
    the next game, so the selector now fails closed on the assessment's
    authoritative validity bit regardless of finding priority.
    """

    return not isinstance(assessment, dict) or assessment.get("valid") is not True


def _contains_explicit_p0(value):
    """Return whether nested audit evidence explicitly classifies a P0.

    Missing priorities remain unknown rather than being silently promoted to
    P0.  The caller separately requires a complete, issue-bearing audit and
    rejects protocol/operational evidence before this classification is used.
    """

    if isinstance(value, dict):
        severity = value.get("severity")
        if isinstance(severity, str) and severity.strip().upper() == "P0":
            return True
        return any(_contains_explicit_p0(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_explicit_p0(item) for item in value)
    return False


def p0_only_batch_assessment_error(assessment):
    """Validate a completed non-release attempt for a P0-only test batch.

    This is deliberately narrower than release eligibility.  It accepts only
    a uniquely ordered GAME_OVER triplet whose protocol and mechanics
    dimensions are clear, contains no protocol/operational event, and
    contains no explicit P0. Strategy-only issues or inconclusive strategy
    checks are retained as evidence but may advance an explicitly requested
    test matrix; otherwise an ordinary unknown can strand the runtime on
    GAME_OVER and prevent the rest of the batch from running.
    """

    if not isinstance(assessment, dict):
        return "assessment_not_object"
    terminal = assessment.get("terminal")
    audit = assessment.get("audit")
    controller_exit = assessment.get("controller_exit")
    if not isinstance(terminal, dict):
        return "terminal_not_object"
    terminal_error = _terminal_validation_error(
        terminal, terminal.get("decision_hash")
    )
    if terminal_error is not None:
        return terminal_error
    if not isinstance(audit, dict):
        return "audit_not_object"
    if audit.get("record_type") != AUDIT_RECORD_TYPE:
        return "audit_record_type"
    binding = {
        "schema_version": 2,
        "policy_version": terminal.get("policy_version"),
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character"),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get("terminal_state_seq"),
    }
    for field, expected in binding.items():
        actual = audit.get(field)
        if type(actual) is not type(expected) or actual != expected:
            return f"audit_binding_{field}"
    if audit.get("termination_kind") not in {None, "game_over"}:
        return "audit_termination_kind"
    audit_status = audit.get("audit_status")
    if audit_status not in {"issues", "inconclusive"}:
        return "audit_status"
    if audit.get("release_gate_passed") is not False:
        return "release_gate"
    issue_count = audit.get("issue_count")
    if (
        type(issue_count) is not int
        or issue_count < 0
        or (audit_status == "issues" and issue_count < 1)
    ):
        return "issue_count"
    for dimension in ("protocol_correctness", "mechanics_coverage"):
        value = audit.get(dimension)
        if (
            not isinstance(value, dict)
            or value.get("status") != "clear"
        ):
            return f"audit_{dimension}"
    # A successful combat-initialization handshake emits bounded
    # ``state_resync_started``/``state_resync_succeeded`` records.  They are
    # retained in the trace for provenance, but the audit marks them
    # non-fatal.  Older reports do not carry ``fatal_event_counts``; keep the
    # historical strict zero-event rule for those reports.
    protocol_events = audit.get("protocol_events")
    if type(protocol_events) is not int or protocol_events < 0:
        return "protocol_events"
    protocol = audit.get("protocol_correctness")
    fatal_event_counts = (
        protocol.get("fatal_event_counts")
        if isinstance(protocol, dict) else None
    )
    if fatal_event_counts is None:
        if protocol_events != 0:
            return "protocol_events"
    elif (
        not isinstance(fatal_event_counts, dict)
        or any(
            type(count) is not int or count < 0
            for count in fatal_event_counts.values()
        )
        or any(fatal_event_counts.values())
    ):
        return "protocol_events"
    for field in ("operational_error_attempts", "operational_error_events"):
        if type(audit.get(field)) is not int or audit.get(field) != 0:
            return field
    if "code_changed" in audit and audit.get("code_changed") is not False:
        return "audit_code_changed"
    if _contains_explicit_p0(audit):
        return "p0_finding"

    if not isinstance(controller_exit, dict):
        return "controller_exit_not_object"
    if controller_exit.get("record_type") != CONTROLLER_EXIT_RECORD_TYPE:
        return "controller_exit_record_type"
    for field, expected in binding.items():
        actual = controller_exit.get(field)
        if type(actual) is not type(expected) or actual != expected:
            return f"controller_exit_binding_{field}"
    if controller_exit.get("controller_exit_status") != "issues":
        return "controller_exit_status"
    if type(controller_exit.get("exit_code")) is not int:
        return "controller_exit_code_type"
    for field in ("stdout_size", "stderr_size"):
        if not _strict_nonnegative_int(controller_exit.get(field)):
            return f"controller_exit_{field}"
    if controller_exit.get("stderr_size") != 0:
        return "controller_exit_stderr"
    for field in (
        "stdout_sha256", "stderr_sha256", "stdout_semantic_sha256",
        "freeze_manifest_sha256", "freeze_source_digest",
    ):
        value = controller_exit.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            return f"controller_exit_{field}"
    if controller_exit.get("stdout_line_count") != 1:
        return "controller_exit_stdout_line_count"
    freeze_generated_at = controller_exit.get("freeze_generated_at")
    if (
        type(freeze_generated_at) not in {int, float}
        or freeze_generated_at <= 0
    ):
        return "controller_exit_freeze_generated_at"
    exit_audit = controller_exit.get("controller_exit_audit")
    if (
        not isinstance(exit_audit, dict)
        or exit_audit.get("audit_status") != "issues"
        or exit_audit.get("release_gate_passed") is not False
        or _contains_explicit_p0(exit_audit)
    ):
        return "controller_exit_audit"
    return None


def _assessment_counts_for_batch(assessment, p0_only_batch):
    if not _assessment_requires_hard_stop(assessment):
        return True
    return bool(
        p0_only_batch
        and p0_only_batch_assessment_error(assessment) is None
    )


def _terminal_assessments(records, decision_hash=None):
    """Assess terminals against one ordered audit and outer exit receipt."""

    terminals = defaultdict(list)
    audits = defaultdict(list)
    controller_exits = defaultdict(list)
    selection_attempts = defaultdict(set)
    terminal_events = []
    audit_events = []
    controller_exit_events = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        if record.get("record_type") == AUDIT_RECORD_TYPE:
            identity = record.get("attempt_id")
            audit_events.append((index, identity, record))
            if _nonempty_string(identity):
                audits[identity].append((index, record))
        elif record.get("record_type") == CONTROLLER_EXIT_RECORD_TYPE:
            identity = record.get("attempt_id")
            controller_exit_events.append((index, identity, record))
            if _nonempty_string(identity):
                controller_exits[identity].append((index, record))
        elif (
            record.get("record_type") == TERMINAL_RECORD_TYPE
            or record.get("termination_kind") in {
                "game_over", "operational_error",
            }
        ):
            identity = record.get("attempt_id")
            terminal_events.append((index, identity, record))
            if _nonempty_string(identity):
                terminals[identity].append((index, record))
                selection_id = record.get("selection_id")
                if _nonempty_string(selection_id):
                    selection_attempts[selection_id].add(identity)

    assessments = []
    for index, identity, terminal in terminal_events:
        reason = None
        audit = None
        controller_exit = None
        if not _nonempty_string(identity):
            reason = "attempt_id"
        elif len(terminals[identity]) != 1:
            reason = "duplicate_terminal"
        elif len(audits[identity]) != 1:
            reason = "missing_or_duplicate_audit"
        elif len(controller_exits[identity]) != 1:
            reason = "missing_or_duplicate_controller_exit"
        elif len(selection_attempts.get(terminal.get("selection_id"), set())) != 1:
            reason = "selection_id_reused_across_attempts"
        else:
            audit_index, audit = audits[identity][0]
            controller_exit_index, controller_exit = controller_exits[identity][0]
            if audit_index <= index:
                reason = "audit_precedes_terminal"
            elif controller_exit_index <= audit_index:
                reason = "controller_exit_precedes_audit"
            elif any(
                other_index > index and other_index < controller_exit_index
                for other_index, _, _ in terminal_events
            ):
                reason = "controller_exit_after_next_terminal"
            else:
                expected_decision_hash = (
                    decision_hash
                    if decision_hash is not None
                    else terminal.get("decision_hash")
                )
                if not _nonempty_string(expected_decision_hash):
                    reason = "decision_hash"
                else:
                    reason = _terminal_validation_error(
                        terminal, expected_decision_hash
                    )
                if reason is None:
                    reason = _audit_validation_error(audit, terminal)
                if reason is None:
                    reason = _controller_exit_validation_error(
                        controller_exit, terminal
                    )
        assessments.append({
            "index": index,
            "attempt_id": identity,
            "terminal": terminal,
            "audit": audit,
            "controller_exit": controller_exit,
            "valid": reason is None,
            "reason": reason,
        })
    for index, identity, audit in audit_events:
        if (
            not _nonempty_string(identity)
            or len(terminals[identity]) != 1
            or len(audits[identity]) != 1
        ):
            assessments.append({
                "index": index,
                "attempt_id": identity,
                "terminal": None,
                "audit": audit,
                "controller_exit": None,
                "valid": False,
                "reason": "orphan_or_duplicate_audit",
            })
    for index, identity, controller_exit in controller_exit_events:
        if (
            not _nonempty_string(identity)
            or len(terminals[identity]) != 1
            or len(audits[identity]) != 1
            or len(controller_exits[identity]) != 1
        ):
            assessments.append({
                "index": index,
                "attempt_id": identity,
                "terminal": None,
                "audit": None,
                "controller_exit": controller_exit,
                "valid": False,
                "reason": "orphan_or_duplicate_controller_exit",
            })
    return sorted(assessments, key=lambda item: item["index"])


def eligible_attempt_triples(records, decision_hash, controller_hash=None):
    """Return the trailing continuous cohort of valid three-part receipts."""

    if not _nonempty_string(decision_hash):
        raise ValueError("decision_hash must be a non-empty string")
    if controller_hash is not None and not _nonempty_string(controller_hash):
        raise ValueError("controller_hash must be a non-empty string")
    required_controller_hash = controller_hash
    cohort = []
    cohort_controller_hash = None
    for assessment in _terminal_assessments(records, decision_hash):
        if not assessment["valid"]:
            cohort = []
            cohort_controller_hash = None
            continue
        terminal = assessment["terminal"]
        terminal_controller_hash = terminal.get("controller_hash")
        if (
            required_controller_hash is not None
            and terminal_controller_hash != required_controller_hash
        ):
            cohort = []
            cohort_controller_hash = None
            continue
        if (
            cohort_controller_hash is not None
            and terminal_controller_hash != cohort_controller_hash
        ):
            cohort = []
        cohort_controller_hash = terminal_controller_hash
        cohort.append((
            terminal,
            assessment["audit"],
            assessment["controller_exit"],
        ))
    return cohort


def eligible_attempt_pairs(records, decision_hash, controller_hash=None):
    """Return terminal/audit pairs admitted by the three-part gate."""

    return [
        (terminal, audit)
        for terminal, audit, _ in eligible_attempt_triples(
            records, decision_hash, controller_hash
        )
    ]


def eligible_attempts(records, decision_hash, controller_hash=None):
    """Return terminals in the trailing, fully audited valid cohort."""

    return [
        terminal
        for terminal, _ in eligible_attempt_pairs(
            records, decision_hash, controller_hash
        )
    ]


def eligible_performance_attempt_triples(records, performance_hash):
    """Return a trailing valid cohort across non-policy source revisions.

    Every attempt is still validated against its own exact decision hash and
    complete terminal/audit/controller-exit chain. Only the analytic grouping
    key is relaxed, so audit or launcher changes no longer fragment observed
    strategy performance.
    """

    if not _nonempty_string(performance_hash):
        raise ValueError("performance_hash must be a non-empty string")
    cohort = []
    for assessment in _terminal_assessments(records):
        terminal = assessment.get("terminal")
        terminal_performance_hash = (
            terminal.get("performance_hash")
            if isinstance(terminal, dict) else None
        )
        if not assessment["valid"]:
            if terminal_performance_hash == performance_hash:
                cohort = []
            continue
        if terminal_performance_hash != performance_hash:
            cohort = []
            continue
        audit = assessment.get("audit")
        if (
            not isinstance(audit, dict)
            or audit.get("performance_hash") != performance_hash
        ):
            cohort = []
            continue
        cohort.append((
            terminal,
            audit,
            assessment["controller_exit"],
        ))
    return cohort


def unresolved_attempt_assessments(records, decision_hash, controller_hash):
    """Return invalid attempt chains belonging to one exact frozen pair.

    Scope is established by any terminal/audit/controller-exit row carrying
    the requested decision/controller hashes.  Once an attempt is in scope,
    every receipt row for that attempt is retained so a missing hash, duplicate
    terminal, bad ordering, or mismatched companion cannot disappear through
    filtering.  Records for a different frozen pair remain independent.
    """

    if not _nonempty_string(decision_hash):
        raise ValueError("decision_hash must be a non-empty string")
    if not _nonempty_string(controller_hash):
        raise ValueError("controller_hash must be a non-empty string")
    relevant = []
    scoped_attempt_ids = set()
    exact_indexes = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        is_receipt = (
            record.get("record_type") in {
                TERMINAL_RECORD_TYPE,
                AUDIT_RECORD_TYPE,
                CONTROLLER_EXIT_RECORD_TYPE,
            }
            or record.get("termination_kind") in {
                "game_over", "operational_error",
            }
        )
        if not is_receipt:
            continue
        relevant.append((index, record))
        if (
            record.get("decision_hash") == decision_hash
            and record.get("controller_hash") == controller_hash
        ):
            exact_indexes.add(index)
            attempt_id = record.get("attempt_id")
            if _nonempty_string(attempt_id):
                scoped_attempt_ids.add(attempt_id)
    scoped = [
        record
        for index, record in relevant
        if index in exact_indexes
        or record.get("attempt_id") in scoped_attempt_ids
    ]
    if not scoped:
        return []
    return [
        item for item in _terminal_assessments(scoped, decision_hash)
        if not item["valid"] and _assessment_requires_hard_stop(item)
    ]


def require_resolved_attempt_history(
    records, decision_hash, controller_hash, *, p0_only_batch=False,
):
    """Fail closed if the exact frozen pair owns any unresolved attempt."""

    unresolved = unresolved_attempt_assessments(
        records, decision_hash, controller_hash
    )
    blocking = [
        item for item in unresolved
        if not _assessment_counts_for_batch(item, p0_only_batch)
    ]
    if blocking:
        details = ",".join(
            f"{item.get('attempt_id') or '<missing>'}:{item.get('reason')}"
            for item in blocking
        )
        raise HistoryValidationError(
            "same-hash unresolved attempt blocks a new selection: " + details
        )
    return True


def _stats(records):
    stats = {
        character: {
            "attempts": 0,
            "heart_wins": 0,
            # A censored run is still evidence: reaching Act 3 with keys is
            # materially better than dying in Act 1.  Keep this diagnostic
            # separate from the authoritative win count.
            "survival_progress": 0.0,
        }
        for character in CHARACTERS
    }
    for item in records:
        character = item.get("character")
        if character not in stats:
            continue
        stats[character]["attempts"] += 1
        stats[character]["heart_wins"] += int(bool(item.get("heart_defeated")))
        stats[character]["survival_progress"] += _survival_progress(item)
    for value in stats.values():
        value["survival_progress"] = round(value["survival_progress"], 3)
    return stats


def cohort_quota_attempts(
    records, decision_hash, controller_hash, *, p0_only_batch=False,
):
    """Return structurally complete attempts used for character balance.

    Normal autonomous play remains release-strict.  An explicitly requested
    P0-only batch may count structurally complete P1/unknown attempts for up to four
    deterministic six-run subcohorts without making them release-eligible or
    posterior evidence.
    """

    attempts = []
    for assessment in _terminal_assessments(records, decision_hash):
        terminal = assessment.get("terminal")
        if (
            not isinstance(terminal, dict)
            or terminal.get("decision_hash") != decision_hash
            or terminal.get("controller_hash") != controller_hash
            or not _assessment_counts_for_batch(
                assessment, p0_only_batch
            )
            or (
                isinstance(terminal.get("selection"), dict)
                and terminal["selection"].get("reason")
                == FOLLOWUP_VALIDATION_REASON
            )
        ):
            continue
        attempts.append(terminal)
    fixed_mode = any(
        isinstance(item.get("selection"), dict)
        and item["selection"].get("reason")
        == "fixed_six_run_character_quota"
        for item in attempts
    )
    if fixed_mode:
        expected = FIXED_QUOTA_SEQUENCE
        maximum = (
            P0_ONLY_MAX_ATTEMPTS if p0_only_batch else len(expected)
        )
        if len(attempts) > maximum:
            if p0_only_batch:
                raise HistoryValidationError(
                    "p0-only batch contains more than 24 attempts"
                )
            raise HistoryValidationError(
                "fixed six-run cohort contains more than six attempts"
            )
        for index, terminal in enumerate(attempts):
            selection = terminal.get("selection")
            expected_character = expected[index % len(expected)]
            if (
                not isinstance(selection, dict)
                or selection.get("reason")
                != "fixed_six_run_character_quota"
                or terminal.get("character") != expected_character
                or selection.get("character") != expected_character
            ):
                if p0_only_batch:
                    raise HistoryValidationError(
                        "p0-only batch character history is not the exact "
                        "repeated IRONCLAD/THE_SILENT/DEFECT sequence"
                    )
                raise HistoryValidationError(
                    "fixed six-run cohort character history is not the exact "
                    "IRONCLAD/THE_SILENT/DEFECT prefix"
                )
    return attempts


def followup_validation_attempts(
    records, decision_hash, controller_hash, *, p0_only_batch=False,
):
    """Return the explicit post-cohort validation batch for this hash pair."""

    attempts = []
    for assessment in _terminal_assessments(records, decision_hash):
        terminal = assessment.get("terminal")
        if (
            not isinstance(terminal, dict)
            or terminal.get("decision_hash") != decision_hash
            or terminal.get("controller_hash") != controller_hash
            or not _assessment_counts_for_batch(
                assessment, p0_only_batch
            )
            or not isinstance(terminal.get("selection"), dict)
            or terminal["selection"].get("reason")
            != FOLLOWUP_VALIDATION_REASON
        ):
            continue
        attempts.append(terminal)
    if len(attempts) > VALIDATION_BATCH_SIZE:
        raise HistoryValidationError(
            "follow-up validation batch contains more than six attempts"
        )
    for index, terminal in enumerate(attempts):
        expected = CHARACTERS[index % len(CHARACTERS)]
        selection = terminal.get("selection")
        if (
            terminal.get("character") != expected
            or selection.get("character") != expected
        ):
            raise HistoryValidationError(
                "follow-up validation batch character history is invalid"
            )
    return attempts


def _survival_progress(record):
    """Return a conservative [0, 1] progress signal for a terminal run.

    This is only a tie-break/prior for Thompson sampling; a Heart victory is
    still the sole success criterion.  Global floor is available in current
    terminal records, while older records may only expose act/floor, so use
    the best valid representation and never infer progress from a malformed
    value.
    """

    if bool(record.get("heart_defeated")) and bool(record.get("victory")):
        return 1.0
    try:
        floor = float(record.get("floor"))
    except (TypeError, ValueError):
        floor = 0.0
    try:
        act = int(record.get("act") or 1)
    except (TypeError, ValueError):
        act = 1
    # Terminal ``floor`` is global in the live protocol.  If it is a small
    # per-act value, reconstruct the same scale from the act number.
    if floor < 17 and act > 1:
        floor += (act - 1) * 17
    progress = floor / 50.0
    if bool(record.get("has_emerald_key")) or (record.get("keys") or {}).get("emerald"):
        progress += 0.02
    if (record.get("keys") or {}).get("sapphire"):
        progress += 0.02
    return max(0.0, min(0.98, progress))


def select_character(
    records, decision_hash, rng=None, bootstrap_runs=BOOTSTRAP_RUNS,
    controller_hash=None, validation_batch=False, p0_only_batch=False,
):
    rng = rng or random.SystemRandom()
    if type(bootstrap_runs) is not int or bootstrap_runs < 1:
        raise ValueError("bootstrap_runs must be a positive integer")
    if not _nonempty_string(controller_hash):
        raise ValueError("controller_hash must be a non-empty string")
    require_resolved_attempt_history(
        records,
        decision_hash,
        controller_hash,
        p0_only_batch=p0_only_batch,
    )
    eligible = eligible_attempts(records, decision_hash, controller_hash)
    stats = _stats(eligible)
    quota_attempts = cohort_quota_attempts(
        records,
        decision_hash,
        controller_hash,
        p0_only_batch=p0_only_batch,
    )
    followup_attempts = followup_validation_attempts(
        records,
        decision_hash,
        controller_hash,
        p0_only_batch=p0_only_batch,
    )
    fixed_quota_mode = any(
        isinstance(item.get("selection"), dict)
        and item["selection"].get("reason")
        == "fixed_six_run_character_quota"
        for item in quota_attempts
    )
    quota_counts = {
        character: 0 for character in CHARACTERS
    }
    for item in quota_attempts:
        character = item.get("character")
        if character in quota_counts:
            quota_counts[character] += 1
    macro_profiles = {
        json.dumps(
            item.get("macro_policy"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in eligible
        if isinstance(item.get("macro_policy"), dict)
    }
    if len(macro_profiles) > 1:
        raise ValueError(
            "one decision_hash contains multiple macro policy profiles"
        )
    macro_policy = (
        json.loads(next(iter(macro_profiles))) if macro_profiles else None
    )

    if validation_batch:
        if len(followup_attempts) >= VALIDATION_BATCH_SIZE:
            raise HistoryValidationError(
                "follow-up validation batch is complete"
            )
        chosen = CHARACTERS[len(followup_attempts) % len(CHARACTERS)]
        reason = FOLLOWUP_VALIDATION_REASON
        samples = {}
    else:
        if fixed_quota_mode and p0_only_batch:
            if len(quota_attempts) >= P0_ONLY_MAX_ATTEMPTS:
                raise HistoryValidationError(
                    "p0-only 24-run batch is complete; optimize before "
                    "starting another game"
                )
            chosen = FIXED_QUOTA_SEQUENCE[
                len(quota_attempts) % len(FIXED_QUOTA_SEQUENCE)
            ]
            reason = "fixed_six_run_character_quota"
            samples = {}
        else:
            if (
                fixed_quota_mode
                and len(quota_attempts)
                == COHORT_RUNS_PER_CHARACTER * len(CHARACTERS)
            ):
                raise HistoryValidationError(
                    "fixed six-run cohort is complete; optimize before "
                    "starting another game"
                )
            quota_missing = [
                character for character in CHARACTERS
                if quota_counts[character] < COHORT_RUNS_PER_CHARACTER
            ]
            if quota_missing:
                # Minimum-count selection gives a deterministic round robin:
                # I -> S -> D -> I -> S -> D.  Container/history order cannot turn a
                # six-game cohort into six games of one character again.
                chosen = min(
                    quota_missing,
                    key=lambda item: (
                        quota_counts[item], CHARACTERS.index(item)
                    ),
                )
                reason = "fixed_six_run_character_quota"
                samples = {}
                bootstrap = []
            else:
                bootstrap = [
                    character
                    for character in CHARACTERS
                    if stats[character]["attempts"] < bootstrap_runs
                ]
            if not quota_missing and bootstrap:
                chosen = min(bootstrap, key=lambda item: (stats[item]["attempts"], CHARACTERS.index(item)))
                reason = "bootstrap_equal_coverage"
                samples = {}
            elif not quota_missing:
                recent = [item["character"] for item in eligible[-STARVATION_WINDOW:]]
                starved = [character for character in CHARACTERS if character not in recent]
                if starved:
                    chosen = min(starved, key=lambda item: (stats[item]["attempts"], CHARACTERS.index(item)))
                    reason = "starvation_guard"
                    samples = {}
                else:
                    samples = {}
                    for character in CHARACTERS:
                        value = stats[character]
                        attempts = value["attempts"]
                        wins = value["heart_wins"]
                        progress = float(value.get("survival_progress", 0.0) or 0.0)
                        # Add at most one pseudo-observation per terminal attempt.
                        # This preserves the dominance of real wins while preventing
                        # an all-zero history from choosing a character blindly.
                        pseudo_success = min(0.75 * attempts, progress * 0.5)
                        pseudo_failure = min(
                            0.75 * attempts,
                            max(
                                0.0,
                                max(0.0, attempts - wins) * 0.5 - pseudo_success,
                            ),
                        )
                        samples[character] = rng.betavariate(
                            1 + wins + pseudo_success,
                            1 + attempts - wins + pseudo_failure,
                        )
                    chosen = max(CHARACTERS, key=lambda item: (samples[item], -CHARACTERS.index(item)))
                    reason = "beta_thompson_sample"

    selection = {
        "selection_id": str(uuid.uuid4()),
        "algorithm": ALGORITHM,
        "created_at": time.time(),
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "goal_mode": "HEART",
        "policy_version": POLICY_VERSION,
        "ascension_level": 0,
        "run_type": "standard",
        "character": chosen,
        "reason": reason,
        # Carry the explicit test-batch exception in the one-use selection
        # artifact.  START is independently revalidated by the orchestrator
        # and the bridge, so the exception must survive both checks without
        # becoming a global switch.
        "p0_only_batch": bool(p0_only_batch),
        "bootstrap_runs": bootstrap_runs,
        "starvation_window": STARVATION_WINDOW,
        "stats": stats,
        "posterior_samples": {key: round(value, 6) for key, value in samples.items()},
        "cohort_quota": {
            "runs_per_character": COHORT_RUNS_PER_CHARACTER,
            "cohort_size": COHORT_RUNS_PER_CHARACTER * len(CHARACTERS),
            "counts": quota_counts,
            "attempt_ids": [
                item.get("attempt_id") for item in quota_attempts
            ],
        },
        "macro_policy": macro_policy,
        "eligible_attempt_ids": [
            item.get("attempt_id")
            for item in eligible
        ],
    }
    if p0_only_batch and reason == "fixed_six_run_character_quota":
        selection["p0_only_subcohort_index"] = (
            len(quota_attempts) // P0_ONLY_SUBCOHORT_SIZE
        )
        selection["p0_only_subcohort_position"] = (
            len(quota_attempts) % P0_ONLY_SUBCOHORT_SIZE
        )
    return validate_selection(
        selection,
        decision_hash,
        controller_hash,
        eligible_attempt_ids=[item.get("attempt_id") for item in eligible],
    )


def validate_selection(
    selection, decision_hash, controller_hash, eligible_attempt_ids=None
):
    """Validate the selector artifact before it can be persisted or consumed."""

    if not isinstance(selection, dict):
        raise ValueError("selection must be an object")
    if not _nonempty_string(decision_hash):
        raise ValueError("decision_hash must be a non-empty string")
    if not _nonempty_string(controller_hash):
        raise ValueError("controller_hash must be a non-empty string")
    if not _nonempty_string(selection.get("selection_id")):
        raise ValueError("selection_id must be a non-empty string")
    if selection.get("algorithm") != ALGORITHM:
        raise ValueError("selection algorithm does not match")
    created_at = selection.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        raise ValueError("selection created_at is invalid")
    expected = {
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "goal_mode": "HEART",
        "policy_version": POLICY_VERSION,
        "ascension_level": 0,
        "run_type": "standard",
        "starvation_window": STARVATION_WINDOW,
    }
    for field, value in expected.items():
        if selection.get(field) != value:
            raise ValueError(f"selection {field} does not match")
    if selection.get("character") not in CHARACTERS:
        raise ValueError("selection character is unsupported")
    if selection.get("reason") not in {
        "fixed_six_run_character_quota", "bootstrap_equal_coverage",
        "starvation_guard", "beta_thompson_sample",
        FOLLOWUP_VALIDATION_REASON,
    }:
        raise ValueError("selection reason is unsupported")
    if "p0_only_batch" in selection and type(
        selection.get("p0_only_batch")
    ) is not bool:
        raise ValueError("selection p0_only_batch is invalid")
    p0_only_batch = selection.get("p0_only_batch") is True
    if (
        type(selection.get("bootstrap_runs")) is not int
        or selection.get("bootstrap_runs") < 1
    ):
        raise ValueError("selection bootstrap_runs is invalid")
    stats = selection.get("stats")
    if not isinstance(stats, dict) or set(stats) != set(CHARACTERS):
        raise ValueError("selection stats are incomplete")
    for value in stats.values():
        if not isinstance(value, dict):
            raise ValueError("selection stats entry is invalid")
        if not _strict_nonnegative_int(value.get("attempts")):
            raise ValueError("selection attempt count is invalid")
        if not _strict_nonnegative_int(value.get("heart_wins")):
            raise ValueError("selection win count is invalid")
        if value.get("heart_wins") > value.get("attempts"):
            raise ValueError("selection win count exceeds attempts")
        progress = value.get("survival_progress")
        if (
            type(progress) not in {int, float}
            or not math.isfinite(float(progress))
            or progress < 0
        ):
            raise ValueError("selection survival progress is invalid")
    posterior_samples = selection.get("posterior_samples")
    if not isinstance(posterior_samples, dict):
        raise ValueError("selection posterior_samples is invalid")
    expected_sample_keys = (
        set(CHARACTERS)
        if selection.get("reason") == "beta_thompson_sample"
        else set()
    )
    if set(posterior_samples) != expected_sample_keys or any(
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or not 0 <= value <= 1
        for value in posterior_samples.values()
    ):
        raise ValueError("selection posterior_samples do not match the reason")
    quota = selection.get("cohort_quota")
    if not isinstance(quota, dict) or set(quota) != {
        "runs_per_character", "cohort_size", "counts", "attempt_ids",
    }:
        raise ValueError("selection cohort_quota is invalid")
    if (
        quota.get("runs_per_character") != COHORT_RUNS_PER_CHARACTER
        or quota.get("cohort_size")
        != COHORT_RUNS_PER_CHARACTER * len(CHARACTERS)
    ):
        raise ValueError("selection cohort quota size is invalid")
    quota_counts = quota.get("counts")
    quota_attempt_ids = quota.get("attempt_ids")
    if (
        not isinstance(quota_counts, dict)
        or set(quota_counts) != set(CHARACTERS)
        or any(
            not _strict_nonnegative_int(value)
            for value in quota_counts.values()
        )
        or not isinstance(quota_attempt_ids, list)
        or any(
            not _nonempty_string(item) for item in quota_attempt_ids
        )
        or len(quota_attempt_ids) != len(set(quota_attempt_ids))
        or sum(quota_counts.values()) != len(quota_attempt_ids)
    ):
        raise ValueError("selection cohort quota evidence is invalid")
    quota_missing = [
        character for character in CHARACTERS
        if quota_counts[character] < COHORT_RUNS_PER_CHARACTER
    ]
    if selection.get("reason") == "fixed_six_run_character_quota":
        if p0_only_batch:
            attempt_index = len(quota_attempt_ids)
            if attempt_index >= P0_ONLY_MAX_ATTEMPTS:
                raise ValueError("selection exceeds the p0-only 24-run batch")
            expected_character = FIXED_QUOTA_SEQUENCE[
                attempt_index % len(FIXED_QUOTA_SEQUENCE)
            ]
            expected_subcohort_index = (
                attempt_index // P0_ONLY_SUBCOHORT_SIZE
            )
            expected_subcohort_position = (
                attempt_index % P0_ONLY_SUBCOHORT_SIZE
            )
            if (
                selection.get("p0_only_subcohort_index")
                != expected_subcohort_index
                or selection.get("p0_only_subcohort_position")
                != expected_subcohort_position
            ):
                raise ValueError("selection p0-only subcohort binding is invalid")
        else:
            if not quota_missing:
                raise ValueError("selection quota reason has no missing character")
            expected_character = min(
                quota_missing,
                key=lambda item: (
                    quota_counts[item], CHARACTERS.index(item)
                ),
            )
        if selection.get("character") != expected_character:
            if p0_only_batch:
                raise ValueError("selection character violates p0-only quota")
            raise ValueError("selection character violates cohort quota")
    elif quota_missing:
        raise ValueError("selection bypasses an incomplete cohort quota")
    elif p0_only_batch and any(
        field in selection
        for field in (
            "p0_only_subcohort_index",
            "p0_only_subcohort_position",
        )
    ):
        raise ValueError("selection p0-only subcohort fields are unsupported")
    if selection.get("macro_policy") is not None and not isinstance(
        selection.get("macro_policy"), dict
    ):
        raise ValueError("selection macro_policy is invalid")
    actual_attempt_ids = selection.get("eligible_attempt_ids")
    if not isinstance(actual_attempt_ids, list) or any(
        not _nonempty_string(item) for item in actual_attempt_ids
    ):
        raise ValueError("selection eligible_attempt_ids are invalid")
    if len(actual_attempt_ids) != len(set(actual_attempt_ids)):
        raise ValueError("selection eligible_attempt_ids contain duplicates")
    if sum(value["attempts"] for value in stats.values()) != len(
        actual_attempt_ids
    ):
        raise ValueError("selection stats do not match eligible attempts")
    if eligible_attempt_ids is not None and actual_attempt_ids != list(
        eligible_attempt_ids
    ):
        raise ValueError("selection eligible_attempt_ids do not match the cohort")
    return selection


def write_selection(path, selection):
    """Atomically persist one validated selection without hiding a stale one."""

    path = Path(path)
    if not isinstance(selection, dict):
        raise ValueError("selection must be an object")
    validate_selection(
        selection,
        selection.get("decision_hash"),
        selection.get("controller_hash"),
    )
    if path.exists():
        raise FileExistsError(f"pending selection already exists: {path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(selection, ensure_ascii=True), encoding="utf-8")
    os.replace(temporary, path)


def validate_launch_prerequisites(
    records,
    decision_hash,
    controller_hash,
    root,
    *,
    freeze_manifest_path,
    cohort_review_path,
    p0_only_batch=False,
):
    """Block selection creation until freeze and review gates are current."""

    import cohort_report
    import cohort_review
    import freeze_manifest

    root = Path(root).resolve()
    require_resolved_attempt_history(
        records,
        decision_hash,
        controller_hash,
        p0_only_batch=p0_only_batch,
    )
    try:
        manifest = freeze_manifest.load_validated_manifest(
            freeze_manifest_path, root, decision_hash, controller_hash
        )
    except freeze_manifest.FreezeManifestError as exc:
        raise ValueError(str(exc)) from exc
    cohort = cohort_report.build_cohort_report(records, decision_hash)
    requirement = cohort_review.review_requirement(cohort)
    if requirement is None:
        return {"manifest": manifest, "cohort": cohort, "review": None}
    try:
        review = json.loads(
            Path(cohort_review_path).read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise ValueError(
            f"mandatory {requirement} cohort review is missing"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cohort review is unreadable") from exc
    if not cohort_review.validate_review_gate(
        review, cohort, root / "logs" / "attempts"
    ):
        raise ValueError(
            f"mandatory {requirement} cohort review is not clear"
        )
    return {"manifest": manifest, "cohort": cohort, "review": review}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, default=Path("run-history.jsonl"))
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--controller-hash", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--bootstrap-runs", type=int, default=BOOTSTRAP_RUNS)
    parser.add_argument("--freeze-manifest", type=Path)
    parser.add_argument("--cohort-review", type=Path)
    args = parser.parse_args()
    if args.bootstrap_runs < 1:
        parser.error("--bootstrap-runs must be at least 1")
    rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
    try:
        records = load_history(args.history)
        root = args.history.resolve().parent
        validate_launch_prerequisites(
            records,
            args.decision_hash,
            args.controller_hash,
            root,
            freeze_manifest_path=(
                root / "freeze-manifest.json"
                if args.freeze_manifest is None else args.freeze_manifest
            ),
            cohort_review_path=(
                root / "cohort-review.json"
                if args.cohort_review is None else args.cohort_review
            ),
        )
        selection = select_character(
            records,
            args.decision_hash,
            rng=rng,
            bootstrap_runs=args.bootstrap_runs,
            controller_hash=args.controller_hash,
        )
        if args.write is not None:
            write_selection(args.write, selection)
    except (FileExistsError, HistoryValidationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(selection, ensure_ascii=False))


if __name__ == "__main__":
    main()
