"""One-run autonomous controller using only protocol-v2 bound actions."""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

import stsctl
import decision_cases
import run_context as run_context_lib
from deepseek_macro import DeepSeekMacroAdvisor
from macro_policy import MacroPolicyConfig, load_local_env
from prompt_assembler import PromptAssembler
from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai import combat_predictor
from spirecomm.communication.action import (
    BossRewardAction,
    BuyCardAction,
    BuyPotionAction,
    BuyRelicAction,
    CancelAction,
    CardRewardAction,
    CardSelectAction,
    ChooseAction,
    ChooseMapNodeAction,
    CombatRewardAction,
    EndTurnAction,
    PlayCardAction,
    PotionAction,
    ProceedAction,
    RestAction,
)
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.game import Game


RESULT_PATH = ROOT / "run-result.json"
RESULT_HISTORY_PATH = ROOT / "run-history.jsonl"
RUN_CONTEXT_PATH = ROOT / "run-context.json"
PENDING_SELECTION_PATH = ROOT / "next-run-selection.json"
TRACE_PATH = ROOT / "autoplay.log"
DECISION_CASES_PATH = ROOT / "decision-cases.jsonl"
RUN_AUDIT_PATH = ROOT / "run-audit.json"
DEATH_REPLAY_PATH = ROOT / "death-replay.json"
COHORT_REPORT_PATH = ROOT / "cohort-report.json"
CONTROLLER_LOCK_PATH = ROOT / ".autoplay-controller.lock"
POLICY_VERSION = "fast-policy-v5"
STRATEGY_REVISION = "unified-heart-policy-2026-08-11-audit-v2-wheel-runtime"
TRACE_SCHEMA_VERSION = 5
AUDIT_ORACLE_VERSION = "independent-oracle-v2"
GLOBAL_TRACE_MAX_BYTES = 8 * 1024 * 1024
ATTEMPT_TRACE_MAX_BYTES = 128 * 1024 * 1024
ATTEMPT_BINDING_FIELDS = (
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "decision_hash",
    "controller_hash",
    "policy_version",
    "selection_id",
    "selection_digest",
)
ACTION_RECEIPT_TIMEOUT_SECONDS = 30.0
# CommunicationMod can hold a HAND_SELECT CHOOSE receipt while Gambling Chip
# (and similar multi-card actions) settles the selected list and exposes the
# confirm action.  The bridge already bounds that settlement at 120 frames;
# give this specific state-changing write enough time to receive its original
# receipt before attempting any read-only recovery.  No command is replayed.
HAND_SELECT_ACTION_RECEIPT_TIMEOUT_SECONDS = 90.0
LATE_RECEIPT_RECHECK_SECONDS = 2.0
STATE_RESYNC_TIMEOUT_SECONDS = 5.0
MAX_STATE_RESYNC_ATTEMPTS = 3
MAX_TRANSITION_SETTLE_ATTEMPTS = 3


def _stable_id(prefix, value):
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:20]}"


# A card play can be acknowledged before CommunicationMod's card queue has
# drained. In a live run the queued power/effect callbacks have taken just
# over twelve one-frame polls; keep the wait bounded while allowing that
# legitimate asynchronous tail to settle before fail-closed rejection.
MAX_PLAYED_CARD_SETTLE_ATTEMPTS = 24
# Potion effects can expose their generated rewards before the consumed
# potion is removed from its slot (notably Entropic Brew). Use the same
# bounded frame budget so verification observes the completed transaction.
MAX_POTION_SETTLE_ATTEMPTS = 24
MAX_RECOVERABLE_PROTOCOL_ERRORS = 3
# A newly entered combat can publish a DEBUG intent for several bridge
# frames while the monster move is being rolled. Those frames deliberately
# expose only the read-only STATE transport command; cap the number of
# resyncs so a genuinely stalled combat still fails closed with evidence.
MAX_COMBAT_INITIALIZATION_RESYNCS = 120
# HAND_SELECT can remain visible while Gambling Chip (or another discard
# effect) finishes applying a previously accepted selection.  Poll the
# authoritative frame without sending a second gameplay command during that
# bounded settlement window.
MAX_HAND_SELECT_SETTLE_POLLS = 120
HAND_SELECT_SETTLE_POLL_SECONDS = 0.1
BARRICADE_LIVENESS_FULL_TURNS = 12
load_local_env(ROOT / ".env")
MACRO_CONFIG = MacroPolicyConfig.from_env()
PROMPT_ASSEMBLER = PromptAssembler(ROOT, MACRO_CONFIG)
MACRO_POLICY_PROFILE = {
    **MACRO_CONFIG.profile(),
    **PROMPT_ASSEMBLER.profile(),
}
TRACE_LOCK = threading.Lock()
_ACTIVE_MACRO_ADVISOR = None
_LAST_AUTHORITATIVE_STATE = None
_LAST_RUN_CONTEXT = None
_COMBAT_TRACE_ENCOUNTERS = {}


def reset_authoritative_error_fallback():
    """Forget process-local state before attaching to a new attempt."""

    global _LAST_AUTHORITATIVE_STATE, _LAST_RUN_CONTEXT
    _LAST_AUTHORITATIVE_STATE = None
    _LAST_RUN_CONTEXT = None


def remember_authoritative_state(state, run_context):
    """Retain the last already-validated frame for bridge-loss auditing.

    ``load_state`` returns a fresh JSON object on every call and the controller
    does not mutate it.  Keeping that object avoids copying the full map and
    combat surface on every action while still retaining an exact state_seq.
    """

    global _LAST_AUTHORITATIVE_STATE, _LAST_RUN_CONTEXT
    if (
        not isinstance(state, dict)
        or type(state.get("state_seq")) is not int
        or state.get("in_game") is not True
        or not isinstance(run_context, dict)
        or not run_context.get("attempt_id")
    ):
        raise SafetyError("cannot retain an unbound authoritative frame")
    _LAST_AUTHORITATIVE_STATE = state
    _LAST_RUN_CONTEXT = dict(run_context)


def _validated_cached_error_context():
    state = _LAST_AUTHORITATIVE_STATE
    context = _LAST_RUN_CONTEXT
    if not isinstance(state, dict) or not isinstance(context, dict):
        return {}
    if state.get("in_game") is not True or is_game_over(state):
        return {}
    try:
        return validate_bound_context(state, context)
    except (run_context_lib.RunContextError, SafetyError):
        return {}


def operational_error_state_sequence(active_context):
    """Use a fresh sequence when readable, else the exact cached live frame."""

    try:
        fresh = stsctl.current_sequence()
    except Exception:
        fresh = 0
    if type(fresh) is not int or fresh < 0:
        fresh = 0
    cached_state = _LAST_AUTHORITATIVE_STATE
    cached_context = _LAST_RUN_CONTEXT
    if (
        isinstance(cached_state, dict)
        and isinstance(cached_context, dict)
        and isinstance(active_context, dict)
        and cached_context.get("attempt_id") == active_context.get("attempt_id")
        and type(cached_state.get("state_seq")) is int
    ):
        # The cached frame is the last complete state whose run binding was
        # validated.  A newer state-meta sequence without a readable frame is
        # not itself authoritative outcome evidence.
        return cached_state["state_seq"]
    return fresh


def operational_error_state_snapshot(active_context):
    """Return the exact cached live frame only for the same failed attempt."""

    state = _LAST_AUTHORITATIVE_STATE
    context = _LAST_RUN_CONTEXT
    if (
        not isinstance(state, dict)
        or not isinstance(context, dict)
        or not isinstance(active_context, dict)
        or context.get("attempt_id") != active_context.get("attempt_id")
        or state.get("in_game") is not True
        or is_game_over(state)
    ):
        return None
    snapshot = authoritative_state_snapshot(state)
    snapshot.update({
        "protocol_version": state.get("protocol_version"),
        "in_game": state.get("in_game"),
        "terminal_state_seq": state.get("state_seq"),
    })
    for field in ATTEMPT_BINDING_FIELDS:
        snapshot[field] = context.get(field)
    return snapshot


def attempt_trace_path(attempt_id, *, create_parent=False, trace_path=None):
    """Return the isolated append-only trace for one bound attempt."""

    raw = str(attempt_id or "").strip()
    if not raw:
        raise SafetyError("attempt trace requires a non-empty attempt id")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", raw):
        component = raw
    else:
        component = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    base_trace = Path(trace_path) if trace_path is not None else TRACE_PATH
    path = base_trace.parent / "logs" / "attempts" / component / "autoplay.log"
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


class ControllerLease:
    """OS-backed single-controller lease; stale metadata is recoverable."""

    def __init__(self, path=CONTROLLER_LOCK_PATH):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise SafetyError("another autoplay controller already owns the lease") from exc
        self.handle = handle
        metadata = json.dumps({
            "pid": os.getpid(),
            "controller_hash": CONTROLLER_HASH,
            "decision_hash": DECISION_HASH,
            "claimed_at": time.time(),
        }, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        handle.seek(0)
        handle.write(metadata)
        handle.truncate()
        handle.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle = self.handle
        self.handle = None
        if handle is None:
            return False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return False


def fingerprint(relative_paths):
    digest = hashlib.sha256()
    expanded = []
    for relative_path in relative_paths:
        path = ROOT / relative_path
        if path.is_dir():
            expanded.extend(
                child for child in path.rglob("*")
                if child.is_file()
                and child.suffix.casefold() in {".py", ".java", ".json", ".txt"}
                and "__pycache__" not in child.parts
            )
        else:
            expanded.append(path)
    for path in sorted(
        {item.resolve() for item in expanded},
        key=lambda item: item.relative_to(ROOT).as_posix(),
    ):
        relative_path = path.relative_to(ROOT).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            path.read_bytes() if path.is_file() else b"<required-path-missing>"
        )
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def decision_fingerprint():
    code_hash = fingerprint((
        "autoplay.py",
        "autoplay_runner.py",
        "campaign_attempt.py",
        "decision_case_corpus.py",
        "decision_cases.py",
        "decision_case_replay.py",
        "decision_case_resolution.py",
        "independent_oracle.py",
        "death_replay.py",
        "strategy_audit.py",
        "cohort_report.py",
        "cohort_review.py",
        "freeze_manifest.py",
        "pre_run_binding.py",
        "policy_contracts.py",
        "launch_game.py",
        "bridge.py",
        "campaign_selector.py",
        "deepseek_macro.py",
        "macro_policy.py",
        "macro_wire.py",
        "prompt_assembler.py",
        "run_context.py",
        "stsctl.py",
        "prompts/sts_macro_v1",
        "knowledge/sts_basegame_v1",
        "test_fixtures/decision-cases-v2.jsonl",
        "test_fixtures/decision-case-resolution-invariants-v1.json",
        "src/spirecomm-master/spirecomm",
        "src/CommunicationMod-1.2.1/src/main/java/communicationmod",
        "CommunicationMod.jar",
    ))
    digest = hashlib.sha256()
    digest.update(code_hash.encode("ascii"))
    digest.update(json.dumps(
        MACRO_POLICY_PROFILE,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"))
    return digest.hexdigest()[:16]


PERFORMANCE_POLICY_PATHS = (
    "deepseek_macro.py",
    "macro_policy.py",
    "macro_wire.py",
    "policy_contracts.py",
    "prompt_assembler.py",
    "prompts/sts_macro_v1",
    "knowledge/sts_basegame_v1",
    "src/spirecomm-master/spirecomm/ai",
)


def performance_fingerprint():
    """Identify live strategic behavior without audit/launcher churn.

    The decision hash remains the fail-closed source binding for one exact
    controller build. This narrower identity is only for comparing outcomes
    produced by the same combat, macro, and model policy across changes to
    supervisors, audit engines, launchers, and evidence tooling.
    """

    code_hash = fingerprint(PERFORMANCE_POLICY_PATHS)
    digest = hashlib.sha256()
    digest.update(code_hash.encode("ascii"))
    digest.update(POLICY_VERSION.encode("utf-8"))
    digest.update(json.dumps(
        MACRO_POLICY_PROFILE,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"))
    return digest.hexdigest()[:16]


def controller_fingerprint():
    return fingerprint((
        "autoplay.py",
        "autoplay_runner.py",
        "campaign_attempt.py",
        "campaign_selector.py",
        "cohort_report.py",
        "cohort_review.py",
        "freeze_manifest.py",
        "pre_run_binding.py",
        "decision_case_corpus.py",
        "decision_case_replay.py",
        "decision_case_resolution.py",
        "test_fixtures/decision-case-resolution-invariants-v1.json",
        "decision_cases.py",
        "independent_oracle.py",
        "death_replay.py",
        "strategy_audit.py",
        "run_context.py",
        "stsctl.py",
        "bridge.py",
        "launch_game.py",
        "src/spirecomm-master/spirecomm/communication",
        "src/spirecomm-master/spirecomm/spire",
        "src/CommunicationMod-1.2.1/src/main/java/communicationmod",
        "CommunicationMod.jar",
    ))


DECISION_HASH = decision_fingerprint()
PERFORMANCE_HASH = performance_fingerprint()
CONTROLLER_HASH = controller_fingerprint()
# Backward-compatible field name for historical analysis tools.  New code
# should use decision_hash/controller_hash explicitly.
STRATEGY_HASH = DECISION_HASH


def run_id(game):
    return run_context_lib.authoritative_run_id(game)


class SafetyError(RuntimeError):
    pass


class MissingReceiptOutcome:
    """Authoritative post-action state recovered after a lost final receipt."""

    def __init__(self, state):
        self.state = state


def _append_bounded_trace(path, encoded, *, max_bytes, scope, attempt_id=None):
    """Append one record without rotating or truncating active evidence."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current_size = path.stat().st_size if path.exists() else 0
    if current_size + len(encoded) > max_bytes:
        overflow = path.parent / "trace-overflow.json"
        temporary = overflow.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1,
            "kind": "trace_size_limit_exceeded",
            "scope": scope,
            "attempt_id": attempt_id,
            "trace_path": str(path),
            "current_size": current_size,
            "record_size": len(encoded),
            "max_bytes": max_bytes,
            "detected_at": time.time(),
        }, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, overflow)
        raise SafetyError(
            f"{scope} trace exceeded the fail-closed size limit: {path}"
        )
    with path.open("ab") as handle:
        handle.write(encoded)


def append_trace(record, *, trace_path=None, isolated_path=None):
    """Write bound records once, to the immutable per-attempt segment only."""

    destination = Path(trace_path) if trace_path is not None else TRACE_PATH
    encoded = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with TRACE_LOCK:
        attempt_id = record.get("attempt_id") if isinstance(record, dict) else None
        if isinstance(attempt_id, str) and attempt_id.strip():
            isolated = (
                Path(isolated_path)
                if isolated_path is not None
                else attempt_trace_path(
                    attempt_id,
                    create_parent=True,
                    trace_path=destination,
                )
            )
            _append_bounded_trace(
                isolated,
                encoded,
                max_bytes=ATTEMPT_TRACE_MAX_BYTES,
                scope="attempt",
                attempt_id=attempt_id,
            )
            return
        _append_bounded_trace(
            destination,
            encoded,
            max_bytes=GLOBAL_TRACE_MAX_BYTES,
            scope="global",
        )


def close_active_macro_advisor():
    """Drain model callbacks before a terminal/error record is persisted."""

    global _ACTIVE_MACRO_ADVISOR
    advisor = _ACTIVE_MACRO_ADVISOR
    _ACTIVE_MACRO_ADVISOR = None
    if advisor is not None:
        advisor.close(wait=True)


def persist_attempt_snapshots(result):
    """Freeze every terminal artifact beside the authoritative attempt log."""

    if not isinstance(result, dict) or not result.get("attempt_id"):
        raise SafetyError("attempt snapshots require a bound terminal result")
    directory = attempt_trace_path(result["attempt_id"], create_parent=True).parent
    context = run_context_lib.load_context(RUN_CONTEXT_PATH)
    state = stsctl.load_state()
    selection = context.get("selection") if isinstance(context, dict) else None
    if not isinstance(context, dict) or not isinstance(selection, dict):
        raise SafetyError("terminal context or selection snapshot is missing")
    validate_terminal_state_binding(state, context)
    bindings = {
        "attempt_id": result.get("attempt_id"),
        "run_id": result.get("run_id"),
        "seed": result.get("seed"),
        "decision_hash": result.get("decision_hash"),
        "controller_hash": result.get("controller_hash"),
        "selection_id": result.get("selection_id"),
        "selection_digest": result.get("selection_digest"),
        "terminal_state_seq": result.get("terminal_state_seq"),
    }
    for artifact_name, artifact in (
        ("run-context", context),
        ("terminal-state", state),
    ):
        for field, wanted in bindings.items():
            observed = artifact.get(field)
            if type(observed) is not type(wanted) or observed != wanted:
                raise SafetyError(
                    f"{artifact_name} snapshot {field} binding mismatch"
                )
    if selection.get("selection_id") != result.get("selection_id"):
        raise SafetyError("selection snapshot binding mismatch")
    selection_snapshot = {
        **selection,
        "schema_version": 2,
        "attempt_id": result.get("attempt_id"),
        "run_id": result.get("run_id"),
        "seed": result.get("seed"),
        "character": result.get("character"),
        "ascension_level": result.get("ascension_level"),
        "run_type": result.get("run_type"),
        "policy_version": result.get("policy_version"),
        "decision_hash": result.get("decision_hash"),
        "controller_hash": result.get("controller_hash"),
        "selection_id": result.get("selection_id"),
        "selection_digest": result.get("selection_digest"),
        "terminal_state_seq": result.get("terminal_state_seq"),
    }
    for name, payload in (
        ("run-context.json", context),
        ("run-result.json", result),
        ("terminal-state.json", state),
        ("selection.json", selection_snapshot),
    ):
        stsctl.atomic_write_json(directory / name, payload)
    return directory


def append_protocol_event(
    event,
    state=None,
    payload=None,
    receipt=None,
    run_context=None,
    **details,
):
    """Append a compact, cohort-bound transport recovery event."""

    state = state if isinstance(state, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    receipt = receipt if isinstance(receipt, dict) else {}
    run_context = run_context if isinstance(run_context, dict) else {}
    game = state.get("game_state") or {}
    record = {
        "record_type": "protocol_event",
        "decision_schema_version": 2,
        "time": time.time(),
        "policy_version": POLICY_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "strategy_hash": STRATEGY_HASH,
        "decision_hash": DECISION_HASH,
        "performance_hash": PERFORMANCE_HASH,
        "controller_hash": CONTROLLER_HASH,
        "macro_policy": MACRO_POLICY_PROFILE,
        "attempt_id": run_context.get("attempt_id"),
        "goal_mode": run_context.get("goal_mode"),
        "run_id": run_context.get("run_id"),
        "seed": run_context.get("seed", game.get("seed")),
        "character": run_context.get("character", game.get("class")),
        "ascension_level": run_context.get(
            "ascension_level", game.get("ascension_level")
        ),
        "run_type": run_context.get("run_type"),
        "selection_id": run_context.get("selection_id"),
        "selection_digest": run_context.get("selection_digest"),
        "event": event,
        "state_seq": state.get("state_seq"),
        "before_seq": state.get("state_seq"),
        "decision_id": state.get("decision_id"),
        "phase": state.get("phase"),
        "action": payload.get("action"),
        "request_id": payload.get("id"),
        "receipt_status": receipt.get("status"),
        "receipt_error": receipt.get("error"),
        "accepted_state_seq": receipt.get("accepted_state_seq"),
        "result_state_seq": receipt.get("result_state_seq"),
        **details,
    }
    append_trace(record)
    return record


def persist_automatic_run_audit(result):
    """Write a fail-closed quality report for the just-finished attempt.

    Auditing runs only after the terminal record has been durably appended,
    so it cannot affect combat frame time or alter a game decision.  A defect
    in the auditor must not corrupt the authoritative run result; it becomes
    an explicit inconclusive report instead of being silently treated as a
    pass.
    """

    attempt_id = result.get("attempt_id") if isinstance(result, dict) else None
    decision_hash = (
        result.get("decision_hash") if isinstance(result, dict) else None
    )
    import campaign_selector
    import cohort_report

    history_before = campaign_selector.load_history(RESULT_HISTORY_PATH)
    isolated_trace = (
        attempt_trace_path(attempt_id, create_parent=True)
        if isinstance(attempt_id, str) and attempt_id
        else None
    )
    attempt_audit_path = (
        isolated_trace.parent / "run-audit.json"
        if isolated_trace is not None else None
    )
    existing_audits = [
        record for record in history_before
        if record.get("record_type") == "run_audit"
        and record.get("attempt_id") == attempt_id
    ]
    if len(existing_audits) > 1:
        raise SafetyError("attempt history contains duplicate audits")
    if len(existing_audits) == 1:
        existing = existing_audits[0]
        report = None
        audit_engine_digest = None
        try:
            import strategy_audit

            audit_engine_digest = strategy_audit.audit_engine_sha256()
            source = (
                attempt_audit_path
                if attempt_audit_path is not None
                and attempt_audit_path.exists()
                else RUN_AUDIT_PATH
            )
            report = json.loads(source.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        if (
            not isinstance(report, dict)
            or report.get("attempt_id") != attempt_id
            or report.get("terminal_state_seq")
            != result.get("terminal_state_seq", result.get("state_seq"))
            or report.get("decision_hash") != decision_hash
            or report.get("audit_engine_sha256") != audit_engine_digest
            or existing.get("release_gate_passed")
            is not report.get("release_gate_passed")
        ):
            raise SafetyError(
                "existing audit history has no matching full audit artifact"
            )
        cohort = cohort_report.build_cohort_report(
            history_before,
            DECISION_HASH,
            performance_hash=PERFORMANCE_HASH,
        )
        cohort_report.write_report(COHORT_REPORT_PATH, cohort)
        return report
    trace_path = None
    audit_succeeded = False
    try:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SafetyError("automatic audit requires a bound attempt id")
        if not isinstance(decision_hash, str) or not decision_hash:
            raise SafetyError("automatic audit requires a decision hash")
        # Lazy import keeps the large historical auditor off the live action
        # path.  The API reads only the current attempt's contiguous trace
        # suffix rather than scanning the full append-only log.
        import strategy_audit

        isolated_trace = attempt_trace_path(attempt_id)
        trace_path = isolated_trace if isolated_trace.exists() else TRACE_PATH
        audit_engine_digest = strategy_audit.audit_engine_sha256()
        report = strategy_audit.audit_attempt_trace(
            trace_path,
            decision_hash,
            attempt_id,
        )
        if not isinstance(report, dict):
            raise SafetyError("automatic audit returned an invalid report")
        expected_binding = {
            "schema_version": 2,
            "policy_version": result.get("policy_version"),
            "attempt_id": result.get("attempt_id"),
            "run_id": result.get("run_id"),
            "seed": result.get("seed"),
            "character": result.get("character", result.get("class")),
            "ascension_level": result.get("ascension_level"),
            "run_type": result.get("run_type"),
            "decision_hash": result.get("decision_hash"),
            "controller_hash": result.get("controller_hash"),
            "selection_id": result.get("selection_id"),
            "selection_digest": result.get("selection_digest"),
            "terminal_state_seq": result.get(
                "terminal_state_seq", result.get("state_seq")
            ),
            "termination_kind": result.get("termination_kind"),
            "audit_engine_sha256": audit_engine_digest,
        }
        for field, wanted in expected_binding.items():
            observed = report.get(field)
            if type(observed) is not type(wanted) or observed != wanted:
                raise SafetyError(
                    f"automatic audit {field} binding mismatch"
                )
        import death_replay

        attempt_replay_path = isolated_trace.parent / "death-replay.json"
        embedded_replay = report.get("death_replay")
        if not isinstance(embedded_replay, dict):
            raise SafetyError("automatic audit omitted death replay")
        death_replay.write_replay(attempt_replay_path, embedded_replay)
        death_replay.write_replay(DEATH_REPLAY_PATH, embedded_replay)
        audit_succeeded = True
    except Exception as exc:
        report = {
            "schema_version": 2,
            "decision_hash": decision_hash,
            "attempt_id": attempt_id,
            "audit_status": "inconclusive",
            "issue_count": 1,
            "review_finding_count": 0,
            "eligible_unknown_count": 1,
            "oracle_disagreement_count": 0,
            "protocol_correctness": {"status": "inconclusive"},
            "mechanics_coverage": {"status": "inconclusive"},
            "strategy_quality": {"status": "inconclusive"},
            "audit_error_class": type(exc).__name__,
            "audit_error": str(exc),
            "issues": [{
                "kind": "automatic_audit_failed",
                "severity": "P1",
                "error_class": type(exc).__name__,
            }],
        }
    eligible_unknown = report.get("eligible_unknown_count")
    if type(eligible_unknown) is not int:
        eligible_unknown = 1
    oracle = report.get("independent_oracle")
    oracle_disagreements = (
        oracle.get("disagreement_count")
        if isinstance(oracle, dict) else report.get("oracle_disagreement_count")
    )
    if type(oracle_disagreements) is not int:
        oracle_disagreements = 1
    model_advice = report.get("model_advice")
    model_conflicts = (
        model_advice.get("conflicts")
        if isinstance(model_advice, dict) else 0
    )
    if type(model_conflicts) is not int:
        model_conflicts = 0
    release_gate_passed = report.get("release_gate_passed") is True
    binding_fields = {
        "schema_version": 2,
        "policy_version": result.get("policy_version"),
        "attempt_id": result.get("attempt_id"),
        "run_id": result.get("run_id"),
        "seed": result.get("seed"),
        "character": result.get("character", result.get("class")),
        "ascension_level": result.get("ascension_level"),
        "run_type": result.get("run_type"),
        "decision_hash": result.get("decision_hash"),
        "performance_hash": result.get("performance_hash"),
        "controller_hash": result.get("controller_hash"),
        "selection_id": result.get("selection_id"),
        "selection_digest": result.get("selection_digest"),
        "terminal_state_seq": result.get(
            "terminal_state_seq", result.get("state_seq")
        ),
        "termination_kind": result.get("termination_kind"),
    }
    report = {
        **binding_fields,
        **report,
        "selection": result.get("selection"),
        "generated_at": time.time(),
        "automatic": True,
        "eligible_unknown": eligible_unknown,
        "eligible_unknown_count": eligible_unknown,
        "oracle_disagreement_count": oracle_disagreements,
        "model_conflict_count": model_conflicts,
        "code_changed": False,
        "operational_error_evidence": (
            {
                "error": result.get("error"),
                "message": result.get("message"),
                "last_authoritative_state": result.get(
                    "last_authoritative_state"
                ),
            }
            if result.get("termination_kind") == "operational_error"
            else None
        ),
        "release_gate_passed": bool(
            audit_succeeded and release_gate_passed
        ),
    }
    stsctl.atomic_write_json(RUN_AUDIT_PATH, report)
    if attempt_audit_path is None:
        raise SafetyError("attempt audit path is unavailable")
    stsctl.atomic_write_json(attempt_audit_path, report)
    audit_envelope = {
        key: report.get(key)
        for key in (
            "schema_version", "policy_version", "attempt_id", "run_id",
            "seed", "character", "ascension_level", "run_type",
            "decision_hash", "controller_hash", "selection_id",
            "selection_digest", "performance_hash",
            "terminal_state_seq", "termination_kind", "generated_at",
            "audit_status", "release_gate_passed", "protocol_events",
            "operational_error_attempts", "operational_error_events",
            "protocol_correctness",
            "mechanics_coverage", "strategy_quality", "issue_count",
            "review_finding_count", "oracle_disagreement_count",
            "eligible_unknown", "model_conflict_count", "model_advice",
            "independent_oracle", "death_replay", "death_observed",
            "code_changed", "operational_error_evidence",
        )
    }
    audit_envelope["record_type"] = "run_audit"
    append_audit_history(audit_envelope)
    landed_history = campaign_selector.load_history(RESULT_HISTORY_PATH)
    cohort = cohort_report.build_cohort_report(
        landed_history,
        DECISION_HASH,
        performance_hash=PERFORMANCE_HASH,
    )
    cohort_report.write_report(COHORT_REPORT_PATH, cohort)
    return report


def _attempt_decision_case_path(record, run_context=None):
    """Return the bounded case shard for the active attempt.

    ``decision-cases.jsonl`` is a frozen historical prefix used by replay.  It
    must not grow while a live run is appending authoritative evidence.  Every
    production record is attempt-bound, so keep its optional compact case next
    to the already bounded attempt trace instead.
    """

    attempt_id = None
    if isinstance(record, dict):
        attempt_id = record.get("attempt_id")
    if not attempt_id and isinstance(run_context, dict):
        attempt_id = run_context.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id.strip():
        return (
            attempt_trace_path(attempt_id, create_parent=True).parent
            / "decision-cases.jsonl"
        )
    return DECISION_CASES_PATH


def _record_decision_case_limit(path, *, state, payload, run_context):
    """Emit one bounded, auditable marker when a live case shard is full."""

    marker = Path(path).with_name("decision-cases-limit.json")
    if not marker.exists():
        try:
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "decision_case_shard_limit",
                        "path": str(path),
                        "max_bytes": decision_cases.ATTEMPT_DECISION_CASES_MAX_BYTES,
                        "detected_at": time.time(),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError:
            # The protocol event below remains the authoritative indication;
            # failure to write this convenience marker is not a controller
            # failure.
            pass
    append_protocol_event(
        "decision_case_shard_limited",
        state,
        payload,
        run_context=run_context,
        error_class="DecisionCaseCorpusLimitError",
    )


def persist_decision_case(record, state=None, payload=None, run_context=None):
    """Persist an optional replay case without risking the live controller.

    The authoritative action trace has already been written before this
    helper is called.  A replay-corpus I/O or serialization failure therefore
    becomes an explicit protocol event, never a gameplay failure.
    """

    case_path = _attempt_decision_case_path(record, run_context)
    live_shard = case_path != DECISION_CASES_PATH
    try:
        return decision_cases.append_decision_case(
            case_path,
            record,
            max_bytes=(
                decision_cases.ATTEMPT_DECISION_CASES_MAX_BYTES
                if live_shard else decision_cases.DECISION_CASES_MAX_BYTES
            ),
        )
    except decision_cases.DecisionCaseCorpusLimitError:
        if live_shard:
            _record_decision_case_limit(
                case_path,
                state=state,
                payload=payload,
                run_context=run_context,
            )
        else:
            append_protocol_event(
                "decision_case_persist_failed",
                state,
                payload,
                run_context=run_context,
                error_class="DecisionCaseCorpusLimitError",
            )
        return False
    except (OSError, TypeError, ValueError) as exc:
        append_protocol_event(
            "decision_case_persist_failed",
            state,
            payload,
            run_context=run_context,
            error_class=type(exc).__name__,
        )
        return False


def _history_record_key(record):
    """Return the canonical identity of one complete terminal record."""

    try:
        return json.dumps(
            record,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SafetyError("result history record is not JSON serializable") from exc


def append_result_history(result):
    """Append one schema-v2 terminal event unless that exact event exists."""

    if not isinstance(result, dict) or result.get("schema_version") != 2:
        raise SafetyError("result history requires a schema-v2 record")
    if result.get("termination_kind") not in {"game_over", "operational_error"}:
        raise SafetyError("result history requires a supported termination kind")
    if not isinstance(result.get("attempt_id"), str) or not result["attempt_id"]:
        raise SafetyError("result history requires a bound attempt id")
    result_key = _history_record_key(result)
    if RESULT_HISTORY_PATH.exists():
        try:
            handle = RESULT_HISTORY_PATH.open("r", encoding="utf-8")
        except OSError as exc:
            raise SafetyError("result history could not be read") from exc
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    previous = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SafetyError(
                        f"result history line {line_number} is malformed"
                    ) from exc
                if not isinstance(previous, dict):
                    raise SafetyError(
                        f"result history line {line_number} is not an object"
                    )
                is_terminal = (
                    previous.get("record_type") == "terminal_result"
                    or previous.get("termination_kind")
                    in {"game_over", "operational_error"}
                )
                if (
                    is_terminal
                    and previous.get("attempt_id") == result.get("attempt_id")
                ):
                    if _history_record_key(previous) == result_key:
                        return False
                    raise SafetyError(
                        "result history already contains a different terminal "
                        "for this attempt"
                    )
    with RESULT_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def append_audit_history(audit):
    """Append the sole strict audit envelope for one terminal attempt."""

    if (
        not isinstance(audit, dict)
        or audit.get("record_type") != "run_audit"
        or audit.get("schema_version") != 2
        or not isinstance(audit.get("attempt_id"), str)
        or not audit.get("attempt_id")
    ):
        raise SafetyError("audit history requires a bound schema-v2 envelope")
    audit_key = _history_record_key(audit)
    if RESULT_HISTORY_PATH.exists():
        try:
            handle = RESULT_HISTORY_PATH.open("r", encoding="utf-8")
        except OSError as exc:
            raise SafetyError("result history could not be read") from exc
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    previous = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SafetyError(
                        f"result history line {line_number} is malformed"
                    ) from exc
                if (
                    isinstance(previous, dict)
                    and previous.get("record_type") == "run_audit"
                    and previous.get("attempt_id") == audit.get("attempt_id")
                ):
                    if _history_record_key(previous) == audit_key:
                        return False
                    raise SafetyError(
                        "result history already contains a different audit "
                        "for this attempt"
                    )
    with RESULT_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(audit, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
    return True


def _trace_lines_reverse(path, chunk_size=64 * 1024):
    """Yield non-empty JSONL records from the end without loading the log.

    The production trace is append-only and can grow close to a gigabyte.  A
    terminal result only needs the contiguous suffix belonging to its current
    attempt, so reading fixed-size blocks backwards keeps recovery bounded by
    that attempt instead of by all historical runs.
    """

    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def trace_action_count(attempt_id):
    """Recover confirmed actions from the current attempt's trace suffix."""

    if not isinstance(attempt_id, str) or not attempt_id:
        raise SafetyError("attempt action trace requires a bound attempt id")
    # A verified attempt always appends ``controller_start`` before it can
    # confirm an action.  Treating a deleted/missing log as a genuine
    # zero-action run would silently corrupt the permanent result record.
    isolated = attempt_trace_path(attempt_id)
    trace_path = isolated if isolated.exists() else TRACE_PATH
    if not trace_path.exists():
        raise SafetyError("attempt action trace is missing")
    count = 0
    found_attempt = False
    found_controller_start = False
    try:
        for raw_line in _trace_lines_reverse(trace_path):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SafetyError(
                    "attempt action trace contains malformed UTF-8"
                ) from exc
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SafetyError(
                    "attempt action trace contains malformed JSON"
                ) from exc
            if not isinstance(record, dict):
                continue
            record_attempt_id = record.get("attempt_id")
            if record_attempt_id == attempt_id:
                found_attempt = True
                if record.get("record_type") == "controller_start":
                    found_controller_start = True
                elif record.get("record_type") == "decision":
                    count += 1
            elif record_attempt_id:
                # Attempts are serialized: once the current attempt has been
                # seen, the next bound record is the preceding attempt's
                # boundary.  If another attempt is newest, fail closed rather
                # than scanning arbitrarily old history for a stale context.
                if found_attempt:
                    break
                raise SafetyError(
                    "attempt action trace is not the current trace segment"
                )
    except OSError as exc:
        raise SafetyError("attempt action trace could not be read") from exc
    if not found_controller_start:
        raise SafetyError("attempt action trace is missing controller_start")
    return count


def validate_bound_context(state, context):
    if state.get("in_game") is not True:
        raise SafetyError("bound run is no longer authoritatively in game")
    return run_context_lib.validate_context_binding(
        state, context, DECISION_HASH, CONTROLLER_HASH
    )


def validate_terminal_state_binding(state, context):
    """Require bridge-authored terminal metadata to match the frozen run."""

    expected = {
        "attempt_id": context.get("attempt_id"),
        "run_id": context.get("run_id"),
        "seed": context.get("seed"),
        "character": context.get("character"),
        "ascension_level": context.get("ascension_level"),
        "run_type": context.get("run_type"),
        "decision_hash": context.get("decision_hash"),
        "controller_hash": context.get("controller_hash"),
        "policy_version": context.get("policy_version"),
        "selection_id": context.get("selection_id"),
        "selection_digest": context.get("selection_digest"),
        "terminal_state_seq": state.get("state_seq"),
    }
    for field, wanted in expected.items():
        observed = state.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise SafetyError(
                f"terminal state {field} does not match run context"
            )
    return True


def option_by(state, predicate, description):
    matches = [item for item in state.get("options") or [] if predicate(item)]
    if len(matches) != 1:
        raise SafetyError(f"{description} resolved to {len(matches)} options")
    return matches[0]


def option_payload(state, option):
    return stsctl.bound_payload(
        state,
        "choose",
        option_id=option["option_id"],
        choice_index=option["choice_index"],
    )


def payload_for_action(state, game, action):
    options = state.get("options") or []
    if isinstance(action, PlayCardAction):
        target = action.target_monster
        card = action.card
        if getattr(card, "has_target", False) and target is None:
            # Never send a targeted card without an authoritative target.  A
            # narrow single-enemy recovery is safe (there is no target choice
            # to guess); multi-enemy or missing-combat states fail closed and
            # are audited as a planner/transport invariant violation.
            active = combat_predictor.active_monsters(game) if game is not None else []
            if len(active) == 1:
                target = active[0]
            else:
                raise SafetyError(
                    "targeted card action lacks a unique authoritative target"
                )
        fields = {"card_instance_id": action.card.uuid}
        if target is not None:
            raw_enemies = (((state.get("game_state") or {}).get("combat_state") or {}).get("monsters") or [])
            target_index = getattr(target, "monster_index", None)
            if not isinstance(target_index, int) or not (0 <= target_index < len(raw_enemies)):
                raise SafetyError("targeted card action target index is not authoritative")
            fields["enemy_instance_id"] = raw_enemies[target_index]["enemy_instance_id"]
        return stsctl.bound_payload(state, "play", **fields)
    if isinstance(action, PotionAction):
        potion_index = next((i for i, item in enumerate(game.potions) if item is action.potion), None)
        if potion_index is None:
            # SimpleAgent selects the first matching real potion; preserve that
            # exact current-state slot and bind its instance ID below.
            potion_index = game.potions.index(action.potion)
        raw_potion = (state.get("game_state") or {}).get("potions")[potion_index]
        fields = {
            "operation": "use" if action.use else "discard",
            "potion_instance_id": raw_potion["potion_instance_id"],
        }
        if action.target_monster is not None:
            raw_enemies = (((state.get("game_state") or {}).get("combat_state") or {}).get("monsters") or [])
            fields["enemy_instance_id"] = raw_enemies[action.target_monster.monster_index]["enemy_instance_id"]
        return stsctl.bound_payload(state, "potion", **fields)
    if isinstance(action, EndTurnAction):
        return stsctl.bound_payload(state, "end", target_id="action:end")
    if isinstance(action, ProceedAction):
        return stsctl.bound_payload(state, "proceed", target_id="action:proceed")
    if isinstance(action, CancelAction):
        return stsctl.bound_payload(state, "return", target_id="action:return")
    if isinstance(action, CardSelectAction):
        if not action.cards:
            raise SafetyError("card selection action has no card")
        screen = (state.get("game_state") or {}).get("screen_state") or {}
        selected_ids = {
            item.get("card_instance_id") or item.get("uuid")
            for item in (screen.get("selected_cards") or screen.get("selected") or [])
        }
        selected_card = next(
            (card for card in action.cards if card.uuid not in selected_ids),
            None,
        )
        if selected_card is None:
            raise SafetyError("card selection action only contains already-selected cards")
        card_id = selected_card.uuid
        option = option_by(
            state,
            lambda item: (item.get("target") or {}).get("card_instance_id") == card_id,
            f"card selection {card_id}",
        )
        return option_payload(state, option)
    if isinstance(action, CardRewardAction):
        if action.bowl:
            option = option_by(state, lambda item: (item.get("target") or {}).get("kind") == "bowl", "singing bowl")
        else:
            option = option_by(
                state,
                lambda item: (item.get("target") or {}).get("card_instance_id") == action.card.uuid,
                f"card reward {action.card.uuid}",
            )
        return option_payload(state, option)
    if isinstance(action, CombatRewardAction):
        reward_index = next(i for i, item in enumerate(game.screen.rewards) if item is action.combat_reward)
        if reward_index >= len(options):
            raise SafetyError("combat reward index is absent from current stable options")
        return option_payload(state, options[reward_index])
    if isinstance(action, ChooseMapNodeAction):
        option = option_by(
            state,
            lambda item: (item.get("target") or {}).get("kind") == "map_node"
            and (item.get("target") or {}).get("x") == action.node.x
            and (item.get("target") or {}).get("y") == action.node.y,
            f"map node ({action.node.x},{action.node.y})",
        )
        return option_payload(state, option)
    if isinstance(action, BossRewardAction):
        option = option_by(
            state,
            lambda item: ((item.get("target") or {}).get("relic") or {}).get("id") == action.relic.relic_id,
            f"boss relic {action.relic.relic_id}",
        )
        return option_payload(state, option)
    if isinstance(action, BuyCardAction):
        option = option_by(
            state,
            lambda item: (((item.get("target") or {}).get("item") or {}).get("card_instance_id")) == action.card.uuid,
            f"shop card {action.card.uuid}",
        )
        return option_payload(state, option)
    if isinstance(action, BuyRelicAction):
        option = option_by(
            state,
            lambda item: (((item.get("target") or {}).get("item") or {}).get("id")) == action.relic.relic_id,
            f"shop relic {action.relic.relic_id}",
        )
        return option_payload(state, option)
    if isinstance(action, BuyPotionAction):
        affordable = [
            potion for potion in game.screen.potions
            if potion.price <= game.gold
        ]
        potion_index = next(
            (index for index, potion in enumerate(affordable) if potion is action.potion),
            None,
        )
        if potion_index is None:
            raise SafetyError(f"shop potion {action.potion.potion_id} is absent")
        potion_options = [
            item for item in options
            if (item.get("target") or {}).get("kind") == "potion"
        ]
        if potion_index >= len(potion_options):
            raise SafetyError(f"shop potion {action.potion.potion_id} has no bound option")
        option = potion_options[potion_index]
        bound_item = (option.get("target") or {}).get("item") or {}
        if (
            bound_item.get("id") != action.potion.potion_id
            or int(bound_item.get("price") or 0) != int(action.potion.price or 0)
        ):
            raise SafetyError(f"shop potion {action.potion.potion_id} binding mismatch")
        return option_payload(state, option)
    if isinstance(action, RestAction):
        wanted = action.rest_option.name.lower()
        option = option_by(state, lambda item: str(item.get("label", "")).lower() == wanted, f"rest option {wanted}")
        return option_payload(state, option)
    if isinstance(action, ChooseAction):
        if action.name is not None:
            wanted = str(action.name).lower()
            named_options = [
                item for item in options
                if str(item.get("label", "")).lower() == wanted
            ]
            if len(named_options) == 1:
                option = named_options[0]
            else:
                # Some events expose several legal buttons with the same
                # visible label (for example all three Sensory Stone choices
                # are named "Recall").  Preserve semantic-name binding for
                # disabled-option reindexing, but disambiguate duplicate
                # labels with the action's authoritative original index.
                indexed_options = [
                    item for item in named_options
                    if item.get("choice_index") == action.choice_index
                ]
                if len(indexed_options) != 1:
                    raise SafetyError(
                        f"named choice {wanted} at index {action.choice_index} "
                        f"resolved to {len(indexed_options)} options"
                    )
                option = indexed_options[0]
        else:
            # Event actions carry CommunicationMod's original choice index,
            # not the position in the filtered stable-options array.  A
            # disabled middle option therefore leaves legal indexes such as
            # 0 and 2; indexing the array would bind the wrong choice or fail.
            option = option_by(
                state,
                lambda item: item.get("choice_index") == action.choice_index,
                f"choice index {action.choice_index}",
            )
        return option_payload(state, option)
    raise SafetyError(f"unsupported agent action: {type(action).__name__}")


def card_in_hand(state, card_id):
    hand = ((((state.get("game_state") or {}).get("combat_state") or {}).get("hand")) or [])
    return any(card.get("card_instance_id") == card_id for card in hand)


def _discard_selection_redrawn_after_reshuffle(before, after, card_id):
    """Prove a discard selection was resolved and immediately redrawn.

    Some discard effects draw enough cards to exhaust the current draw pile.
    CommunicationMod then reports the post-reshuffle hand, so the exact card
    selected for discard can legitimately be back in hand.  The normal
    HAND_SELECT verifier must remain fail-closed; only accept this exception
    when the authoritative pile cardinality and reshuffle deltas prove that
    a real discard-triggered draw occurred.
    """

    before_game = before.get("game_state") or {}
    after_game = after.get("game_state") or {}
    if (
        before_game.get("room_phase") != "COMBAT"
        or after_game.get("room_phase") != "COMBAT"
    ):
        return False
    before_screen = before_game.get("screen_state") or {}
    selected = (
        before_screen.get("selected_cards")
        or before_screen.get("selected")
        or []
    )
    if not any(
        isinstance(item, dict)
        and item.get("card_instance_id") == card_id
        for item in selected
    ):
        return False

    before_combat = before_game.get("combat_state") or {}
    after_combat = after_game.get("combat_state") or {}
    pile_names = ("hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo")
    before_piles = {
        name: before_combat.get(name) or [] for name in pile_names
    }
    after_piles = {
        name: after_combat.get(name) or [] for name in pile_names
    }
    if any(
        not isinstance(before_piles[name], list)
        or not isinstance(after_piles[name], list)
        for name in pile_names
    ):
        return False

    # The selected card is represented by the overlay, not by a combat pile,
    # in the authoritative pre-confirm frame.
    if any(
        isinstance(card, dict) and card.get("card_instance_id") == card_id
        for card in before_piles["hand"]
    ):
        return False
    after_occurrences = [
        (name, card)
        for name in pile_names
        for card in after_piles[name]
        if isinstance(card, dict)
        and card.get("card_instance_id") == card_id
    ]
    if len(after_occurrences) != 1 or after_occurrences[0][0] != "hand":
        return False

    before_selected_count = len(
        [item for item in selected if isinstance(item, dict)]
    )
    before_cycle_count = sum(
        len(before_piles[name]) for name in pile_names
    ) + before_selected_count
    after_screen = after_game.get("screen_state") or {}
    after_selected = (
        after_screen.get("selected_cards")
        or after_screen.get("selected")
        or []
    )
    after_cycle_count = sum(
        len(after_piles[name]) for name in pile_names
    ) + len([item for item in after_selected if isinstance(item, dict)])
    if before_cycle_count != after_cycle_count:
        return False

    # A plain discard grows the discard pile and leaves the draw pile alone.
    # A discard-triggered draw that returns the selected card must instead
    # consume the discard pile while replenishing the draw pile via reshuffle.
    return (
        len(after_piles["draw_pile"]) > len(before_piles["draw_pile"])
        and len(after_piles["discard_pile"]) < len(before_piles["discard_pile"])
    )


def deck_card(state, card_id):
    for card in (state.get("game_state") or {}).get("deck") or []:
        if card.get("card_instance_id") == card_id:
            return card
    return None


def verify_effect(before, payload, after, pending_selection=None):
    action = payload["action"]
    if after["state_seq"] <= before["state_seq"]:
        raise SafetyError("result state did not advance")
    if action == "play" and (after.get("game_state") or {}).get("room_phase") == "COMBAT":
        before_combat = (before.get("game_state") or {}).get("combat_state") or {}
        after_combat = (after.get("game_state") or {}).get("combat_state") or {}
        same_turn = before_combat.get("turn") == after_combat.get("turn")
        # Time Warp and similar effects can end the turn as the card resolves.
        # The exact physical card may then be shuffled and drawn into the next
        # hand, so presence is only a failed-play postcondition on the same turn.
        if (
            same_turn
            and card_in_hand(after, payload["card_instance_id"])
            and not _rebound_sweeping_beam_same_turn_redraw(
                before, after, payload
            )
        ):
            raise SafetyError("played card instance is still in hand")
    if action == "potion":
        old_id = payload["potion_instance_id"]
        if (
            any(
                item.get("potion_instance_id") == old_id
                for item in (after.get("game_state") or {}).get("potions") or []
            )
            and not _entropic_brew_effect_settled(before, after, payload)
        ):
            raise SafetyError("used/discarded potion instance is still present")
    if action == "choose":
        chosen = next(item for item in before.get("options") or [] if item["option_id"] == payload["option_id"])
        target = chosen.get("target") or {}
        kind = target.get("kind")
        if kind == "card" and before.get("phase") == "CARD_REWARD":
            source_card = target.get("card") or {}
            card_id = source_card.get("id")
            before_game = before.get("game_state") or {}
            after_game = after.get("game_state") or {}
            if before_game.get("room_phase") == "COMBAT":
                if after_game.get("room_phase") == "COMBAT":
                    pile_names = ("hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo")
                    before_combat = before_game.get("combat_state") or {}
                    after_combat = after_game.get("combat_state") or {}
                    old_count = sum(1 for pile in pile_names for item in (before_combat.get(pile) or []) if item.get("id") == card_id)
                    new_count = sum(1 for pile in pile_names for item in (after_combat.get(pile) or []) if item.get("id") == card_id)
                    if new_count <= old_count:
                        raise SafetyError("combat card reward did not create the requested card in any combat pile")
                # A lethal poison/thorns tick can end combat while the chosen
                # generated card is resolving.  Combat piles are then cleared,
                # so the authoritative COMPLETE transition is the only durable
                # postcondition and no card instance should be expected.
            else:
                before_deck = before_game.get("deck") or []
                after_deck = after_game.get("deck") or []
                old_count = sum(1 for item in before_deck if item.get("id") == card_id and item.get("upgrades") == source_card.get("upgrades"))
                new_count = sum(1 for item in after_deck if item.get("id") == card_id and item.get("upgrades") == source_card.get("upgrades"))
                if new_count <= old_count:
                    raise SafetyError("chosen card reward was not added as a new deck instance")
        if kind == "map_node":
            before_floor = (before.get("game_state") or {}).get("floor", -1)
            after_floor = (after.get("game_state") or {}).get("floor", -1)
            if after_floor <= before_floor:
                raise SafetyError("chosen route did not advance the floor")
        if kind == "relic" and before.get("phase") == "BOSS_REWARD":
            relic_id = (target.get("relic") or {}).get("id")
            if not any(item.get("id") == relic_id for item in (after.get("game_state") or {}).get("relics") or []):
                raise SafetyError("chosen boss relic was not acquired")
        if kind == "card" and before.get("phase") in {"GRID", "HAND_SELECT"}:
            card_id = target.get("card_instance_id")
            screen = (after.get("game_state") or {}).get("screen_state") or {}
            selected = screen.get("selected_cards") or screen.get("selected") or []
            selected_visible = any(item.get("card_instance_id") == card_id for item in selected)
            # Base-game transform screens clear selectedCards as soon as their
            # confirm overlay appears.  The exact instance is still bound in
            # the receipt and is verified against the deck after confirm.
            confirm_overlay = bool(screen.get("confirm_up")) and not (after.get("options") or [])
            remaining_ids = {
                (item.get("target") or {}).get("card_instance_id")
                for item in (after.get("options") or [])
            }
            # A card effect can replace one GRID with another GRID while
            # keeping the same number of candidates (for example a discard
            # pile selection opened by Headbutt).  The bridge already treats
            # a changed decision surface as the authoritative acknowledgement
            # for that accepted, bound choice.  Mirror that rule here instead
            # of assuming every valid selection must shrink the option list.
            decision_surface_changed = (
                after.get("decision_id") != before.get("decision_id")
                or {
                    (item.get("target") or {}).get("card_instance_id")
                    for item in (before.get("options") or [])
                }
                != remaining_ids
            )
            selection_progress = (
                after.get("phase") == before.get("phase")
                and card_id not in remaining_ids
                and decision_surface_changed
            )
            if after.get("phase") in {"GRID", "HAND_SELECT"} and not (selected_visible or confirm_overlay or selection_progress):
                raise SafetyError("selected card instance is neither visible nor on a confirm overlay")
            if before.get("phase") == "GRID" and after.get("phase") != "GRID":
                for old in pending_selection or []:
                    if "AstrolabeTransform" in str(old.get("_selection_action")) and deck_card(after, old.get("card_instance_id")) is not None:
                        raise SafetyError(f"Astrolabe did not transform requested card instance {old.get('card_instance_id')}")
    if action == "proceed" and before.get("phase") in {"GRID", "HAND_SELECT"}:
        screen = (before.get("game_state") or {}).get("screen_state") or {}
        selected = screen.get("selected_cards") or []
        effective_selected = list(selected)
        for pending in pending_selection or []:
            if not any(item.get("card_instance_id") == pending.get("card_instance_id") for item in effective_selected):
                effective_selected.append(pending)
        for old in effective_selected:
            card_id = old.get("card_instance_id")
            current = deck_card(after, card_id)
            if screen.get("for_upgrade") and (current is None or current.get("upgrades", 0) <= old.get("upgrades", 0)):
                raise SafetyError(f"upgrade was not applied to requested card instance {card_id}")
            if (screen.get("for_purge") or screen.get("for_transform")) and current is not None:
                raise SafetyError(f"remove/transform did not remove requested card instance {card_id}")
            if before.get("phase") == "HAND_SELECT":
                selection_action = str(old.get("_selection_action") or before_game_action(before) or "")
                still_in_combat = (after.get("game_state") or {}).get("room_phase") == "COMBAT"
                removes_from_hand = any(
                    marker in selection_action
                    for marker in ("Discard", "Exhaust", "PutOnBottom", "PutOnDeck", "Setup")
                )
                retains_in_hand = "Retain" in selection_action
                present = card_in_hand(after, card_id)
                if (
                    still_in_combat
                    and removes_from_hand
                    and present
                    and not _discard_selection_redrawn_after_reshuffle(
                        before, after, card_id
                    )
                ):
                    raise SafetyError(f"{selection_action} left requested card in hand: {card_id}")
                if still_in_combat and retains_in_hand and not present and not old.get("ethereal"):
                    raise SafetyError(f"{selection_action} failed to retain requested card: {card_id}")


def verify_missing_receipt_effect(
    before, payload, after, pending_selection=None
):
    """Prove a lost-receipt action from an action-specific postcondition.

    A changed decision binding only proves that *something* progressed while
    the original receipt was unavailable.  It does not prove that an accepted
    write succeeded.  Keep this allow-list deliberately narrow: actions whose
    existing verifier binds an exact card, potion, map node, or relic may be
    recovered, while event/shop choices and generic screen transitions fail
    closed instead of being guessed from a phase change.
    """

    verify_effect(before, payload, after, pending_selection)
    action = payload.get("action")

    # WAIT has no durable gameplay target; an authoritative later frame is its
    # complete effect and replaying it is unnecessary.
    if action == "wait":
        return

    if action == "end":
        before_game = before.get("game_state") or {}
        after_game = after.get("game_state") or {}
        before_turn = (before_game.get("combat_state") or {}).get("turn")
        after_turn = (after_game.get("combat_state") or {}).get("turn")
        after_room_phase = after_game.get("room_phase")
        left_combat = (
            before_game.get("room_phase") == "COMBAT"
            and isinstance(after_room_phase, str)
            and after_room_phase != "COMBAT"
        )
        terminal = after_game.get("screen_type") == "GAME_OVER"
        turn_advanced = (
            type(before_turn) is int
            and type(after_turn) is int
            and after_turn > before_turn
        )
        if not (turn_advanced or left_combat or terminal):
            raise SafetyError(
                "missing END receipt has no authoritative turn advance"
            )
        return

    # The generic verifier proves the exact card left the hand (or the combat
    # resolved while it was playing) and the exact potion instance disappeared.
    if action == "play":
        before_game = before.get("game_state") or {}
        if before_game.get("room_phase") != "COMBAT":
            raise SafetyError("missing PLAY receipt was not bound to combat")
        after_game = after.get("game_state") or {}
        after_room_phase = after_game.get("room_phase")
        if after_room_phase == "COMBAT":
            return
        if (
            after_game.get("screen_type") == "GAME_OVER"
            or (
                isinstance(after_room_phase, str)
                and after_room_phase != "COMBAT"
            )
        ):
            return
        raise SafetyError(
            "missing PLAY receipt has no authoritative combat transition"
        )

    if action == "potion":
        return

    if action == "choose":
        chosen = next(
            (
                item
                for item in before.get("options") or []
                if item.get("option_id") == payload.get("option_id")
            ),
            None,
        )
        target = (chosen or {}).get("target") or {}
        kind = target.get("kind")
        phase = before.get("phase")
        exact_choice = (
            (phase == "CARD_REWARD" and kind == "card")
            or (phase == "MAP" and kind == "map_node")
            or (phase == "BOSS_REWARD" and kind == "relic")
            or (phase in {"GRID", "HAND_SELECT"} and kind == "card")
        )
        if not exact_choice:
            raise SafetyError(
                "missing CHOOSE receipt has no exact target postcondition"
            )
        return

    if action == "proceed" and before.get("phase") in {
        "GRID", "HAND_SELECT",
    }:
        screen = (before.get("game_state") or {}).get("screen_state") or {}
        selected = list(screen.get("selected_cards") or [])
        selected.extend(pending_selection or [])
        if (
            not selected
            or after.get("phase") in {before.get("phase"), "ERROR"}
        ):
            raise SafetyError(
                "missing selection PROCEED receipt has no confirmed transition"
            )
        return

    if action == "key" and before.get("phase") == "OVERLAY_SETTINGS":
        if after.get("phase") in {"OVERLAY_SETTINGS", "ERROR"}:
            raise SafetyError(
                "missing overlay KEY receipt did not close the settings overlay"
            )
        return

    raise SafetyError(
        f"missing {str(action or 'unknown').upper()} receipt has no supported "
        "action-specific postcondition"
    )


def record_confirmed_effect(agent, game, before, payload):
    """Update policy-local limits only after ``verify_effect`` has succeeded."""

    if payload.get("action") == "choose" and before.get("phase") in {
        "GRID", "HAND_SELECT",
    }:
        selected = next(
            (
                option
                for option in before.get("options") or []
                if option.get("option_id") == payload.get("option_id")
            ),
            None,
        )
        card_uuid = ((selected or {}).get("target") or {}).get(
            "card_instance_id"
        )
        if card_uuid:
            agent.confirm_card_selection(game, card_uuid)
    if payload.get("action") == "play":
        card_uuid = payload.get("card_instance_id")
        if card_uuid:
            agent.confirm_card_play(game, card_uuid)
    if (
        payload.get("action") == "potion"
        and payload.get("operation") == "use"
        and (before.get("game_state") or {}).get("room_phase") == "COMBAT"
    ):
        agent.confirm_potion_use(game)


def before_game_action(state):
    return (state.get("game_state") or {}).get("current_action")


def recoverable_protocol_error(receipt, authoritative_state=None):
    """Classify a bridge precondition race that is safe to re-plan.

    A state-changing command is only sent to the game after the bridge has
    resolved its card/potion/enemy binding.  When a CommunicationMod frame
    changes between the controller's read and that resolution, the bridge
    rejects the command before dispatch (for example, a transient combat
    frame can briefly expose no unique enemy).  Retrying the *same* payload
    would be unsafe, but a bounded read-only resync followed by a fresh
    planner decision is safe.  Keep this list exact: generic rejections still
    fail closed.
    """

    error = str(receipt.get("error") or "")
    if receipt.get("status") == "failed" and "Invalid command:" in error:
        return True
    if receipt.get("status") != "rejected":
        return False
    if any(
        marker in error
        for marker in (
            "card_instance_id does not resolve uniquely in current hand",
            "bound card is not playable",
            "enemy_instance_id does not resolve uniquely",
            "bound enemy is not a legal target",
            "potion_instance_id does not resolve uniquely",
            "non-targeted card must not include enemy_instance_id",
            "non-targeted potion operation must not include enemy_instance_id",
        )
    ):
        return True

    # A route choice can enter a new combat between the controller's state
    # read and bridge command resolution. The old frame still contains a
    # legal-looking CHOOSE target, while the authoritative frame has already
    # switched to COMBAT_INITIALIZING and exposes only STATE/WAIT until the
    # monster intent is rolled. Re-planning after a bounded read-only resync
    # is safe; replaying the stale choice is not. Keep the condition narrow
    # so an ordinary planner/phase bug remains fail-closed.
    if "command is not legal in current state: choose" in error.lower():
        current = authoritative_state
        if not isinstance(current, dict):
            try:
                current = stsctl.load_state()
            except Exception:
                current = {}
        return (
            current.get("phase") == "COMBAT_INITIALIZING"
            and current.get("ready_for_command") is not True
            and current.get("unstable_reason") == "uninitialized_monster_intent"
        )
    return False


def overlay_recovery_payload(state):
    """Return the one safe bound action for a recognized blocking overlay."""

    game = state.get("game_state") or {}
    if (
        state.get("phase") == "OVERLAY_SETTINGS"
        and str(game.get("screen_name") or "").upper() == "SETTINGS"
        and "key" in {str(item).lower() for item in state.get("legal_actions") or []}
    ):
        return stsctl.bound_payload(
            state,
            "key",
            key="CANCEL",
            target_id="key:CANCEL",
        )
    return None


def action_progress_vector(state):
    """Return authoritative gameplay fields that must not change in a live-lock.

    Repeating an action pattern is not itself evidence of a loop: deterministic
    card engines can alternate the same two cards while steadily reducing an
    enemy's HP.  Keep this vector limited to durable gameplay progress so UI
    animation details do not hide a genuine open/close cycle.
    """

    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}

    def powers(character):
        return tuple(sorted(
            (
                str(power.get("id") or power.get("name") or ""),
                int(power.get("amount") or 0),
            )
            for power in character.get("powers") or []
        ))

    def cards(pile):
        return tuple(
            str(card.get("card_instance_id") or card.get("uuid") or card.get("id") or "")
            for card in pile or []
        )

    monsters = tuple(
        (
            str(monster.get("enemy_instance_id") or monster.get("id") or monster.get("name") or ""),
            int(monster.get("current_hp") or 0),
            int(monster.get("block") or 0),
            bool(monster.get("half_dead")),
            bool(monster.get("is_gone")),
            str(monster.get("intent") or ""),
            int(monster.get("move_adjusted_damage") or 0),
            int(monster.get("move_hits") or 0),
            powers(monster),
        )
        for monster in combat.get("monsters") or []
    )
    relics = tuple(
        (
            str(relic.get("id") or relic.get("name") or ""),
            int(relic.get("counter") or 0),
        )
        for relic in game.get("relics") or []
    )
    orbs = tuple(
        (
            str(orb.get("id") or orb.get("name") or ""),
            int(orb.get("passive_amount") or 0),
            int(orb.get("evoke_amount") or 0),
        )
        for orb in player.get("orbs") or []
    )
    return (
        int(game.get("current_hp") or 0),
        int(game.get("max_hp") or 0),
        int(game.get("gold") or 0),
        int(combat.get("turn") or 0),
        int(player.get("energy") or 0),
        int(player.get("block") or 0),
        powers(player),
        orbs,
        monsters,
        cards(combat.get("hand")),
        cards(combat.get("draw_pile")),
        cards(combat.get("discard_pile")),
        cards(combat.get("exhaust_pile")),
        relics,
        tuple(
            str(potion.get("potion_instance_id") or potion.get("id") or "")
            for potion in game.get("potions") or []
        ),
    )


def action_cycle_signature(state, payload):
    """Compact semantic signature used to detect successful A↔B live-locks."""

    game = state.get("game_state") or {}
    return (
        game.get("seed"),
        game.get("act"),
        game.get("floor"),
        state.get("phase"),
        state.get("decision_id"),
        payload.get("action"),
        payload.get("option_id"),
        payload.get("card_instance_id"),
        payload.get("potion_instance_id"),
        payload.get("enemy_instance_id"),
        payload.get("target_id"),
        action_progress_vector(state),
    )


def repeating_action_cycle(signatures, max_period=4, repeats=4):
    """Return a repeated tail pattern, or ``None`` when progress is plausible."""

    values = list(signatures)
    for period in range(1, max_period + 1):
        width = period * repeats
        if len(values) < width:
            continue
        pattern = values[-period:]
        if values[-width:] == pattern * repeats:
            return pattern
    return None


def _barricade_progress_action_visible(state):
    """Return whether the current frame exposes a legal Attack.

    Barricade liveness is a controller fuse, not a reason to abort a valid
    fight while the player still has a concrete way to reduce the retained
    enemy Block. The check deliberately uses only authoritative hand, energy,
    target, and card-cost fields; missing fields fail closed.
    """

    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    hand = combat.get("hand")
    if not isinstance(player, dict) or not isinstance(hand, list):
        return False
    if any(
        _token(power.get("id") or power.get("name"))
        in {"entangled", "entangledpower"}
        for power in player.get("powers") or []
        if isinstance(power, dict)
    ):
        return False
    energy = player.get("energy")
    if type(energy) is not int or energy <= 0:
        return False
    living = [
        monster for monster in combat.get("monsters") or []
        if isinstance(monster, dict)
        and type(monster.get("current_hp")) is int
        and monster["current_hp"] > 0
        and not bool(monster.get("half_dead"))
        and not bool(monster.get("is_gone"))
    ]
    if not living:
        return False
    for card in hand:
        if not isinstance(card, dict) or card.get("is_playable") is False:
            continue
        if _token(card.get("type") or card.get("card_type")) != "attack":
            continue
        cost = card.get("cost")
        if type(cost) is not int:
            cost = card.get("current_cost")
        if type(cost) is not int:
            continue
        affordable = energy > 0 if cost == -1 else 0 <= cost <= energy
        if not affordable:
            continue
        damage = max(
            int(card.get("damage") or 0),
            int(card.get("base_damage") or 0),
        )
        if damage > 0:
            return True
    return False


def _barricade_loss_is_immediately_lethal(state):
    """Return whether the visible enemy move can kill before more progress.

    A liveness fuse should not turn a soon-to-be-authoritative normal death
    into an operational controller error. The bridge's move-adjusted damage
    is already the resolved per-hit value, so this conservative check only
    subtracts the player's current Block and does not invent debuffs.
    """

    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    hp = game.get("current_hp")
    block = player.get("block")
    if type(hp) is not int or hp <= 0 or type(block) is not int:
        return False
    incoming = 0
    for monster in combat.get("monsters") or []:
        if not isinstance(monster, dict):
            continue
        if (
            type(monster.get("current_hp")) is not int
            or monster["current_hp"] <= 0
            or bool(monster.get("half_dead"))
            or bool(monster.get("is_gone"))
        ):
            continue
        if "attack" not in _token(monster.get("intent")):
            continue
        damage = max(0, int(monster.get("move_adjusted_damage") or 0))
        hits = max(1, int(monster.get("move_hits") or 0))
        incoming += damage * hits
    return incoming > 0 and max(0, incoming - max(0, block)) >= hp


def advance_barricade_liveness_guard(tracker, state):
    """Return the next immutable no-progress tracker for one narrow combat.

    Spheric Guardian retains Block through ``Barricade``.  A policy which only
    values HP damage can therefore defend forever while the retained Block
    grows.  Sample at most once per authoritative combat turn and fail closed
    only after twelve *complete* turns with neither enemy-HP progress nor a
    net reduction from the anchor Block.  Bosses, multi-enemy fights, and
    enemies without Barricade are deliberately outside this safety fuse.
    """

    game = state.get("game_state") or {}
    if (
        game.get("room_phase") != "COMBAT"
        or _token(game.get("room_type")) == "monsterroomboss"
    ):
        return None
    combat = game.get("combat_state") or {}
    turn = combat.get("turn")
    if type(turn) is not int or turn < 1:
        return None
    living = [
        monster
        for monster in combat.get("monsters") or []
        if type(monster.get("current_hp")) is int
        and monster["current_hp"] > 0
        and not bool(monster.get("half_dead"))
        and not bool(monster.get("is_gone"))
    ]
    if len(living) != 1:
        return None
    monster = living[0]
    if not any(
        _token(power.get("id") or power.get("name"))
        in {"barricade", "barricadepower"}
        for power in monster.get("powers") or []
    ):
        return None
    enemy_id = str(monster.get("enemy_instance_id") or "").strip()
    hp = monster.get("current_hp")
    block = monster.get("block")
    if not enemy_id or type(block) is not int or block < 0:
        return None

    combat_key = (
        state.get("attempt_id"),
        state.get("run_id"),
        game.get("seed"),
        game.get("act"),
        game.get("floor"),
        game.get("room_type"),
    )
    same_scope = (
        isinstance(tracker, dict)
        and tracker.get("combat_key") == combat_key
        and tracker.get("enemy_id") == enemy_id
        and type(tracker.get("last_turn")) is int
        and type(tracker.get("anchor_turn")) is int
        and type(tracker.get("anchor_hp")) is int
        and type(tracker.get("anchor_block")) is int
    )
    if not same_scope or turn < tracker["last_turn"]:
        return {
            "combat_key": combat_key,
            "enemy_id": enemy_id,
            "anchor_turn": turn,
            "anchor_hp": hp,
            "anchor_block": block,
            "last_turn": turn,
        }
    if turn == tracker["last_turn"]:
        return tracker
    if hp < tracker["anchor_hp"] or block < tracker["anchor_block"]:
        return {
            "combat_key": combat_key,
            "enemy_id": enemy_id,
            "anchor_turn": turn,
            "anchor_hp": hp,
            "anchor_block": block,
            "last_turn": turn,
        }

    complete_turns = turn - tracker["anchor_turn"]
    if complete_turns >= BARRICADE_LIVENESS_FULL_TURNS:
        # Do not abort while this authoritative frame still exposes a legal
        # Attack (which can reduce Barricade's retained Block), or when the
        # current move is already lethal. Reset the anchor for one bounded
        # grace window and let the normal planner/game-over path resolve it.
        # If neither condition holds, preserve the fail-closed fuse for a
        # genuine no-action live-lock.
        if (
            _barricade_progress_action_visible(state)
            or _barricade_loss_is_immediately_lethal(state)
        ):
            return {
                **tracker,
                "anchor_turn": turn,
                "anchor_hp": hp,
                "anchor_block": block,
                "last_turn": turn,
            }
        raise SafetyError(
            "Barricade liveness guard detected no net enemy durability "
            f"progress for {complete_turns} complete turns: "
            f"enemy={enemy_id} anchor_turn={tracker['anchor_turn']} "
            f"turn={turn} hp={tracker['anchor_hp']}->{hp} "
            f"block={tracker['anchor_block']}->{block}"
        )
    return {
        **tracker,
        "last_turn": turn,
    }


_AUDIT_FIELDS = {
    "kind", "id", "name", "type", "rarity", "tier", "upgrades", "price",
    "card_instance_id", "potion_instance_id", "reward_type", "gold",
    "x", "y", "symbol", "act", "event_id", "slot",
    "cost", "is_playable", "has_target", "damage", "base_damage", "block", "base_block",
    "magic_number", "exhausts", "action", "consequence_contract",
    "probabilistic_outcomes", "probability", "hp_delta", "max_hp_delta",
    "gold_delta", "card_changes", "relic_changes", "potion_changes",
    "curse", "current_cost", "future_costs", "gain", "remove",
    "upgrade", "transform", "replace", "counter", "amount", "authority",
    "reason", "status", "omamori_applicable", "omamori_charges_consumed",
    "mechanism_id", "choice_index", "original_button_index", "disabled",
    "operation", "potion_id", "event_contract", "event_class", "event_stage",
    "option_kind", "gold_gain", "gold_loss", "hp_damage",
    "parent_phase", "can_discard", "can_use", "requires_target",
    "neow_contract", "contract_version", "contract_kind", "reward_kind",
    "drawback_kind", "parameters", "hp_bonus", "cursed",
    "rest_option",
    "drawback_def_kind", "screen_num", "resource_effect",
    "parent_choice_context", "source_option_id", "source_choice_index",
    "select_count", "selection_domain", "cards", "selection_context",
    "current_action",
    "relic_id", "card_type", "in_bottle_flame", "in_bottle_lightning",
    "in_bottle_tornado",
    "max_cards", "can_pick_zero", "selected",
    "audit_projection_version",
    "misc", "combat_cost", "free_to_play_once", "retain", "ethereal",
}


AUDIT_PROJECTION_VERSION = 3


def _compact_typed_contract(value, *, _depth=0):
    """Clone a small typed contract without dropping nested mechanism keys."""

    if _depth > 8:
        return None
    if value is None or type(value) in {bool, int, float, str}:
        if isinstance(value, str) and len(value) > 1024:
            return None
        return value
    if isinstance(value, list):
        if len(value) > 64:
            return None
        return [
            _compact_typed_contract(item, _depth=_depth + 1)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 64 or any(
            not isinstance(key, str) or len(key) > 128
            for key in value
        ):
            return None
        return {
            key: _compact_typed_contract(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    return None


def compact_audit_value(value):
    if isinstance(value, list):
        return [compact_audit_value(item) for item in value]
    if isinstance(value, dict):
        compacted = {}
        for key, item in value.items():
            if key in {"event_contract", "neow_contract"}:
                compacted[key] = _compact_typed_contract(item)
            elif key in _AUDIT_FIELDS or key in {
                "card", "relic", "potion", "item", "reward", "link",
            }:
                compacted[key] = compact_audit_value(item)
        return compacted
    return value


def compact_option(option):
    target = compact_audit_value(option.get("target") or {})
    if isinstance(target, dict) and target:
        # This is audit-schema provenance, not base-game mechanism authority.
        # Persist it on every newly compacted target so independent replay can
        # apply newly added mechanics without retroactively reinterpreting
        # immutable historical records that predate those mechanics tables.
        target["audit_projection_version"] = AUDIT_PROJECTION_VERSION
    return {
        "option_id": option.get("option_id"),
        "choice_index": option.get("choice_index"),
        # Required as raw display evidence by the replay schema.  Identity
        # and mechanics bind exclusively through the typed target below;
        # label/text are deliberately absent from ``_AUDIT_FIELDS`` so they
        # can never become target authority.
        "label": option.get("label"),
        "target": target,
    }


_STRATEGIC_NONCOMBAT_PHASES = {
    "BOSS_REWARD", "CARD_REWARD", "CHEST", "COMBAT_REWARD", "EVENT",
    "GRID", "HAND_SELECT", "MAP", "REST", "SHOP_ROOM",
    "SHOP_SCREEN", "SAPPHIRE_KEY", "NEOW",
}


def has_strategic_noncombat_surface(state, payload=None):
    """Classify real choice surfaces from raw protocol state.

    Transition-only confirmation frames and navigation-only MAP/GRID cancel
    commands are not competing strategy candidates.  They remain ordinary
    bound decision records, but must not create fake missing-score findings.
    """

    phase = str((state or {}).get("phase") or "").upper()
    if phase not in _STRATEGIC_NONCOMBAT_PHASES:
        return False
    # ``wait`` advances an animation/transition frame; it does not commit one
    # of the currently rendered semantic options.  Match and Keep in
    # particular keeps the remaining cards visible while the previous pair
    # flips back over.  Treating that transport wait as a strategic choice
    # fabricates candidate rows with no producer and makes the independent
    # audit report every visible card as an unbound policy alternative.
    if str((payload or {}).get("action") or "").strip().casefold() in {
        "wait", "state",
    }:
        return False
    if _resource_preparation_options(state, payload or {}):
        return True
    options = [item for item in (state.get("options") or []) if isinstance(item, dict)]
    if not options:
        return False
    return True


def _audit_choice_aliases(value):
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
        prefix, suffix = raw.split(":", 1)
        if prefix in {"event", "match", "neow", "option"}:
            aliases.update(_audit_choice_aliases(suffix.rsplit(":", 1)[-1]))
        elif prefix == "shop":
            aliases.update(_audit_choice_aliases(raw.split(":")[-2]))
    if "@" in raw:
        aliases.update(_audit_choice_aliases(raw.rsplit("@", 1)[-1]))
    if raw in {"cancel", "leave", "return", "skip", "decline"}:
        aliases.update({"action:return", "actionreturn"})
    if raw in {"continue", "done", "proceed"}:
        aliases.update({"action:proceed", "actionproceed"})
    return aliases


def _aliases_from_mapping(value):
    aliases = set()
    if not isinstance(value, dict):
        return aliases
    for key in (
        "id", "name", "label", "option_id", "choice_index", "item_id",
        "card_instance_id", "potion_instance_id", "relic_id", "reward_type",
    ):
        aliases.update(_audit_choice_aliases(value.get(key)))
    for key in ("card", "relic", "potion", "item", "reward", "target"):
        aliases.update(_aliases_from_mapping(value.get(key)))
    if all(value.get(key) is not None for key in ("symbol", "x", "y")):
        aliases.update(_audit_choice_aliases(
            f'{value["symbol"]}@{value["x"]},{value["y"]}'
        ))
        aliases.update(_audit_choice_aliases(
            f'map:{value["x"]},{value["y"]}'
        ))
    return aliases


def _decision_candidate_rows(decision):
    decision = decision if isinstance(decision, dict) else {}
    rows = []
    for source, candidates in (
        ("local", decision.get("candidates")),
        (
            "model_replay",
            (((decision.get("model_advice") or {}).get("replay") or {}).get(
                "candidates"
            )),
        ),
    ):
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            candidate_id = candidate.get(
                "candidate_id", candidate.get("choice_id", candidate.get("id"))
            )
            facts = dict(candidate.get("facts") or {})
            original_facts = dict(facts)
            for key in (
                "selection_eligible", "veto_reason", "choice_index",
                "action", "semantic_id", "uncertainty", "local_reason",
                "reason_codes", "score_rule_id", "score_formula",
                "score_inputs", "score_components", "operation",
            ):
                if key in candidate:
                    facts.setdefault(key, candidate.get(key))
            aliases = _audit_choice_aliases(candidate_id)
            aliases.update(_aliases_from_mapping(facts))
            aliases.update(_audit_choice_aliases(candidate.get("choice_id")))
            aliases.update(_audit_choice_aliases(candidate.get("semantic_id")))
            aliases.update(_audit_choice_aliases(candidate.get("choice_index")))
            consequence_present = "consequences" in candidate
            consequences = candidate.get("consequences")
            if not consequence_present and "consequences" in original_facts:
                consequence_present = True
                consequences = original_facts.get("consequences")
            if (
                isinstance(consequences, dict)
                and any(
                    key in _PRODUCER_SCORING_FIELDS_V2
                    for key in consequences
                )
            ):
                # Persist the partition vocabulary used for this raw producer
                # row.  Historical candidates have no marker and continue to
                # use the v1 vocabulary during independent replay.
                candidate.setdefault("producer_partition_version", 2)
            rows.append({
                "candidate_id": None if candidate_id is None else str(candidate_id),
                "aliases": aliases,
                "local_score": candidate.get(
                    "local_score", candidate.get("score")
                ),
                "facts": facts,
                "consequences": consequences,
                "consequence_present": consequence_present,
                "raw_candidate": _audit_clone(candidate),
                "typed_binding_complete": bool(
                    ("choice_index" in candidate or "choice_index" in original_facts)
                    and ("action" in candidate or "action" in original_facts)
                    and (
                        str(candidate.get("action", original_facts.get("action")) or "").casefold()
                        != "potion"
                        or "operation" in candidate
                        or "operation" in original_facts
                    )
                ),
                "probability_outcomes": (
                    candidate.get("probability_outcomes")
                    or facts.get("probability_outcomes")
                ),
                "uncertainty": candidate.get(
                    "uncertainty", facts.get("uncertainty")
                ),
                "source": source,
            })
    explicit = decision.get("candidate_consequences")
    if isinstance(explicit, dict):
        for candidate_id, consequences in explicit.items():
            rows.append({
                "candidate_id": str(candidate_id),
                "aliases": _audit_choice_aliases(candidate_id),
                "local_score": None,
                "facts": {},
                "consequences": consequences,
                "consequence_present": True,
                "raw_candidate": {
                    "candidate_id": str(candidate_id),
                    "consequences": _audit_clone(consequences),
                },
                "typed_binding_complete": False,
                "source": "explicit_consequence",
            })
    return rows


def _audit_clone(value):
    """Copy a JSON-shaped producer value without applying protocol filters."""

    if isinstance(value, dict):
        return {str(key): _audit_clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_clone(item) for item in value]
    if isinstance(value, tuple):
        return [_audit_clone(item) for item in value]
    return value


_CONSEQUENCE_KNOWLEDGE_FIELDS = (
    "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
    "relic_changes", "potion_changes", "curse", "probabilistic_outcomes",
    "current_cost", "future_costs",
)

_PRODUCER_EFFECT_FIELDS = {
    "hp_delta", "current_hp_delta", "max_hp_delta", "gold_delta",
    "card_changes", "relic_changes", "potion_changes", "curse",
    "probabilistic_outcomes", "probability_outcomes", "current_cost",
    "future_costs", "route", "event_id", "campfire_option",
    "selected_card", "key_changes", "operation", "deck_size_delta",
    "max_hp_gain", "post_purchase_gold", "price", "leave",
    "potion_id", "potion_slot", "bound_purchase_potion_id",
    "bound_purchase_listing_id", "bound_purchase_choice_index",
    "bound_purchase_item_id", "bound_purchase_price",
    "bound_reward_potion_id", "mechanism_id", "neow_contract",
    "reward_kind", "drawback_kind", "parameters", "random_effects",
    "acquired_benefit",
    "reward_type", "relic_id", "rest_option", "card_id",
    "card_instance_id",
}

_PRODUCER_SCORING_FIELDS_V1 = {
    "path_survival_risk", "route_summary", "score_components", "utility",
    "value", "quality", "priority", "risk", "penalty", "bonus",
    "reason", "reason_codes", "uncertainty", "strategic_value",
    "card_gain_requires_successful_pair", "known_curse",
    "known_pair_count", "match_stage", "revealed_label", "unknown_card",
    "card_change_kind", "card_delta", "card_delta_max", "card_delta_min",
    "card_gain", "card_loss", "card_option_value_model",
    "card_reward_candidates_per_screen", "card_reward_pool",
    "emerald_route_pressure", "event_outcome_id",
    "expected_card_option_value", "hp_change_kind", "lost_card",
    "marginal_card_option_values", "optional_card_reward_count",
    "selection_optional", "shop_arrival_survival_floor",
}
_PRODUCER_SCORING_FIELDS_V2 = {
    "first_shop_arrival_hp",
    "question_room_value_components",
    "relic_delta", "relic_value", "potion_delta",
    "card_delta", "upgrade_delta", "upgrade_value", "best_removal_value",
    "empty_slots_filled", "replacement_choices", "replacement_option_value",
    "legal", "sozu_blocks", "hp_loss_type", "lethal", "curse_delta",
    "raw_curse_delta", "effective_curse_delta", "curse_probability",
    "expected_effective_curse_delta", "expected_omamori_charge_use",
    "omamori_charges_before", "omamori_charges_after_if_triggered",
    "expected_omamori_charges_after", "omamori_prevented_curse_delta",
    "curse_change_kind", "curse_id", "curse_trade_profile",
    "potion_loss", "potion_gain", "potion_change_kind", "lost_potion",
    "hp_gain_before_damage", "damage_amount", "upgraded_card_delta",
    "healing_locked", "rare_relic_delta", "combat_rewards",
    "future_combat_gold",
    "nominal_hp_loss", "card_transform_delta", "reward_value",
    "best_upgrade_value", "combat_delta", "expected_hp_loss",
    "death_risk", "upper_hp_loss",
}
_PRODUCER_SCORING_FIELDS = (
    _PRODUCER_SCORING_FIELDS_V1 | _PRODUCER_SCORING_FIELDS_V2
)


def _producer_partition_scoring_fields(raw_candidate):
    if (
        isinstance(raw_candidate, dict)
        and raw_candidate.get("producer_partition_version") == 2
    ):
        return _PRODUCER_SCORING_FIELDS
    return _PRODUCER_SCORING_FIELDS_V1


def _split_producer_consequence(row):
    """Separate producer effect claims from scoring features, preserving raw."""

    present = bool(isinstance(row, dict) and row.get("consequence_present"))
    raw = row.get("consequences") if isinstance(row, dict) else None
    claim = {}
    scoring = {}
    unclassified = []
    raw_candidate = (
        row.get("raw_candidate") if isinstance(row, dict) else None
    )
    scoring_fields = _producer_partition_scoring_fields(raw_candidate)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in _PRODUCER_EFFECT_FIELDS:
                claim[key] = _audit_clone(value)
            elif key in scoring_fields:
                scoring[key] = _audit_clone(value)
            else:
                unclassified.append(str(key))
    elif present:
        unclassified.append("<non_object_consequences>")
    return {
        "raw": {"present": present, "value": _audit_clone(raw)},
        "claim": claim,
        "scoring": scoring,
        "unclassified": sorted(set(unclassified)),
    }


def _empty_consequence(raw_text=""):
    consequence = {
        "schema_version": 1,
        "scope": "immediate_protocol_transition",
        "hp_delta": None,
        "max_hp_delta": None,
        "gold_delta": None,
        "card_changes": {"gain": [], "remove": [], "upgrade": [], "transform": []},
        "relic_changes": {"gain": [], "remove": [], "counter": []},
        "potion_changes": {"gain": [], "remove": [], "replace": []},
        "curse": {
            "gain": [], "remove": [], "probability": None,
            "omamori_applicable": None, "omamori_charges_consumed": None,
        },
        "probabilistic_outcomes": [],
        "current_cost": {"gold": None, "hp": None, "max_hp": None},
        "future_costs": [],
        "raw_effect_text": str(raw_text or ""),
        "uncertainty": [],
        "uncertainty_classification": {
            "status": "unresolved",
            "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        },
    }
    consequence["field_knowledge"] = {
        field: {
            "status": "unknown",
            "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        }
        for field in _CONSEQUENCE_KNOWLEDGE_FIELDS
    }
    return consequence


def _known_consequence_field(
    consequence, field, value, *, authority, reason
):
    consequence[field] = _audit_clone(value)
    consequence["field_knowledge"][field] = {
        "status": "known",
        "authority": str(authority),
        "reason": str(reason),
    }


def _not_applicable_consequence_field(
    consequence, field, value, *, authority, reason
):
    consequence[field] = _audit_clone(value)
    consequence["field_knowledge"][field] = {
        "status": "not_applicable",
        "authority": str(authority),
        "reason": str(reason),
    }


def _mark_numeric_consequences(consequence, hp, max_hp, gold, reason):
    for field, value in (
        ("hp_delta", hp), ("max_hp_delta", max_hp), ("gold_delta", gold),
    ):
        _known_consequence_field(
            consequence, field, value,
            authority="production_mechanics_projection", reason=reason,
        )


def _mark_empty_inventory_consequences(consequence, reason):
    for field, value in (
        ("card_changes", {"gain": [], "remove": [], "upgrade": [], "transform": []}),
        ("relic_changes", {"gain": [], "remove": [], "counter": []}),
        ("potion_changes", {"gain": [], "remove": [], "replace": []}),
        (
            "curse",
            {
                "gain": [], "remove": [], "probability": 0.0,
                "omamori_applicable": False,
                "omamori_charges_consumed": 0,
            },
        ),
    ):
        _known_consequence_field(
            consequence, field, value,
            authority="production_mechanics_projection", reason=reason,
        )


def _mark_deterministic_costs(consequence, *, gold=0, hp=0, max_hp=0, reason):
    _known_consequence_field(
        consequence,
        "current_cost",
        {"gold": gold, "hp": hp, "max_hp": max_hp},
        authority="protocol_target", reason=reason,
    )
    _known_consequence_field(
        consequence, "future_costs", [],
        authority="production_mechanics_projection", reason=reason,
    )


def _mark_no_probability(consequence, reason):
    _known_consequence_field(
        consequence, "probabilistic_outcomes", [],
        authority="production_mechanics_projection", reason=reason,
    )
    consequence["uncertainty_classification"] = {
        "status": "none",
        "authority": "production_mechanics_projection",
        "reason": reason,
    }


def _normalized_game_id(value):
    return "".join(
        character for character in str(value or "").casefold()
        if character.isalnum()
    )


_PASSIVE_RELIC_PICKUPS = {
    "anchor", "artofwar", "blackstar", "boot", "bustedcrown", "chemicalx",
    "clockworksouvenir", "coffeedripper", "datadisk", "dreamcatcher",
    "orichalcum", "pocketwatch", "questioncard", "runiccube",
    "runicdome", "runicpyramid", "sacredbark", "singingbowl",
    "slaverscollar", "sneckoeye", "toolbox", "toyornithopter",
    "thecourier", "whitebeaststatue",
    "akabeko", "ancientteaset", "bagofmarbles", "bagofpreparation",
    "birdfacedurn", "bloodvial", "bloodyidol", "bluecandle", "brimstone",
    "bronzescales", "calipers", "captainswheel", "ceramicfish",
    "championbelt", "charonsashes", "cloakclasp", "cursedkey",
    "darkstoneperiapt", "deadbranch", "duvudoll", "ectoplasm",
    "emotionchip", "enchiridion", "eternalfeather", "faceofcleric",
    "fossilizedhelix", "frozeneye", "gamblingchip", "ginger", "girya",
    "goldplatedcables", "goldeneye", "goldenidol", "gremlinhorn",
    "handdrill", "happyflower", "icecream", "incenseburner", "inkbottle",
    "inserter", "juzubracelet", "kunai", "lantern", "letteropener",
    "lizardtail", "magicflower", "markofpain", "markofthebloom", "mawbank",
    "mealticket", "meatonthebone", "medicalkit", "melange", "matryoshka",
    "membershipcard", "mercuryhourglass", "moltenegg2", "frozenegg2",
    "mummifiedhand",
    "mutagenicstrength", "neowsblessing", "neowslament", "ninjascroll",
    "nlothsgift",
    "nunchaku", "oddmushroom", "oddlysmoothstone", "omamori",
    "orangepellets", "ornamentalfan", "papercrane", "paperphrog",
    "peacepipe", "pennib", "philosopherstone", "philosophersstone",
    "prayerwheel",
    "preservedinsect", "prismaticshard", "redmask", "redskull",
    "regalpillow", "runiccapacitor", "selfformingclay", "shovel",
    "shuriken", "sling", "slingofcourage", "smilingmask",
    "sneckoskull", "horncleat", "strangespoon", "toxicegg2",
    "stonecalendar", "strikedummy", "sundial", "symbioticvirus",
    "teardroplocket", "theabacus", "threadandneedle", "tinychest", "torii",
    "toughbandages", "tungstenrod", "turnip", "twistedfunnel",
    "unceasingtop", "vajra", "velvetchoker", "violetlotus",
    "warpedtongs", "wingboots", "wingedgreaves", "wristblade",
}
_IMMEDIATE_RELIC_PICKUPS = {
    "mango": {"hp_delta": 14, "max_hp_delta": 14, "gold_delta": 0},
    "oldcoin": {"hp_delta": 0, "max_hp_delta": 0, "gold_delta": 300},
    "pear": {"hp_delta": 10, "max_hp_delta": 10, "gold_delta": 0},
    "strawberry": {"hp_delta": 7, "max_hp_delta": 7, "gold_delta": 0},
}

_STARTER_RELIC_REPLACEMENTS = {
    "blackblood": "Burning Blood",
    "frozencore": "Cracked Core",
    "holywater": "PureWater",
    "ringoftheserpent": "Snake Ring",
}


def _apply_relic_pickup_package(
    consequence, state, relic, relic_id, reason, *, price=None,
):
    gold_cost = price if type(price) is int else 0
    if relic_id in _STARTER_RELIC_REPLACEMENTS:
        removed_id = _STARTER_RELIC_REPLACEMENTS[relic_id]
        owned = [
            item for item in (_state_game(state or {}).get("relics") or [])
            if isinstance(item, dict)
            and _normalized_game_id(item.get("id") or item.get("name"))
            == _normalized_game_id(removed_id)
        ]
        if len(owned) != 1:
            return False
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [compact_audit_value(owned[0])], "counter": [],
            }, authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        return True
    if relic_id == "emptycage":
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(consequence, reason=reason)
        _known_consequence_field(
            consequence, "future_costs", [{
                "kind": "relic_grid_selection", "relic_id": "Empty Cage",
                "domain": "current_deck", "operation": "remove",
                "select_count": 2, "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }], authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "the two removed card UUIDs settle on the following GRID surface"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "base_game_relic_mechanics",
            "reason": "Empty Cage opens an exact two-card removal GRID",
        }
        return True
    if relic_id == "astrolabe":
        _mark_numeric_consequences(consequence, 0, 0, -gold_cost, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason
        )
        _known_consequence_field(
            consequence, "future_costs", [{
                "kind": "astrolabe_grid_selection",
                "operation": "transform_and_upgrade",
                "select_count": 3,
                "domain": "current_deck",
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }], authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "the three transformed card UUIDs settle on the following GRID surface"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "base_game_relic_mechanics",
            "reason": "Astrolabe opens an exact three-card transform-and-upgrade GRID",
        }
        return True
    if relic_id == "orrery" and type(price) is int:
        _mark_numeric_consequences(consequence, 0, 0, -gold_cost, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason
        )
        _known_consequence_field(
            consequence, "future_costs", [{
                "kind": "optional_card_reward_sequence",
                "relic_id": "Orrery",
                "card_pool": "CHARACTER",
                "reward_count": 5,
                "candidates_per_reward": max(
                    1,
                    3
                    + int(_state_has_relic(state, "QuestionCard"))
                    - 2 * int(_state_has_relic(state, "BustedCrown")),
                ),
                "select_count_per_reward": 1,
                "can_skip": True,
                "timing": "after_relic_purchase",
            }], authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "the chosen card UUIDs settle across the following five "
            "CARD_REWARD surfaces"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "base_game_relic_mechanics",
            "reason": (
                "Orrery opens five consecutive character card reward choices"
            ),
        }
        return True
    if relic_id == "tinyhouse":
        game = _state_game(state or {})
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        relic_ids = {
            _normalized_game_id(item.get("id") or item.get("name"))
            for item in (game.get("relics") or [])
            if isinstance(item, dict)
        }
        _known_domain_consequence_field(
            consequence, "hp_delta", {
                "kind": "tiny_house_effective_heal",
                "minimum": 0,
                "maximum": min(5, max(0, max_hp + 5 - current_hp)),
            }, "tiny_house_heal_with_mark_of_the_bloom_domain",
        )
        _known_consequence_field(
            consequence, "max_hp_delta", 5,
            authority="base_game_relic_mechanics", reason=reason,
        )
        _known_consequence_field(
            consequence, "gold_delta",
            -gold_cost,
            authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_empty_inventory_consequences(consequence, reason)
        upgradeable_cards = [
            item for item in (game.get("deck") or [])
            if isinstance(item, dict)
            and int(item.get("upgrades") or 0) == 0
            and str(item.get("type") or "").upper()
            not in {"CURSE", "STATUS"}
        ]
        _known_domain_consequence_field(
            consequence, "card_changes", {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
                "random_upgrade": [{
                    "kind": "tiny_house_random_card_upgrade",
                    "domain": "current_upgradable_deck",
                    "count": min(1, len(upgradeable_cards)),
                    "selection_mode": "random",
                }],
            }, "tiny_house_random_card_upgrade_domain",
        )
        potions = game.get("potions")
        potion_slot_available = bool(
            isinstance(potions, list)
            and any(
                isinstance(item, dict)
                and _normalized_game_id(item.get("id")) == "potionslot"
                for item in potions
            )
        )
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason,
        )
        future_reward_surfaces = []
        if "ectoplasm" not in relic_ids:
            future_reward_surfaces.append({
                "kind": "tiny_house_gold_reward_surface",
                "gold_amount": 50,
                "timing": "after_relic_pickup",
            })
        future_reward_surfaces.append({
                "kind": "tiny_house_card_reward_surface",
                "selection_mode": "player_choice_or_skip",
                "timing": "after_relic_pickup",
        })
        if potion_slot_available and "sozu" not in relic_ids:
            future_reward_surfaces.append({
                "kind": "tiny_house_potion_reward_surface",
                "domain": "base_game_potion_pool",
                "count": 1,
                "selection_mode": "random",
                "timing": "after_relic_pickup",
            })
        _known_consequence_field(
            consequence, "future_costs", future_reward_surfaces,
            authority="base_game_relic_mechanics", reason=reason,
        )
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", [],
            "tiny_house_random_upgrade_and_potion_domains",
        )
        consequence["uncertainty"] = [
            "the random upgraded card settles at pickup; gold, card, and "
            "available potion rewards settle on following reward surfaces"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_random_domain",
            "authority": "base_game_relic_mechanics",
            "reason": (
                "Tiny House has bounded random upgrade and potion identities"
            ),
        }
        return True
    if relic_id == "pandorasbox":
        starter_ids = {
            "striker", "defendr", "strikeg", "defendg",
            "strikeb", "defendb", "strikep", "defendp",
        }
        removed = [
            compact_audit_value(card)
            for card in (_state_game(state or {}).get("deck") or [])
            if isinstance(card, dict)
            and _normalized_game_id(card.get("id") or card.get("name"))
            in starter_ids
        ]
        if not removed:
            return False
        _mark_numeric_consequences(consequence, 0, 0, -gold_cost, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "card_changes", {
                "gain": [], "remove": removed, "upgrade": [],
                "transform": [],
            }, authority="base_game_relic_mechanics", reason=reason,
        )
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason
        )
        _known_consequence_field(
            consequence, "future_costs", [{
                "kind": "pandoras_box_random_transforms",
                "operation": "transform",
                "source_count": len(removed),
                "result_count": len(removed),
                "result_domain": "character_cards",
                "result_selection_mode": "random",
                "timing": "following_grid_settlement",
            }], authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "replacement card identities settle on the following GRID frame"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "base_game_relic_mechanics",
            "reason": "Pandora's Box transforms every basic Strike and Defend",
        }
        return True
    if relic_id == "potionbelt":
        _mark_numeric_consequences(
            consequence, 0, 0, -gold_cost, reason
        )
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _known_domain_consequence_field(
            consequence, "potion_changes", {
                "gain": [],
                "random_gain": [{
                    "kind": "potion_slot_gain", "id": "Potion Slot",
                    "count": 2,
                    "identity_binding": "authoritative_after_inventory",
                }],
                "remove": [], "replace": [],
            }, "potion_belt_two_runtime_slots",
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason
        )
        _mark_no_probability(consequence, reason)
        return True
    if relic_id in {
        "bottledflame", "bottledlightning", "bottledtornado", "cauldron",
    }:
        _mark_numeric_consequences(
            consequence, 0, 0, -gold_cost, reason
        )
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [compact_audit_value(relic)],
                "remove": [], "counter": [],
            }, authority="protocol_relic_target", reason=reason,
        )
        _mark_deterministic_costs(
            consequence, gold=gold_cost, reason=reason
        )
        bottle = {
            "bottledflame": (
                "bottled_flame_grid_selection", "bottle_attack",
                "current_deck_attacks", "Attack",
            ),
            "bottledlightning": (
                "bottled_lightning_grid_selection", "bottle_skill",
                "current_deck_skills", "Skill",
            ),
            "bottledtornado": (
                "bottled_tornado_grid_selection", "bottle_power",
                "current_deck_powers", "Power",
            ),
        }.get(relic_id)
        future = (
            [{
                "kind": bottle[0], "operation": bottle[1],
                "select_count": 1, "domain": bottle[2],
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }]
            if bottle is not None else [{
                "kind": "cauldron_potion_reward_surface",
                "visible_count": 5,
                "domain": "base_game_potion_pool",
                "selection_mode": "claim_visible_rewards",
                "timing": "after_relic_pickup",
            }]
        )
        _known_consequence_field(
            consequence, "future_costs", future,
            authority="base_game_relic_mechanics", reason=reason,
        )
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "the exact follow-up choices bind on the next protocol surface"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "base_game_relic_mechanics",
            "reason": (
                f"the bottled relic opens one current-deck {bottle[3]} choice"
                if bottle is not None else
                "Cauldron opens five visible potion rewards"
            ),
        }
        return True
    return False


def _apply_calling_bell_pickup(consequence, relic, reason):
    """Project Calling Bell's complete on-equip package."""

    bell_curse = {
        "id": "CurseOfTheBell", "card_id": "CurseOfTheBell",
        "type": "CURSE", "rarity": "CURSE", "count": 1,
    }
    random_relics = [
        {
            "kind": "random_relic_gain", "domain": f"base_game_{rarity}_relic_pool",
            "rarity": rarity.upper(), "count": 1,
            "selection_mode": "random",
        }
        for rarity in ("common", "uncommon", "rare")
    ]
    _mark_numeric_consequences(consequence, 0, 0, 0, reason)
    _known_consequence_field(
        consequence, "card_changes", {
            "gain": [_audit_clone(bell_curse)], "remove": [],
            "upgrade": [], "transform": [],
        }, authority="base_game_relic_mechanics", reason=reason,
    )
    _known_domain_consequence_field(
        consequence, "relic_changes", {
            "gain": [compact_audit_value(relic)],
            "random_gain": _audit_clone(random_relics),
            "remove": [], "counter": [],
        }, "calling_bell_three_relic_rarity_pools",
    )
    _known_consequence_field(
        consequence, "potion_changes", {
            "gain": [], "remove": [], "replace": [],
        }, authority="base_game_relic_mechanics", reason=reason,
    )
    _known_consequence_field(
        consequence, "curse", {
            "gain": [_audit_clone(bell_curse)], "remove": [],
            "probability": 1.0, "omamori_applicable": False,
            "omamori_charges_consumed": 0,
        }, authority="base_game_relic_mechanics", reason=reason,
    )
    _mark_deterministic_costs(consequence, reason=reason)
    _known_domain_consequence_field(
        consequence, "probabilistic_outcomes", [],
        "calling_bell_three_relic_rarity_pools",
    )
    consequence["random_effects"] = _audit_clone(random_relics)
    consequence["uncertainty"] = [
        "three relic identities remain inside their typed rarity pools"
    ]
    consequence["uncertainty_classification"] = {
        "status": "classified_random_domain",
        "authority": "base_game_relic_mechanics",
        "reason": reason,
    }


def _map_entry_hp_delta(state, target):
    """Return deterministic healing that fires when the chosen room opens."""

    game = _state_game(state)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return None
    relic_ids = {
        _normalized_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return 0
    kind = str((target or {}).get("kind") or "").casefold()
    symbol = str((target or {}).get("symbol") or "").upper()
    combat_entry = kind == "map_boss" or symbol in {"M", "E"}
    heal = 0
    if combat_entry and "bloodvial" in relic_ids:
        heal += 2
    if kind == "map_boss" and "pantograph" in relic_ids:
        heal += 25
    if symbol == "$" and "mealticket" in relic_ids:
        heal += 15
    if symbol == "R" and "eternalfeather" in relic_ids:
        heal += 3 * (len(game.get("deck") or []) // 5)
    return max(0, min(max_hp - current_hp, heal))


def _map_entry_gold_delta(state, target):
    """Return the deterministic Maw Bank gain for a map transition.

    Maw Bank's runtime state is represented by its counter: ``-2`` is the
    exact ``usedUp`` marker written when gold is spent.  It otherwise grants
    12 gold whenever a room is entered, including a boss room.  Do not invent
    a zero value if the bridge omitted that state; the consequence must remain
    unknown rather than falsely certifying the transition.
    """

    game = _state_game(state)
    relics = game.get("relics")
    if not isinstance(relics, list):
        return None
    maw_banks = [
        relic for relic in relics
        if isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        == "mawbank"
    ]
    if not maw_banks:
        return 0
    if len(maw_banks) != 1:
        return None
    counter = maw_banks[0].get("counter")
    if type(counter) is not int:
        return None
    kind = str((target or {}).get("kind") or "").casefold()
    if kind not in {"map_node", "map_boss"}:
        return None
    return 0 if counter == -2 else 12


def _state_game(state):
    state = state if isinstance(state, dict) else {}
    game = state.get("game_state")
    return game if isinstance(game, dict) else state


def _card_gain_after_egg_relics(card, game):
    """Apply deterministic egg upgrades to a newly gained card template.

    Event option previews describe the base card. The base game applies egg
    relics when the card enters the master deck, so an exact consequence
    claim must project that intervening trigger. The rule is type-driven and
    therefore applies equally to every character.
    """

    gained = compact_audit_value(card)
    if not isinstance(gained, dict):
        return gained
    upgrades = gained.get("upgrades")
    if type(upgrades) is not int or upgrades != 0:
        return gained
    card_type = str(gained.get("type") or "").upper()
    relic_ids = {
        _normalized_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    egg_ids = {
        "ATTACK": {"moltenegg", "moltenegg2"},
        "SKILL": {"toxicegg", "toxicegg2"},
        "POWER": {"frozenegg", "frozenegg2"},
    }.get(card_type, set())
    if not relic_ids.intersection(egg_ids):
        return gained
    gained["upgrades"] = 1
    name = gained.get("name")
    if isinstance(name, str) and name and not name.endswith("+"):
        gained["name"] = f"{name}+"
    return gained


_GRID_OPERATION_FLAGS = {
    "for_upgrade": "grid_upgrade",
    "for_transform": "grid_transform",
    "for_purge": "grid_purge",
}


def _validated_grid_target(target, state):
    """Bind one GRID card target to a unique protocol card and operation.

    The option target is not enough to identify what selecting a card means:
    the same ``kind=card`` surface is used for upgrades, transforms, purges and
    unrelated selection screens.  Require the authoritative GRID flags and an
    exact UUID/object match against the screen before projecting any deck
    consequence.
    """

    game = _state_game(state)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    if any(type(screen.get(flag)) is not bool for flag in _GRID_OPERATION_FLAGS):
        return None, None, "grid_operation_flags_missing"
    operations = [
        operation for flag, operation in _GRID_OPERATION_FLAGS.items()
        if screen[flag]
    ]
    if len(operations) == 0:
        current_action = str(game.get("current_action") or "")
        combat = game.get("combat_state")
        combat = combat if isinstance(combat, dict) else {}
        if (
            str(game.get("room_phase") or "").upper() == "COMBAT"
            and current_action in {
                "BetterDiscardPileToHandAction",
                "DiscardPileToTopOfDeckAction",
                "SkillFromDeckToHandAction",
            }
            and isinstance(
                combat.get(
                    "draw_pile"
                    if current_action == "SkillFromDeckToHandAction"
                    else "discard_pile"
                ),
                list,
            )
        ):
            operations = [{
                "BetterDiscardPileToHandAction": (
                    "grid_combat_discard_to_hand"
                ),
                "DiscardPileToTopOfDeckAction": (
                    "grid_combat_discard_to_top"
                ),
                "SkillFromDeckToHandAction": (
                    "grid_combat_draw_to_hand"
                ),
            }[current_action]]
        else:
            operations = []
    if len(operations) == 0:
        parent = screen.get("parent_choice_context")
        parent = parent if isinstance(parent, dict) else {}
        contract = parent.get("neow_contract")
        contract = contract if isinstance(contract, dict) else {}
        future = _NEOW_FUTURE_REWARDS.get(contract.get("reward_kind"))
        expected_operation = (
            future.get("operation")
            if isinstance(future, dict)
            and future.get("kind") == "neow_grid_selection"
            else None
        )
        valid_neow_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "NEOW"
            and parent.get("mechanism_id") == _neow_mechanism_id(contract)
            and expected_operation in {"upgrade", "transform", "remove"}
            and parent.get("operation") == expected_operation
            and parent.get("select_count") == future.get("select_count")
        )
        event_contract = parent.get("event_contract")
        checked_event, event_mechanism = _canonical_staged_event_contract(
            event_contract, "duplicator", 0, game
        )
        valid_duplicator_parent = bool(
            checked_event is not None
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and parent.get("mechanism_id") == event_mechanism
            and parent.get("operation") == "duplicate"
            and parent.get("select_count") == 1
        )
        offered_note_card = parent.get("offered_card")
        offered_note_card = (
            offered_note_card if isinstance(offered_note_card, dict) else {}
        )
        note_mechanism = {
            "event_id": "NoteForYourself",
            "event_class": (
                "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
            ),
            "original_button_index": 0,
            "operation": "note_exchange",
            "select_count": 1,
            "offered_card": offered_note_card,
        }
        valid_note_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _normalized_game_id(parent.get("event_id"))
            == "noteforyourself"
            and parent.get("event_class") == note_mechanism["event_class"]
            and parent.get("original_button_index") == 0
            and parent.get("operation") == "note_exchange"
            and parent.get("select_count") == 1
            and offered_note_card.get("id")
            and offered_note_card.get("card_instance_id")
            and parent.get("mechanism_id")
            == _stable_id("event-grid-mechanism", note_mechanism)
        )
        drug_dealer_mechanism = {
            "event_id": "Drug Dealer",
            "event_class": "com.megacrit.cardcrawl.events.city.DrugDealer",
            "original_button_index": 1,
            "operation": "transform",
            "select_count": 2,
        }
        valid_drug_dealer_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _normalized_game_id(parent.get("event_id")) == "drugdealer"
            and parent.get("event_class")
            == drug_dealer_mechanism["event_class"]
            and parent.get("original_button_index") == 1
            and parent.get("operation") == "transform"
            and parent.get("select_count") == 2
            and parent.get("mechanism_id")
            == _stable_id("event-grid-mechanism", drug_dealer_mechanism)
        )
        library_mechanism = {
            "event_id": "The Library",
            "event_class": "com.megacrit.cardcrawl.events.city.TheLibrary",
            "original_button_index": 0,
            "operation": "gain",
            "select_count": 1,
            "selection_domain": "library_card_offering",
        }
        valid_library_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _normalized_game_id(parent.get("event_id")) == "thelibrary"
            and parent.get("event_class") == library_mechanism["event_class"]
            and parent.get("original_button_index") == 0
            and parent.get("operation") == "gain"
            and parent.get("select_count") == 1
            and parent.get("selection_domain") == "library_card_offering"
            and parent.get("mechanism_id")
            == _stable_id("event-grid-mechanism", library_mechanism)
        )
        bottle_contracts = {
            "bottledflame": ("bottle_attack", "ATTACK"),
            "bottledlightning": ("bottle_skill", "SKILL"),
            "bottledtornado": ("bottle_power", "POWER"),
        }
        bottle_contract = bottle_contracts.get(
            _normalized_game_id(parent.get("relic_id"))
        )
        valid_bottle_parent = bool(
            bottle_contract is not None
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "COMBAT_REWARD"
            and parent.get("operation") == bottle_contract[0]
            and str(parent.get("card_type") or "").upper()
            == bottle_contract[1]
            and parent.get("select_count") == 1
        )
        boss_relic_grid = {
            "astrolabe": ("transform", 3, "grid_transform"),
            "emptycage": ("remove", 2, "grid_remove"),
        }.get(_normalized_game_id(parent.get("relic_id")))
        valid_boss_relic_parent = bool(
            boss_relic_grid is not None
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "BOSS_REWARD"
            and parent.get("operation") == boss_relic_grid[0]
            and parent.get("select_count") == boss_relic_grid[1]
        )
        if valid_neow_parent:
            operations = [f"grid_{expected_operation}"]
        elif valid_duplicator_parent:
            operations = ["grid_duplicate"]
        elif valid_note_parent:
            operations = ["grid_note_exchange"]
        elif valid_drug_dealer_parent:
            operations = ["grid_transform"]
        elif valid_library_parent:
            operations = ["grid_gain"]
        elif valid_bottle_parent:
            operations = [f"grid_{bottle_contract[0]}"]
        elif valid_boss_relic_parent:
            operations = [boss_relic_grid[2]]
        else:
            return None, None, "grid_parent_choice_context_invalid"
    if len(operations) != 1:
        return None, None, "grid_operation_not_unique"

    target_card = target.get("card")
    target_card = target_card if isinstance(target_card, dict) else {}
    target_uuid = target.get("card_instance_id")
    card_uuid = target_card.get("card_instance_id")
    if not (
        isinstance(target_uuid, str) and target_uuid
        and isinstance(card_uuid, str) and card_uuid == target_uuid
    ):
        return None, None, "grid_target_uuid_missing_or_mismatched"
    matches = [
        card for card in (screen.get("cards") or [])
        if isinstance(card, dict)
        and card.get("card_instance_id") == target_uuid
    ]
    if len(matches) != 1:
        return None, None, "grid_target_uuid_not_unique_on_screen"
    visible_card = compact_audit_value(matches[0])
    if compact_audit_value(target_card) != visible_card:
        return None, None, "grid_target_card_facts_mismatch"
    if operations[0] in {
        "grid_combat_discard_to_hand", "grid_combat_discard_to_top",
        "grid_combat_draw_to_hand",
    }:
        source_pile = (
            "draw_pile"
            if operations[0] == "grid_combat_draw_to_hand"
            else "discard_pile"
        )
        source_matches = [
            card for card in (
                (game.get("combat_state") or {}).get(source_pile) or []
            )
            if isinstance(card, dict)
            and card.get("card_instance_id") == target_uuid
        ]
        if len(source_matches) != 1:
            return None, None, "grid_combat_source_binding_mismatch"
    expected_bottle_type = {
        "grid_bottle_attack": "ATTACK",
        "grid_bottle_skill": "SKILL",
        "grid_bottle_power": "POWER",
    }.get(operations[0])
    if (
        expected_bottle_type is not None
        and str(visible_card.get("type") or "").upper()
        != expected_bottle_type
    ):
        return None, None, "grid_bottle_card_type_mismatch"
    return operations[0], visible_card, None


def _validated_hand_select_target(target, state):
    """Bind one HAND_SELECT option to its exact transient combat card."""

    game = _state_game(state)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    current_action = game.get("current_action")
    max_cards = screen.get("max_cards")
    can_pick_zero = screen.get("can_pick_zero")
    if not isinstance(current_action, str) or not current_action:
        return None, None, "hand_select_current_action_missing"
    if type(max_cards) is not int or max_cards < 0:
        return None, None, "hand_select_max_cards_invalid"
    if type(can_pick_zero) is not bool:
        return None, None, "hand_select_can_pick_zero_missing"
    target_card = target.get("card")
    target_card = target_card if isinstance(target_card, dict) else {}
    target_uuid = target.get("card_instance_id")
    if not (
        isinstance(target_uuid, str) and target_uuid
        and target_card.get("card_instance_id") == target_uuid
    ):
        return None, None, "hand_select_target_uuid_missing_or_mismatched"
    matches = [
        card for card in (screen.get("hand") or [])
        if isinstance(card, dict)
        and card.get("card_instance_id") == target_uuid
    ]
    if len(matches) != 1:
        return None, None, "hand_select_target_uuid_not_unique_on_screen"
    visible_card = compact_audit_value(matches[0])
    if compact_audit_value(target_card) != visible_card:
        return None, None, "hand_select_target_card_facts_mismatch"
    return visible_card, {
        "current_action": current_action,
        "max_cards": max_cards,
        "can_pick_zero": can_pick_zero,
    }, None


def _validated_shop_purge(target, state):
    """Return the exact legal purge price, independently bound to the screen."""

    game = _state_game(state)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    price = target.get("price")
    screen_price = screen.get("purge_cost")
    gold = game.get("gold")
    if screen.get("purge_available") is not True:
        return None, "shop_purge_not_protocol_available"
    if (
        type(price) is not int or price < 0
        or type(screen_price) is not int or screen_price < 0
        or price != screen_price
    ):
        return None, "shop_purge_cost_binding_invalid"
    if type(gold) is not int or gold < price:
        return None, "shop_purge_affordability_not_proven"
    return price, None


def _shop_purge_followup(price=None):
    return [{
        "kind": "shop_purge_grid_selection",
        "operation": "purge",
        "select_count": 1,
        "identity_binding": "subsequent_grid_card_instance_id",
        "commit_timing": "after_grid_confirmation",
        "gold_cost": price,
    }]


def _grid_confirmation_followup(operation, selected_card):
    return [{
        "kind": "grid_confirmation_effect",
        "operation": operation,
        "selected_card": compact_audit_value(selected_card),
        "commit_timing": "after_grid_confirmation",
    }]


def _state_has_relic(state, relic_id):
    wanted = _normalized_game_id(relic_id)
    return any(
        _normalized_game_id(relic.get("id") or relic.get("name")) == wanted
        for relic in (_state_game(state).get("relics") or [])
        if isinstance(relic, dict)
    )


_NEOW_REWARD_KINDS = {
    "RANDOM_COLORLESS_2", "THREE_CARDS", "ONE_RANDOM_RARE_CARD",
    "REMOVE_CARD", "UPGRADE_CARD", "RANDOM_COLORLESS", "TRANSFORM_CARD",
    "THREE_SMALL_POTIONS", "RANDOM_COMMON_RELIC", "TEN_PERCENT_HP_BONUS",
    "HUNDRED_GOLD", "THREE_ENEMY_KILL", "REMOVE_TWO",
    "TRANSFORM_TWO_CARDS", "ONE_RARE_RELIC", "THREE_RARE_CARDS",
    "TWO_FIFTY_GOLD", "TWENTY_PERCENT_HP_BONUS", "BOSS_RELIC",
}
_NEOW_DRAWBACK_KINDS = {
    "NONE", "TEN_PERCENT_HP_LOSS", "NO_GOLD", "CURSE", "PERCENT_DAMAGE",
}
_NEOW_STARTER_RELIC_IDS = {
    "burningblood", "crackedcore", "purewater", "ringofthesnake",
}
_NEOW_FUTURE_REWARDS = {
    "RANDOM_COLORLESS_2": {
        "kind": "neow_card_reward", "domain": "colorless_cards",
        "rarities": ["RARE"], "visible_count": 3, "choose_count": 1,
        "selection_mode": "player_choice",
    },
    "RANDOM_COLORLESS": {
        "kind": "neow_card_reward", "domain": "colorless_cards",
        "rarities": ["UNCOMMON", "RARE"], "visible_count": 3,
        "choose_count": 1, "selection_mode": "player_choice",
    },
    "THREE_RARE_CARDS": {
        "kind": "neow_card_reward", "domain": "character_cards",
        "rarities": ["RARE"], "visible_count": 3, "choose_count": 1,
        "selection_mode": "player_choice",
    },
    "THREE_CARDS": {
        "kind": "neow_card_reward", "domain": "character_cards",
        "rarities": ["COMMON", "UNCOMMON", "RARE"], "visible_count": 3,
        "choose_count": 1, "selection_mode": "player_choice",
    },
    "UPGRADE_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "upgrade", "select_count": 1,
        "selection_mode": "player_choice",
    },
    "REMOVE_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "remove", "select_count": 1,
        "selection_mode": "player_choice",
    },
    "REMOVE_TWO": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "remove", "select_count": 2,
        "selection_mode": "player_choice",
    },
    "TRANSFORM_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "transform", "select_count": 1,
        "selection_mode": "player_choice",
        "result_domain": "character_cards", "result_selection_mode": "random",
    },
    "TRANSFORM_TWO_CARDS": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "transform", "select_count": 2,
        "selection_mode": "player_choice",
        "result_domain": "character_cards", "result_selection_mode": "random",
    },
    "THREE_SMALL_POTIONS": {
        "kind": "neow_potion_rewards", "domain": "potion_pool",
        "reward_count": 3, "selection_mode": "claim_visible_rewards",
    },
}
_NEOW_RANDOM_REWARDS = {
    "ONE_RANDOM_RARE_CARD": {
        "kind": "card_gain", "domain": "character_cards",
        "rarities": ["RARE"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "RANDOM_COMMON_RELIC": {
        "kind": "relic_gain", "domain": "common_relics",
        "rarities": ["COMMON"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "ONE_RARE_RELIC": {
        "kind": "relic_gain", "domain": "rare_relics",
        "rarities": ["RARE"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "BOSS_RELIC": {
        "kind": "relic_gain", "domain": "boss_relics",
        "rarities": ["BOSS"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
}


def _neow_mechanism_id(contract):
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"neow-mechanism:{hashlib.sha256(encoded).hexdigest()[:20]}"


def _validated_neow_contract(target, state):
    contract = target.get("neow_contract")
    if not isinstance(contract, dict) or contract.get("contract_version") != 1:
        return None, "typed_neow_contract_missing"
    kind = contract.get("contract_kind")
    if kind == "NEOW_DIALOG_ADVANCE":
        if (
            set(contract) != {
                "contract_version", "contract_kind", "screen_num",
                "resource_effect",
            }
            or type(contract.get("screen_num")) is not int
            or contract.get("screen_num") == 3
            or contract.get("resource_effect") != "NONE"
        ):
            return None, "typed_neow_dialog_contract_invalid"
    elif kind == "NEOW_REWARD":
        parameters = contract.get("parameters")
        reward_kind = contract.get("reward_kind")
        drawback_kind = contract.get("drawback_kind")
        game = _state_game(state)
        max_hp = game.get("max_hp")
        if (
            set(contract) != {
                "contract_version", "contract_kind", "reward_kind",
                "drawback_kind", "parameters",
            }
            or reward_kind not in _NEOW_REWARD_KINDS
            or drawback_kind not in _NEOW_DRAWBACK_KINDS
            or not isinstance(parameters, dict)
            or set(parameters) != {
                "hp_bonus", "cursed", "drawback_def_kind",
            }
            or type(parameters.get("hp_bonus")) is not int
            or not isinstance(max_hp, int)
            or isinstance(max_hp, bool)
            or parameters.get("hp_bonus") != max(0, max_hp // 10)
            or type(parameters.get("cursed")) is not bool
            or parameters.get("cursed") is not (drawback_kind == "CURSE")
            or parameters.get("drawback_def_kind")
            != (None if drawback_kind == "NONE" else drawback_kind)
        ):
            return None, "typed_neow_reward_contract_invalid"
    else:
        return None, "typed_neow_contract_kind_invalid"
    if target.get("mechanism_id") != _neow_mechanism_id(contract):
        return None, "typed_neow_mechanism_id_invalid"
    return _audit_clone(contract), None


def _known_domain_consequence_field(consequence, field, value, reason):
    consequence[field] = _audit_clone(value)
    consequence["field_knowledge"][field] = {
        "status": "known_domain",
        "authority": "production_mechanics_projection",
        "reason": reason,
    }


def _neow_starter_relic(state):
    matches = [
        relic for relic in (_state_game(state).get("relics") or [])
        if isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        in _NEOW_STARTER_RELIC_IDS
    ]
    return compact_audit_value(matches[0]) if len(matches) == 1 else None


def _neow_omamori(state):
    matches = [
        relic for relic in (_state_game(state).get("relics") or [])
        if isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        == "omamori"
    ]
    return matches[0] if len(matches) == 1 else None


def _apply_neow_consequence(consequence, target, state):
    """Project one bridge-typed Neow mechanism without localized text."""

    contract, invalid_reason = _validated_neow_contract(target, state)
    if contract is None:
        consequence["uncertainty"].append(invalid_reason)
        return False
    consequence.update({
        "event_id": target.get("event_id"),
        "mechanism_id": target.get("mechanism_id"),
        "neow_contract": contract,
        "operation": (
            "neow_dialog_advance"
            if contract["contract_kind"] == "NEOW_DIALOG_ADVANCE"
            else "neow_reward"
        ),
        "random_effects": [],
    })
    if contract["contract_kind"] == "NEOW_DIALOG_ADVANCE":
        reason = "typed_neow_dialog_advance_has_no_resource_effect"
        consequence.update({
            "reward_kind": None,
            "drawback_kind": "NONE",
            "parameters": {
                "screen_num": contract["screen_num"],
                "resource_effect": contract["resource_effect"],
            },
        })
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        return True

    reward_kind = contract["reward_kind"]
    drawback_kind = contract["drawback_kind"]
    parameters = _audit_clone(contract["parameters"])
    consequence.update({
        "reward_kind": reward_kind,
        "drawback_kind": drawback_kind,
        "parameters": parameters,
    })
    game = _state_game(state)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    gold = game.get("gold")
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (current_hp, max_hp, gold)
    ):
        consequence["uncertainty"].append(
            "authoritative_neow_resource_state_missing"
        )
        return True

    hp_bonus = parameters["hp_bonus"]
    reward_hp = (
        hp_bonus if reward_kind == "TEN_PERCENT_HP_BONUS"
        else 2 * hp_bonus if reward_kind == "TWENTY_PERCENT_HP_BONUS"
        else 0
    )
    reward_gold = (
        100 if reward_kind == "HUNDRED_GOLD"
        else 250 if reward_kind == "TWO_FIFTY_GOLD" else 0
    )
    drawback_hp = 0
    drawback_max_hp = 0
    drawback_gold = 0
    if drawback_kind == "TEN_PERCENT_HP_LOSS":
        drawback_max_hp = hp_bonus
        drawback_hp = max(0, current_hp - max(0, max_hp - hp_bonus))
    elif drawback_kind == "PERCENT_DAMAGE":
        drawback_hp = (current_hp // 10) * 3
    elif drawback_kind == "NO_GOLD":
        drawback_gold = gold
    reason = "typed_neow_reward_and_drawback_projection"
    _mark_numeric_consequences(
        consequence,
        reward_hp - drawback_hp,
        reward_hp - drawback_max_hp,
        reward_gold - drawback_gold,
        reason,
    )
    _mark_empty_inventory_consequences(consequence, reason)
    _mark_deterministic_costs(
        consequence, gold=drawback_gold, hp=drawback_hp,
        max_hp=drawback_max_hp, reason=reason,
    )
    _mark_no_probability(consequence, reason)

    future = _NEOW_FUTURE_REWARDS.get(reward_kind)
    if future is not None:
        _known_consequence_field(
            consequence, "future_costs", [_audit_clone(future)],
            authority="production_mechanics_projection", reason=reason,
        )

    random_effects = []
    random_reward = _NEOW_RANDOM_REWARDS.get(reward_kind)
    if random_reward is not None:
        random_effects.append(_audit_clone(random_reward))
        if random_reward["kind"] == "card_gain":
            card_changes = _audit_clone(consequence["card_changes"])
            card_changes["random_gain"] = [_audit_clone(random_reward)]
            _known_domain_consequence_field(
                consequence, "card_changes", card_changes, reason
            )
        else:
            relic_changes = _audit_clone(consequence["relic_changes"])
            relic_changes["random_gain"] = [_audit_clone(random_reward)]
            if reward_kind == "BOSS_RELIC":
                starter = _neow_starter_relic(state)
                if starter is None:
                    consequence["field_knowledge"]["relic_changes"] = {
                        "status": "unknown",
                        "authority": "protocol_state",
                        "reason": "starter_relic_not_uniquely_observable",
                    }
                    consequence["uncertainty"].append(
                        "starter relic replacement target is not unique"
                    )
                else:
                    relic_changes["remove"] = [starter]
                    _known_domain_consequence_field(
                        consequence, "relic_changes", relic_changes, reason
                    )
            else:
                _known_domain_consequence_field(
                    consequence, "relic_changes", relic_changes, reason
                )
    elif reward_kind == "THREE_ENEMY_KILL":
        _known_consequence_field(
            consequence, "relic_changes", {
                "gain": [{"id": "NeowsBlessing", "counter": 3}],
                "remove": [], "counter": [],
            }, authority="production_mechanics_projection", reason=reason,
        )

    if drawback_kind == "CURSE":
        curse_effect = {
            "kind": "curse_gain", "domain": "curses",
            "rarities": ["CURSE"], "count": 1, "timing": "immediate",
            "selection_mode": "random",
        }
        omamori = _neow_omamori(state)
        charge = omamori.get("counter") if isinstance(omamori, dict) else 0
        if not isinstance(charge, int) or isinstance(charge, bool):
            for field in ("card_changes", "curse", "relic_changes"):
                consequence["field_knowledge"][field] = {
                    "status": "unknown", "authority": "protocol_state",
                    "reason": "omamori_counter_not_observable",
                }
            consequence["uncertainty"].append(
                "Omamori counter is required to classify the curse drawback"
            )
        elif charge > 0:
            curse_value = _audit_clone(consequence["curse"])
            curse_value.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 1, "blocked": True,
                "random_gain": [],
            })
            _known_consequence_field(
                consequence, "curse", curse_value,
                authority="production_mechanics_projection", reason=reason,
            )
            relic_changes = _audit_clone(consequence["relic_changes"])
            relic_changes["counter"] = [{
                "id": omamori.get("id") or omamori.get("name") or "Omamori",
                "delta": -1,
            }]
            _known_consequence_field(
                consequence, "relic_changes", relic_changes,
                authority="production_mechanics_projection", reason=reason,
            )
        else:
            random_effects.append(_audit_clone(curse_effect))
            card_changes = _audit_clone(consequence["card_changes"])
            card_changes["random_gain"] = [_audit_clone(curse_effect)]
            _known_domain_consequence_field(
                consequence, "card_changes", card_changes, reason
            )
            curse_value = _audit_clone(consequence["curse"])
            curse_value.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 0, "blocked": False,
                "random_gain": [_audit_clone(curse_effect)],
            })
            _known_domain_consequence_field(
                consequence, "curse", curse_value, reason
            )

    consequence["random_effects"] = random_effects
    if random_effects:
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", [],
            "typed_neow_random_identity_domain",
        )
        consequence["uncertainty"].append(
            "random identity is unresolved but its JAR reward domain is typed"
        )
    if future is not None:
        consequence["uncertainty"].append(
            "Neow reward opens an independently typed downstream choice surface"
        )
    if random_effects:
        consequence["uncertainty_classification"] = {
            "status": "classified_random_domain",
            "authority": "production_mechanics_projection",
            "reason": "random identity domain, count, timing, and selection mode are typed",
        }
    elif future is not None:
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "production_mechanics_projection",
            "reason": "downstream Neow choice surface is typed by JAR mechanics",
        }
    return True


def _valid_protocol_probability_contract(outcomes):
    if not isinstance(outcomes, list) or not outcomes:
        return False
    total = 0.0
    supported = {
        "probability", "hp_delta", "current_hp_delta", "max_hp_delta",
        "gold_delta", "card_changes", "relic_changes", "potion_changes",
        "curse", "key_changes", "label", "id", "name", "raw_text",
    }
    for outcome in outcomes:
        if not isinstance(outcome, dict) or set(outcome) - supported:
            return False
        probability = outcome.get("probability")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or probability < 0 or probability > 1
        ):
            return False
        if not (set(outcome) - {"probability", "label", "id", "name", "raw_text"}):
            return False
        total += float(probability)
    return abs(total - 1.0) <= 1e-9


def _apply_protocol_consequence_contract(consequence, target):
    contract = target.get("consequence_contract")
    if not isinstance(contract, dict):
        return
    consequence["target_consequence_claim"] = _audit_clone(contract)
    consequence["uncertainty"].append(
        "target consequence claim requires independent mechanism validation"
    )


_PRODUCTION_PROBABILITY_MECHANISMS = {
    "coin_flip_gold_10_or_zero": [
        {"probability": 0.5, "gold_delta": 10},
        {"probability": 0.5, "gold_delta": 0},
    ],
}


def _apply_protocol_probability_contract(consequence, target):
    mechanism_id = str(target.get("mechanism_id") or "")
    outcomes = _PRODUCTION_PROBABILITY_MECHANISMS.get(mechanism_id)
    target_claim = target.get("probabilistic_outcomes")
    if outcomes is None:
        if target_claim is not None:
            consequence["target_probability_claim"] = _audit_clone(target_claim)
            consequence["uncertainty"].append(
                "target probability claim lacks a classified mechanism id"
            )
        return
    if target_claim is not None and _audit_clone(target_claim) != outcomes:
        consequence["target_probability_claim"] = _audit_clone(target_claim)
        consequence["uncertainty"].append(
            "target probability claim contradicts the production mechanism table"
        )
    _known_consequence_field(
        consequence, "probabilistic_outcomes", outcomes,
        authority="production_mechanics_projection",
        reason="exhaustive_observable_probability_partition",
    )
    consequence["uncertainty_classification"] = {
        "status": "exhaustive_probability",
        "authority": "production_mechanics_projection",
        "reason": "branch probabilities sum to one and effects are typed",
    }


def _visible_option_ids(state):
    return [
        str(option.get("option_id"))
        for option in (state.get("options") or [])
        if isinstance(option, dict) and option.get("option_id") is not None
    ]


def _resource_preparation_options(state, payload):
    """Expose exact held-potion use/discard alternatives for slot preparation."""

    if str(payload.get("action") or "").strip().casefold() != "potion":
        return []
    commands = {
        str(command or "").strip().casefold()
        for command in state.get("available_commands") or []
    }
    if "potion" not in commands:
        return []
    result = []
    for fallback_slot, potion in enumerate(
        _state_game(state).get("potions") or []
    ):
        if not isinstance(potion, dict):
            continue
        instance_id = potion.get("potion_instance_id")
        potion_id = potion.get("id")
        if instance_id is None or str(potion_id or "").casefold() in {
            "potionslot", "potion slot",
        }:
            continue
        slot = potion.get("slot")
        if type(slot) is not int:
            slot = fallback_slot
        operations = []
        if potion.get("can_discard") is True:
            operations.append("discard")
        if (
            potion.get("can_use") is True
            and potion.get("requires_target") is not True
        ):
            operations.append("use")
        for operation in operations:
            target = {
                "kind": "potion_resource",
                "operation": operation,
                "potion_instance_id": instance_id,
                "potion_id": potion_id,
                "slot": slot,
                "potion": compact_audit_value(potion),
                "parent_phase": str(state.get("phase") or ""),
            }
            result.append({
                "option_id": f"{instance_id}:{operation}",
                "choice_index": slot,
                "label": f"{operation}:{potion.get('name') or potion_id}",
                "target": target,
            })
    return result


def _validated_mausoleum_contract(target, state):
    """Bind the base-game Mausoleum surface without display text."""

    game = _state_game(state)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    if (
        _normalized_game_id(screen.get("event_id")) != "themausoleum"
        or _normalized_game_id(target.get("event_id")) != "themausoleum"
    ):
        return None, "mausoleum_event_id_mismatch"
    ascension = game.get("ascension_level")
    if type(ascension) is not int or ascension < 0:
        return None, "mausoleum_ascension_level_missing"
    raw_options = list(screen.get("options") or [])
    options = [
        item for item in raw_options
        if isinstance(item, dict)
        and item.get("disabled") is not True
        and type(item.get("choice_index")) is int
    ]
    choice_index = target.get("choice_index")
    if (
        len(raw_options) == 1
        and len(options) == 1
        and options[0].get("choice_index") == 0
        and choice_index == 0
    ):
        return {
            "choice_index": 0,
            "ascension_level": ascension,
            "dialog_noop": True,
        }, None
    if (
        len(options) != 2
        or {item["choice_index"] for item in options} != {0, 1}
    ):
        return None, "mausoleum_initial_surface_mismatch"
    by_index = {item["choice_index"]: item for item in options}
    if type(choice_index) is not int or choice_index not in by_index:
        return None, "mausoleum_choice_index_binding_mismatch"
    source_card = by_index[choice_index].get("card")
    if (
        "card" in target
        and _audit_clone(source_card) != _audit_clone(target.get("card"))
    ):
        return None, "mausoleum_target_card_binding_mismatch"
    open_card = by_index[0].get("card")
    if (
        not isinstance(open_card, dict)
        or _normalized_game_id(
            open_card.get("id") or open_card.get("card_id")
        ) != "writhe"
        or str(open_card.get("type") or "").upper() != "CURSE"
        or str(open_card.get("rarity") or "").upper() != "CURSE"
        or by_index[1].get("card") is not None
    ):
        return None, "mausoleum_writhe_preview_mismatch"
    omamori = [
        relic for relic in game.get("relics") or []
        if isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        == "omamori"
    ]
    if len(omamori) > 1:
        return None, "mausoleum_omamori_identity_ambiguous"
    if omamori:
        counter = omamori[0].get("counter")
        if type(counter) is not int or counter < 0:
            return None, "mausoleum_omamori_counter_missing"
        omamori_id = omamori[0].get("id") or "Omamori"
    else:
        counter = 0
        omamori_id = "Omamori"
    return {
        "choice_index": choice_index,
        "ascension_level": ascension,
        "omamori_charges": counter,
        "omamori_id": omamori_id,
    }, None


def _mausoleum_random_relic_effect():
    return {
        "kind": "random_relic_gain",
        "domain": "base_game_non_boss_relic_pool",
        "excluded_rarities": ["BOSS"],
        "count": 1,
        "timing": "immediate",
        "selection_mode": "random",
    }


def _mausoleum_writhe_effect():
    return {
        "id": "Writhe", "card_id": "Writhe", "type": "CURSE",
        "rarity": "CURSE", "count": 1,
    }


def _apply_mausoleum_consequence(consequence, target, state):
    contract, error = _validated_mausoleum_contract(target, state)
    if contract is None:
        consequence["uncertainty"].append(error)
        return False
    reason = "typed_base_game_mausoleum_branch"
    consequence.update({
        "event_id": target.get("event_id"),
        "mechanism_id": "base_game_the_mausoleum_v1",
        "random_effects": [],
    })
    _mark_numeric_consequences(consequence, 0, 0, 0, reason)
    _mark_deterministic_costs(consequence, reason=reason)
    if contract.get("dialog_noop") is True:
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_no_probability(consequence, reason)
        consequence.update({
            "operation": "mausoleum_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True
    if contract["choice_index"] == 1:
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_no_probability(consequence, reason)
        consequence.update({
            "operation": "mausoleum_leave",
            "event_outcome_id": "leave",
            "leave": True,
        })
        return True

    trigger_probability = (
        1.0 if contract["ascension_level"] >= 15 else 0.5
    )
    counter = contract["omamori_charges"]
    blocked = counter > 0
    effective_probability = 0.0 if blocked else trigger_probability
    darkstone_gain = (
        6 if not blocked and _state_has_relic(state, "Darkstone Periapt") else 0
    )
    random_relic = _mausoleum_random_relic_effect()
    writhe = _mausoleum_writhe_effect()
    card_changes = {
        "gain": [], "remove": [], "upgrade": [], "transform": [],
        "conditional_gain": ([{
            **_audit_clone(writhe),
            "probability": effective_probability,
            "condition": "writhe_branch_not_blocked",
        }] if effective_probability > 0 else []),
    }
    relic_changes = {
        "gain": [], "random_gain": [_audit_clone(random_relic)],
        "remove": [], "counter": [],
    }
    potion_changes = {"gain": [], "remove": [], "replace": []}
    curse = {
        "gain": [], "remove": [],
        "conditional_gain": (
            [] if blocked else [_audit_clone(writhe)]
        ),
        "probability": trigger_probability,
        "effective_gain_probability": effective_probability,
        "omamori_applicable": True,
        "omamori_charges_before": counter,
        "omamori_charges_consumed": 1 if blocked else 0,
        "omamori_charge_use_probability": (
            trigger_probability if blocked else 0.0
        ),
        "blocked": blocked,
    }
    _known_domain_consequence_field(
        consequence, "card_changes", card_changes,
        "mausoleum_exhaustive_writhe_branch",
    )
    _known_domain_consequence_field(
        consequence, "relic_changes", relic_changes,
        "mausoleum_non_boss_relic_domain",
    )
    _known_consequence_field(
        consequence, "potion_changes", potion_changes,
        authority="production_mechanics_projection", reason=reason,
    )
    _known_consequence_field(
        consequence, "curse", curse,
        authority="production_mechanics_projection", reason=reason,
    )

    empty_cards = {
        "gain": [], "remove": [], "upgrade": [], "transform": [],
    }
    empty_curse = {
        "gain": [], "remove": [], "probability": 0.0,
        "omamori_applicable": False,
        "omamori_charges_consumed": 0,
    }

    def outcome(probability, *, triggered):
        outcome_cards = _audit_clone(empty_cards)
        outcome_relics = {
            "gain": [], "random_gain": [_audit_clone(random_relic)],
            "remove": [], "counter": [],
        }
        outcome_curse = _audit_clone(empty_curse)
        if triggered and blocked:
            outcome_relics["counter"] = [{
                "id": contract["omamori_id"],
                "before": counter,
                "after": counter - 1,
                "delta": -1,
            }]
            outcome_curse.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 1, "blocked": True,
                "card_id": "Writhe",
            })
            outcome_id = "writhe_blocked_by_omamori"
        elif triggered:
            outcome_cards["gain"] = [_audit_clone(writhe)]
            outcome_curse.update({
                "gain": [_audit_clone(writhe)],
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 0, "blocked": False,
                "card_id": "Writhe",
            })
            outcome_id = "writhe_added"
        else:
            outcome_id = "no_writhe"
        return {
            "id": outcome_id,
            "probability": probability,
            "hp_delta": (
                darkstone_gain if triggered and not blocked else 0
            ),
            "max_hp_delta": (
                darkstone_gain if triggered and not blocked else 0
            ),
            "gold_delta": 0,
            "card_changes": outcome_cards,
            "relic_changes": outcome_relics,
            "potion_changes": _audit_clone(potion_changes),
            "curse": outcome_curse,
        }

    outcomes = [outcome(trigger_probability, triggered=True)]
    if trigger_probability < 1.0:
        outcomes.append(outcome(
            1.0 - trigger_probability, triggered=False
        ))
    _known_domain_consequence_field(
        consequence, "probabilistic_outcomes", outcomes,
        "mausoleum_exhaustive_writhe_partition_and_relic_domain",
    )
    consequence.update({
        "operation": "mausoleum_open_coffin",
        "event_outcome_id": "open_coffin",
        "random_effects": [
            _audit_clone(random_relic),
            {
                "kind": "mausoleum_writhe_branch",
                "card_id": "Writhe",
                "probability": trigger_probability,
                "omamori_blocked": blocked,
            },
        ],
        "uncertainty": [
            "random non-boss relic identity remains inside the typed domain"
        ],
        "uncertainty_classification": {
            "status": "exhaustive_probability",
            "authority": "production_mechanics_projection",
            "reason": (
                "Writhe trigger/Omamori branches sum to one and the relic "
                "identity domain excludes boss relics"
            ),
        },
    })
    return True


_EXACT_A0_EVENT_MECHANISMS = {
    "thecleric": None,
    "designer": None,
    "worldofgoop": None,
    "cursedtome": None,
    "knowingskull": None,
    "deadadventurer": None,
    "scrapooze": None,
    "facetrader": None,
    "duplicator": None,
    "bonfireelementals": None,
}

_INDEXED_A0_EVENT_IDS = {
    "addict", "backtobasics", "beggar", "bigfish", "drugdealer",
    "liarsgame", "vampires", "goldenshrine",
    "goldenwing", "lab", "livingwall", "nest", "noteforyourself",
    "anoteforyourself",
    "transmorgrifier",
    "thewomaninblue",
    "forgottenaltar", "ghosts", "mindbloom", "upgradeshrine",
    "wemeetagain", "shininglight", "mushrooms", "maskedbandits",
    "colosseum", "windinghalls", "nloth", "themoaihead",
    "accursedblacksmith", "mysterioussphere", "spireheart",
    "purifier", "tomboflordredmask", "thelibrary",
}

_INDEXED_A0_PROGRESS_EVENTS = {
    "liarsgame": (
        "com.megacrit.cardcrawl.events.exordium.Sssserpent", "event_stage",
    ),
    "vampires": (
        "com.megacrit.cardcrawl.events.city.Vampires", "screen_num",
    ),
    "goldenshrine": (
        "com.megacrit.cardcrawl.events.shrines.GoldShrine", "event_stage",
    ),
    "shininglight": (
        "com.megacrit.cardcrawl.events.exordium.ShiningLight", "event_stage",
    ),
    "mushrooms": (
        "com.megacrit.cardcrawl.events.exordium.Mushrooms", "screen_num",
    ),
    "maskedbandits": (
        "com.megacrit.cardcrawl.events.city.MaskedBandits", "event_stage",
    ),
    "colosseum": (
        "com.megacrit.cardcrawl.events.city.Colosseum", "event_stage",
    ),
    "windinghalls": (
        "com.megacrit.cardcrawl.events.beyond.WindingHalls", "screen_num",
    ),
    "sensorystone": (
        "com.megacrit.cardcrawl.events.beyond.SensoryStone", "event_stage",
    ),
    "accursedblacksmith": (
        "com.megacrit.cardcrawl.events.shrines.AccursedBlacksmith", "screen_num",
    ),
    "mysterioussphere": (
        "com.megacrit.cardcrawl.events.beyond.MysteriousSphere", "event_stage",
    ),
    "spireheart": (
        "com.megacrit.cardcrawl.events.beyond.SpireHeart", "event_stage",
    ),
    "thelibrary": (
        "com.megacrit.cardcrawl.events.city.TheLibrary", "screen_num",
    ),
}


def _indexed_event_surface(state, event_id):
    game = _state_game(state or {})
    if game.get("ascension_level") != 0:
        return None
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    screen_options = screen.get("options")
    options = (
        screen_options
        if isinstance(screen_options, list) and screen_options
        else (state or {}).get("options") or []
    )
    indexes = []
    for option in options:
        option = option if isinstance(option, dict) else {}
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        option_event_id = target.get("event_id") or screen.get("event_id")
        if _normalized_game_id(option_event_id) != event_id:
            return None
        index = option.get("original_button_index")
        if index is None:
            index = target.get("original_button_index")
        if type(index) is not int:
            return None
        indexes.append(index)
    return tuple(sorted(indexes)) if len(indexes) == len(set(indexes)) else None


def _indexed_event_grid_effect(event_id, operation, count, *, dialog_steps=0):
    value = {
        "kind": "event_grid_selection",
        "event_id": event_id,
        "domain": "current_deck",
        "operation": operation,
        "select_count": count,
        "selection_mode": "player_choice",
    }
    if operation == "transform":
        value.update({
            "result_domain": "character_cards",
            "result_selection_mode": "random",
        })
    if dialog_steps:
        value["dialog_steps_before_grid"] = dialog_steps
    return [value]


def _apply_fixed_event_curse_gain(
    consequence, state, card, count, reason,
):
    game = _state_game(state or {})
    omamori = next(
        (
            relic for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
            and _normalized_game_id(relic.get("id") or relic.get("name"))
            == "omamori"
        ),
        None,
    )
    charges = omamori.get("counter") if isinstance(omamori, dict) else 0
    if type(charges) is not int or charges < 0:
        return False
    consumed = min(count, charges)
    gained = count - consumed
    card_gain = []
    if gained:
        gained_card = compact_audit_value(card)
        gained_card["count"] = gained
        card_gain.append(gained_card)
    _known_consequence_field(
        consequence, "card_changes", {
            "gain": card_gain, "remove": [], "upgrade": [], "transform": [],
        }, authority="base_game_event_mechanics", reason=reason,
    )
    _known_consequence_field(
        consequence, "curse", {
            "gain": card_gain, "remove": [], "probability": 1.0,
            "omamori_applicable": True,
            "omamori_charges_consumed": consumed,
        }, authority="base_game_event_mechanics", reason=reason,
    )
    if consumed:
        relic_changes = _audit_clone(consequence.get("relic_changes") or {
            "gain": [], "remove": [], "counter": [],
        })
        relic_changes["counter"] = [{
            "id": omamori.get("id") or omamori.get("name") or "Omamori",
            "delta": -consumed,
        }]
        _known_consequence_field(
            consequence, "relic_changes", relic_changes,
            authority="base_game_event_mechanics", reason=reason,
        )
    if gained and any(
        isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        == "darkstoneperiapt"
        for relic in (game.get("relics") or [])
    ):
        hp_gain = 6 * gained
        current_hp_delta = consequence.get("hp_delta")
        max_hp_delta = consequence.get("max_hp_delta")
        if type(current_hp_delta) is not int or type(max_hp_delta) is not int:
            return False
        _mark_numeric_consequences(
            consequence,
            current_hp_delta + hp_gain,
            max_hp_delta + hp_gain,
            consequence.get("gold_delta", 0),
            reason,
        )
    return True


def _apply_indexed_a0_event_consequence(
    consequence, target, state, event_id, raw_text=None,
):
    """Type base-game A0 event surfaces with stable original button ids."""

    surface = _indexed_event_surface(state, event_id)
    index = target.get("original_button_index")
    if surface is None or type(index) is not int or index not in surface:
        return False
    progress = None
    progress_spec = _INDEXED_A0_PROGRESS_EVENTS.get(event_id)
    if progress_spec is not None:
        expected_class, progress_field = progress_spec
        screen = _state_game(state or {}).get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        progress = screen.get(progress_field)
        if (
            screen.get("event_class") != expected_class
            or (
                "event_class" in target
                and target.get("event_class") != expected_class
            )
            or (
                progress_field in target
                and target.get(progress_field) != progress
            )
            or (
                progress_field == "event_stage"
                and (not isinstance(progress, str) or not progress)
            )
            or (
                progress_field == "screen_num"
                and (type(progress) is not int or progress < 0)
            )
        ):
            return False
    expected_surface = {
        "addict": (0, 1, 2),
        "backtobasics": (0, 1),
        "beggar": (0, 1),
        "bigfish": (0, 1, 2),
        "lab": (0,),
        "nest": (0, 1),
        "noteforyourself": (0, 1),
        "anoteforyourself": (0, 1),
        "transmorgrifier": (0, 1),
        "purifier": (0, 1),
        "tomboflordredmask": (0, 1, 2),
        "thewomaninblue": (0, 1, 2, 3),
        "forgottenaltar": (0, 1, 2),
        "ghosts": (0, 1),
        "mindbloom": (0, 1, 2),
        "upgradeshrine": (0, 1),
        "wemeetagain": (0, 1, 2, 3),
        "shininglight": (0, 1) if progress == "INTRO" else (0,),
        "mushrooms": (0, 1) if progress == 0 else (0,),
        "maskedbandits": (0, 1) if progress == "INTRO" else (0,),
        "colosseum": (0, 1) if progress == "POST_COMBAT" else (0,),
        "windinghalls": (0, 1, 2) if progress == 1 else (0,),
        "nloth": (0, 1, 2),
        "themoaihead": (
            (0, 1, 2)
            if _state_has_relic(state, "Golden Idol") else (0, 2)
        ),
        "sensorystone": (0, 1, 2) if progress == "INTRO_2" else (0,),
        "accursedblacksmith": (0, 1, 2) if progress == 0 else (0,),
        "mysterioussphere": (0, 1) if progress == "INTRO" else (0,),
        "spireheart": (0,),
        "liarsgame": (0, 1) if progress == "INTRO" else (0,),
        "vampires": (
            (0, 1, 2)
            if progress == 0 and _state_has_relic(state, "Blood Vial")
            else (0, 1) if progress == 0 else (0,)
        ),
        "goldenshrine": (0, 1, 2) if progress == "INTRO" else (0,),
        "thelibrary": (0, 1) if progress == 0 else (0,),
    }.get(event_id, (0, 1, 2))
    if surface == (0,) and event_id in {
        "addict", "backtobasics", "beggar", "bigfish",
        "thewomaninblue", "forgottenaltar", "ghosts", "mindbloom",
        "upgradeshrine", "wemeetagain", "drugdealer", "livingwall",
        "nest", "noteforyourself", "anoteforyourself",
        "transmorgrifier", "purifier", "nloth", "themoaihead",
    }:
        reason = f"indexed_a0_{event_id}_terminal_dialog"
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        consequence.update({
            "event_id": target.get("event_id"),
            "original_button_index": index,
            "operation": f"{event_id}_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
            "leave": True,
        })
        return True
    if surface != expected_surface:
        # Event dialogs reuse original button index zero for terminal
        # Continue/Leave screens.  The index is only a mechanics identity on
        # the complete initial surface; do not reinterpret a later singleton.
        return False
    reason = f"indexed_a0_{event_id}_mechanics"
    consequence.update({
        "event_id": target.get("event_id"),
        "original_button_index": index,
    })

    _mark_numeric_consequences(consequence, 0, 0, 0, reason)
    _mark_empty_inventory_consequences(consequence, reason)
    _mark_deterministic_costs(consequence, reason=reason)
    _mark_no_probability(consequence, reason)

    operation = None
    future = []
    classification = None
    if event_id == "nloth":
        if index == 2:
            operation = "nloth_leave"
            consequence["leave"] = True
        else:
            label_token = _normalized_game_id(raw_text)
            relic_matches = []
            for relic in (_state_game(state or {}).get("relics") or []):
                if not isinstance(relic, dict):
                    continue
                identifiers = {
                    _normalized_game_id(relic.get("id")),
                    _normalized_game_id(relic.get("name")),
                }
                if any(
                    token and token in label_token for token in identifiers
                ):
                    relic_matches.append(relic)
            if len(relic_matches) != 1:
                return False
            traded = compact_audit_value(relic_matches[0])
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [{"id": "Nloth's Gift"}],
                    "remove": [traded], "counter": [],
                }, authority="protocol_event_option_label", reason=reason,
            )
            consequence.update({
                "relic_id": traded.get("id"),
                "acquired_benefit": {
                    "kind": "relic", "id": "Nloth's Gift",
                },
            })
            operation = "nloth_trade_relic"
    elif event_id == "themoaihead":
        game = _state_game(state or {})
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        if index == 0:
            max_hp_loss = max(1, (max_hp + 4) // 8)
            new_max_hp = max(1, max_hp - max_hp_loss)
            new_hp = (
                min(current_hp, new_max_hp)
                if _state_has_relic(state, "Mark of the Bloom")
                else new_max_hp
            )
            _mark_numeric_consequences(
                consequence, new_hp - current_hp, -max_hp_loss, 0, reason
            )
            _mark_deterministic_costs(
                consequence, max_hp=max_hp_loss, reason=reason
            )
            operation = "moai_head_jump_inside"
        elif index == 1:
            idol = next(
                (
                    relic for relic in (game.get("relics") or [])
                    if isinstance(relic, dict)
                    and _normalized_game_id(
                        relic.get("id") or relic.get("name")
                    ) == "goldenidol"
                ),
                None,
            )
            if idol is None:
                return False
            gold_gain = 0 if _state_has_relic(state, "Ectoplasm") else 333
            _mark_numeric_consequences(
                consequence, 0, 0, gold_gain, reason
            )
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [], "remove": [compact_audit_value(idol)],
                    "counter": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            operation = "moai_head_offer_golden_idol"
        else:
            operation = "moai_head_leave"
            consequence["leave"] = True
    elif event_id == "liarsgame":
        if progress == "INTRO":
            if index == 0:
                gold_gain = (
                    0 if _state_has_relic(state, "Ectoplasm") else 175
                )
                future = [{
                    "kind": "event_deferred_settlement",
                    "event_id": "Liars Game",
                    "event_stage": "AGREE",
                    "gold_delta": gold_gain,
                    "card_gain": {
                        "id": "Doubt", "card_id": "Doubt",
                        "type": "CURSE", "rarity": "CURSE", "count": 1,
                    },
                    "timing": "next_event_confirmation",
                }]
                operation = "liars_game_prepare_agreement"
                consequence["event_outcome_id"] = "accept_gold_and_doubt"
                classification = "classified_future"
            else:
                operation = "liars_game_decline"
                consequence["event_outcome_id"] = "decline"
                consequence["leave"] = True
        elif progress == "AGREE":
            gold_gain = 0 if _state_has_relic(state, "Ectoplasm") else 175
            _mark_numeric_consequences(
                consequence, 0, 0, gold_gain, reason
            )
            doubt = {
                "id": "Doubt", "card_id": "Doubt", "type": "CURSE",
                "rarity": "CURSE", "count": 1,
            }
            if not _apply_fixed_event_curse_gain(
                consequence, state, doubt, 1, reason
            ):
                return False
            operation = "liars_game_settle_agreement"
            consequence["event_outcome_id"] = "settle_gold_and_doubt"
        elif progress in {"DISAGREE", "COMPLETE"}:
            operation = "liars_game_leave"
            consequence["event_outcome_id"] = "leave"
            consequence["leave"] = True
        else:
            return False
    elif event_id == "vampires":
        game = _state_game(state or {})
        if progress == 0:
            relics = [
                relic for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            ]
            blood_vials = [
                relic for relic in relics
                if _normalized_game_id(relic.get("id") or relic.get("name"))
                == "bloodvial"
            ]
            has_vial = len(blood_vials) == 1
            decline_index = 2 if has_vial else 1
            if index == decline_index:
                operation = "vampires_decline"
                consequence["event_outcome_id"] = "decline"
                consequence["leave"] = True
            else:
                if index not in ({0, 1} if has_vial else {0}):
                    return False
                deck = game.get("deck")
                current_hp = game.get("current_hp")
                max_hp = game.get("max_hp")
                if (
                    not isinstance(deck, list)
                    or type(current_hp) is not int
                    or type(max_hp) is not int
                    or max_hp < 1
                ):
                    return False
                starter_strikes = {
                    "striker", "strikeg", "strikeb", "strikep",
                }
                removed = [
                    compact_audit_value(card)
                    for card in deck
                    if isinstance(card, dict)
                    and _normalized_game_id(card.get("id") or card.get("name"))
                    in starter_strikes
                ]
                bite = {
                    "id": "Bite", "card_id": "Bite", "type": "ATTACK",
                    "rarity": "SPECIAL", "count": 5,
                }
                trade_vial = bool(has_vial and index == 1)
                max_hp_loss = 0 if trade_vial else (max_hp * 3 + 9) // 10
                hp_loss = max(0, current_hp - (max_hp - max_hp_loss))
                _mark_numeric_consequences(
                    consequence, -hp_loss, -max_hp_loss, 0, reason
                )
                _mark_deterministic_costs(
                    consequence, hp=0, max_hp=max_hp_loss, reason=reason
                )
                _known_consequence_field(
                    consequence, "card_changes", {
                        "gain": [bite], "remove": removed,
                        "upgrade": [], "transform": [],
                    }, authority="base_game_event_mechanics", reason=reason,
                )
                if trade_vial:
                    _known_consequence_field(
                        consequence, "relic_changes", {
                            "gain": [],
                            "remove": [compact_audit_value(blood_vials[0])],
                            "counter": [],
                        }, authority="base_game_event_mechanics", reason=reason,
                    )
                consequence["acquired_benefit"] = {
                    "kind": "card_package", "id": "Bite",
                    "card_id": "Bite", "count": 5,
                    "removed_starter_strike_count": len(removed),
                }
                operation = (
                    "vampires_trade_blood_vial_for_bites"
                    if trade_vial else "vampires_trade_max_hp_for_bites"
                )
                consequence["event_outcome_id"] = (
                    "trade_blood_vial_for_bites"
                    if trade_vial else "accept_bites"
                )
        elif progress in {1, 2}:
            operation = "vampires_leave"
            consequence["event_outcome_id"] = "leave"
            consequence["leave"] = True
        else:
            return False
    elif event_id == "goldenshrine":
        if progress == "INTRO":
            if index == 0:
                gold_gain = (
                    0 if _state_has_relic(state, "Ectoplasm") else 100
                )
                _mark_numeric_consequences(
                    consequence, 0, 0, gold_gain, reason
                )
                operation = "golden_shrine_pray"
                consequence["event_outcome_id"] = "pray"
            elif index == 1:
                gold_gain = (
                    0 if _state_has_relic(state, "Ectoplasm") else 275
                )
                _mark_numeric_consequences(
                    consequence, 0, 0, gold_gain, reason
                )
                regret = {
                    "id": "Regret", "card_id": "Regret", "type": "CURSE",
                    "rarity": "CURSE", "count": 1,
                }
                if not _apply_fixed_event_curse_gain(
                    consequence, state, regret, 1, reason
                ):
                    return False
                operation = "golden_shrine_desecrate"
                consequence["event_outcome_id"] = "desecrate"
            else:
                operation = "golden_shrine_leave"
                consequence["event_outcome_id"] = "leave"
                consequence["leave"] = True
        elif progress == "COMPLETE":
            operation = "golden_shrine_leave"
            consequence["event_outcome_id"] = "leave"
            consequence["leave"] = True
        else:
            return False
    elif (
        event_id in _INDEXED_A0_PROGRESS_EVENTS
        and event_id != "spireheart"
        and surface == (0,)
    ):
        terminal = False
        if event_id == "mushrooms" and progress == 2:
            operation = "mushrooms_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "The Mushroom Lair",
                "reward": {
                    "gold_range": [20, 30],
                    "relic_id": "Odd Mushroom",
                    "duplicate_relic_id": "Circlet",
                },
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        elif event_id == "colosseum" and progress == "FIGHT":
            operation = "colosseum_start_first_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Colosseum Slavers",
                "reward": {"normal_combat_rewards": False},
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        elif event_id == "mysterioussphere" and progress == "PRE_COMBAT":
            operation = "mysterious_sphere_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "2 Orb Walkers",
                "reward": {"event_relic_rewards": True},
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        else:
            operation = f"{event_id}_dialog_advance_noop"
            terminal = bool(
                event_id == "shininglight" and progress == "COMPLETE"
                or event_id == "mushrooms" and progress == 1
                or event_id == "maskedbandits" and progress == "END"
                or event_id == "colosseum" and progress == "LEAVE"
                or event_id == "windinghalls" and progress == 2
                or event_id == "sensorystone" and progress == "LEAVE"
                or event_id == "accursedblacksmith" and progress == 2
                or event_id == "mysterioussphere" and progress == "END"
                or event_id == "thelibrary" and progress == 1
            )
            if terminal:
                consequence["leave"] = True
        consequence["event_outcome_id"] = (
            "leave" if terminal else "dialog_advance"
        )
    elif event_id == "thelibrary" and progress == 0:
        game = _state_game(state or {})
        if index == 0:
            operation = "library_prepare_card_choice"
            future = [{
                "kind": "event_grid_selection",
                "event_id": "The Library",
                "domain": "library_card_offering",
                "operation": "gain",
                "visible_count": 20,
                "select_count": 1,
                "selection_mode": "player_choice",
            }]
            classification = "classified_future"
        else:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            relic_ids = {
                _normalized_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            # Base game uses MathUtils.round(maxHealth * 0.33F), not integer
            # division by three (50 max HP heals 17, for example).
            requested_heal = (max_hp * 33 + 50) // 100
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, requested_heal
            )
            _mark_numeric_consequences(consequence, heal, 0, 0, reason)
            operation = "library_sleep"
            consequence["event_outcome_id"] = "sleep"
            consequence["leave"] = True
    elif event_id == "shininglight" and progress == "INTRO":
        if index == 1:
            operation = "shining_light_leave"
            consequence["leave"] = True
        else:
            game = _state_game(state or {})
            max_hp = game.get("max_hp")
            if type(max_hp) is not int:
                return False
            relic_ids = {
                _normalized_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            hp_loss = max_hp // 5
            if "torii" in relic_ids and 1 < hp_loss <= 5:
                hp_loss = 1
            if "tungstenrod" in relic_ids and hp_loss > 0:
                hp_loss -= 1
            _mark_numeric_consequences(
                consequence, -hp_loss, 0, 0, reason
            )
            _mark_deterministic_costs(
                consequence, hp=hp_loss, reason=reason
            )
            candidates = [
                compact_audit_value(card)
                for card in (game.get("deck") or [])
                if isinstance(card, dict)
                and int(card.get("upgrades") or 0) == 0
                and str(card.get("type") or "").upper()
                not in {"CURSE", "STATUS"}
            ]
            count = min(2, len(candidates))
            random_upgrade = {
                "kind": "event_random_card_upgrades",
                "event_id": event_id,
                "selection_mode": "random_without_replacement",
                "count": count,
                "eligible_card_instance_ids": sorted(
                    card.get("card_instance_id")
                    for card in candidates
                    if card.get("card_instance_id")
                ),
            }
            _known_domain_consequence_field(
                consequence, "card_changes", {
                    "gain": [], "remove": [], "upgrade": [],
                    "transform": [], "random_upgrade": [random_upgrade],
                }, "shining_light_random_upgradable_cards",
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "shining_light_random_upgradable_cards",
            )
            consequence["random_effects"] = [random_upgrade]
            operation = "shining_light_enter"
            classification = "classified_random_domain"
    elif event_id == "mushrooms" and progress == 0:
        game = _state_game(state or {})
        if index == 0:
            operation = "mushrooms_prepare_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "The Mushroom Lair",
                "requires_confirmation": True,
                "reward": {
                    "gold_range": [20, 30],
                    "relic_id": "Odd Mushroom",
                    "duplicate_relic_id": "Circlet",
                },
            }]
            classification = "classified_future"
        else:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if (
                type(current_hp) is not int or type(max_hp) is not int
                or card is None
                or _normalized_game_id(card.get("id")) != "parasite"
            ):
                return False
            relic_ids = {
                _normalized_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, max_hp // 4
            )
            _mark_numeric_consequences(consequence, heal, 0, 0, reason)
            if not _apply_fixed_event_curse_gain(
                consequence, state, card, 1, reason
            ):
                return False
            operation = "mushrooms_heal_and_take_parasite"
    elif event_id == "maskedbandits" and progress == "INTRO":
        game = _state_game(state or {})
        if index == 0:
            gold = game.get("gold")
            if type(gold) is not int:
                return False
            _mark_numeric_consequences(consequence, 0, 0, -gold, reason)
            _mark_deterministic_costs(
                consequence, gold=gold, reason=reason
            )
            operation = "masked_bandits_pay_all_gold"
            consequence["leave"] = True
        else:
            operation = "masked_bandits_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Masked Bandits",
                "reward": {
                    "relic_id": "Red Mask",
                    "duplicate_relic_id": "Circlet",
                },
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
    elif event_id in {"noteforyourself", "anoteforyourself"} and surface == (0, 1):
        if index == 1:
            operation = "note_for_yourself_leave"
            consequence["leave"] = True
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or not _normalized_game_id(card.get("id")):
                return False
            offered = compact_audit_value(card)
            offered_id = offered.get("id") or offered.get("card_id")
            future = [{
                "kind": "event_grid_exchange",
                "event_id": event_id,
                "domain": "current_deck",
                "operation": "exchange_for_offered_card",
                "select_count": 1,
                "selection_mode": "player_choice",
                "offered_card": offered,
            }]
            consequence["acquired_benefit"] = {
                "kind": "card_package", "id": offered_id,
                "card_id": offered_id, "count": 1,
            }
            operation = "note_for_yourself_prepare_exchange"
            classification = "classified_future"
    elif event_id == "colosseum" and progress == "POST_COMBAT":
        if index == 0:
            operation = "colosseum_leave_after_first_combat"
            consequence["leave"] = True
        else:
            operation = "colosseum_start_second_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Colosseum Nobs",
                "reward": {"event_relic_rewards": True},
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
    elif event_id == "windinghalls" and progress == 1:
        game = _state_game(state or {})
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        high_ascension = int(game.get("ascension_level") or 0) >= 15
        madness_hp_loss = max(
            1,
            (
                (max_hp * 9 + 25) // 50
                if high_ascension else (max_hp + 4) // 8
            ),
        )
        focus_heal = max(
            1,
            (
                (max_hp + 2) // 5
                if high_ascension else (max_hp + 2) // 4
            ),
        )
        retrace_max_hp_loss = max(1, (max_hp + 10) // 20)
        relic_ids = {
            _normalized_game_id(relic.get("id") or relic.get("name"))
            for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        }
        if index == 0:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _normalized_game_id(card.get("id")) != "madness":
                return False
            hp_loss = madness_hp_loss
            if "tungstenrod" in relic_ids and hp_loss > 0:
                hp_loss -= 1
            _mark_numeric_consequences(
                consequence, -hp_loss, 0, 0, reason
            )
            _mark_deterministic_costs(
                consequence, hp=hp_loss, reason=reason
            )
            gained = compact_audit_value(card)
            gained["count"] = 2
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [gained], "remove": [], "upgrade": [],
                    "transform": [],
                }, authority="protocol_event_card_preview", reason=reason,
            )
            operation = "winding_halls_embrace_madness"
        elif index == 1:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _normalized_game_id(card.get("id")) != "writhe":
                return False
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, focus_heal
            )
            _mark_numeric_consequences(consequence, heal, 0, 0, reason)
            if not _apply_fixed_event_curse_gain(
                consequence, state, card, 1, reason
            ):
                return False
            operation = "winding_halls_focus"
        else:
            max_hp_loss = retrace_max_hp_loss
            new_max_hp = max(1, max_hp - max_hp_loss)
            hp_delta = min(current_hp, new_max_hp) - current_hp
            _mark_numeric_consequences(
                consequence, hp_delta, -max_hp_loss, 0, reason
            )
            _mark_deterministic_costs(
                consequence, max_hp=max_hp_loss, reason=reason
            )
            operation = "winding_halls_retrace_steps"
    elif event_id == "sensorystone" and progress == "INTRO_2":
        game = _state_game(state or {})
        relic_ids = {
            _normalized_game_id(relic.get("id") or relic.get("name"))
            for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        }
        hp_loss = {0: 0, 1: 5, 2: 10}[index]
        if "tungstenrod" in relic_ids and hp_loss > 0:
            hp_loss -= 1
        _mark_numeric_consequences(consequence, -hp_loss, 0, 0, reason)
        _mark_deterministic_costs(
            consequence, hp=hp_loss, reason=reason
        )
        operation = f"sensory_stone_recall_{index + 1}"
        future = [{
            "kind": "optional_card_reward_sequence",
            "event_id": event_id,
            "card_pool": "COLORLESS",
            "reward_count": index + 1,
            "candidates_per_reward": 3,
            "can_skip": True,
            "timing": "after_event_choice",
        }]
        classification = "classified_future"
    elif event_id == "accursedblacksmith" and progress == 0:
        if index == 0:
            operation = "accursed_blacksmith_open_upgrade_grid"
            future = _indexed_event_grid_effect(event_id, "upgrade", 1)
            classification = "classified_future"
        elif index == 1:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _normalized_game_id(card.get("id")) != "pain":
                return False
            if not _apply_fixed_event_curse_gain(
                consequence, state, card, 1, reason
            ):
                return False
            relic_changes = _audit_clone(consequence.get("relic_changes"))
            relic_changes["gain"] = [{"id": "Warped Tongs"}]
            _known_consequence_field(
                consequence, "relic_changes", relic_changes,
                authority="base_game_event_mechanics", reason=reason,
            )
            consequence["relic_id"] = "Warped Tongs"
            operation = "accursed_blacksmith_rummage"
        else:
            operation = "accursed_blacksmith_leave"
            consequence["leave"] = True
    elif event_id == "mysterioussphere" and progress == "INTRO":
        if index == 0:
            operation = "mysterious_sphere_prepare_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "2 Orb Walkers",
                "requires_confirmation": True,
                "reward": {"event_relic_rewards": True},
            }]
            classification = "classified_future"
        else:
            operation = "mysterious_sphere_leave"
            consequence["leave"] = True
    elif event_id == "spireheart":
        game = _state_game(state or {})
        enters_act_four = bool(
            progress == "GO_TO_ENDING"
            and game.get("act") == 3
            and all(
                game.get(field) is True
                for field in (
                    "has_ruby_key", "has_emerald_key", "has_sapphire_key",
                )
            )
        )
        if enters_act_four:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if (
                type(current_hp) is not int
                or type(max_hp) is not int
                or not 0 <= current_hp <= max_hp
            ):
                return False
            _mark_numeric_consequences(
                consequence, max_hp - current_hp, 0, 0, reason
            )
            operation = "spire_heart_enter_act_four"
            consequence["event_outcome_id"] = "enter_act_four"
        else:
            operation = "spire_heart_dialog_advance_noop"
            consequence["event_outcome_id"] = "dialog_advance"
    elif event_id == "bigfish" and surface == (0, 1, 2):
        game = _state_game(state or {})
        if index == 0:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            heal = min(max_hp - current_hp, max_hp // 3)
            _mark_numeric_consequences(consequence, heal, 0, 0, reason)
            operation = "big_fish_banana"
        elif index == 1:
            _mark_numeric_consequences(consequence, 5, 5, 0, reason)
            operation = "big_fish_donut"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _normalized_game_id(card.get("id")) != "regret":
                return False
            _known_domain_consequence_field(
                consequence, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "big_fish_random_non_boss_relic",
            )
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [compact_audit_value(card)], "remove": [],
                    "upgrade": [], "transform": [],
                }, authority="protocol_event_card_preview", reason=reason,
            )
            _known_consequence_field(
                consequence, "curse", {
                    "gain": [compact_audit_value(card)], "remove": [],
                    "probability": 1.0, "omamori_applicable": True,
                    "omamori_charges_consumed": 0,
                }, authority="base_game_event_mechanics", reason=reason,
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "big_fish_random_non_boss_relic",
            )
            operation = "big_fish_box"
            classification = "classified_random_domain"
            consequence["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
    elif event_id == "thewomaninblue" and surface == (0, 1, 2, 3):
        if index == 3:
            operation = "woman_in_blue_leave"
            consequence["leave"] = True
        else:
            costs = {0: 20, 1: 30, 2: 40}
            counts = {0: 1, 1: 2, 2: 3}
            cost = costs[index]
            requested = counts[index]
            slots = sum(
                1 for potion in (_state_game(state or {}).get("potions") or [])
                if isinstance(potion, dict)
                and _normalized_game_id(potion.get("id")) == "potionslot"
            )
            acquired = min(requested, slots)
            _mark_numeric_consequences(consequence, 0, 0, -cost, reason)
            _mark_deterministic_costs(
                consequence, gold=cost, reason=reason
            )
            _known_domain_consequence_field(
                consequence, "potion_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_potion_gain",
                        "domain": "base_game_potion_pool",
                        "count": acquired,
                    }] if acquired else [],
                    "remove": [], "replace": [],
                }, "woman_in_blue_random_potion_pool",
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "woman_in_blue_random_potion_pool",
            )
            operation = f"woman_in_blue_buy_{requested}"
            classification = "classified_random_domain"
            consequence["random_effects"] = [{
                "kind": "random_potion_gain",
                "domain": "base_game_potion_pool", "count": acquired,
                "selection_mode": "random",
            }]
    elif event_id == "backtobasics" and surface == (0, 1):
        if index == 0:
            operation = "back_to_basics_remove"
            future = _indexed_event_grid_effect(event_id, "remove", 1)
            classification = "classified_future"
        else:
            basics = []
            for card in (_state_game(state or {}).get("deck") or []):
                card_id = _normalized_game_id(
                    card.get("id") if isinstance(card, dict) else None
                )
                if (
                    isinstance(card, dict)
                    and card_id in {
                        "striker", "strikeg", "strikeb", "strikep",
                        "defendr", "defendg", "defendb", "defendp",
                    }
                    and card.get("upgrades") == 0
                ):
                    basics.append(compact_audit_value(card))
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [], "remove": [], "upgrade": basics,
                    "transform": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            operation = "back_to_basics_upgrade_basics"
    elif event_id == "addict" and surface == (0, 1, 2):
        if index == 2:
            operation = "addict_leave"
            consequence["leave"] = True
        else:
            if index == 0:
                _mark_numeric_consequences(consequence, 0, 0, -85, reason)
                _mark_deterministic_costs(
                    consequence, gold=85, reason=reason
                )
            _known_domain_consequence_field(
                consequence, "hp_delta", {"min": 0, "max": 14},
                "addict_random_non_boss_relic_hp_domain",
            )
            _known_domain_consequence_field(
                consequence, "max_hp_delta", {"min": 0, "max": 14},
                "addict_random_non_boss_relic_max_hp_domain",
            )
            _known_domain_consequence_field(
                consequence, "gold_delta", {
                    "min": -85 if index == 0 else 0,
                    "max": 215 if index == 0 else 300,
                }, "addict_random_non_boss_relic_gold_domain",
            )
            _known_domain_consequence_field(
                consequence, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "addict_random_non_boss_relic",
            )
            if index == 1:
                card = target.get("card")
                card = card if isinstance(card, dict) else None
                if card is None or _normalized_game_id(card.get("id")) != "shame":
                    return False
                _known_consequence_field(
                    consequence, "card_changes", {
                        "gain": [compact_audit_value(card)], "remove": [],
                        "upgrade": [], "transform": [],
                    }, authority="protocol_event_card_preview", reason=reason,
                )
                _known_consequence_field(
                    consequence, "curse", {
                        "gain": [compact_audit_value(card)], "remove": [],
                        "probability": 1.0, "omamori_applicable": True,
                        "omamori_charges_consumed": 0,
                    }, authority="base_game_event_mechanics", reason=reason,
                )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "addict_random_non_boss_relic",
            )
            operation = "addict_buy_relic" if index == 0 else "addict_rob"
            classification = "classified_random_domain"
            consequence["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
    elif event_id == "beggar" and surface == (0, 1):
        if index == 0:
            _mark_numeric_consequences(consequence, 0, 0, -75, reason)
            _mark_deterministic_costs(
                consequence, gold=75, reason=reason
            )
            operation = "beggar_pay_for_remove"
            future = _indexed_event_grid_effect(event_id, "remove", 1)
            classification = "classified_future"
        else:
            operation = "beggar_leave"
            consequence["leave"] = True
    elif event_id == "livingwall" and surface == (0, 1, 2):
        operation = {0: "remove", 1: "transform", 2: "upgrade"}[index]
        future = _indexed_event_grid_effect(event_id, operation, 1)
        classification = "classified_future"
    elif event_id == "nest" and surface == (0, 1):
        if index == 0:
            _mark_numeric_consequences(consequence, 0, 0, 99, reason)
            operation = "nest_steal_gold"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if (
                card is None
                or _normalized_game_id(card.get("id")) != "ritualdagger"
            ):
                return False
            _mark_numeric_consequences(consequence, -6, 0, 0, reason)
            _mark_deterministic_costs(consequence, hp=6, reason=reason)
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [compact_audit_value(card)], "remove": [],
                    "upgrade": [], "transform": [],
                }, authority="protocol_event_card_preview", reason=reason,
            )
            consequence["acquired_benefit"] = {
                "kind": "card_package", "id": card.get("id"),
                "card_id": card.get("id"), "count": 1,
            }
            operation = "nest_join_cult"
    elif event_id == "transmorgrifier" and surface == (0, 1):
        if index == 0:
            operation = "transmorgrifier_open_transform_grid"
            future = _indexed_event_grid_effect(
                event_id, "transform", 1
            )
            classification = "classified_future"
        else:
            operation = "transmorgrifier_leave"
            consequence["leave"] = True
    elif event_id == "drugdealer" and surface == (0, 1, 2):
        if index == 0:
            game = _state_game(state or {})
            max_hp_loss = min(3, max(0, int(game.get("max_hp") or 0) - 1))
            _mark_numeric_consequences(
                consequence, 0, -max_hp_loss, 0, reason
            )
            _mark_deterministic_costs(
                consequence, max_hp=max_hp_loss, reason=reason
            )
            card = (
                target.get("card")
                if isinstance(target.get("card"), dict) else None
            )
            if card is None or _normalized_game_id(card.get("id")) != "jax":
                return False
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [compact_audit_value(card)], "remove": [],
                    "upgrade": [], "transform": [],
                }, authority="protocol_event_card_preview", reason=reason,
            )
            operation = "drug_dealer_take_jax"
        elif index == 1:
            operation = "drug_dealer_transform_two"
            future = _indexed_event_grid_effect(event_id, "transform", 2)
            classification = "classified_future"
        else:
            operation = "drug_dealer_take_mutagenic_strength"
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [{"id": "MutagenicStrength"}],
                    "remove": [], "counter": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            consequence["relic_id"] = "MutagenicStrength"
    elif event_id == "goldenwing" and surface == (0, 1, 2):
        if index == 0:
            _mark_numeric_consequences(consequence, -7, 0, 0, reason)
            _mark_deterministic_costs(consequence, hp=7, reason=reason)
            operation = "golden_wing_pray_remove"
            future = _indexed_event_grid_effect(
                event_id, "remove", 1, dialog_steps=1
            )
            classification = "classified_future"
        elif index == 1:
            operation = "golden_wing_destroy_for_gold"
            _known_domain_consequence_field(
                consequence, "gold_delta", {"min": 50, "max": 80},
                "inclusive_uniform_50_to_80",
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "inclusive_uniform_50_to_80",
            )
            classification = "classified_random_domain"
        else:
            operation = "golden_wing_leave"
            consequence["leave"] = True
    elif event_id == "lab" and surface == (0,):
        operation = "lab_receive_random_potions"
        future = [{
            "kind": "lab_random_potion_rewards",
            "count": 3,
            "selection_mode": "random",
            "timing": "after_event_choice",
        }]
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", [],
            "base_game_potion_reward_pool",
        )
        classification = "classified_random_domain"
    elif event_id == "ghosts" and surface == (0, 1):
        if index == 1:
            operation = "ghosts_decline"
        else:
            game = _state_game(state or {})
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            ascension = game.get("ascension_level")
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if (
                type(current_hp) is not int or type(max_hp) is not int
                or type(ascension) is not int or card is None
                or _normalized_game_id(card.get("id"))
                not in {"apparition", "ghostly"}
            ):
                return False
            post_max_hp = max(1, max_hp // 2)
            max_hp_loss = max_hp - post_max_hp
            received = 3 if ascension >= 15 else 5
            gained_card = _card_gain_after_egg_relics(card, game)
            gained_card["count"] = received
            _mark_numeric_consequences(
                consequence, min(current_hp, post_max_hp) - current_hp,
                -max_hp_loss, 0, reason,
            )
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [gained_card], "remove": [], "upgrade": [],
                    "transform": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            consequence["acquired_benefit"] = {
                "kind": "card_package", "id": card.get("id"),
                "card_id": card.get("id"), "count": received,
            }
            operation = "ghosts_accept"
    elif event_id == "forgottenaltar" and surface == (0, 1, 2):
        game = _state_game(state or {})
        if index == 0:
            relic_ids = {
                _normalized_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            if "goldenidol" not in relic_ids:
                return False
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [{"id": "Bloody Idol"}],
                    "remove": [{"id": "Golden Idol"}], "counter": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            operation = "forgotten_altar_offer_golden_idol"
        elif index == 1:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            max_hp_gain = 5
            damage = (max_hp + 2) // 4
            post_gain_hp = min(current_hp + max_hp_gain, max_hp + max_hp_gain)
            _mark_numeric_consequences(
                consequence, post_gain_hp - damage - current_hp,
                max_hp_gain, 0, reason,
            )
            operation = "forgotten_altar_sacrifice"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _normalized_game_id(card.get("id")) != "decay":
                return False
            if not _apply_fixed_event_curse_gain(
                consequence, state, card, 1, reason
            ):
                return False
            operation = "forgotten_altar_desecrate"
    elif event_id == "mindbloom" and surface == (0, 1, 2):
        game = _state_game(state or {})
        floor = game.get("floor")
        if type(floor) is not int:
            return False
        if index == 0:
            operation = "mind_bloom_war_future_combat"
            future = [{
                "kind": "mind_bloom_act1_boss_combat",
                "reward": {
                    "rare_relic_count": 1,
                    "gold": 25 if int(game.get("ascension_level") or 0) >= 15 else 50,
                    "normal_combat_rewards": True,
                },
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
            consequence["random_effects"] = [{
                "kind": "relic_gain",
                "domain": "rare_relics",
                "rarities": ["RARE"],
                "count": 1,
                "timing": "after_optional_boss_combat",
                "selection_mode": "random",
            }]
        elif index == 1:
            upgrades = [
                compact_audit_value(card)
                for card in (game.get("deck") or [])
                if isinstance(card, dict)
                and int(card.get("upgrades") or 0) == 0
                and str(card.get("type") or "").upper()
                not in {"CURSE", "STATUS"}
            ]
            _known_consequence_field(
                consequence, "card_changes", {
                    "gain": [], "remove": [], "upgrade": upgrades,
                    "transform": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [{"id": "Mark of the Bloom"}],
                    "remove": [], "counter": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            consequence["relic_id"] = "Mark of the Bloom"
            operation = "mind_bloom_awake"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if card is None or type(current_hp) is not int or type(max_hp) is not int:
                return False
            if floor >= 41:
                if _normalized_game_id(card.get("id")) != "doubt":
                    return False
                _mark_numeric_consequences(
                    consequence, max_hp - current_hp, 0, 0, reason
                )
                if not _apply_fixed_event_curse_gain(
                    consequence, state, card, 1, reason
                ):
                    return False
                operation = "mind_bloom_healthy"
            else:
                if _normalized_game_id(card.get("id")) != "normality":
                    return False
                _mark_numeric_consequences(consequence, 0, 0, 999, reason)
                if not _apply_fixed_event_curse_gain(
                    consequence, state, card, 2, reason
                ):
                    return False
                operation = "mind_bloom_rich"
    elif event_id == "upgradeshrine" and surface == (0, 1):
        if index == 0:
            operation = "upgrade_shrine_open_grid"
            future = _indexed_event_grid_effect(event_id, "upgrade", 1)
            classification = "classified_future"
        else:
            operation = "upgrade_shrine_leave"
            consequence["leave"] = True
    elif event_id == "wemeetagain" and surface == (0, 1, 2, 3):
        game = _state_game(state or {})
        if index == 3:
            operation = "we_meet_again_attack"
        else:
            _known_domain_consequence_field(
                consequence, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "we_meet_again_random_non_boss_relic",
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "we_meet_again_random_non_boss_relic",
            )
            classification = "classified_random_domain"
            consequence["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
            if index == 0:
                held = [
                    compact_audit_value(potion)
                    for potion in (game.get("potions") or [])
                    if isinstance(potion, dict)
                    and _normalized_game_id(potion.get("id")) != "potionslot"
                ]
                if not held:
                    return False
                _known_domain_consequence_field(
                    consequence, "potion_changes", {
                        "gain": [], "remove_domain": held, "remove_count": 1,
                        "replace": [],
                    }, "we_meet_again_visible_held_potion_domain",
                )
                operation = "we_meet_again_give_potion"
            elif index == 1:
                _known_domain_consequence_field(
                    consequence, "gold_delta", {
                        "kind": "event_generated_offered_gold_loss",
                        "minimum": -max(0, int(game.get("gold") or 0)),
                        "maximum": 0,
                    }, "we_meet_again_hidden_offer_amount",
                )
                operation = "we_meet_again_give_gold"
            else:
                card = target.get("card")
                card = card if isinstance(card, dict) else None
                if card is None:
                    return False
                _known_consequence_field(
                    consequence, "card_changes", {
                        "gain": [], "remove": [compact_audit_value(card)],
                        "upgrade": [], "transform": [],
                    }, authority="protocol_event_card_preview", reason=reason,
                )
                operation = "we_meet_again_give_card"
    elif event_id == "purifier" and surface == (0, 1):
        if index == 0:
            operation = "purifier_open_remove_grid"
            future = _indexed_event_grid_effect(
                event_id, "remove", 1
            )
            classification = "classified_future"
        else:
            operation = "purifier_leave"
            consequence["leave"] = True
    elif event_id == "tomboflordredmask" and surface == (0, 1, 2):
        if index == 1:
            game = _state_game(state or {})
            gold = game.get("gold")
            if type(gold) is not int:
                return False
            _mark_numeric_consequences(
                consequence, 0, 0, -max(0, gold), reason
            )
            _mark_deterministic_costs(
                consequence, reason=reason, gold=max(0, gold)
            )
            _known_consequence_field(
                consequence, "relic_changes", {
                    "gain": [{"id": "Red Mask"}],
                    "remove": [], "counter": [],
                }, authority="base_game_event_mechanics", reason=reason,
            )
            consequence["relic_id"] = "Red Mask"
            operation = "tomb_red_mask_offer_all_gold"
        else:
            operation = "tomb_red_mask_leave"
            consequence["leave"] = True
    else:
        return False

    consequence["operation"] = operation
    if event_id in {"liarsgame", "vampires", "goldenshrine"}:
        consequence["mechanism_id"] = (
            f"base_game_{event_id}_a0_progress_v1"
        )
        consequence["random_effects"] = []
    if future:
        _known_consequence_field(
            consequence, "future_costs", future,
            authority="base_game_event_mechanics", reason=reason,
        )
    if classification:
        consequence["uncertainty"] = [
            "exact downstream identity settles on the typed follow-up surface"
        ]
        consequence["uncertainty_classification"] = {
            "status": classification,
            "authority": "base_game_event_mechanics",
            "reason": reason,
        }
    return True


def _canonical_goop_event_contract(value, original_index):
    """Validate the bridge's reflection-backed Goop instance contract."""

    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "original_button_index", "option_kind", "parameters",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or value.get("event_id") != "World of Goop"
        or value.get("event_class") != (
            "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
        )
        or value.get("original_button_index") != original_index
        or not isinstance(value.get("parameters"), dict)
    ):
        return None, None
    option_kind = value.get("option_kind")
    parameters = value["parameters"]
    expected_fields = {
        (0, "GATHER"): {"gold_gain", "hp_damage"},
        (1, "LEAVE"): {"gold_loss"},
        (0, "CONTINUE"): set(),
    }.get((original_index, option_kind))
    if (
        expected_fields is None
        or set(parameters) != expected_fields
        or any(
            type(item) is not int or item < 0
            for item in parameters.values()
        )
        or (
            option_kind == "GATHER"
            and parameters != {"gold_gain": 75, "hp_damage": 11}
        )
    ):
        return None, None
    contract = json.loads(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ))
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:20]
    return contract, f"event-mechanism:{digest}"


def _exact_typed_contract_value(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _exact_typed_contract_value(value[key], expected[key])
            for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _exact_typed_contract_value(item, wanted)
            for item, wanted in zip(value, expected)
        )
    return value == expected


def _canonical_staged_event_contract(value, event_id, original_index, game):
    """Validate one frozen A0 staged event contract and stable mechanism."""

    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "event_stage", "original_button_index", "option_kind",
        "instance_parameters", "parameters",
    }
    identities = {
        "thecleric": (
            "The Cleric", "com.megacrit.cardcrawl.events.exordium.Cleric",
        ),
        "designer": (
            "Designer", "com.megacrit.cardcrawl.events.shrines.Designer",
        ),
        "cursedtome": (
            "Cursed Tome",
            "com.megacrit.cardcrawl.events.city.CursedTome",
        ),
        "knowingskull": (
            "Knowing Skull",
            "com.megacrit.cardcrawl.events.city.KnowingSkull",
        ),
        "deadadventurer": (
            "Dead Adventurer",
            "com.megacrit.cardcrawl.events.exordium.DeadAdventurer",
        ),
        "scrapooze": (
            "Scrap Ooze",
            "com.megacrit.cardcrawl.events.exordium.ScrapOoze",
        ),
        "facetrader": (
            "Face Trader",
            "com.megacrit.cardcrawl.events.shrines.FaceTrader",
        ),
        "duplicator": (
            "Duplicator",
            "com.megacrit.cardcrawl.events.shrines.Duplicator",
        ),
        "bonfireelementals": (
            "Bonfire Elementals",
            "com.megacrit.cardcrawl.events.shrines.Bonfire",
        ),
    }
    identity = identities.get(event_id)
    if (
        identity is None
        or not isinstance(value, dict)
        or set(value) != required
        or value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or value.get("event_id") != identity[0]
        or value.get("event_class") != identity[1]
        or type(value.get("event_stage")) is not str
        or value.get("original_button_index") != original_index
        or type(value.get("option_kind")) is not str
        or not isinstance(value.get("instance_parameters"), dict)
        or not isinstance(value.get("parameters"), dict)
    ):
        return None, None
    stage = value["event_stage"]
    kind = value["option_kind"]
    instance = value["instance_parameters"]
    parameters = value["parameters"]
    key = (stage, original_index, kind)
    expected = None

    if event_id == "thecleric":
        max_hp = game.get("max_hp")
        if (
            set(instance) != {
                "heal_amount", "heal_gold_cost", "purify_cost",
            }
            or type(instance.get("heal_amount")) is not int
            or type(max_hp) is not int
            or instance.get("heal_amount") != int(max_hp * 0.25)
            or instance.get("heal_gold_cost") != 35
            or instance.get("purify_cost") != 50
        ):
            return None, None
        if key == ("MAIN", 0, "HEAL"):
            expected = {
                "gold_cost": 35, "heal_amount": instance["heal_amount"],
            }
        elif key == ("MAIN", 1, "PURIFY"):
            expected = {
                "gold_cost_if_purgeable": 50,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }
        elif key in {
            ("MAIN", 2, "LEAVE"), ("RESULT", 0, "CONTINUE"),
        }:
            expected = {}

    elif event_id == "designer":
        if (
            set(instance) != {
                "adjustment_upgrades_one", "clean_up_removes_cards",
                "adjust_cost", "clean_up_cost", "full_service_cost",
                "hp_loss",
            }
            or type(instance.get("adjustment_upgrades_one")) is not bool
            or type(instance.get("clean_up_removes_cards")) is not bool
            or (
                instance.get("adjust_cost"), instance.get("clean_up_cost"),
                instance.get("full_service_cost"), instance.get("hp_loss"),
            ) != (40, 60, 90, 3)
        ):
            return None, None
        if key in {
            ("INTRO", 0, "OPEN_SERVICES"),
            ("DONE", 0, "CONTINUE"),
        }:
            expected = {}
        elif stage == "MAIN" and original_index == 0:
            if instance["adjustment_upgrades_one"]:
                expected_kind = "ADJUSTMENT_GRID_UPGRADE"
                expected = {
                    "gold_cost": 40, "upgrade_select_count": 1,
                    "selection_mode": "PLAYER_SELECT",
                }
            else:
                expected_kind = "ADJUSTMENT_RANDOM_UPGRADE"
                expected = {
                    "gold_cost": 40, "upgrade_max_count": 2,
                    "selection_mode": "RANDOM_UP_TO_AVAILABLE",
                }
            if kind != expected_kind:
                return None, None
        elif stage == "MAIN" and original_index == 1:
            if instance["clean_up_removes_cards"]:
                expected_kind = "CLEAN_UP_GRID_PURGE"
                expected = {
                    "gold_cost": 60,
                    "selection_mode": "PLAYER_SELECT",
                    "purge_select_count": 1,
                }
            else:
                expected_kind = "CLEAN_UP_GRID_TRANSFORM"
                expected = {
                    "gold_cost": 60,
                    "selection_mode": "PLAYER_SELECT",
                    "transform_select_count": 2,
                    "transform_result": "RANDOM",
                }
            if kind != expected_kind:
                return None, None
        elif key == ("MAIN", 2, "FULL_SERVICE"):
            expected = {
                "gold_cost": 90, "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                ),
            }
        elif key == ("MAIN", 3, "PUNCH_AND_LEAVE"):
            expected = {"hp_loss": 3}

    elif event_id == "knowingskull":
        if (
            set(instance) != {
                "potion_cost", "card_cost", "gold_cost", "leave_cost",
                "gold_reward",
            }
            or any(
                type(instance.get(name)) is not int
                for name in (
                    "potion_cost", "card_cost", "gold_cost", "leave_cost",
                    "gold_reward",
                )
            )
            or instance.get("leave_cost") != 6
            or instance.get("gold_reward") != 90
            or any(
                instance[name] < instance["leave_cost"]
                for name in ("potion_cost", "card_cost", "gold_cost")
            )
        ):
            return None, None
        expected = {
            ("INTRO_1", 0, "OPEN_QUESTIONS"): {},
            ("ASK", 0, "TAKE_POTION"): {
                "hp_loss": instance["potion_cost"],
                "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            },
            ("ASK", 1, "TAKE_GOLD"): {
                "hp_loss": instance["gold_cost"],
                "gold_gain": 90,
            },
            ("ASK", 2, "TAKE_CARD"): {
                "hp_loss": instance["card_cost"],
                "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            },
            ("ASK", 3, "LEAVE"): {"hp_loss": 6},
            ("COMPLETE", 0, "CONTINUE"): {},
        }.get(key)

    elif event_id == "cursedtome":
        book_ids = ("Necronomicon", "Enchiridion", "Nilry's Codex")
        relics = game.get("relics")
        if not isinstance(relics, list) or any(
            not isinstance(relic, dict) or type(relic.get("id")) is not str
            for relic in relics
        ):
            return None, None
        owned = {relic["id"] for relic in relics}
        expected_pool = [item for item in book_ids if item not in owned]
        expected_pool = expected_pool or ["Circlet"]
        if (
            set(instance) != {
                "final_hp_loss", "damage_taken", "random_relic_pool",
            }
            or instance.get("final_hp_loss") != 10
            or type(instance.get("damage_taken")) is not int
            or instance.get("random_relic_pool") != expected_pool
        ):
            return None, None
        damage_taken = instance["damage_taken"]
        expected_damage = {
            "INTRO": {0}, "PAGE_1": {0}, "PAGE_2": {1},
            "PAGE_3": {3}, "LAST_PAGE": {6}, "END": {0, 9, 16},
        }.get(stage)
        if expected_damage is None or damage_taken not in expected_damage:
            return None, None
        expected_by_key = {
            ("INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN"): {
                "future_hp_loss_to_complete": 16,
                "random_relic_count": 1,
                "reward_surface": "COMBAT_REWARD",
                "selection_mode": "UNIFORM_MISC_RNG",
            },
            ("INTRO", 1, "LEAVE"): {},
            ("PAGE_1", 0, "READ_PAGE_1"): {"hp_loss": 1},
            ("PAGE_2", 0, "READ_PAGE_2"): {"hp_loss": 2},
            ("PAGE_3", 0, "READ_PAGE_3"): {"hp_loss": 3},
            ("LAST_PAGE", 0, "COMPLETE_RANDOM_BOOK"): {
                "hp_loss": 10, "random_relic_count": 1,
                "reward_surface": "COMBAT_REWARD",
                "selection_mode": "UNIFORM_MISC_RNG",
            },
            ("LAST_PAGE", 1, "STOP"): {"hp_loss": 3},
            ("END", 0, "PROCEED"): {},
        }
        expected = expected_by_key.get(key)

    elif event_id == "deadadventurer":
        expected_keys = {
            "num_rewards", "encounter_chance_percent", "remaining_rewards",
            "enemy_index", "encounter_id",
        }
        rewards = instance.get("remaining_rewards")
        enemy_ids = ("3 Sentries", "Gremlin Nob", "Lagavulin Event")
        num_rewards = instance.get("num_rewards")
        chance = instance.get("encounter_chance_percent")
        enemy = instance.get("enemy_index")
        if (
            set(instance) != expected_keys
            or type(num_rewards) is not int or num_rewards not in {0, 1, 2, 3}
            or chance != 25 + 25 * num_rewards
            or enemy not in {0, 1, 2}
            or instance.get("encounter_id") != enemy_ids[enemy]
            or not isinstance(rewards, list)
            or any(
                type(item) is not str
                or item not in ("GOLD", "NOTHING", "RELIC")
                for item in rewards
            )
            or len(rewards) != 3 - num_rewards
            or len(rewards) != len(set(rewards))
        ):
            return None, None
        if key == ("INTRO", 0, "SEARCH") and rewards:
            reward = rewards[0]
            expected = {
                "roll_min": 0, "roll_max_inclusive": 99,
                "encounter_roll_lt": chance,
                "encounter_id": enemy_ids[enemy],
                "encounter_reward_gold_min": 25,
                "encounter_reward_gold_max": 35,
                "success_reward_kind": reward,
                "success_gold_gain": 30 if reward == "GOLD" else 0,
                "success_random_relic_count": 1 if reward == "RELIC" else 0,
                "success_relic_selection_mode": (
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                    if reward == "RELIC" else "NONE"
                ),
            }
        elif key == ("INTRO", 1, "LEAVE") and rewards:
            expected = {}
        elif key == ("FAIL", 0, "FIGHT"):
            expected = {
                "encounter_id": enemy_ids[enemy],
                "combat_reward_gold_min": 25,
                "combat_reward_gold_max": 35,
            }
        elif key in {
            ("SUCCESS", 0, "CONTINUE"),
            ("ESCAPE", 0, "CONTINUE"),
        }:
            expected = {}

    elif event_id == "scrapooze":
        if (
            set(instance) != {
                "relic_chance_percent_displayed", "damage",
                "total_damage_dealt", "screen_num",
            }
            or any(type(instance.get(name)) is not int for name in instance)
        ):
            return None, None
        chance = instance["relic_chance_percent_displayed"]
        attempts = (chance - 25) // 10 if chance >= 25 else -1
        base_damage = 3
        if (
            chance < 25 or chance > 105 or (chance - 25) % 10
            or instance["damage"] != base_damage + attempts
            or instance["total_damage_dealt"]
            != attempts * (2 * base_damage + attempts - 1) // 2
            or instance["screen_num"] not in {0, 1}
        ):
            return None, None
        if key == ("MAIN", 0, "REACH_INSIDE") and instance["screen_num"] == 0:
            expected = {
                "hp_loss": instance["damage"],
                "roll_min": 0, "roll_max_inclusive": 99,
                "success_roll_min_inclusive": 99 - chance,
                "success_random_relic_count": 1,
                "success_relic_selection_mode": (
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                ),
            }
        elif key == ("MAIN", 1, "LEAVE") and instance["screen_num"] == 0:
            expected = {}
        elif key == ("RESULT", 0, "CONTINUE") and instance["screen_num"] == 1:
            expected = {}

    elif event_id == "facetrader":
        relics = game.get("relics")
        max_hp = game.get("max_hp")
        if not isinstance(relics, list) or any(
            not isinstance(relic, dict) or type(relic.get("id")) is not str
            for relic in relics
        ) or type(max_hp) is not int:
            return None, None
        face_ids = (
            "CultistMask", "FaceOfCleric", "GremlinMask", "NlothsMask",
            "SsserpentHead",
        )
        owned = {relic["id"] for relic in relics}
        pool = [item for item in face_ids if item not in owned] or ["Circlet"]
        if not _exact_typed_contract_value(instance, {
            "gold_reward": 75,
            "damage": max(1, max_hp // 10),
            "random_face_pool": pool,
        }):
            return None, None
        expected = {
            ("INTRO", 0, "OPEN"): {},
            ("MAIN", 0, "TOUCH"): {
                "hp_loss": instance["damage"], "gold_gain": 75,
            },
            ("MAIN", 1, "TRADE"): {
                "random_relic_pool": pool,
                "random_relic_count": 1,
                "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
            },
            ("MAIN", 2, "LEAVE"): {},
            ("RESULT", 0, "CONTINUE"): {},
        }.get(key)

    elif event_id == "duplicator":
        if instance not in ({"screen_num": 0}, {"screen_num": 2}):
            return None, None
        expected = {
            ("MAIN", 0, "DUPLICATE"): {
                "duplicate_select_count": 1,
                "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
            },
            ("MAIN", 1, "LEAVE"): {},
            ("RESULT", 0, "CONTINUE"): {},
        }.get(key)
        if stage == "MAIN" and instance["screen_num"] != 0:
            return None, None
        if stage == "RESULT" and instance["screen_num"] != 2:
            return None, None

    elif event_id == "bonfireelementals":
        if instance != {"card_select": False}:
            return None, None
        expected = {
            ("INTRO", 0, "CONTINUE"): {},
            ("CHOOSE", 0, "OFFER_CARD"): {
                "offer_select_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
                ),
            },
            ("COMPLETE", 0, "CONTINUE"): {},
        }.get(key)

    if expected is None or not _exact_typed_contract_value(
        parameters, expected
    ):
        return None, None
    contract = json.loads(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ))
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:20]
    return contract, f"event-mechanism:{digest}"


def _validated_exact_a0_event_surface(target, state, event_id):
    """Bind compressed choice to its original A0 branch, never text/order."""

    game = _state_game(state)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    if (
        _normalized_game_id(target.get("event_id")) != event_id
        or _normalized_game_id(screen.get("event_id")) != event_id
    ):
        return None, "typed_event_id_binding_mismatch"
    if game.get("ascension_level") != 0:
        return None, "typed_event_requires_exact_a0"
    raw_options = list(screen.get("options") or [])
    enabled = []
    for item in raw_options:
        if not isinstance(item, dict) or type(item.get("disabled")) is not bool:
            return None, "typed_event_option_shape_invalid"
        if item["disabled"]:
            if "choice_index" in item:
                return None, "typed_event_disabled_option_has_choice_index"
            continue
        if type(item.get("choice_index")) is not int:
            return None, "typed_event_enabled_choice_index_missing"
        enabled.append(item)
    choice_index = target.get("choice_index")
    matches = [
        item for item in enabled
        if item.get("choice_index") == choice_index
    ]
    if type(choice_index) is not int or len(matches) != 1:
        return None, "typed_event_choice_index_binding_mismatch"
    raw_match = matches[0]
    originals = [item.get("original_button_index") for item in raw_options]
    if (
        not all(type(value) is int for value in originals)
        or sorted(originals) != list(range(len(raw_options)))
    ):
        return None, "typed_event_original_button_index_surface_invalid"
    if sorted(item["choice_index"] for item in enabled) != list(
        range(len(enabled))
    ):
        return None, "typed_event_compressed_choice_index_surface_invalid"
    original_index = raw_match["original_button_index"]
    if target.get("original_button_index") != original_index:
        return None, "typed_event_original_button_index_target_mismatch"

    surface_contracts = {}
    surface_mechanisms = {}
    for item in raw_options:
        item_original = item["original_button_index"]
        if event_id == "worldofgoop":
            checked, checked_mechanism = _canonical_goop_event_contract(
                item.get("event_contract"), item_original
            )
        else:
            checked, checked_mechanism = _canonical_staged_event_contract(
                item.get("event_contract"), event_id, item_original, game
            )
        if checked is None or item_original in surface_contracts:
            return None, "typed_event_contract_missing_invalid_or_mismatched"
        surface_contracts[item_original] = checked
        surface_mechanisms[item_original] = checked_mechanism

    if event_id == "worldofgoop":
        kinds = {
            item_index: item_contract.get("option_kind")
            for item_index, item_contract in surface_contracts.items()
        }
        if kinds not in (
            {0: "GATHER", 1: "LEAVE"}, {0: "CONTINUE"},
        ):
            return None, "typed_event_contract_stage_surface_mismatch"
    else:
        stages = {
            item_contract["event_stage"]
            for item_contract in surface_contracts.values()
        }
        instances = {
            json.dumps(
                item_contract["instance_parameters"], ensure_ascii=True,
                sort_keys=True, separators=(",", ":"),
            )
            for item_contract in surface_contracts.values()
        }
        if len(stages) != 1 or len(instances) != 1:
            return None, "typed_event_contract_stage_or_instance_mismatch"
        event_stage = next(iter(stages))
        expected_originals = {
            ("thecleric", "MAIN"): {0, 1, 2},
            ("thecleric", "RESULT"): {0},
            ("designer", "INTRO"): {0},
            ("designer", "MAIN"): {0, 1, 2, 3},
            ("designer", "DONE"): {0},
            ("cursedtome", "INTRO"): {0, 1},
            ("cursedtome", "PAGE_1"): {0},
            ("cursedtome", "PAGE_2"): {0},
            ("cursedtome", "PAGE_3"): {0},
            ("cursedtome", "LAST_PAGE"): {0, 1},
            ("cursedtome", "END"): {0},
            ("knowingskull", "INTRO_1"): {0},
            ("knowingskull", "ASK"): {0, 1, 2, 3},
            ("knowingskull", "COMPLETE"): {0},
            ("deadadventurer", "INTRO"): {0, 1},
            ("deadadventurer", "FAIL"): {0},
            ("deadadventurer", "SUCCESS"): {0},
            ("deadadventurer", "ESCAPE"): {0},
            ("scrapooze", "MAIN"): {0, 1},
            ("scrapooze", "RESULT"): {0},
            ("facetrader", "INTRO"): {0},
            ("facetrader", "MAIN"): {0, 1, 2},
            ("facetrader", "RESULT"): {0},
            ("duplicator", "MAIN"): {0, 1},
            ("duplicator", "RESULT"): {0},
            ("bonfireelementals", "INTRO"): {0},
            ("bonfireelementals", "CHOOSE"): {0},
            ("bonfireelementals", "COMPLETE"): {0},
        }.get((event_id, event_stage))
        if set(surface_contracts) != expected_originals:
            return None, "typed_event_contract_stage_surface_mismatch"

    event_contract = surface_contracts[original_index]
    mechanism_id = surface_mechanisms[original_index]
    target_contract = target.get("event_contract")
    target_checked = (
        _canonical_goop_event_contract(target_contract, original_index)
        if event_id == "worldofgoop" else
        _canonical_staged_event_contract(
            target_contract, event_id, original_index, game
        )
    )
    if (
        target_checked[0] != event_contract
        or target_checked[1] != mechanism_id
        or target.get("mechanism_id") != mechanism_id
    ):
        return None, "typed_event_contract_target_binding_mismatch"
    return {
        "stage": event_contract.get("event_stage"),
        "option_kind": event_contract.get("option_kind"),
        "choice_index": choice_index,
        "original_button_index": original_index,
        "event_contract": event_contract,
        "mechanism_id": mechanism_id,
    }, None


def _mark_typed_event_transition(
    consequence, *, hp, max_hp, gold, current_cost, future_costs, reason
):
    _mark_numeric_consequences(consequence, hp, max_hp, gold, reason)
    _mark_empty_inventory_consequences(consequence, reason)
    _known_consequence_field(
        consequence, "current_cost", current_cost,
        authority="production_mechanics_projection", reason=reason,
    )
    _known_consequence_field(
        consequence, "future_costs", future_costs,
        authority="production_mechanics_projection", reason=reason,
    )
    _mark_no_probability(consequence, reason)


def _apply_exact_a0_event_consequence(
    consequence, target, state, event_id
):
    """Project exact A0 event pieces and keep private fields fail-closed."""

    contract, error = _validated_exact_a0_event_surface(
        target, state, event_id
    )
    if contract is None:
        consequence["uncertainty"].append(error)
        return False
    mechanism_id = contract["mechanism_id"]
    consequence.update({
        "event_id": target.get("event_id"),
        "mechanism_id": mechanism_id,
        "original_button_index": contract["original_button_index"],
        "random_effects": [],
    })
    index = contract["original_button_index"]
    option_kind = contract.get("option_kind")
    parameters = (contract.get("event_contract") or {}).get("parameters")

    if option_kind in {
        "CONTINUE", "OPEN", "OPEN_SERVICES", "OPEN_QUESTIONS", "PROCEED",
    }:
        reason = "typed_event_single_dialog_has_no_immediate_effect"
        operation = {
            "thecleric": "cleric_dialog_advance_noop",
            "designer": "designer_dialog_advance_noop",
            "worldofgoop": "world_of_goop_dialog_advance_noop",
            "cursedtome": "cursed_tome_dialog_advance_noop",
            "knowingskull": "knowing_skull_dialog_advance_noop",
            "deadadventurer": "dead_adventurer_dialog_advance_noop",
            "scrapooze": "scrap_ooze_dialog_advance_noop",
            "facetrader": "face_trader_dialog_advance_noop",
            "duplicator": "duplicator_dialog_advance_noop",
            "bonfireelementals": "bonfire_dialog_advance_noop",
        }[event_id]
        _mark_typed_event_transition(
            consequence,
            hp=0, max_hp=0, gold=0,
            current_cost={"gold": 0, "hp": 0, "max_hp": 0},
            future_costs=[], reason=reason,
        )
        consequence.update({
            "operation": operation,
            "event_outcome_id": "dialog_advance",
        })
        return True

    game = _state_game(state)

    def mark_typed_random_fields(
        *, hp, gold_domain, relic_domain, current_cost, future_costs,
        outcomes, reason
    ):
        _known_consequence_field(
            consequence, "hp_delta", hp,
            authority="production_mechanics_projection", reason=reason,
        )
        _known_consequence_field(
            consequence, "max_hp_delta", 0,
            authority="production_mechanics_projection", reason=reason,
        )
        if len(gold_domain) == 1:
            _known_consequence_field(
                consequence, "gold_delta", gold_domain[0],
                authority="production_mechanics_projection", reason=reason,
            )
        else:
            _known_domain_consequence_field(
                consequence, "gold_delta", gold_domain,
                reason + "_gold_domain",
            )
        for field, value in (
            ("card_changes", {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
            }),
            ("potion_changes", {"gain": [], "remove": [], "replace": []}),
            ("curse", {
                "gain": [], "remove": [], "probability": 0.0,
                "omamori_applicable": False,
                "omamori_charges_consumed": 0,
            }),
        ):
            _known_consequence_field(
                consequence, field, value,
                authority="production_mechanics_projection", reason=reason,
            )
        if isinstance(relic_domain, list) and len(relic_domain) == 1:
            _known_consequence_field(
                consequence, "relic_changes", relic_domain[0],
                authority="production_mechanics_projection", reason=reason,
            )
        else:
            _known_domain_consequence_field(
                consequence, "relic_changes", relic_domain,
                reason + "_relic_domain",
            )
        _known_consequence_field(
            consequence, "current_cost", current_cost,
            authority="production_mechanics_projection", reason=reason,
        )
        _known_consequence_field(
            consequence, "future_costs", future_costs,
            authority="production_mechanics_projection", reason=reason,
        )
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", outcomes,
            reason + "_exhaustive_outcomes",
        )
        consequence["uncertainty_classification"] = {
            "status": "exhaustive_probability",
            "authority": "typed_event_contract",
            "reason": reason + " exhaustive bytecode branch partition",
        }

    if event_id == "thecleric":
        reason = "typed_base_game_the_cleric_a0_branch"
        if option_kind == "HEAL":
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                consequence["uncertainty"].append(
                    "cleric_hp_state_missing"
                )
                return False
            heal = min(
                max(0, max_hp - current_hp), parameters["heal_amount"]
            )
            gold_cost = parameters["gold_cost"]
            _mark_typed_event_transition(
                consequence,
                hp=heal, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "cleric_heal",
                "event_outcome_id": "heal",
            })
        elif option_kind == "PURIFY":
            gold_cost = parameters["gold_cost_if_purgeable"]
            future = [{
                "kind": "cleric_grid_selection",
                "operation": "grid_purge",
                "select_count": parameters["purge_select_count"],
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            }]
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=future, reason=reason,
            )
            consequence.update({
                "operation": "cleric_open_purge_grid",
                "event_outcome_id": "purify",
                "uncertainty": [
                    "exact removed card UUID binds on the following GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "Cleric purge commits after GRID confirmation",
                },
            })
        elif option_kind == "LEAVE":
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "cleric_leave",
                "event_outcome_id": "leave",
                "leave": True,
            })
        else:
            consequence["uncertainty"].append(
                "typed_cleric_option_kind_unhandled"
            )
            return False
        return True

    if event_id == "designer":
        reason = "typed_base_game_designer_a0_branch"
        if option_kind == "ADJUSTMENT_GRID_UPGRADE":
            gold_cost = parameters["gold_cost"]
            future = [{
                "kind": "designer_grid_selection",
                "operation": "grid_upgrade",
                "select_count": parameters["upgrade_select_count"],
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            }]
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=future, reason=reason,
            )
            consequence.update({
                "operation": "designer_adjustment_grid_upgrade",
                "event_outcome_id": "adjustment_grid_upgrade",
                "uncertainty": [
                    "exact upgraded card UUID binds on the following GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "typed Designer GRID upgrade contract",
                },
            })
            return True
        if option_kind == "ADJUSTMENT_RANDOM_UPGRADE":
            gold_cost = parameters["gold_cost"]
            count = parameters["upgrade_max_count"]
            future = [{
                "kind": "designer_random_card_upgrade",
                "operation": "random_upgrade",
                "max_count": count,
                "count_semantics": "up_to_available",
                "domain": "current_upgradable_deck",
                "selection_mode": "random",
                "commit_timing": "after_event_choice",
            }]
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=future, reason=reason,
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "designer_random_upgrade_identity_domain",
            )
            consequence.update({
                "operation": "designer_adjustment_random_upgrade",
                "event_outcome_id": "adjustment_random_upgrade",
                "random_effects": [{
                    "kind": "random_card_upgrade",
                    "max_count": count,
                    "count_semantics": "up_to_available",
                    "domain": "current_upgradable_deck",
                    "selection_mode": "random",
                }],
                "uncertainty": [
                    "exact upgraded UUIDs settle after the event choice"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "typed random-up-to-available upgrade domain",
                },
            })
            return True
        if option_kind in {
            "CLEAN_UP_GRID_PURGE", "CLEAN_UP_GRID_TRANSFORM",
        }:
            gold_cost = parameters["gold_cost"]
            transform = option_kind == "CLEAN_UP_GRID_TRANSFORM"
            count = parameters[
                "transform_select_count" if transform else
                "purge_select_count"
            ]
            future = [{
                "kind": "designer_grid_selection",
                "operation": "grid_transform" if transform else "grid_purge",
                "select_count": count,
                "identity_binding": (
                    "subsequent_grid_card_instance_ids"
                    if transform else "subsequent_grid_card_instance_id"
                ),
                "commit_timing": "after_grid_confirmation",
            }]
            if transform:
                future[0]["transform_result"] = "random"
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=future, reason=reason,
            )
            if transform:
                _known_domain_consequence_field(
                    consequence, "probabilistic_outcomes", [],
                    "designer_random_transform_result_domain",
                )
            consequence.update({
                "operation": (
                    "designer_clean_up_grid_transform"
                    if transform else "designer_clean_up_grid_purge"
                ),
                "event_outcome_id": (
                    "clean_up_grid_transform"
                    if transform else "clean_up_grid_purge"
                ),
                "random_effects": ([{
                    "kind": "random_card_transform_results",
                    "count": count,
                    "domain": "base_game_card_pool",
                    "selection_mode": "random",
                }] if transform else []),
                "uncertainty": [
                    "exact selected UUIDs and results settle on GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "typed Designer cleanup GRID contract",
                },
            })
            return True
        if option_kind == "FULL_SERVICE":
            gold_cost = parameters["gold_cost"]
            random_count = parameters["random_upgrade_max_count"]
            future = [
                {
                    "kind": "designer_grid_selection",
                    "operation": "grid_purge",
                    "select_count": parameters["purge_select_count"],
                    "identity_binding": "subsequent_grid_card_instance_id",
                    "commit_timing": "after_grid_confirmation",
                },
                {
                    "kind": "designer_random_card_upgrade",
                    "operation": "random_upgrade",
                    "max_count": random_count,
                    "count_semantics": "up_to_available",
                    "domain": "post_purge_upgradable_deck",
                    "selection_mode": "random",
                    "commit_timing": "after_grid_confirmation",
                },
            ]
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_cost,
                current_cost={
                    "gold": gold_cost, "hp": 0, "max_hp": 0,
                },
                future_costs=future, reason=reason,
            )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "designer_random_upgrade_identity_domain",
            )
            consequence.update({
                "operation": "designer_full_service",
                "event_outcome_id": "full_service",
                "random_effects": [{
                    "kind": "random_card_upgrade",
                    "max_count": random_count,
                    "count_semantics": "up_to_available",
                    "domain": "post_purge_upgradable_deck",
                    "selection_mode": "random",
                }],
                "uncertainty": [
                    "exact card UUIDs bind on the following GRID/settlement"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "Full Service purge and random-upgrade domain are typed",
                },
            })
            return True
        if option_kind == "PUNCH_AND_LEAVE":
            hp_loss = parameters["hp_loss"]
            _mark_typed_event_transition(
                consequence,
                hp=-hp_loss, max_hp=0, gold=0,
                current_cost={
                    "gold": 0, "hp": hp_loss, "max_hp": 0,
                },
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "designer_punch_and_leave",
                "event_outcome_id": "punch_and_leave",
                "leave": True,
            })
            return True
        consequence["uncertainty"].append(
            "typed_designer_option_kind_unhandled"
        )
        return False

    if event_id == "knowingskull":
        reason = "typed_base_game_knowing_skull_a0_branch"
        relics = game.get("relics")
        potions = game.get("potions")
        if not isinstance(relics, list) or any(
            not isinstance(relic, dict)
            or not isinstance(relic.get("id") or relic.get("name"), str)
            for relic in relics
        ):
            consequence["uncertainty"].append(
                "knowing_skull_relic_state_missing"
            )
            return False
        relic_ids = {
            _normalized_game_id(relic.get("id") or relic.get("name"))
            for relic in relics
        }
        reduction = 1 if "tungstenrod" in relic_ids else 0

        def realized_hp_loss(raw):
            return max(0, int(raw) - reduction)

        leave_loss = realized_hp_loss(
            contract["event_contract"]["instance_parameters"]["leave_cost"]
        )
        raw_hp_loss = parameters.get("hp_loss")
        if type(raw_hp_loss) is not int:
            consequence["uncertainty"].append(
                "knowing_skull_hp_loss_missing"
            )
            return False
        hp_loss = realized_hp_loss(raw_hp_loss)
        future_costs = [] if option_kind == "LEAVE" else [{
            "kind": "knowing_skull_exit_reserve",
            "reserved_hp_loss": leave_loss,
            "commit_timing": "when_leaving_event",
        }]
        gold_delta = (
            parameters["gold_gain"]
            if option_kind == "TAKE_GOLD" and "ectoplasm" not in relic_ids
            else 0
        )
        _mark_typed_event_transition(
            consequence,
            hp=-hp_loss, max_hp=0, gold=gold_delta,
            current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
            future_costs=future_costs, reason=reason,
        )
        consequence["nominal_hp_loss"] = raw_hp_loss
        if option_kind == "TAKE_POTION":
            if not isinstance(potions, list):
                consequence["uncertainty"].append(
                    "knowing_skull_potion_slots_missing"
                )
                return False
            obtainable = (
                "sozu" not in relic_ids
                and any(
                    isinstance(potion, dict)
                    and _normalized_game_id(potion.get("id")) == "potionslot"
                    for potion in potions
                )
            )
            random_effect = {
                "kind": "random_potion_gain",
                "count": parameters["reward_count"],
                "domain": "base_game_potion_pool",
                "selection_mode": "random",
            }
            if obtainable:
                _known_domain_consequence_field(
                    consequence, "potion_changes",
                    {"gain": [random_effect], "remove": [], "replace": []},
                    "knowing_skull_random_potion_domain",
                )
            consequence.update({
                "operation": "knowing_skull_take_potion",
                "event_outcome_id": "take_potion",
                "potion_reward_obtainable": obtainable,
                "random_effects": [random_effect],
                "uncertainty": [
                    "random potion identity is bounded by the base-game pool"
                ],
                "uncertainty_classification": {
                    "status": "classified_random_domain",
                    "authority": "typed_event_contract",
                    "reason": "Knowing Skull random potion domain is authoritative",
                },
            })
            return True
        if option_kind == "TAKE_GOLD":
            consequence.update({
                "operation": "knowing_skull_take_gold",
                "event_outcome_id": "take_gold",
                "nominal_gold_gain": parameters["gold_gain"],
            })
            return True
        if option_kind == "TAKE_CARD":
            random_effect = {
                "kind": "random_card_gain",
                "count": parameters["reward_count"],
                "domain": "base_game_colorless_uncommon_pool",
                "selection_mode": parameters["selection_mode"].casefold(),
            }
            _known_domain_consequence_field(
                consequence, "card_changes",
                {"gain": [random_effect], "remove": [], "upgrade": [],
                 "transform": []},
                "knowing_skull_random_card_domain",
            )
            consequence.update({
                "operation": "knowing_skull_take_card",
                "event_outcome_id": "take_card",
                "random_effects": [random_effect],
                "uncertainty": [
                    "random card identity is bounded by the colorless uncommon pool"
                ],
                "uncertainty_classification": {
                    "status": "classified_random_domain",
                    "authority": "typed_event_contract",
                    "reason": "Knowing Skull random card domain is authoritative",
                },
            })
            return True
        if option_kind == "LEAVE":
            consequence.update({
                "operation": "knowing_skull_leave",
                "event_outcome_id": "leave",
                "leave": True,
            })
            return True
        consequence["uncertainty"].append(
            "typed_knowing_skull_option_kind_unhandled"
        )
        return False

    if event_id == "deadadventurer":
        reason = "typed_base_game_dead_adventurer_a0_branch"
        instance = contract["event_contract"]["instance_parameters"]
        if option_kind == "SEARCH":
            chance = parameters["encounter_roll_lt"] / 100.0
            reward_kind = parameters["success_reward_kind"]
            success_gold = parameters["success_gold_gain"]
            random_relic = {
                "kind": "random_relic_gain",
                "count": parameters["success_random_relic_count"],
                "domain": "base_game_non_boss_relic_tiers",
                "selection_mode": "random_tier_then_screenless_relic",
            }
            outcomes = [
                {
                    "outcome_id": "encounter", "probability": chance,
                    "gold_delta": 0,
                    "future_combat": parameters["encounter_id"],
                    "combat_reward_gold_domain": [25, 35],
                },
                {
                    "outcome_id": "success", "probability": 1.0 - chance,
                    "reward_kind": reward_kind,
                    "gold_delta": success_gold,
                    "random_relic_count": parameters[
                        "success_random_relic_count"
                    ],
                },
            ]
            relic_domain = [{
                "gain": [], "remove": [], "counter": [],
            }]
            if reward_kind == "RELIC":
                relic_domain.append({
                    "gain": [random_relic], "remove": [], "counter": [],
                })
            mark_typed_random_fields(
                hp=0, gold_domain=sorted({0, success_gold}),
                relic_domain=relic_domain,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[{
                    "kind": "dead_adventurer_possible_combat",
                    "encounter_id": parameters["encounter_id"],
                    "probability": chance,
                }], outcomes=outcomes, reason=reason,
            )
            consequence.update({
                "operation": "dead_adventurer_search",
                "event_outcome_id": "search_random_branch",
                "random_effects": [random_relic]
                if reward_kind == "RELIC" else [],
                "uncertainty": [
                    "encounter/success branch is exhaustive and the shuffled success reward is bound"
                ],
            })
            return True
        if option_kind == "FIGHT":
            future = [{
                "kind": "dead_adventurer_combat",
                "encounter_id": parameters["encounter_id"],
                "combat_reward_gold_domain": [
                    parameters["combat_reward_gold_min"],
                    parameters["combat_reward_gold_max"],
                ],
                "preserved_success_rewards": instance["remaining_rewards"],
            }]
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=future, reason=reason,
            )
            consequence.update({
                "operation": "dead_adventurer_enter_combat",
                "event_outcome_id": "fight",
                "uncertainty": [
                    "combat outcome and 25-35 gold roll settle after entering combat"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "typed_event_contract",
                    "reason": "Dead Adventurer encounter and reward domain are bound",
                },
            })
            return True
        if option_kind == "LEAVE":
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "dead_adventurer_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True
        return False

    if event_id == "scrapooze":
        reason = "typed_base_game_scrap_ooze_a0_branch"
        if option_kind == "REACH_INSIDE":
            hp_loss = parameters["hp_loss"]
            success_probability = (
                100 - parameters["success_roll_min_inclusive"]
            ) / 100.0
            random_relic = {
                "kind": "random_relic_gain", "count": 1,
                "domain": "base_game_non_boss_relic_tiers",
                "selection_mode": "random_tier_then_screenless_relic",
            }
            outcomes = [
                {
                    "outcome_id": "success",
                    "probability": success_probability,
                    "hp_delta": -hp_loss, "random_relic_count": 1,
                },
                {
                    "outcome_id": "failure",
                    "probability": 1.0 - success_probability,
                    "hp_delta": -hp_loss, "next_damage": hp_loss + 1,
                },
            ]
            mark_typed_random_fields(
                hp=-hp_loss, gold_domain=[0],
                relic_domain=[
                    {"gain": [], "remove": [], "counter": []},
                    {"gain": [random_relic], "remove": [], "counter": []},
                ],
                current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
                future_costs=[], outcomes=outcomes, reason=reason,
            )
            consequence.update({
                "operation": "scrap_ooze_reach_inside",
                "event_outcome_id": "reach_random_branch",
                "random_effects": [random_relic],
                "uncertainty": [
                    "success/failure roll is exhaustive; relic identity remains in the base-game non-boss domain"
                ],
            })
            return True
        if option_kind == "LEAVE":
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "scrap_ooze_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True
        return False

    if event_id == "facetrader":
        reason = "typed_base_game_face_trader_a0_branch"
        if option_kind == "TOUCH":
            hp_loss = parameters["hp_loss"]
            _mark_typed_event_transition(
                consequence, hp=-hp_loss, max_hp=0,
                gold=parameters["gold_gain"],
                current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "face_trader_touch",
                "event_outcome_id": "touch",
            })
            return True
        if option_kind == "TRADE":
            pool = list(parameters["random_relic_pool"])
            probability = 1.0 / len(pool)
            outcomes = [
                {
                    "outcome_id": "gain_face_relic",
                    "probability": probability, "relic_id": relic_id,
                }
                for relic_id in pool
            ]
            relic_domain = [
                {"gain": [{"id": relic_id}], "remove": [], "counter": []}
                for relic_id in pool
            ]
            mark_typed_random_fields(
                hp=0, gold_domain=[0], relic_domain=relic_domain,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], outcomes=outcomes, reason=reason,
            )
            consequence.update({
                "operation": "face_trader_trade",
                "event_outcome_id": "trade_random_face",
                "random_effects": [{
                    "kind": "random_relic_gain", "count": 1,
                    "domain": pool, "selection_mode": "uniform_shuffle_first",
                }],
                "uncertainty": [
                    "the random face relic is uniformly selected from the reflected unowned pool"
                ],
            })
            return True
        if option_kind == "LEAVE":
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "face_trader_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True
        return False

    if event_id == "duplicator":
        reason = "typed_base_game_duplicator_a0_branch"
        if option_kind == "DUPLICATE":
            future = [{
                "kind": "duplicator_grid_selection",
                "operation": "grid_duplicate",
                "select_count": parameters["duplicate_select_count"],
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            }]
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=future, reason=reason,
            )
            consequence.update({
                "operation": "duplicator_open_duplicate_grid",
                "event_outcome_id": "duplicate",
                "uncertainty": [
                    "exact duplicated card UUID binds on the following GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "Duplicator commits after GRID confirmation",
                },
            })
            return True
        if option_kind == "LEAVE":
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "duplicator_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True
        return False

    if event_id == "bonfireelementals":
        reason = "typed_base_game_bonfire_a0_branch"
        if option_kind == "OFFER_CARD":
            future = [{
                "kind": "bonfire_grid_selection",
                "operation": "grid_offer_card",
                "select_count": parameters["offer_select_count"],
                "domain": "current_purgeable_unbottled_deck",
                "identity_binding": "subsequent_grid_card_instance_id",
                "reward_rule": "base_game_card_rarity",
                "commit_timing": "after_grid_confirmation",
            }]
            _mark_typed_event_transition(
                consequence, hp=0, max_hp=0, gold=0,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=future, reason=reason,
            )
            consequence.update({
                "operation": "bonfire_open_offer_grid",
                "event_outcome_id": "offer_card",
                "uncertainty": [
                    "offered card identity and rarity reward settle on GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": (
                        "Bonfire removal and rarity reward commit after GRID"
                    ),
                },
            })
            return True
        return False

    if event_id == "worldofgoop":
        reason = "typed_base_game_world_of_goop_a0_branch"
        current_gold = game.get("gold")
        if type(current_gold) is not int or current_gold < 0:
            consequence["uncertainty"].append(
                "world_of_goop_current_gold_missing"
            )
            return False
        event_contract = contract.get("event_contract")
        parameters = (
            event_contract.get("parameters")
            if isinstance(event_contract, dict) else None
        )
        option_kind = (
            event_contract.get("option_kind")
            if isinstance(event_contract, dict) else None
        )
        if index == 0 and option_kind == "GATHER":
            relic_ids = {
                _normalized_game_id(relic.get("id") or relic.get("name"))
                for relic in game.get("relics") or []
                if isinstance(relic, dict)
            }
            raw_damage = parameters["hp_damage"]
            damage = max(
                0, raw_damage - (1 if "tungstenrod" in relic_ids else 0)
            )
            _mark_typed_event_transition(
                consequence,
                hp=-damage, max_hp=0, gold=parameters["gold_gain"],
                current_cost={"gold": 0, "hp": damage, "max_hp": 0},
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "world_of_goop_gather_gold",
                "event_outcome_id": "gather_gold",
            })
            return True
        if index == 1 and option_kind == "LEAVE":
            gold_loss = parameters["gold_loss"]
            valid_loss = (
                gold_loss == current_gold
                if current_gold < 20 else
                20 <= gold_loss <= min(50, current_gold)
            )
            if not valid_loss:
                consequence["uncertainty"].append(
                    "world_of_goop_gold_loss_out_of_domain"
                )
                return False
            _mark_typed_event_transition(
                consequence,
                hp=0, max_hp=0, gold=-gold_loss,
                current_cost={
                    "gold": gold_loss, "hp": 0, "max_hp": 0,
                },
                future_costs=[], reason=reason,
            )
            consequence.update({
                "operation": "world_of_goop_leave",
                "event_outcome_id": "leave_gold",
                "leave": True,
            })
            return True
        consequence["uncertainty"].append(
            "world_of_goop_event_contract_kind_mismatch"
        )
        return False

    reason = "typed_base_game_cursed_tome_a0_branch"
    event_contract = contract["event_contract"]
    relic_pool = list(
        event_contract["instance_parameters"]["random_relic_pool"]
    )
    if option_kind == "ENTER_RANDOM_BOOK_CHAIN":
        future = [{
            "kind": "cursed_tome_reading_chain",
            "total_hp_loss_to_complete": parameters[
                "future_hp_loss_to_complete"
            ],
            "reward_surface": parameters["reward_surface"],
            "random_relic_count": parameters["random_relic_count"],
        }]
        _mark_typed_event_transition(
            consequence,
            hp=0, max_hp=0, gold=0,
            current_cost={"gold": 0, "hp": 0, "max_hp": 0},
            future_costs=future, reason=reason,
        )
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", [],
            "cursed_tome_random_relic_pool",
        )
        consequence.update({
            "operation": "cursed_tome_enter_random_book_chain",
            "event_outcome_id": "enter_random_book_chain",
            "random_effects": [{
                "kind": "random_relic_gain", "domain": relic_pool,
                "count": parameters["random_relic_count"],
                "selection_mode": "random",
            }],
            "uncertainty": [
                "random book identity is bounded by the typed relic pool"
            ],
            "reason_codes": [
                "unsupported_necronomicon_activation_state"
            ],
            "uncertainty_classification": {
                "status": "exhaustive_domain",
                "authority": "typed_event_contract",
                "reason": "Cursed Tome random relic pool is authoritative",
            },
        })
        return True
    if option_kind == "LEAVE":
        _mark_typed_event_transition(
            consequence,
            hp=0, max_hp=0, gold=0,
            current_cost={"gold": 0, "hp": 0, "max_hp": 0},
            future_costs=[], reason=reason,
        )
        consequence.update({
            "operation": "cursed_tome_leave",
            "event_outcome_id": "leave",
            "leave": True,
        })
        return True
    if option_kind in {
        "READ_PAGE_1", "READ_PAGE_2", "READ_PAGE_3", "STOP",
    }:
        hp_loss = parameters["hp_loss"]
        _mark_typed_event_transition(
            consequence,
            hp=-hp_loss, max_hp=0, gold=0,
            current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
            future_costs=[], reason=reason,
        )
        consequence.update({
            "operation": (
                "cursed_tome_stop" if option_kind == "STOP" else
                f"cursed_tome_{option_kind.casefold()}"
            ),
            "event_outcome_id": option_kind.casefold(),
        })
        if option_kind == "STOP":
            consequence["leave"] = True
        return True
    if option_kind == "COMPLETE_RANDOM_BOOK":
        hp_loss = parameters["hp_loss"]
        future = [{
            "kind": "cursed_tome_random_relic_reward",
            "reward_surface": parameters["reward_surface"],
            "random_relic_count": parameters["random_relic_count"],
        }]
        _mark_typed_event_transition(
            consequence,
            hp=-hp_loss, max_hp=0, gold=0,
            current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
            future_costs=future, reason=reason,
        )
        _known_domain_consequence_field(
            consequence, "probabilistic_outcomes", [],
            "cursed_tome_random_relic_pool",
        )
        consequence.update({
            "operation": "cursed_tome_complete_random_book",
            "event_outcome_id": "complete_random_book",
            "random_effects": [{
                "kind": "random_relic_gain", "domain": relic_pool,
                "count": parameters["random_relic_count"],
                "selection_mode": "random",
            }],
            "uncertainty": [
                "random book identity is bounded by the typed relic pool"
            ],
            "uncertainty_classification": {
                "status": "exhaustive_domain",
                "authority": "typed_event_contract",
                "reason": "Cursed Tome random relic pool is authoritative",
            },
        })
        return True
    consequence["uncertainty"].append(
        "typed_cursed_tome_option_kind_unhandled"
    )
    return False


def _healing_potion_delta(game, potion_id):
    """Return exact immediate HP/max-HP deltas for a healing potion use."""

    if not isinstance(game, dict):
        return None
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return (
            (5, 5)
            if _normalized_game_id(potion_id) == "fruitjuice"
            else None
        )
    relic_ids = {
        _normalized_game_id(relic.get("id") or relic.get("name"))
        for relic in game.get("relics") or []
        if isinstance(relic, dict)
    }
    multiplier = 2 if "sacredbark" in relic_ids else 1
    healing_blocked = "markofthebloom" in relic_ids
    magic_flower = "magicflower" in relic_ids
    toy = "toyornithopter" in relic_ids
    hp = max(0, current_hp)
    cap = max(hp, max_hp)
    original_hp, original_cap = hp, cap

    def heal(amount):
        nonlocal hp
        if healing_blocked:
            return
        amount = max(0, int(amount))
        if magic_flower:
            amount = (amount * 3 + 1) // 2
        hp = min(cap, hp + amount)

    potion_id = _normalized_game_id(potion_id)
    if potion_id == "bloodpotion":
        heal(int(cap * 0.20 * multiplier))
    elif potion_id == "fruitjuice":
        gain = 5 * multiplier
        cap += gain
        heal(gain)
    else:
        return None
    if toy:
        heal(5)
    return hp - original_hp, cap - original_cap


def _gold_reward_hp_delta(game, gold_gain):
    """Return the exact immediate Bloody Idol heal for a gold reward."""

    if type(gold_gain) is not int or gold_gain <= 0:
        return 0
    relic_ids = {
        _normalized_game_id(relic.get("id") or relic.get("name"))
        for relic in game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "bloodyidol" not in relic_ids or "markofthebloom" in relic_ids:
        return 0
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return 0
    return max(0, min(5, max_hp - current_hp))


def _structured_option_consequence(
    phase, target, raw_text, candidate=None, *, state=None
):
    """Normalize only consequences independently visible in the protocol.

    Producer candidate facts remain in ``decision.candidates`` and are later
    persisted as ``producer_candidates``.  They are deliberately not embedded
    as a supposedly independent ``producer_evidence`` blob here: doing that
    makes the independent oracle either trust the producer or mark every real
    candidate unknown.  This projection is recomputable from ``target`` and
    ``raw_text`` alone.
    """

    target = target if isinstance(target, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    consequence = _empty_consequence(raw_text)
    # Candidate facts are intentionally unused in this trust boundary.  Keep
    # the argument for API compatibility with callers and to make that choice
    # explicit to reviewers.
    del candidate

    kind = str(target.get("kind") or "").casefold()
    card = target.get("card") if isinstance(target.get("card"), dict) else {}
    relic = target.get("relic") if isinstance(target.get("relic"), dict) else {}
    item = target.get("item") if isinstance(target.get("item"), dict) else {}
    phase = str(phase or "").upper()
    state_options = (state or {}).get("options") or []
    if not state_options:
        state_options = (
            (_state_game(state or {}).get("screen_state") or {}).get(
                "options"
            ) or []
        )

    if kind == "protocol_action":
        action = str(target.get("action") or "").casefold()
        game = _state_game(state or {})
        boss_act_transition = (
            phase == "COMBAT_REWARD"
            and action == "proceed"
            and str(game.get("room_type") or "") == "TreasureRoomBoss"
            and game.get("act") in {1, 2}
            and type(game.get("current_hp")) is int
            and type(game.get("max_hp")) is int
        )
        if boss_act_transition:
            reason = "automatic_post_boss_act_transition_heal"
            hp_delta = (
                0 if _state_has_relic(state or {}, "MarkOfTheBloom")
                else max(0, game["max_hp"] - game["current_hp"])
            )
            _mark_numeric_consequences(
                consequence, hp_delta, 0, 0, reason
            )
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
        elif phase == "HAND_SELECT" and action in {"proceed", "return"}:
            reason = "hand_select_confirmation_settles_previously_queued_effects"
            for field, value in (
                ("hp_delta", None), ("max_hp_delta", None),
                ("gold_delta", None),
                (
                    "card_changes",
                    {"gain": [], "remove": [], "upgrade": [], "transform": []},
                ),
                (
                    "relic_changes",
                    {"gain": [], "remove": [], "counter": []},
                ),
                (
                    "potion_changes",
                    {"gain": [], "remove": [], "replace": []},
                ),
                (
                    "curse",
                    {
                        "gain": [], "remove": [], "probability": None,
                        "omamori_applicable": None,
                        "omamori_charges_consumed": None,
                    },
                ),
                ("probabilistic_outcomes", []),
            ):
                _not_applicable_consequence_field(
                    consequence, field, value,
                    authority="protocol_queued_action_boundary",
                    reason=reason,
                )
            _mark_deterministic_costs(consequence, reason=reason)
            consequence["uncertainty"] = [
                "state changes on confirmation belong to the queued combat action"
            ]
            consequence["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "protocol_queued_action_boundary",
                "reason": reason,
            }
        else:
            reason = "protocol_action_has_no_immediate_inventory_or_resource_effect"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
        if phase in {"BOSS_REWARD", "CARD_REWARD", "COMBAT_REWARD", "CHEST"}:
            foregone = _visible_option_ids(state or {})
            _known_consequence_field(
                consequence, "future_costs",
                [{"kind": "foregone_visible_option_ids", "choice_ids": foregone}],
                authority="protocol_visible_choice_surface",
                reason="return_or_proceed_forfeits_visible_reward_choices",
            )
        if action in {"proceed", "return"}:
            consequence["operation"] = (
                "hand_select_confirm_queued_action"
                if phase == "HAND_SELECT" else action
            )
    elif kind in {"map_node", "map_boss"}:
        reason = "typed_map_entry_and_room_relic_effects"
        entry_heal = _map_entry_hp_delta(state or {}, target)
        entry_gold = _map_entry_gold_delta(state or {}, target)
        if entry_heal is not None:
            _known_consequence_field(
                consequence, "hp_delta", entry_heal,
                authority="production_mechanics_projection", reason=reason,
            )
            _known_consequence_field(
                consequence, "max_hp_delta", 0,
                authority="production_mechanics_projection", reason=reason,
            )
        if entry_gold is not None:
            _known_consequence_field(
                consequence, "gold_delta", entry_gold,
                authority="production_mechanics_projection", reason=reason,
            )
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        if kind == "map_node":
            consequence["route"] = {
                key: target.get(key) for key in ("symbol", "x", "y")
            }
        else:
            consequence["route"] = {"boss": True, "act": target.get("act")}
        consequence["uncertainty"].append(
            "future room outcomes remain probabilistic; entrance is exact"
        )
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "protocol_map_coordinate",
            "reason": "route entrance is deterministic and future rooms are out of immediate scope",
        }
    elif phase == "REST" and kind == "rest":
        rest_option = str(target.get("rest_option") or "").upper()
        reason = f"typed_campfire_{rest_option.casefold()}"
        game = _state_game(state or {})
        if rest_option == "REST":
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if all(type(value) is int for value in (current_hp, max_hp)):
                heal = max_hp * 3 // 10
                if _state_has_relic(state, "Mark of the Bloom"):
                    heal = 0
                elif _state_has_relic(state, "RegalPillow"):
                    heal += 15
                heal = max(0, min(max_hp - current_hp, heal))
                _mark_numeric_consequences(consequence, heal, 0, 0, reason)
                _mark_empty_inventory_consequences(consequence, reason)
                _mark_deterministic_costs(consequence, reason=reason)
                _mark_no_probability(consequence, reason)
                consequence["operation"] = "campfire_rest"
        elif rest_option in {"SMITH", "TOKE"}:
            operation = "upgrade" if rest_option == "SMITH" else "remove"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            _known_consequence_field(
                consequence, "future_costs",
                [{
                    "kind": "campfire_grid_selection",
                    "operation": operation,
                    "select_count": 1,
                    "timing": "after_campfire_choice",
                }],
                authority="protocol_multistage_operation", reason=reason,
            )
            consequence["operation"] = (
                "campfire_smith" if rest_option == "SMITH" else "campfire_toke"
            )
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "exact card is selected on the following GRID surface",
            }
        elif rest_option == "RECALL":
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            consequence["operation"] = "campfire_recall"
            consequence["key_changes"] = {"gain": ["ruby_key"]}
        elif rest_option == "DIG":
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            random_relic = {
                "kind": "random_relic_reward",
                "domain": "base_game_non_boss_relic_pool",
                "count": 1,
                "selection_mode": "random",
                "timing": "following_combat_reward_surface",
            }
            _known_consequence_field(
                consequence, "future_costs", [random_relic],
                authority="base_game_relic_mechanics", reason=reason,
            )
            consequence.update({
                "operation": "campfire_dig",
                "random_effects": [random_relic],
                "uncertainty": [
                    "random relic identity is exposed on the following reward surface"
                ],
                "uncertainty_classification": {
                    "status": "classified_random_domain",
                    "authority": "base_game_relic_mechanics",
                    "reason": "Shovel opens one typed random non-boss relic reward",
                },
            })
        elif rest_option == "LIFT":
            girya = [
                relic for relic in game.get("relics") or []
                if isinstance(relic, dict)
                and _normalized_game_id(relic.get("id") or relic.get("name"))
                == "girya"
            ]
            if len(girya) == 1 and type(girya[0].get("counter")) is int:
                _mark_numeric_consequences(consequence, 0, 0, 0, reason)
                _mark_empty_inventory_consequences(consequence, reason)
                _known_consequence_field(
                    consequence, "relic_changes", {
                        "gain": [], "remove": [], "counter": [{
                            "id": girya[0].get("id") or "Girya",
                            "delta": 1,
                        }],
                    }, authority="authoritative_relic_counter", reason=reason,
                )
                _mark_deterministic_costs(consequence, reason=reason)
                _mark_no_probability(consequence, reason)
                consequence["operation"] = "campfire_lift"
        consequence["campfire_option"] = rest_option
    elif phase == "CHEST" and kind == "chest":
        reason = "typed_chest_open_transition"
        game = _state_game(state or {})
        relics = [
            relic for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        ]
        relic_ids = {
            _normalized_game_id(relic.get("id") or relic.get("name"))
            for relic in relics
        }
        cursed_key = "cursedkey" in relic_ids
        omamori = next((
            relic for relic in relics
            if _normalized_game_id(relic.get("id") or relic.get("name"))
            == "omamori"
        ), None)
        charges = omamori.get("counter") if omamori is not None else 0
        if type(charges) is not int or charges < 0:
            consequence["uncertainty"].append(
                "cursed_key_omamori_counter_missing"
            )
            return consequence
        consumed = 1 if cursed_key and charges > 0 else 0
        gained = 1 if cursed_key and not consumed else 0
        darkstone_gain = (
            6 if gained and "darkstoneperiapt" in relic_ids else 0
        )
        _mark_numeric_consequences(
            consequence, darkstone_gain, darkstone_gain, 0, reason
        )
        _mark_empty_inventory_consequences(consequence, reason)
        if cursed_key:
            random_curse = {
                "kind": "random_curse_gain",
                "domain": "base_game_curse_pool",
                "count": gained,
                "selection_mode": "random",
                "timing": "on_chest_open",
            }
            _known_domain_consequence_field(
                consequence, "card_changes", {
                    "gain": [], "random_gain": (
                        [random_curse] if gained else []
                    ),
                    "remove": [], "upgrade": [], "transform": [],
                }, "cursed_key_random_curse_identity_domain",
            )
            _known_domain_consequence_field(
                consequence, "curse", {
                    "gain": [], "random_gain": (
                        [random_curse] if gained else []
                    ),
                    "remove": [], "probability": 1.0,
                    "omamori_applicable": True,
                    "omamori_charges_consumed": consumed,
                }, "cursed_key_random_curse_identity_domain",
            )
            if consumed:
                _known_consequence_field(
                    consequence, "relic_changes", {
                        "gain": [], "remove": [], "counter": [{
                            "id": omamori.get("id") or "Omamori",
                            "delta": -1,
                        }],
                    }, authority="base_game_relic_mechanics", reason=reason,
                )
            _known_domain_consequence_field(
                consequence, "probabilistic_outcomes", [],
                "cursed_key_random_curse_identity_domain",
            )
            consequence["random_effects"] = (
                [random_curse] if gained else []
            )
        _mark_deterministic_costs(consequence, reason=reason)
        if not cursed_key:
            _mark_no_probability(consequence, reason)
        _known_consequence_field(
            consequence, "future_costs",
            [{"kind": "chest_reward_surface", "timing": "after_open"}],
            authority="protocol_multistage_operation", reason=reason,
        )
        consequence["operation"] = "open_chest"
        consequence["uncertainty_classification"] = {
            "status": (
                "classified_random_domain" if cursed_key
                else "classified_future"
            ),
            "authority": (
                "production_mechanics_projection" if cursed_key
                else "protocol_multistage_operation"
            ),
            "reason": (
                "Cursed Key curse identity is random and the chest reward follows"
                if cursed_key else
                "reward identity is exposed on the following reward surface"
            ),
        }
    elif phase == "SHOP_ROOM" and kind == "shop_room":
        reason = "typed_shop_room_entry_transition"
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        _known_consequence_field(
            consequence, "future_costs",
            [{"kind": "shop_inventory_surface", "timing": "after_entry"}],
            authority="protocol_multistage_operation", reason=reason,
        )
        consequence["operation"] = "enter_shop"
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "protocol_multistage_operation",
            "reason": "shop inventory is exposed on the following SHOP_SCREEN surface",
        }
    elif phase == "GRID" and kind == "card":
        operation, selected_card, grid_error = _validated_grid_target(
            target, state or {}
        )
        if grid_error is not None:
            consequence["uncertainty"].append(grid_error)
        else:
            reason = f"{operation}_exact_card_instance"
            if (
                operation != "grid_transform"
                or not _state_has_relic(state or {}, "Darkstone Periapt")
            ):
                _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            else:
                _known_consequence_field(
                    consequence, "gold_delta", 0,
                    authority="production_mechanics_projection",
                    reason=reason,
                )
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            if operation == "grid_gain":
                _known_consequence_field(
                    consequence, "card_changes", {
                        "gain": [_audit_clone(selected_card)],
                        "remove": [], "upgrade": [], "transform": [],
                    }, authority="accepted_event_grid_binding", reason=reason,
                )
                _known_consequence_field(
                    consequence, "future_costs", [],
                    authority="accepted_event_grid_binding", reason=reason,
                )
                _mark_no_probability(consequence, reason)
            else:
                _known_consequence_field(
                    consequence, "future_costs",
                    _grid_confirmation_followup(operation, selected_card),
                    authority="protocol_multistage_operation", reason=reason,
                )
            if operation == "grid_transform":
                _known_domain_consequence_field(
                    consequence, "probabilistic_outcomes", [],
                    "grid_transform_random_identity_domain",
                )
                random_transform = {
                    "kind": "grid_transform_result",
                    "domain": "game_card_transform_pool",
                    "count": 1,
                    "timing": "after_grid_confirmation",
                    "selection_mode": "random",
                }
                grid_parent = (
                    (_state_game(state or {}).get("screen_state") or {}).get(
                        "parent_choice_context"
                    ) or {}
                )
                if (
                    isinstance(grid_parent, dict)
                    and grid_parent.get("authority")
                    == "accepted_protocol_choice"
                    and grid_parent.get("parent_phase") == "BOSS_REWARD"
                    and _normalized_game_id(grid_parent.get("relic_id"))
                    == "astrolabe"
                    and grid_parent.get("operation") == "transform"
                    and grid_parent.get("select_count") == 3
                ):
                    random_transform["result_upgrades"] = 1
                consequence["random_effects"] = [random_transform]
                consequence["uncertainty"] = [
                    "exact transform commits after grid confirmation; result identity is random"
                ]
                consequence["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "production_mechanics_projection",
                    "reason": "exact source UUID, commit timing, and transform result domain are typed",
                }
            elif operation != "grid_gain":
                _mark_no_probability(consequence, reason)
                consequence["uncertainty"] = [
                    "exact card effect commits after grid confirmation"
                ]
                consequence["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "protocol_multistage_operation",
                    "reason": "selected UUID and confirmation timing are exact",
                }
            consequence["selected_card"] = _audit_clone(selected_card)
            consequence["operation"] = operation
    elif phase == "HAND_SELECT" and kind == "card":
        selected_card, context, hand_error = _validated_hand_select_target(
            target, state or {}
        )
        if hand_error is not None:
            consequence["uncertainty"].append(hand_error)
        else:
            reason = "hand_select_exact_transient_card_instance"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            _known_consequence_field(
                consequence, "future_costs", [{
                    "kind": "hand_select_action_resolution",
                    "current_action": context["current_action"],
                    "max_cards": context["max_cards"],
                    "can_pick_zero": context["can_pick_zero"],
                    "selected_card": selected_card,
                    "timing": "after_hand_selection_confirmation",
                }], authority="authoritative_hand_select_context",
                reason=reason,
            )
            consequence["operation"] = "hand_select_card"
            consequence["selected_card"] = _audit_clone(selected_card)
            consequence["uncertainty"] = [
                "the bound combat action resolves after hand selection confirmation"
            ]
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "authoritative_hand_select_context",
                "reason": "selected UUID and queued action are protocol-visible",
            }
    elif kind == "card" and card:
        game = _state_game(state or {})
        transient_combat_reward = bool(
            phase == "CARD_REWARD"
            and isinstance(game.get("combat_state"), dict)
            and str(game.get("room_phase") or "").upper() == "COMBAT"
        )
        if transient_combat_reward:
            reason = "temporary_combat_card_acquisition"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            consequence["operation"] = "add_temporary_combat_card"
        else:
            reason = "card_reward_acquisition"
            gold_gain = 9 if _state_has_relic(state, "CeramicFish") else 0
            _mark_numeric_consequences(consequence, 0, 0, gold_gain, reason)
            _known_consequence_field(
                consequence, "card_changes",
                {"gain": [compact_audit_value(card)], "remove": [], "upgrade": [], "transform": []},
                authority="protocol_card_target", reason=reason,
            )
            for field, value in (
                ("relic_changes", {"gain": [], "remove": [], "counter": []}),
                ("potion_changes", {"gain": [], "remove": [], "replace": []}),
                ("curse", {"gain": [], "remove": [], "probability": 0.0,
                           "omamori_applicable": False, "omamori_charges_consumed": 0}),
            ):
                _known_consequence_field(
                    consequence, field, value,
                    authority="production_mechanics_projection", reason=reason,
                )
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            if phase == "CARD_REWARD":
                consequence["operation"] = "gain_card_reward"
    elif kind in {"card", "relic", "potion"} and item:
        price_value = item.get("price")
        price = int(price_value) if type(price_value) is int else None
        reason = f"shop_{kind}_purchase"
        classified_shop_relic_future = None
        pickup_package = False
        item_id = item.get("id") or item.get("name")
        acquired_benefit = {"kind": kind, "id": item_id}
        acquired_benefit[
            {"card": "card_id", "relic": "relic_id", "potion": "potion_id"}[kind]
        ] = item_id
        instance_field = {
            "card": "card_instance_id",
            "relic": None,
            "potion": "potion_instance_id",
        }[kind]
        if instance_field and item.get(instance_field) is not None:
            acquired_benefit[instance_field] = item.get(instance_field)
        _known_consequence_field(
            consequence,
            "acquired_benefit",
            acquired_benefit,
            authority="protocol_shop_target",
            reason=reason,
        )
        if kind == "card":
            card_gold = 9 if _state_has_relic(state, "CeramicFish") else 0
            if price is not None:
                _mark_numeric_consequences(
                    consequence, 0, 0, card_gold - price, reason
                )
                _mark_deterministic_costs(
                    consequence, gold=price, reason=reason
                )
            _mark_empty_inventory_consequences(consequence, reason)
            _known_consequence_field(
                consequence, "card_changes",
                {"gain": [compact_audit_value(item)], "remove": [], "upgrade": [], "transform": []},
                authority="protocol_shop_target", reason=reason,
            )
        elif kind == "potion":
            if price is not None:
                _mark_numeric_consequences(consequence, 0, 0, -price, reason)
                _mark_deterministic_costs(
                    consequence, gold=price, reason=reason
                )
            _mark_empty_inventory_consequences(consequence, reason)
            _known_consequence_field(
                consequence, "potion_changes",
                {"gain": [compact_audit_value(item)], "remove": [], "replace": []},
                authority="protocol_shop_target", reason=reason,
            )
        else:
            relic_id = _normalized_game_id(item.get("id") or item.get("name"))
            pickup = _IMMEDIATE_RELIC_PICKUPS.get(relic_id)
            pickup_package = _apply_relic_pickup_package(
                consequence, state or {}, item, relic_id, reason,
                price=price,
            )
            if pickup_package:
                pass
            elif price is not None and relic_id == "dollysmirror":
                _mark_numeric_consequences(
                    consequence, 0, 0, -price, reason
                )
                _mark_deterministic_costs(
                    consequence, gold=price, reason=reason
                )
                _mark_empty_inventory_consequences(consequence, reason)
                classified_shop_relic_future = [{
                    "kind": "dollys_mirror_grid_selection",
                    "operation": "duplicate",
                    "select_count": 1,
                    "domain": "current_deck",
                    "selection_mode": "player_choice",
                    "timing": "after_relic_purchase",
                }]
                _known_consequence_field(
                    consequence, "future_costs",
                    classified_shop_relic_future,
                    authority="base_game_relic_mechanics", reason=reason,
                )
            elif price is not None and (
                pickup is not None or relic_id in _PASSIVE_RELIC_PICKUPS
            ):
                pickup = pickup or {"hp_delta": 0, "max_hp_delta": 0, "gold_delta": 0}
                _mark_numeric_consequences(
                    consequence, pickup["hp_delta"], pickup["max_hp_delta"],
                    pickup["gold_delta"] - price, reason,
                )
                _mark_deterministic_costs(
                    consequence, gold=price, reason=reason
                )
                _mark_empty_inventory_consequences(consequence, reason)
            _known_consequence_field(
                consequence, "relic_changes",
                {"gain": [compact_audit_value(item)], "remove": [], "counter": []},
                authority="protocol_shop_target", reason=reason,
            )
        if not pickup_package:
            _mark_no_probability(consequence, reason)
        consequence["operation"] = {
            "card": "buy_card_then_rerank_shop",
            "relic": "buy_relic_then_rerank_shop",
            "potion": "buy_potion_then_rerank_shop",
        }[kind]
        if classified_shop_relic_future:
            consequence["uncertainty"] = [
                "the duplicated card UUID settles on the following GRID surface"
            ]
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "base_game_relic_mechanics",
                "reason": (
                    "Dollys Mirror always opens one current-deck card "
                    "duplication choice after purchase"
                ),
            }
    elif kind == "relic" and relic:
        reason = "relic_reward_acquisition"
        relic_id = _normalized_game_id(relic.get("id") or relic.get("name"))
        pickup = _IMMEDIATE_RELIC_PICKUPS.get(relic_id)
        pickup_package = _apply_relic_pickup_package(
            consequence, state or {}, relic, relic_id, reason
        )
        if relic_id == "callingbell":
            _apply_calling_bell_pickup(consequence, relic, reason)
        elif pickup_package:
            pass
        elif pickup is not None:
            _mark_numeric_consequences(
                consequence, pickup["hp_delta"], pickup["max_hp_delta"],
                pickup["gold_delta"], reason,
            )
        elif relic_id in _PASSIVE_RELIC_PICKUPS:
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        if pickup is not None or relic_id in _PASSIVE_RELIC_PICKUPS:
            _mark_empty_inventory_consequences(consequence, reason)
        if relic_id != "callingbell" and not pickup_package:
            _known_consequence_field(
                consequence, "relic_changes",
                {"gain": [compact_audit_value(relic)], "remove": [], "counter": []},
                authority="protocol_relic_target", reason=reason,
            )
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
        if phase == "BOSS_REWARD":
            consequence["operation"] = "gain_boss_relic"
    elif kind == "potion" and target.get("potion"):
        potion = target.get("potion")
        reason = "potion_reward_acquisition"
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "potion_changes",
            {"gain": [compact_audit_value(potion)], "remove": [], "replace": []},
            authority="protocol_potion_target", reason=reason,
        )
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
    elif kind == "purge":
        price, purge_error = (
            _validated_shop_purge(target, state or {})
            if phase == "SHOP_SCREEN"
            else (None, "purge_outside_shop_screen")
        )
        if purge_error is not None:
            consequence["uncertainty"].append(purge_error)
        else:
            reason = "shop_purge_exact_protocol_cost"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _known_consequence_field(
                consequence, "future_costs", _shop_purge_followup(price),
                authority="protocol_multistage_operation", reason=reason,
            )
            _mark_no_probability(consequence, reason)
            consequence["operation"] = "open_card_purge_grid"
            consequence["uncertainty"] = [
                "removed card UUID is bound only by the subsequent GRID choice and settlement"
            ]
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "gold and exact card removal commit after the subsequent grid confirmation",
            }
    elif kind == "bowl":
        reason = "singing_bowl_card_reward_option"
        _mark_numeric_consequences(consequence, 2, 2, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        consequence["operation"] = "singing_bowl"
    elif kind == "event_option":
        event_id = _normalized_game_id(target.get("event_id"))
        screen_event_id = _normalized_game_id(
            (_state_game(state or {}).get("screen_state") or {}).get(
                "event_id"
            )
        )
        mechanism_id = str(target.get("mechanism_id") or "")
        consequence["event_id"] = target.get("event_id")
        choice_index = target.get("choice_index")
        if choice_index is None:
            choice_index = target.get("index")
        if phase == "NEOW":
            _apply_neow_consequence(consequence, target, state or {})
        elif event_id == "goldenidol":
            original_index = target.get("original_button_index")
            if original_index is None:
                original_index = choice_index
            option_count = len(state_options)
            is_leave = bool(
                (option_count == 2 and original_index == 1)
                or (option_count == 1 and original_index == 0)
            )
            if is_leave:
                reason = "golden_idol_typed_branch"
                _mark_numeric_consequences(consequence, 0, 0, 0, reason)
                _mark_empty_inventory_consequences(consequence, reason)
                _mark_deterministic_costs(consequence, reason=reason)
                _mark_no_probability(consequence, reason)
                consequence["operation"] = "golden_idol_leave"
                consequence["leave"] = True
            else:
                consequence["uncertainty"].append(
                    "golden idol non-leave outcome settles on the following event surface"
                )
        elif event_id in _INDEXED_A0_EVENT_IDS:
            if not _apply_indexed_a0_event_consequence(
                consequence, target, state or {}, event_id, raw_text
            ):
                consequence["uncertainty"].append(
                    "event outcome is unclassified without typed protocol mechanics"
                )
        elif (
            event_id in _EXACT_A0_EVENT_MECHANISMS
            or screen_event_id in _EXACT_A0_EVENT_MECHANISMS
        ):
            exact_event_id = (
                event_id
                if event_id in _EXACT_A0_EVENT_MECHANISMS
                else screen_event_id
            )
            _apply_exact_a0_event_consequence(
                consequence, target, state or {}, exact_event_id
            )
        elif mechanism_id == "gain_gold_exact" and type(target.get("amount")) is int:
            reason = "typed_gain_gold_event_primitive"
            _mark_numeric_consequences(
                consequence, 0, 0, target["amount"], reason
            )
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
        elif (
            event_id == "themausoleum"
            or _normalized_game_id(
                (_state_game(state or {}).get("screen_state") or {}).get(
                    "event_id"
                )
            ) == "themausoleum"
        ):
            _apply_mausoleum_consequence(
                consequence, target, state or {}
            )
        elif event_id == "designer" and type(choice_index) is int:
            game = _state_game(state or {})
            ascension = game.get("ascension_level")
            if type(ascension) is not int:
                consequence["uncertainty"].append(
                    "designer_ascension_level_missing"
                )
            elif choice_index in {0, 1, 2, 3}:
                high_ascension = ascension >= 15
                reason = "designer_typed_base_game_branch"
                gold_costs = (
                    {0: 50, 1: 75, 2: 110}
                    if high_ascension else {0: 40, 1: 60, 2: 90}
                )
                hp_cost = (
                    (5 if high_ascension else 3)
                    if choice_index == 3 else 0
                )
                gold_cost = gold_costs.get(choice_index, 0)
                _mark_numeric_consequences(
                    consequence, -hp_cost, 0, -gold_cost, reason
                )
                _mark_empty_inventory_consequences(consequence, reason)
                _mark_deterministic_costs(
                    consequence,
                    gold=gold_cost,
                    hp=hp_cost,
                    reason=reason,
                )
                if choice_index == 0:
                    operation = "designer_adjustments"
                    outcome_id = "adjustments"
                    future = [{
                        "kind": "designer_grid_selection",
                        "operation": "grid_upgrade",
                        "select_count": 1,
                        "identity_binding": (
                            "subsequent_grid_card_instance_id"
                        ),
                        "commit_timing": "after_grid_confirmation",
                    }]
                elif choice_index == 1:
                    operation = "designer_clean_up"
                    outcome_id = "clean_up"
                    future = [{
                        "kind": "designer_grid_selection",
                        "operation": "grid_purge",
                        "select_count": 1,
                        "identity_binding": (
                            "subsequent_grid_card_instance_id"
                        ),
                        "commit_timing": "after_grid_confirmation",
                    }]
                elif choice_index == 2:
                    operation = "designer_full_service"
                    outcome_id = "full_service"
                    future = [
                        {
                            "kind": "designer_grid_selection",
                            "operation": "grid_purge",
                            "select_count": 1,
                            "identity_binding": (
                                "subsequent_grid_card_instance_id"
                            ),
                            "commit_timing": "after_grid_confirmation",
                        },
                        {
                            "kind": "designer_random_card_upgrade",
                            "operation": "random_upgrade",
                            "count": 1,
                            "domain": "post_purge_upgradable_deck",
                            "selection_mode": "random",
                            "commit_timing": "after_grid_confirmation",
                        },
                    ]
                    _known_domain_consequence_field(
                        consequence,
                        "probabilistic_outcomes",
                        [],
                        "designer_random_upgrade_identity_domain",
                    )
                    consequence["random_effects"] = [{
                        "kind": "random_card_upgrade",
                        "count": 1,
                        "domain": "post_purge_upgradable_deck",
                        "selection_mode": "random",
                    }]
                else:
                    operation = "designer_punch_and_leave"
                    outcome_id = "punch_and_leave"
                    future = []
                    consequence["leave"] = True
                _known_consequence_field(
                    consequence,
                    "future_costs",
                    future,
                    authority="protocol_multistage_operation",
                    reason=reason,
                )
                if choice_index != 2:
                    _mark_no_probability(consequence, reason)
                consequence["operation"] = operation
                consequence["event_outcome_id"] = outcome_id
                if future:
                    consequence["uncertainty"] = [
                        "exact card UUID is bound by the following GRID surface"
                    ]
                    consequence["uncertainty_classification"] = {
                        "status": "classified_future",
                        "authority": "protocol_multistage_operation",
                        "reason": (
                            "Designer follow-up operations and random domain "
                            "are typed; exact card UUID settles downstream"
                        ),
                    }
        elif (
            event_id == "sensorystone"
            and target.get("event_class")
            and (
                target.get("event_stage") is not None
                or target.get("screen_num") is not None
            )
        ):
            if not _apply_indexed_a0_event_consequence(
                consequence, target, state or {}, event_id, raw_text
            ):
                consequence["uncertainty"].append(
                    "event progress evidence is invalid or incomplete"
                )
        elif event_id == "sensorystone" and type(choice_index) is int:
            hp_costs = {0: 0, 1: -5, 2: -10}
            reward_counts = {0: 1, 1: 2, 2: 3}
            if choice_index in hp_costs:
                reason = "sensory_stone_typed_branch"
                _mark_numeric_consequences(
                    consequence, hp_costs[choice_index], 0, 0, reason
                )
                _mark_empty_inventory_consequences(consequence, reason)
                _mark_deterministic_costs(consequence, reason=reason)
                _known_consequence_field(
                    consequence, "future_costs", [{
                        "kind": "deferred_colorless_card_reward_choices",
                        "count": reward_counts[choice_index],
                    }], authority="production_mechanics_projection", reason=reason,
                )
                _mark_no_probability(consequence, reason)
                consequence["uncertainty"] = [
                    "card identities are bound by subsequent CARD_REWARD states"
                ]
                consequence["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "production_mechanics_projection",
                    "reason": "SensoryStone opens a known count of downstream colorless rewards",
                }
        elif event_id in {"matchandkeep", "matchkeep"}:
            reason = "match_and_keep_position_flip"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            consequence["field_knowledge"]["card_changes"] = {
                "status": "not_observable",
                "authority": "protocol_hidden_event_state",
                "reason": "hidden card identity and match pairing are not exposed",
            }
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            consequence["uncertainty"] = [
                "hidden MatchAndKeep card identity is not protocol-visible"
            ]
            consequence["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "protocol_hidden_event_state",
                "reason": "all visible positions are exact but card identities remain hidden",
            }
        elif event_id == "wheelofchange" and len(
            state_options
        ) == 1:
            reason = "wheel_of_change_singleton_random_outcome"
            for field in _CONSEQUENCE_KNOWLEDGE_FIELDS:
                consequence["field_knowledge"][field] = {
                    "status": "not_observable",
                    "authority": "base_game_forced_random_event",
                    "reason": reason,
                }
            consequence["operation"] = "wheel_of_change_forced_progress"
            consequence["uncertainty"] = [
                "Wheel of Change has one forced action; its randomized outcome is protocol-hidden"
            ]
            consequence["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "base_game_forced_random_event",
                "reason": reason,
                "homogeneity_proven": True,
            }
        elif event_id == "falling" and len(
            state_options
        ) == 1 and not card:
            reason = "falling_singleton_dialog_progress"
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            consequence["operation"] = "falling_forced_progress"
        elif event_id == "falling" and card:
            original_index = target.get("original_button_index")
            expected_type = {0: "SKILL", 1: "POWER", 2: "ATTACK"}.get(
                original_index
            )
            if expected_type and str(card.get("type") or "").upper() == expected_type:
                reason = "falling_exact_offered_card_sacrifice"
                removed_card = compact_audit_value(card)
                # Falling displays a transient copy. Bind settlement to the
                # semantic card identity rather than that preview UUID.
                removed_card.pop("card_instance_id", None)
                _mark_numeric_consequences(consequence, 0, 0, 0, reason)
                _mark_empty_inventory_consequences(consequence, reason)
                _known_consequence_field(
                    consequence, "card_changes", {
                        "gain": [], "remove": [removed_card],
                        "upgrade": [], "transform": [],
                    }, authority="protocol_event_card_preview", reason=reason,
                )
                _mark_deterministic_costs(consequence, reason=reason)
                _mark_no_probability(consequence, reason)
                consequence.update({
                    "operation": "falling_sacrifice_offered_card",
                    "event_outcome_id": "sacrifice_offered_card",
                })
            else:
                consequence["uncertainty"].append(
                    "Falling offered card type does not match its base-game button"
                )
        else:
            consequence["uncertainty"].append(
                "event outcome is unclassified without typed protocol mechanics"
            )
    elif kind == "reward":
        reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
        reward_type = str(reward.get("reward_type") or "").casefold()
        reason = f"combat_reward_{reward_type or 'unknown'}"
        consequence["operation"] = (
            "gain_linked_relic"
            if phase == "SAPPHIRE_KEY" and reward_type == "relic"
            else "gain_sapphire_key"
            if phase == "SAPPHIRE_KEY" and reward_type == "sapphire_key"
            else "collect_independent_reward"
            if phase == "SAPPHIRE_KEY"
            else "collect_combat_reward"
        )
        if reward_type in {"gold", "stolen_gold"} and type(reward.get("gold")) is int:
            reward_heal = _gold_reward_hp_delta(
                _state_game(state or {}), reward["gold"]
            )
            _mark_numeric_consequences(
                consequence, reward_heal, 0, reward["gold"], reason
            )
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
        elif reward_type in {"sapphire_key", "emerald_key"}:
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            consequence["key_changes"] = {"gain": [reward_type]}
        elif reward_type == "relic" and isinstance(reward.get("relic"), dict):
            reward_relic = reward["relic"]
            relic_id = _normalized_game_id(
                reward_relic.get("id") or reward_relic.get("name")
            )
            pickup = _IMMEDIATE_RELIC_PICKUPS.get(relic_id)
            pickup_package = _apply_relic_pickup_package(
                consequence, state or {}, reward_relic, relic_id, reason
            )
            if pickup_package:
                pass
            elif pickup is not None:
                _mark_numeric_consequences(
                    consequence, pickup["hp_delta"], pickup["max_hp_delta"],
                    pickup["gold_delta"], reason,
                )
            elif relic_id in _PASSIVE_RELIC_PICKUPS:
                _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            if pickup is not None or relic_id in _PASSIVE_RELIC_PICKUPS:
                _mark_empty_inventory_consequences(consequence, reason)
            if not pickup_package:
                _known_consequence_field(
                    consequence, "relic_changes",
                    {"gain": [compact_audit_value(reward_relic)], "remove": [], "counter": []},
                    authority="protocol_relic_target", reason=reason,
                )
                _mark_deterministic_costs(consequence, reason=reason)
                _mark_no_probability(consequence, reason)
        elif reward_type == "potion" and isinstance(reward.get("potion"), dict):
            reward_potion = compact_audit_value(reward["potion"])
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            _known_consequence_field(
                consequence, "future_costs",
                [{
                    "kind": "potion_reward_acquisition_or_replacement",
                    "potion": reward_potion,
                    "timing": "after_reward_choice",
                }],
                authority="protocol_multistage_operation", reason=reason,
            )
            consequence["potion_id"] = reward_potion.get("id")
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "potion identity is exact; a full belt exposes resource preparation before acquisition",
            }
        elif reward_type == "card":
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
            _mark_empty_inventory_consequences(consequence, reason)
            _mark_deterministic_costs(consequence, reason=reason)
            _mark_no_probability(consequence, reason)
            _known_consequence_field(
                consequence, "future_costs",
                [{"kind": "card_reward_surface", "timing": "after_reward_choice"}],
                authority="protocol_multistage_operation", reason=reason,
            )
            consequence["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "card identities are exposed on the following CARD_REWARD surface",
            }
    elif kind in {"sapphire_key", "key"}:
        reason = "sapphire_key_reward"
        _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        _mark_empty_inventory_consequences(consequence, reason)
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        consequence["key_changes"] = {"gain": ["sapphire_key"]}
        consequence["operation"] = "gain_sapphire_key"
    elif kind == "potion_resource":
        operation = str(target.get("operation") or "").casefold()
        reason = f"resource_preparation_{operation}_exact_held_potion"
        potion = target.get("potion") if isinstance(target.get("potion"), dict) else {
            "id": target.get("potion_id"),
            "potion_instance_id": target.get("potion_instance_id"),
            "slot": target.get("slot"),
        }
        potion_id = _normalized_game_id(
            potion.get("id") or target.get("potion_id")
        )
        if operation == "discard":
            _mark_numeric_consequences(consequence, 0, 0, 0, reason)
        elif operation == "use":
            delta = _healing_potion_delta(_state_game(state or {}), potion_id)
            if delta is not None:
                _mark_numeric_consequences(
                    consequence, delta[0], delta[1], 0, reason
                )
        _mark_empty_inventory_consequences(consequence, reason)
        _known_consequence_field(
            consequence, "potion_changes",
            {"gain": [], "remove": [compact_audit_value(potion)], "replace": []},
            authority="protocol_target", reason=reason,
        )
        _mark_deterministic_costs(consequence, reason=reason)
        _mark_no_probability(consequence, reason)
        consequence["uncertainty"] = [
            "parent strategic choice remains pending after resource preparation"
        ]
        consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "production_mechanics_projection",
            "reason": "the next authoritative screen audits the purchase or reward choice",
        }

    _apply_protocol_consequence_contract(consequence, target)
    _apply_protocol_probability_contract(consequence, target)

    if kind == "map_node":
        consequence["route"] = {
            key: target.get(key) for key in ("symbol", "x", "y")
        }

    # Text extraction is deliberately field-local.  It never turns an absent
    # localized keyword into a known zero for other resources.
    numeric_patterns = (
        ("gold_delta", r"\b(?:gain|obtain|receive)\s+(\d+)\s+gold\b", 1),
        ("gold_delta", r"\b(?:lose|pay|spend)\s+(\d+)\s+gold\b", -1),
        ("hp_delta", r"\b(?:heal|gain)\s+(\d+)\s+(?:hp|health)\b", 1),
        ("hp_delta", r"\b(?:lose|pay)\s+(\d+)\s+(?:hp|health)\b", -1),
        (
            "max_hp_delta",
            r"\b(?:gain|increase)\s+(\d+)\s+max(?:imum)?\s+(?:hp|health)\b",
            1,
        ),
        (
            "max_hp_delta",
            r"\b(?:lose|decrease)\s+(\d+)\s+max(?:imum)?\s+(?:hp|health)\b",
            -1,
        ),
        ("gold_delta", r"(?:获得|得到)\s*(\d+)\s*(?:金币|金钱)", 1),
        ("gold_delta", r"(?:失去|支付|花费)\s*(\d+)\s*(?:金币|金钱)", -1),
        ("hp_delta", r"(?:回复|恢复|获得)\s*(\d+)\s*(?:点)?(?:生命|生命值)", 1),
        ("hp_delta", r"(?:失去|支付)\s*(\d+)\s*(?:点)?(?:生命|生命值)", -1),
        ("max_hp_delta", r"(?:获得|增加)\s*(\d+)\s*(?:点)?最大生命", 1),
        ("max_hp_delta", r"(?:失去|降低)\s*(\d+)\s*(?:点)?最大生命", -1),
    )
    typed_event_ids = (
        set(_EXACT_A0_EVENT_MECHANISMS)
        | _INDEXED_A0_EVENT_IDS
        | {"themausoleum", "goldenidol"}
    )
    protected_typed_event = bool(
        kind == "event_option"
        and (
            _normalized_game_id(target.get("event_id")) in typed_event_ids
            or _normalized_game_id(
                (_state_game(state or {}).get("screen_state") or {}).get(
                    "event_id"
                )
            ) in typed_event_ids
        )
    )
    if phase != "NEOW" and not protected_typed_event:
        for field, pattern, sign in numeric_patterns:
            match = re.search(pattern, str(raw_text or ""), flags=re.IGNORECASE)
            if match:
                _known_consequence_field(
                    consequence, field, sign * int(match.group(1)),
                    authority="protocol_visible_text",
                    reason="localized_numeric_effect_explicitly_visible",
                )

    if phase == "REST":
        consequence["campfire_option"] = str(raw_text or "")
    return consequence


def _canonical_choice_semantic_id(phase, option):
    """Derive a localized-text-independent identity for one protocol choice."""

    target = option.get("target") or {}
    kind = str(target.get("kind") or "option").casefold()
    index = option.get("choice_index")
    if kind == "potion_resource":
        operation = str(target.get("operation") or "").casefold()
        instance_id = target.get("potion_instance_id")
        return f"potion-{operation}:{instance_id}:{index}"
    if phase == "NEOW":
        return f"neow:{index}"
    if phase == "MAP" and all(target.get(key) is not None for key in ("x", "y")):
        return f"{target.get('symbol') or '?'}@{target['x']},{target['y']}"
    if phase == "EVENT":
        event_id = str(target.get("event_id") or "").casefold()
        prefix = "match-position" if "match" in event_id else "event"
        return f"{prefix}:{index}"
    if phase in {"GRID", "HAND_SELECT"}:
        return f"grid:{target.get('card_instance_id') or index}"
    if phase == "SHOP_SCREEN":
        item = target.get("item") if isinstance(target.get("item"), dict) else {}
        identity = (
            item.get("card_instance_id")
            or item.get("relic_id")
            or item.get("potion_instance_id")
            or item.get("id")
            or index
        )
        return f"shop:{kind}:{identity}:{index}"
    if phase == "BOSS_REWARD":
        relic = target.get("relic") if isinstance(target.get("relic"), dict) else {}
        return f"relic:{relic.get('id') or index}:{index}"
    if phase == "CARD_REWARD":
        card = target.get("card") if isinstance(target.get("card"), dict) else {}
        return f"card:{target.get('card_instance_id') or card.get('id') or index}"
    if phase in {"COMBAT_REWARD", "SAPPHIRE_KEY"}:
        reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
        return f"reward:{str(reward.get('reward_type') or kind).casefold()}:{index}"
    return f"{kind}:{index}"


def canonical_legal_choices(state, payload, decision):
    """Record every protocol-visible legal non-combat choice exactly once."""

    phase = str(state.get("phase") or "")
    if not has_strategic_noncombat_surface(state, payload):
        return []
    resource_options = _resource_preparation_options(state, payload)
    resource_preparation = bool(resource_options)
    options = [
        compact_option(item) for item in (
            resource_options if resource_preparation else state.get("options") or []
        )
    ]
    choices = []
    for option in options:
        target = option.get("target") or {}
        raw_text = " ".join(str(value or "") for value in (
            option.get("label"), target.get("label"), target.get("text"),
        )).strip()
        choices.append({
            "choice_schema_version": 1,
            "choice_id": option.get("option_id"),
            "choice_index": option.get("choice_index"),
            "action": (
                "potion" if str(target.get("kind") or "").casefold()
                == "potion_resource" else "choose"
            ),
            "operation": (
                str(target.get("operation") or "").casefold()
                if str(target.get("kind") or "").casefold()
                == "potion_resource" else None
            ),
            "legal": True,
            "visible": True,
            "label": option.get("label"),
            "raw_text": raw_text,
            "semantic_id": _canonical_choice_semantic_id(phase, option),
            "target": target,
            "aliases": sorted(
                _audit_choice_aliases(option.get("option_id"))
                | _audit_choice_aliases(option.get("choice_index"))
                | _audit_choice_aliases(option.get("label"))
                | _aliases_from_mapping(target)
            ),
        })

    commands = [] if resource_preparation else [
        str(command or "").strip().casefold()
        for command in state.get("available_commands") or []
    ]
    action_commands = []
    for command in commands:
        # CommunicationMod exposes several global or parameterized commands
        # alongside the current choice screen.  They are not standalone
        # alternatives in that screen: ``choose`` is represented by every
        # concrete option above, while play/potion/key/click need a target or
        # arguments and state/wait are transport operations.
        if not command or command in {
            "state", "wait", "choose", "play", "potion", "key", "click",
            "end", "start", "resume",
        }:
            continue
        action = (
            "return" if command in {"skip", "cancel", "leave"}
            else "proceed" if command == "confirm"
            else command
        )
        if (
            action == "return"
            and phase in {"MAP", "GRID", "HAND_SELECT"}
        ):
            # These commands only close/reopen a mandatory navigation or
            # selection overlay; they are not irreversible alternatives to a
            # semantic route/card choice.
            continue
        row = (f"action:{action}", action, command)
        if all(existing[0] != row[0] for existing in action_commands):
            action_commands.append(row)
    payload_action = str(payload.get("action") or "").strip().casefold()
    for choice_id, label, raw_command in action_commands:
        choices.append({
            "choice_schema_version": 1,
            "choice_id": choice_id,
            "choice_index": None,
            "action": label,
            "operation": None,
            "legal": True,
            "visible": True,
            "label": label,
            "raw_text": raw_command,
            "semantic_id": (
                "shop_leave"
                if phase == "SHOP_SCREEN" and label == "return"
                else label
            ),
            "target": {"kind": "protocol_action", "action": label},
            "aliases": sorted(_audit_choice_aliases(choice_id) | _audit_choice_aliases(label)),
        })

    candidate_rows = _decision_candidate_rows(decision)
    advice = (decision or {}).get("model_advice") or {}
    rankings = advice.get("rankings") or []
    final_choice_ids = [
        str(value) for value in advice.get("final_choice_ids") or []
        if value is not None
    ]
    for choice in choices:
        choice.pop("aliases", None)
        choice_action = str(choice.get("action") or "").strip().casefold()
        choice_operation = (
            str(choice.get("operation") or "").strip().casefold()
            or None
        )
        choice_index = choice.get("choice_index")
        local_matches = [
            row for row in candidate_rows
            if row["source"] == "local"
            and row.get("typed_binding_complete") is True
            and (row.get("facts") or {}).get("choice_index")
            == choice_index
            and str((row.get("facts") or {}).get("action") or "").strip().casefold()
            == choice_action
            and (
                str((row.get("facts") or {}).get("operation") or "").strip().casefold()
                or None
            ) == choice_operation
        ]
        evidence_row = local_matches[0] if len(local_matches) == 1 else None
        exact_model_ids = {str(choice.get("choice_id"))}
        if evidence_row and evidence_row.get("candidate_id") is not None:
            exact_model_ids.add(str(evidence_row["candidate_id"]))
        evidence_candidate = (
            evidence_row.get("raw_candidate")
            if isinstance(evidence_row, dict) else None
        )
        if isinstance(evidence_candidate, dict):
            exact_model_ids.update(
                str(evidence_candidate[key])
                for key in (
                    "id", "candidate_id", "choice_id", "semantic_id",
                )
                if evidence_candidate.get(key) is not None
            )
        model_matches = [
            row for row in rankings
            if isinstance(row, dict)
            and str(
                row.get("candidate_id", row.get("choice_id", row.get("id")))
            ) in exact_model_ids
        ]
        consequence_target = dict(choice.get("target") or {})
        consequence_target.setdefault("choice_index", choice_index)
        consequence = _structured_option_consequence(
            phase,
            consequence_target,
            choice.get("raw_text"),
            evidence_row,
            state=state,
        )
        uncertainty = list(consequence.get("uncertainty") or [])
        model_applied = advice.get("applied") is True
        if choice_action == "choose":
            selected = bool(
                payload_action == "choose"
                and payload.get("option_id") is not None
                and str(payload.get("option_id")) == str(choice.get("choice_id"))
            )
        elif choice_action == "potion":
            target_instance_id = (
                (choice.get("target") or {}).get("potion_instance_id")
            )
            selected = bool(
                payload_action == "potion"
                and str(payload.get("operation") or "").casefold()
                == choice_operation
                and payload.get("potion_instance_id") is not None
                and str(payload.get("potion_instance_id"))
                == str(target_instance_id)
            )
        else:
            target_id = payload.get("target_id")
            selected = bool(
                payload_action == choice_action
                and (
                    target_id is None
                    or str(target_id) == str(choice.get("choice_id"))
                )
            )
        exact_model_choice = str(
            advice.get("model_choice_id") or ""
        ) in exact_model_ids
        exact_model_final = bool(
            len(final_choice_ids) == 1
            and final_choice_ids[0] in exact_model_ids
        )
        row_model_applied = bool(
            selected and model_applied and exact_model_choice and exact_model_final
        )
        split_claim = _split_producer_consequence(evidence_row)
        raw_candidate = (
            evidence_row.get("raw_candidate")
            if isinstance(evidence_row, dict) else None
        )
        raw_candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
        raw_facts = raw_candidate.get("facts")
        raw_facts = raw_facts if isinstance(raw_facts, dict) else {}
        def producer_score_field(name):
            return _audit_clone(
                raw_candidate.get(name, raw_facts.get(name))
            )
        choice.update({
            "candidate_ids": sorted({
                row["candidate_id"] for row in local_matches
                if row.get("candidate_id") is not None
            }),
            "candidate_binding": (
                "unique" if len(local_matches) == 1
                else "missing" if not local_matches else "ambiguous"
            ),
            "local_score": (
                evidence_row.get("local_score") if evidence_row else None
            ),
            "local_reason": (
                (evidence_row.get("facts") or {}).get("local_reason")
                if evidence_row else None
            ),
            "reason_codes": list(
                (evidence_row.get("facts") or {}).get("reason_codes") or []
            ) if evidence_row else [],
            "score_rule_id": producer_score_field("score_rule_id"),
            "score_formula": producer_score_field("score_formula"),
            "score_inputs": producer_score_field("score_inputs"),
            "score_components": producer_score_field("score_components"),
            "model_score": (
                model_matches[0].get("score")
                if len(model_matches) == 1 else None
            ),
            "model_confidence": advice.get("confidence"),
            "model_evidence_status": str(
                advice.get("status") or "not_consulted"
            ),
            "model_evidence_reason": str(
                advice.get("reason")
                or advice.get("error_class")
                or (
                    "exact_model_ranking_bound"
                    if len(model_matches) == 1 else
                    "no_exact_model_ranking_for_typed_candidate"
                )
            ),
            "selected": selected,
            "final_source": (
                "model" if row_model_applied
                else "local" if selected else "not_selected"
            ),
            "override": {
                "applied": row_model_applied,
                "gate_passed": bool(
                    row_model_applied and (
                        advice.get("override_gate_passed")
                        or advice.get("gate_passed")
                        or model_applied
                    )
                ),
                "status": str(advice.get("status") or "not_applied"),
            },
            "producer_candidate_raw": (
                _audit_clone(evidence_row.get("raw_candidate"))
                if evidence_row else None
            ),
            "producer_consequence_raw": split_claim["raw"],
            "producer_consequence_claim": split_claim["claim"],
            "producer_scoring_facts": split_claim["scoring"],
            "unclassified_producer_fields": split_claim["unclassified"],
            "consequences": consequence,
            "probability_outcomes": list(
                consequence.get("probabilistic_outcomes") or []
            ),
            "uncertainty": "; ".join(uncertainty) if uncertainty else None,
        })
        facts = evidence_row.get("facts") if evidence_row else {}
        choice["selection_eligible"] = (
            facts.get("selection_eligible") is not False
        )
        choice["veto_reason"] = facts.get("veto_reason")
    return choices


def observable_inventory_snapshot(game):
    game = game if isinstance(game, dict) else {}
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    player = combat.get("player")
    player = player if isinstance(player, dict) else {}
    block = game.get("block")
    if block is None:
        block = player.get("block")
    if block is None and game.get("room_phase") != "COMBAT":
        # Block is combat-scoped in Slay the Spire.  The protocol omits a
        # combat player outside combat, which authoritatively means that the
        # observable non-combat value is zero rather than unknown.
        block = 0
    return {
        "current_hp": game.get("current_hp"),
        "max_hp": game.get("max_hp"),
        "gold": game.get("gold"),
        "block": block,
        "deck": [compact_audit_value(card) for card in game.get("deck") or []],
        "relics": [
            {
                "id": relic.get("id"),
                "name": relic.get("name"),
                "counter": relic.get("counter"),
                "tier": relic.get("tier"),
            }
            for relic in game.get("relics") or []
        ],
        "potions": [compact_audit_value(potion) for potion in game.get("potions") or []],
        "keys": {
            "ruby": game.get("has_ruby_key"),
            "emerald": game.get("has_emerald_key"),
            "sapphire": game.get("has_sapphire_key"),
        },
    }


def observable_inventory_delta(before, after):
    def identity(item, fallback):
        return str(
            item.get("card_instance_id")
            or item.get("potion_instance_id")
            or item.get("id")
            or fallback
        )

    result = {}
    for field in ("current_hp", "max_hp", "gold", "block"):
        left, right = before.get(field), after.get(field)
        result[f"{field}_delta"] = (
            None if left is None or right is None else int(right) - int(left)
        )
    for field in ("deck", "relics", "potions"):
        left = {identity(item, index): item for index, item in enumerate(before.get(field) or [])}
        right = {identity(item, index): item for index, item in enumerate(after.get(field) or [])}
        result[field] = {
            "added": [right[key] for key in sorted(right.keys() - left.keys())],
            "removed": [left[key] for key in sorted(left.keys() - right.keys())],
            "changed": [
                {"before": left[key], "after": right[key]}
                for key in sorted(left.keys() & right.keys())
                if left[key] != right[key]
            ],
        }
    result["keys_before"] = before.get("keys") or {}
    result["keys_after"] = after.get("keys") or {}
    return result


_COMBAT_CHOICE_SURFACE_PHASES = {"GRID", "CARD_REWARD", "HAND_SELECT"}
_COMBAT_CARD_PILES = (
    "hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo",
)


def _card_instance_id_claim(cards, *, source_present=None):
    """Preserve cardinality while exposing every protocol UUID verbatim."""

    observed_list = isinstance(cards, list)
    cards = cards if observed_list else []
    return {
        "source_present": (
            observed_list if source_present is None
            else bool(source_present)
        ),
        "count": len(cards),
        "card_instance_ids": [
            card.get("card_instance_id") if isinstance(card, dict) else None
            for card in cards
        ],
    }


def combat_choice_observable_snapshot(state):
    """Bound protocol facts needed to replay an in-combat choice overlay."""

    state = state if isinstance(state, dict) else {}
    game = state.get("game_state")
    game = game if isinstance(game, dict) else {}
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else None
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    phase = str(state.get("phase") or "").upper()

    selected_field = None
    selected_cards = []
    for field in ("selected_cards", "selected"):
        if isinstance(screen.get(field), list):
            selected_field = field
            selected_cards = screen[field]
            break
    visible_field = None
    visible_cards = []
    for field in ("cards", "hand"):
        if isinstance(screen.get(field), list):
            visible_field = field
            visible_cards = screen[field]
            break

    piles = {}
    for field in _COMBAT_CARD_PILES:
        piles[field] = (
            _card_instance_id_claim(
                combat.get(field),
                source_present=(
                    field in combat and isinstance(combat.get(field), list)
                ),
            )
            if isinstance(combat, dict) else None
        )
    return {
        "state_seq": state.get("state_seq"),
        "phase": phase,
        "room_phase": game.get("room_phase"),
        "screen_type": game.get("screen_type"),
        "current_action": game.get("current_action"),
        "combat_state_present": isinstance(combat, dict),
        "screen_selection": {
            "source_field": selected_field,
            **_card_instance_id_claim(
                selected_cards, source_present=selected_field is not None
            ),
        },
        "screen_visible_cards": {
            "source_field": visible_field,
            **_card_instance_id_claim(
                visible_cards, source_present=visible_field is not None
            ),
        },
        "piles": piles,
    }


def combat_choice_transition_claim(before, after):
    """Return an independent before/after claim for a combat choice screen."""

    before = before if isinstance(before, dict) else {}
    before_game = before.get("game_state")
    before_game = before_game if isinstance(before_game, dict) else {}
    if (
        str(before.get("phase") or "").upper()
        not in _COMBAT_CHOICE_SURFACE_PHASES
        or str(before_game.get("room_phase") or "").upper() != "COMBAT"
    ):
        return None
    return {
        "schema_version": 1,
        "authority": "authoritative_protocol_before_after",
        "before": combat_choice_observable_snapshot(before),
        "after": combat_choice_observable_snapshot(after),
    }


def selected_shop_observable_acquisition(state, payload, inventory_delta):
    """Bind one shop target to an independently observed inventory gain.

    The producer score/reason is deliberately absent from this path.  A
    benefit is emitted only when the exact selected protocol option names a
    typed shop item and the authoritative before/after inventory delta has
    exactly one matching acquisition.  Missing or ambiguous evidence remains
    absent so downstream review stays fail-closed.
    """

    if (
        str((state or {}).get("phase") or "").upper() != "SHOP_SCREEN"
        or str((payload or {}).get("action") or "").casefold() != "choose"
    ):
        return None
    option_id = (payload or {}).get("option_id")
    matches = [
        option for option in (state or {}).get("options") or []
        if isinstance(option, dict)
        and option.get("option_id") == option_id
    ]
    if len(matches) != 1:
        return None
    target = matches[0].get("target")
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "").casefold()
    if kind not in {"card", "relic", "potion"}:
        return None
    item = target.get("item")
    item = item if isinstance(item, dict) else {}
    collection = {
        "card": "deck", "relic": "relics", "potion": "potions",
    }[kind]
    observed = (inventory_delta or {}).get(collection)
    added = observed.get("added") if isinstance(observed, dict) else None
    if not isinstance(added, list):
        return None

    expected_ids = {
        _normalized_game_id(item.get(field))
        for field in ("id", "card_id", "relic_id", "potion_id", "name")
        if item.get(field) is not None
    }
    expected_ids.discard("")
    if not expected_ids:
        return None

    def item_ids(value):
        return {
            _normalized_game_id(value.get(field))
            for field in (
                "id", "card_id", "relic_id", "potion_id", "name"
            )
            if isinstance(value, dict) and value.get(field) is not None
        } - {""}

    matching_added = [
        value for value in added
        if isinstance(value, dict) and expected_ids & item_ids(value)
    ]
    if len(matching_added) != 1:
        return None
    observed_item = matching_added[0]

    # Card UUIDs are stable across the shop-to-deck transition when both
    # surfaces expose them.  A disagreement is stronger evidence than the
    # shared card name and must block the acquisition proof.  Potion instance
    # IDs are slot/surface annotations and are therefore reported when
    # observed, but not required to equal the listing annotation.
    expected_card_instance = item.get("card_instance_id")
    observed_card_instance = observed_item.get("card_instance_id")
    if (
        kind == "card"
        and expected_card_instance is not None
        and observed_card_instance is not None
        and str(expected_card_instance) != str(observed_card_instance)
    ):
        return None

    observed_id = (
        observed_item.get("id")
        or observed_item.get(f"{kind}_id")
        or item.get("id")
    )
    benefit = {"kind": kind, "id": observed_id}
    benefit[{"card": "card_id", "relic": "relic_id", "potion": "potion_id"}[kind]] = observed_id
    instance_field = {
        "card": "card_instance_id",
        "relic": None,
        "potion": "potion_instance_id",
    }[kind]
    if instance_field and observed_item.get(instance_field) is not None:
        benefit[instance_field] = observed_item.get(instance_field)
    return benefit


def selected_event_card_package_acquisition(state, payload, inventory_delta):
    """Bind Ghosts' Apparition package to the authoritative deck delta."""

    if (
        str((state or {}).get("phase") or "").upper() != "EVENT"
        or str((payload or {}).get("action") or "").casefold() != "choose"
    ):
        return None
    option_id = (payload or {}).get("option_id")
    matches = [
        option for option in (state or {}).get("options") or []
        if isinstance(option, dict) and option.get("option_id") == option_id
    ]
    if len(matches) != 1:
        return None
    target = matches[0].get("target")
    target = target if isinstance(target, dict) else {}
    if (
        str(target.get("kind") or "").casefold() != "event_option"
        or _normalized_game_id(target.get("event_id")) != "ghosts"
        or target.get("original_button_index") != 0
    ):
        return None
    card = target.get("card")
    card = card if isinstance(card, dict) else {}
    card_id = card.get("id") or card.get("card_id")
    if not card_id:
        return None
    game = _state_game(state or {})
    expected_count = 3 if int(game.get("ascension_level") or 0) >= 15 else 5
    deck_delta = (inventory_delta or {}).get("deck")
    added = deck_delta.get("added") if isinstance(deck_delta, dict) else None
    removed = deck_delta.get("removed") if isinstance(deck_delta, dict) else None
    changed = deck_delta.get("changed") if isinstance(deck_delta, dict) else None
    if (
        not isinstance(added, list)
        or len(added) != expected_count
        or (removed or [])
        or (changed or [])
        or any(
            _normalized_game_id(item.get("id"))
            != _normalized_game_id(card_id)
            for item in added if isinstance(item, dict)
        )
        or not all(isinstance(item, dict) for item in added)
    ):
        return None
    return {
        "kind": "card_package", "id": card_id,
        "card_id": card_id, "count": expected_count,
    }


def _bounded_power_snapshot(power):
    power = power if isinstance(power, dict) else {}
    snapshot = {
        "id": power.get("id"),
        "name": power.get("name"),
        "amount": power.get("amount"),
    }
    # CommunicationMod exposes StasisPower's private held card.  Preserve its
    # exact protocol UUID so the independent oracle can bind removal from
    # draw/discard/limbo and the later return without trusting planner output.
    if isinstance(power.get("card"), dict):
        snapshot["card"] = compact_audit_value(power["card"])
    return snapshot


def _bounded_orb_snapshot(orb):
    orb = orb if isinstance(orb, dict) else {}
    return {
        "id": orb.get("id"),
        "name": orb.get("name"),
        "passive_amount": orb.get("passive_amount"),
        "evoke_amount": orb.get("evoke_amount"),
    }


def _bounded_relic_snapshot(relic):
    relic = relic if isinstance(relic, dict) else {}
    return {
        "id": relic.get("id"),
        "name": relic.get("name"),
        "tier": relic.get("tier"),
        "counter": relic.get("counter"),
    }


def _bounded_potion_snapshot(potion):
    potion = potion if isinstance(potion, dict) else {}
    return {
        **compact_audit_value(potion),
        "can_use": potion.get("can_use"),
        "requires_target": potion.get("requires_target"),
    }


def _bounded_player_snapshot(player, game):
    player = player if isinstance(player, dict) else {}
    game = game if isinstance(game, dict) else {}
    return {
        "current_hp": player.get("current_hp", game.get("current_hp")),
        "max_hp": player.get("max_hp", game.get("max_hp")),
        "block": player.get("block"),
        "energy": player.get("energy"),
        **{key: player[key] for key in ("max_orbs", "orb_slots", "facing_left")
           if key in player},
        "powers": [
            _bounded_power_snapshot(power)
            for power in player.get("powers") or []
        ],
        "orbs": [
            _bounded_orb_snapshot(orb)
            for orb in player.get("orbs") or []
        ],
    }


def _bounded_monster_snapshot(monster):
    monster = monster if isinstance(monster, dict) else {}
    return {
        "enemy_instance_id": monster.get("enemy_instance_id"),
        "id": monster.get("id"),
        "name": monster.get("name"),
        "monster_index": monster.get("monster_index"),
        "current_hp": monster.get("current_hp"),
        "max_hp": monster.get("max_hp"),
        "block": monster.get("block"),
        "intent": monster.get("intent"),
        **{key: monster[key] for key in (
            "move_id", "last_move_id", "second_last_move_id",
            "move_base_damage", "is_escaping", "miscBool", "miscInt",
        ) if key in monster},
        "move_adjusted_damage": monster.get("move_adjusted_damage"),
        "move_hits": monster.get("move_hits"),
        "is_gone": monster.get("is_gone"),
        "half_dead": monster.get("half_dead"),
        "powers": [
            _bounded_power_snapshot(power)
            for power in monster.get("powers") or []
        ],
    }


def _bounded_event_option_snapshot(option):
    """Keep only typed event identity/effect evidence, never display prose."""

    if not isinstance(option, dict):
        return None
    bounded = {}
    for field in (
        "disabled", "choice_index", "original_button_index",
        "mechanism_id",
    ):
        if field in option:
            bounded[field] = option.get(field)
    if "card" in option:
        bounded["card"] = compact_audit_value(option.get("card"))
    for field in ("event_contract", "neow_contract"):
        if field in option:
            bounded[field] = _compact_typed_contract(option.get(field))
    return bounded


def authoritative_state_snapshot(state):
    """Return a bounded, protocol-derived snapshot for independent replay.

    The snapshot deliberately excludes the map graph, descriptions and other
    potentially large presentation fields.  Every value comes from the exact
    protocol frame identified by ``state_seq``; no planner projection is
    mixed into this evidence envelope.
    """

    state = state if isinstance(state, dict) else {}
    game = state.get("game_state")
    game = game if isinstance(game, dict) else {}
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else None
    player = (combat or {}).get("player")
    player = player if isinstance(player, dict) else {}
    observable = observable_inventory_snapshot(game)
    game_snapshot = {
        "class": game.get("class"),
        "ascension_level": game.get("ascension_level"),
        "seed": game.get("seed"),
        "act": game.get("act"),
        "floor": game.get("floor"),
        "current_hp": game.get("current_hp"),
        "max_hp": game.get("max_hp"),
        "block": observable.get("block"),
        "gold": game.get("gold"),
        "room_phase": game.get("room_phase"),
        "room_type": game.get("room_type"),
        "screen_type": game.get("screen_type"),
        "screen_name": game.get("screen_name"),
        "act_boss": game.get("act_boss"),
        "current_action": game.get("current_action"),
        "deck": [
            compact_audit_value(card) for card in game.get("deck") or []
        ],
        "relics": [
            _bounded_relic_snapshot(relic)
            for relic in game.get("relics") or []
        ],
        "potions": [
            _bounded_potion_snapshot(potion)
            for potion in game.get("potions") or []
        ],
        "has_ruby_key": game.get("has_ruby_key"),
        "has_emerald_key": game.get("has_emerald_key"),
        "has_sapphire_key": game.get("has_sapphire_key"),
        "keys": dict(observable.get("keys") or {}),
    }
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    phase = str(state.get("phase") or "").upper()
    if phase == "GRID":
        # Preserve only the facts required to independently recover the exact
        # operation, UUID binding, and multi-select commit boundary.
        # Presentation text and container order are deliberately irrelevant.
        game_snapshot["screen_state"] = {
            "for_upgrade": screen.get("for_upgrade"),
            "for_transform": screen.get("for_transform"),
            "for_purge": screen.get("for_purge"),
            "cards": [
                compact_audit_value(card) for card in screen.get("cards") or []
            ],
            "selected_cards": [
                compact_audit_value(card)
                for card in screen.get("selected_cards") or []
            ],
            "num_cards": screen.get("num_cards"),
            "any_number": screen.get("any_number"),
            "confirm_up": screen.get("confirm_up"),
            "parent_choice_context": compact_audit_value(
                screen.get("parent_choice_context") or {}
            ),
        }
    elif phase == "SHOP_SCREEN":
        game_snapshot["screen_state"] = {
            "purge_available": screen.get("purge_available"),
            "purge_cost": screen.get("purge_cost"),
        }
    elif phase == "CARD_REWARD":
        # Busted Crown changes the actual rewardGroup size.  Preserve the raw
        # screen cards independently of producer candidate construction; bowl
        # and skip remain separate controls and never count as cards.
        game_snapshot["screen_state"] = {
            "cards": [
                compact_audit_value(card) for card in screen.get("cards") or []
            ],
            "bowl_available": screen.get("bowl_available"),
            "skip_available": screen.get("skip_available"),
        }
    elif phase == "HAND_SELECT":
        game_snapshot["screen_state"] = {
            "hand": [
                compact_audit_value(card) for card in screen.get("hand") or []
            ],
            "selected": [
                compact_audit_value(card)
                for card in screen.get("selected") or []
            ],
            "max_cards": screen.get("max_cards"),
            "can_pick_zero": screen.get("can_pick_zero"),
        }
    elif phase == "EVENT":
        raw_options = screen.get("options")
        game_snapshot["screen_state"] = {
            "event_id": screen.get("event_id"),
            # Progress-bound base-game events reuse button indices across
            # multiple dialogs.  These bounded scalar facts are required to
            # distinguish (for example) Masked Bandits' initial pay/fight
            # screen from its terminal Continue screen.  They originate in
            # the same authoritative protocol frame as the options and let
            # the independent oracle verify the typed option projection.
            "event_class": screen.get("event_class"),
            "event_stage": screen.get("event_stage"),
            "screen_num": screen.get("screen_num"),
            # A missing/non-list surface remains None so an independent
            # consumer cannot mistake absent evidence for zero legal buttons.
            "options": (
                [
                    _bounded_event_option_snapshot(option)
                    for option in raw_options
                ]
                if isinstance(raw_options, list) else None
            ),
        }
    if combat is not None:
        game_snapshot["combat_state"] = {
            "turn": combat.get("turn"),
            **({"card_in_play": compact_audit_value(combat["card_in_play"])}
               if isinstance(combat.get("card_in_play"), dict) else {}),
            **{key: combat[key] for key in (
                "draw_pile_order_known",
                "cards_played_this_turn", "attacks_played_this_turn",
                "skills_played_this_turn", "powers_played_this_combat",
                "cards_discarded_this_turn", "times_damaged",
                "lightning_channeled", "frost_channeled",
                "emotion_chip_pending", "centennial_puzzle_used_this_combat",
            ) if key in combat},
            "player": _bounded_player_snapshot(player, game),
            "monsters": [
                _bounded_monster_snapshot(monster)
                for monster in combat.get("monsters") or []
            ],
            "hand": [
                compact_audit_value(card) for card in combat.get("hand") or []
            ],
            "draw_pile": [
                compact_audit_value(card)
                for card in combat.get("draw_pile") or []
            ],
            "discard_pile": [
                compact_audit_value(card)
                for card in combat.get("discard_pile") or []
            ],
            "exhaust_pile": [
                compact_audit_value(card)
                for card in combat.get("exhaust_pile") or []
            ],
            "limbo": [
                compact_audit_value(card) for card in combat.get("limbo") or []
            ],
        }
    return {
        "protocol_version": state.get("protocol_version"),
        "attempt_id": state.get("attempt_id"),
        "run_id": state.get("run_id"),
        "seed": state.get("seed"),
        "character": state.get("character"),
        "ascension_level": state.get("ascension_level"),
        "run_type": state.get("run_type"),
        "decision_hash": state.get("decision_hash"),
        "controller_hash": state.get("controller_hash"),
        "policy_version": state.get("policy_version"),
        "selection_id": state.get("selection_id"),
        "selection_digest": state.get("selection_digest"),
        "state_seq": state.get("state_seq"),
        "phase": state.get("phase"),
        "decision_id": state.get("decision_id"),
        "ready_for_command": state.get("ready_for_command"),
        "available_commands": list(state.get("available_commands") or []),
        "game_state": game_snapshot,
    }


def _settlement_observed_outcome(decision_outcome):
    decision_outcome = (
        decision_outcome if isinstance(decision_outcome, dict) else {}
    )
    fields = (
        "current_hp_delta", "hp_delta", "max_hp_delta", "gold_delta",
        "block_delta", "deck", "relics", "potions", "keys_before",
        "keys_after", "phase_before", "phase_after", "room_phase_before",
        "room_phase_after", "room_type_before", "room_type_after",
        "screen_type_before", "screen_type_after", "act_before",
        "act_after", "floor_before", "floor_after",
        "combat_choice_transition",
    )
    return {
        field: decision_outcome[field]
        for field in fields
        if field in decision_outcome
    }


def _settlement_probability_match(outcomes, observed):
    """Return the uniquely observed exhaustive probability branch, if any.

    This intentionally supports only directly observable numeric deltas.  A
    curse, transform, delayed effect or partially described branch is not
    upgraded to authoritative evidence merely because one visible number
    happens to match.
    """

    if not isinstance(outcomes, list) or len(outcomes) < 2:
        return None
    for field in ("deck", "relics", "potions"):
        delta = observed.get(field)
        if not isinstance(delta, dict) or any(
            delta.get(kind) for kind in ("added", "removed", "changed")
        ):
            # Numeric-only outcome rows cannot explain an inventory change.
            return None
    if observed.get("keys_before") != observed.get("keys_after"):
        return None
    numeric_fields = {
        "current_hp_delta": ("current_hp_delta", "hp_delta"),
        "max_hp_delta": ("max_hp_delta",),
        "gold_delta": ("gold_delta",),
        "block_delta": ("block_delta",),
    }
    metadata_fields = {
        "probability", "label", "id", "name", "description", "raw_text",
    }
    probability_total = 0.0
    matches = []
    for index, outcome in enumerate(outcomes):
        if not isinstance(outcome, dict):
            return None
        probability = outcome.get("probability")
        if (
            not isinstance(probability, (int, float))
            or isinstance(probability, bool)
            or probability < 0
            or probability > 1
        ):
            return None
        probability_total += float(probability)
        allowed_effect_fields = {
            alias for aliases in numeric_fields.values() for alias in aliases
        }
        effect_fields = set(outcome) - metadata_fields
        if not effect_fields or not effect_fields <= allowed_effect_fields:
            return None
        matched = True
        for observed_field, aliases in numeric_fields.items():
            claims = [outcome[alias] for alias in aliases if alias in outcome]
            if claims and any(claim != claims[0] for claim in claims[1:]):
                return None
            expected = claims[0] if claims else 0
            actual = observed.get(observed_field)
            if (
                not isinstance(expected, (int, float))
                or isinstance(expected, bool)
                or not isinstance(actual, (int, float))
                or isinstance(actual, bool)
                or float(expected) != float(actual)
            ):
                matched = False
                break
        if matched:
            matches.append(index)
    if abs(probability_total - 1.0) > 1e-9 or len(matches) != 1:
        return None
    return matches[0]


def authoritative_choice_settlement(record):
    """Describe what the immediate protocol delta proves about one choice."""

    record = record if isinstance(record, dict) else {}
    selected = [
        row for row in record.get("legal_choices_before") or []
        if isinstance(row, dict) and row.get("selected") is True
    ]
    selected_id = (
        str(selected[0].get("choice_id"))
        if len(selected) == 1 and selected[0].get("choice_id") is not None
        else None
    )
    selected_target_id = selected_id
    if len(selected) == 1 and selected[0].get("action") == "potion":
        selected_target_id = (
            (selected[0].get("target") or {}).get("potion_instance_id")
        )
    observed = _settlement_observed_outcome(record.get("decision_outcome"))
    settlement = {
        "status": "unresolved",
        "authority": "protocol_state_delta",
        "fully_observable": False,
        "choice_id": selected_id,
        "before_seq": record.get("before_seq"),
        "after_seq": record.get("after_seq"),
        "observed_outcome": observed,
    }
    if len(selected) != 1:
        settlement["reason"] = "selected_choice_not_unique"
        return settlement
    if not (
        type(record.get("before_seq")) is int
        and type(record.get("after_seq")) is int
        and record["after_seq"] > record["before_seq"]
    ):
        settlement["reason"] = "authoritative_state_sequence_not_advanced"
        return settlement
    if (
        str(record.get("requested_target_id") or "")
        != str(selected_target_id or "")
        or str(record.get("resolved_target_id") or "")
        != str(selected_target_id or "")
    ):
        settlement["reason"] = "choice_receipt_binding_unproven"
        return settlement
    if (
        len(selected) == 1
        and selected[0].get("action") == "potion"
        and str(record.get("potion_operation") or "").casefold()
        != str(selected[0].get("operation") or "").casefold()
    ):
        settlement["reason"] = "potion_operation_binding_unproven"
        return settlement
    required_deltas = (
        "current_hp_delta", "max_hp_delta", "gold_delta", "block_delta",
        "deck", "relics", "potions", "keys_before", "keys_after",
    )
    if any(field not in observed or observed[field] is None for field in required_deltas):
        settlement["reason"] = "observable_protocol_delta_incomplete"
        return settlement

    choice = selected[0]
    consequences = choice.get("consequences")
    consequences = consequences if isinstance(consequences, dict) else {}
    probability_outcomes = choice.get("probability_outcomes")
    if not isinstance(probability_outcomes, list):
        probability_outcomes = consequences.get("probabilistic_outcomes")
    probability_outcomes = (
        probability_outcomes if isinstance(probability_outcomes, list) else []
    )
    uncertainty = choice.get("uncertainty") or consequences.get("uncertainty")
    future_costs = consequences.get("future_costs")
    curse = consequences.get("curse")
    curse = curse if isinstance(curse, dict) else {}
    curse_probability = curse.get("probability")
    if future_costs is not None and not isinstance(future_costs, list):
        settlement["reason"] = "future_costs_not_structured"
        return settlement
    if isinstance(future_costs, list) and any(
        not isinstance(item, dict) for item in future_costs
    ):
        settlement["reason"] = "future_costs_not_structured"
        return settlement
    if future_costs:
        classified_future = all(
            isinstance(item, dict)
            and (
                item.get("kind") == "foregone_visible_option_ids"
                and isinstance(item.get("choice_ids"), list)
                and all(isinstance(value, str) for value in item["choice_ids"])
                or item.get("kind") == "deferred_colorless_card_reward_choices"
                and type(item.get("count")) is int
                and item["count"] > 0
                or item.get("kind") == "shop_purge_grid_selection"
                and item.get("operation") == "purge"
                and item.get("select_count") == 1
                and item.get("identity_binding")
                == "subsequent_grid_card_instance_id"
                and item.get("commit_timing") == "after_grid_confirmation"
                and type(item.get("gold_cost")) is int
                and item.get("gold_cost") >= 0
                or item.get("kind") == "cleric_grid_selection"
                and item.get("operation") == "grid_purge"
                and item.get("select_count") == 1
                and item.get("identity_binding")
                == "subsequent_grid_card_instance_id"
                and item.get("commit_timing") == "after_grid_confirmation"
                or item.get("kind") == "designer_grid_selection"
                and item.get("operation") == "grid_purge"
                and item.get("select_count") == 1
                and item.get("identity_binding")
                == "subsequent_grid_card_instance_id"
                and item.get("commit_timing") == "after_grid_confirmation"
                or item.get("kind") == "designer_random_card_upgrade"
                and item.get("operation") == "random_upgrade"
                and (
                    item.get("count") == 1
                    or (
                        item.get("max_count") == 1
                        and item.get("count_semantics") == "up_to_available"
                    )
                )
                and item.get("domain") == "post_purge_upgradable_deck"
                and item.get("selection_mode") == "random"
                and item.get("commit_timing") == "after_grid_confirmation"
                or item.get("kind") == "knowing_skull_exit_reserve"
                and type(item.get("reserved_hp_loss")) is int
                and item.get("reserved_hp_loss") >= 0
                and item.get("commit_timing") == "when_leaving_event"
                or item.get("kind") == "grid_confirmation_effect"
                and item.get("operation") in {
                    "grid_upgrade", "grid_transform", "grid_purge",
                    "grid_duplicate",
                }
                and isinstance(item.get("selected_card"), dict)
                and bool(
                    item["selected_card"].get("card_instance_id")
                    or item["selected_card"].get("uuid")
                )
                and item.get("commit_timing") == "after_grid_confirmation"
                or item in _NEOW_FUTURE_REWARDS.values()
            )
            for item in future_costs
        )
        if classified_future:
            settlement["classified_future_costs"] = _audit_clone(
                future_costs
            )
        else:
            settlement["reason"] = "delayed_consequence_not_settled"
            return settlement
    if (
        not probability_outcomes
        and isinstance(curse_probability, (int, float))
        and not isinstance(curse_probability, bool)
        and 0 < float(curse_probability) < 1
    ):
        settlement["reason"] = "probabilistic_curse_outcome_unstructured"
        return settlement
    if probability_outcomes:
        matched_index = _settlement_probability_match(
            probability_outcomes, observed
        )
        if matched_index is None:
            settlement["reason"] = "probabilistic_outcome_not_uniquely_proven"
            return settlement
        settlement["matched_probability_outcome_index"] = matched_index
    elif uncertainty:
        # Some choices are immediately and exactly settled while explicitly
        # declaring irreducible *future* uncertainty (route outcomes, future
        # card value, or a protocol-hidden Match card).  That declaration is
        # not missing evidence.  Arbitrary event uncertainty remains blocked
        # unless it has exhaustive probability branches above.
        phase = str(record.get("phase") or "").upper()
        target = choice.get("target")
        target = target if isinstance(target, dict) else {}
        target_kind = str(target.get("kind") or "").casefold()
        event_id = _normalized_game_id(target.get("event_id"))
        potion_operation = str(
            choice.get("operation") or target.get("operation") or ""
        ).casefold()
        neow_contract, _neow_error = _validated_neow_contract(
            target, record.get("authoritative_state_before") or {}
        )
        classified_future_uncertainty = bool(
            phase in {
                "BOSS_REWARD", "CARD_REWARD", "GRID", "HAND_SELECT",
                "MAP", "NEOW", "REST", "SAPPHIRE_KEY", "SHOP_ROOM",
                "SHOP_SCREEN",
            }
            or (
                phase == "EVENT"
                and event_id == "sensorystone"
                and consequences.get("future_costs")
                and all(
                    isinstance(item, dict)
                    and item.get("kind")
                    == "deferred_colorless_card_reward_choices"
                    and type(item.get("count")) is int
                    and item["count"] > 0
                    for item in consequences["future_costs"]
                )
            )
            or (
                phase == "EVENT"
                and event_id in {"thecleric", "designer"}
                and consequences.get("future_costs")
                and all(
                    isinstance(item, dict)
                    and item.get("kind") in {
                        "cleric_grid_selection",
                        "designer_grid_selection",
                        "designer_random_card_upgrade",
                    }
                    for item in consequences["future_costs"]
                )
            )
            or (
                phase == "EVENT"
                and event_id == "knowingskull"
                and consequences.get("future_costs")
                and all(
                    isinstance(item, dict)
                    and item.get("kind") == "knowing_skull_exit_reserve"
                    and type(item.get("reserved_hp_loss")) is int
                    and item.get("reserved_hp_loss") >= 0
                    and item.get("commit_timing") == "when_leaving_event"
                    for item in consequences["future_costs"]
                )
            )
            or (
                target_kind == "potion_resource"
                and potion_operation in {"use", "discard"}
                and target.get("potion_instance_id") is not None
            )
            or (
                phase == "NEOW"
                and neow_contract is not None
            )
            or (
                str(selected_id or "").startswith("match:")
                and consequences.get("operation")
                == "flip_match_position"
            )
        )
        if not classified_future_uncertainty:
            settlement["reason"] = "unstructured_or_multistage_uncertainty"
            return settlement
        settlement["classified_future_uncertainty"] = compact_audit_value(
            uncertainty
        )

    settlement.update({
        "status": "observed",
        "fully_observable": True,
        "reason": (
            "probabilistic_outcome_uniquely_observed"
            if probability_outcomes
            else "immediate_delta_observed_future_uncertainty_classified"
            if uncertainty
            else "deterministic_choice_delta_observed"
        ),
        "observation_scope": "immediate_protocol_transition",
    })
    return settlement


def combat_post_action_context(before_game, after_game, action=None):
    """Return observable player/enemy deltas after one bound combat action.

    A lethal play can move the bridge directly from COMBAT to a reward/event
    screen.  In that transition the defeated monster is no longer serialized,
    so matching by ``enemy_instance_id`` would incorrectly report zero
    damage.  For PLAY/POTION actions only, treat every living monster present
    in the before-frame as having final HP zero when combat has ended.  END is
    deliberately excluded because a poison/orb/end-turn tick may be what
    ended the combat and must not be attributed to a hero card.
    """

    before_combat = before_game.get("combat_state") or {}
    after_combat = after_game.get("combat_state") or {}
    after_monsters = {
        monster.get("enemy_instance_id"): monster
        for monster in after_combat.get("monsters") or []
        if monster.get("enemy_instance_id")
    }
    enemy_changes = []
    matched_enemy_count = 0
    total_enemy_hp_loss = 0
    for monster in before_combat.get("monsters") or []:
        enemy_id = monster.get("enemy_instance_id")
        if not enemy_id:
            continue
        after_monster = after_monsters.get(enemy_id)
        before_hp = monster.get("current_hp")
        after_hp = (
            after_monster.get("current_hp")
            if isinstance(after_monster, dict) else None
        )
        hp_loss = None
        inferred_final_hp = (
            action in {"play", "potion"}
            and before_game.get("room_phase") == "COMBAT"
            and after_game.get("room_phase") != "COMBAT"
            and before_hp is not None
            and int(before_hp or 0) > 0
            and (
                not isinstance(after_monster, dict)
                or after_monster.get("is_gone") is True
            )
        )
        if before_hp is not None and after_hp is not None:
            matched_enemy_count += 1
            hp_loss = max(0, int(before_hp) - int(after_hp))
            total_enemy_hp_loss += hp_loss
        elif inferred_final_hp:
            matched_enemy_count += 1
            after_hp = 0
            hp_loss = max(0, int(before_hp))
            total_enemy_hp_loss += hp_loss
        enemy_changes.append({
            "enemy_instance_id": enemy_id,
            "id": monster.get("id"),
            "hp_before": before_hp,
            "hp_after": after_hp,
            "hp_loss": hp_loss,
            "block_before": monster.get("block"),
            "block_after": (
                after_monster.get("block")
                if isinstance(after_monster, dict) else None
            ),
            "is_gone_after": (
                after_monster.get("is_gone")
                if isinstance(after_monster, dict) else None
            ),
            "half_dead_after": (
                after_monster.get("half_dead")
                if isinstance(after_monster, dict) else None
            ),
            "final_hp_inferred": bool(inferred_final_hp),
        })
    hp_before = before_game.get("current_hp")
    hp_after = after_game.get("current_hp")
    hp_delta = (
        None
        if hp_before is None or hp_after is None
        else int(hp_after) - int(hp_before)
    )
    before_enemy_hp_total = sum(
        max(0, int(monster.get("current_hp") or 0))
        for monster in before_combat.get("monsters") or []
        if isinstance(monster, dict)
    )
    after_enemy_hp_total = sum(
        max(0, int(monster.get("current_hp") or 0))
        for monster in after_combat.get("monsters") or []
        if isinstance(monster, dict)
    )
    return {
        # Keep the signed delta as the primary observable.  ``player_hp_loss``
        # remains for compatibility, while the gain is needed to explain an
        # END forecast that is larger than the net loss because a relic,
        # combat transition, or bridge packet restored HP in the same frame.
        "player_hp_delta": hp_delta,
        "player_hp_loss": None if hp_delta is None else max(0, -hp_delta),
        "player_hp_gain": None if hp_delta is None else max(0, hp_delta),
        "enemy_hp_loss": total_enemy_hp_loss,
        "enemy_hp_before_total": before_enemy_hp_total,
        "enemy_hp_after_total": after_enemy_hp_total,
        "matched_enemy_count": matched_enemy_count,
        "observed_enemy_count": len(enemy_changes),
        "enemy_hp_changes": enemy_changes,
    }


def end_turn_resource_context(before_combat, decision):
    """Explain an END action that still had energy/cards available.

    The planner may legitimately pass after a passive kill proof, a card-cap
    boundary, or a no-positive-marginal-action search.  Persist the exact
    reachable cards and the reason so the audit can distinguish those cases
    from an unexplained early END.
    """

    player = before_combat.get("player") or {}
    energy = max(0, int(player.get("energy") or 0))
    monsters = before_combat.get("monsters") or []
    living = [
        monster for monster in monsters
        if isinstance(monster, dict)
        and int(monster.get("current_hp") or 0) > 0
        and not monster.get("is_gone")
        and not monster.get("half_dead")
    ]
    playable = []
    for card in before_combat.get("hand") or []:
        if not isinstance(card, dict) or not card.get("is_playable"):
            continue
        raw_cost = card.get("cost")
        try:
            cost = int(raw_cost)
        except (TypeError, ValueError):
            continue
        effective_cost = energy if cost == -1 else max(0, cost)
        if effective_cost > energy:
            continue
        if card.get("has_target") and not living:
            continue
        playable.append(card)
    decision = decision if isinstance(decision, dict) else {}
    reason = str(decision.get("reason") or "")
    search = decision.get("search") or {}
    if bool(search.get("true_combat_end")) or reason == "all_enemies_passively_doomed":
        safety_class = "safe_terminal_line"
        reason_detail = "searched_terminal_line_or_passive_kill"
    elif reason == "velvet_choker_card_limit":
        safety_class = "safe_card_limit"
        reason_detail = "card_play_limit_reached"
    elif not playable:
        safety_class = "no_legal_resources"
        reason_detail = "no_affordable_playable_card_with_a_live_target"
    elif reason == "no_positive_marginal_action":
        safety_class = "evaluated_no_positive_marginal_action"
        reason_detail = (
            "all_affordable_candidates_failed_the_survival_or_risk_budget"
        )
    elif reason == "no_playable_card_or_target":
        safety_class = "inconsistent_no_playable_reason"
        reason_detail = "planner_reported_no_legal_action"
    else:
        safety_class = "resources_unexplained"
        reason_detail = "planner_reason_not_classified"
    return {
        "energy_before": energy,
        "playable_card_count": len(playable),
        "playable_card_ids": [card.get("id") for card in playable],
        "playable_card_types": [card.get("type") for card in playable],
        "reason": reason,
        "safety_class": safety_class,
        "reason_detail": reason_detail,
    }


def _token(value):
    return "".join(
        character for character in str(value or "").lower() if character.isalnum()
    )


def bind_shop_protocol_surface(game, state):
    """Project the parsed shop onto the exact protocol-visible listings.

    The game screen can retain hidden or unaffordable relic/card objects that
    CommunicationMod correctly omits from ``options``.  The policy used to
    enumerate those container objects and synthesize new indexes, which can
    bind an affordable protocol option to a different item.  Filter and
    annotate the parsed objects from the authoritative option surface before
    the agent scores them.  Ambiguous identity is a protocol error, never a
    reason to fall back to container order.
    """

    if str(state.get("phase") or "") != "SHOP_SCREEN":
        return game
    screen = getattr(game, "screen", None)
    if screen is None:
        raise SafetyError("SHOP_SCREEN missing parsed screen")
    options = state.get("options")
    if not isinstance(options, list):
        raise SafetyError("SHOP_SCREEN missing authoritative options")
    indexed = []
    seen_indexes = set()
    for option in options:
        if not isinstance(option, dict) or type(option.get("choice_index")) is not int:
            raise SafetyError("shop option missing exact choice_index")
        index = option["choice_index"]
        if index in seen_indexes:
            raise SafetyError("shop option choice_index duplicated")
        seen_indexes.add(index)
        indexed.append((index, option))
    indexed.sort(key=lambda row: row[0])

    source = {
        "card": list(getattr(screen, "cards", []) or []),
        "relic": list(getattr(screen, "relics", []) or []),
        "potion": list(getattr(screen, "potions", []) or []),
    }
    bound = {kind: [] for kind in source}
    used = {kind: set() for kind in source}

    def matches(kind, item, facts):
        if kind == "card":
            expected_instance = facts.get("card_instance_id")
            if not expected_instance or getattr(item, "uuid", None) != expected_instance:
                return False
            expected_id = facts.get("id")
            return not expected_id or _token(getattr(item, "card_id", "")) == _token(expected_id)
        if kind == "relic":
            expected_id = facts.get("id")
            if not expected_id or _token(getattr(item, "relic_id", "")) != _token(expected_id):
                return False
        else:
            expected_id = facts.get("id")
            if not expected_id or _token(getattr(item, "potion_id", "")) != _token(expected_id):
                return False
            if facts.get("name") is not None and getattr(item, "name", None) != facts.get("name"):
                return False
            for attribute, field in (
                ("description", "description"),
                ("can_use", "can_use"),
                ("can_discard", "can_discard"),
                ("requires_target", "requires_target"),
            ):
                if field in facts and facts[field] is not None:
                    if getattr(item, attribute, None) != facts[field]:
                        return False
        if facts.get("price") is not None:
            try:
                return int(getattr(item, "price", -1)) == int(facts["price"])
            except (TypeError, ValueError):
                return False
        return True

    def listing_group_key(kind, facts):
        """Return the exact identity used for indistinguishable listings.

        CommunicationMod disambiguates repeated shop descriptors by appending
        an occurrence suffix to ``option_id`` (for example ``:1``).  The
        parsed shop object has no corresponding UUID, so two identical
        protocol descriptors can only be joined by a counted, ordered group.
        Keep the group key limited to fields that ``matches`` verifies; a
        single protocol option for multiple objects remains ambiguous.
        """

        if not isinstance(facts, dict):
            return None
        if kind == "card":
            fields = {
                "id": facts.get("id"),
                "card_instance_id": facts.get("card_instance_id"),
                "price": facts.get("price"),
            }
        elif kind == "relic":
            fields = {
                "id": facts.get("id"),
                "price": facts.get("price"),
            }
        elif kind == "potion":
            fields = {
                field: facts.get(field)
                for field in (
                    "id", "name", "price", "description", "can_use",
                    "can_discard", "requires_target",
                )
            }
        else:
            return None
        return kind, json.dumps(fields, sort_keys=True, ensure_ascii=True)

    option_groups = {}
    for index, option in indexed:
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        kind = str(target.get("kind") or "").casefold()
        if kind not in source:
            continue
        facts = target.get("item")
        key = listing_group_key(kind, facts)
        if key is not None:
            option_groups.setdefault(key, []).append((index, option))

    purge_indexes = []
    for index, option in indexed:
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        kind = str(target.get("kind") or "").casefold()
        if kind == "purge":
            purge_indexes.append(index)
            continue
        if kind not in source:
            raise SafetyError(f"unsupported shop option kind: {kind or '<missing>'}")
        facts = target.get("item")
        facts = facts if isinstance(facts, dict) else {}
        candidates = [
            (position, item)
            for position, item in enumerate(source[kind])
            if position not in used[kind] and matches(kind, item, facts)
        ]
        if len(candidates) != 1:
            key = listing_group_key(kind, facts)
            group = option_groups.get(key) if key is not None else None
            all_group_items = [
                (position, item)
                for position, item in enumerate(source[kind])
                if matches(kind, item, facts)
            ]
            if not group or len(group) != len(all_group_items):
                raise SafetyError(
                    f"shop {kind} option did not bind uniquely at choice_index={index}"
                )
            ordinal = next(
                (position for position, (group_index, _) in enumerate(group)
                 if group_index == index),
                None,
            )
            bound_group_positions = {
                position
                for position, item in enumerate(source[kind])
                if position in used[kind] and matches(kind, item, facts)
            }
            if (
                ordinal is None
                or len(bound_group_positions) != ordinal
                or ordinal >= len(all_group_items)
                or all_group_items[ordinal][0] in used[kind]
            ):
                raise SafetyError(
                    f"shop {kind} repeated listing order is not authoritative "
                    f"at choice_index={index}"
                )
            position, item = all_group_items[ordinal]
        else:
            position, item = candidates[0]
        used[kind].add(position)
        setattr(item, "protocol_choice_index", index)
        setattr(item, "protocol_option_id", option.get("option_id"))
        bound[kind].append(item)
    if len(purge_indexes) > 1:
        raise SafetyError("shop purge option duplicated")
    if purge_indexes:
        setattr(screen, "protocol_purge_choice_index", purge_indexes[0])
    screen.cards = bound["card"]
    screen.relics = bound["relic"]
    screen.potions = bound["potion"]
    return game


_CURRENT_CARD_NON_ATTACK = object()
_CURRENT_ATTACK_UNRESOLVED = object()


def _random_attack_hp_loss_bounds(game, card, targets, hits, repeat_count=1):
    """Return exact aggregate HP-loss bounds for random-target Attacks.

    Sword Boomerang and Rip and Tear expose every packet but not the random
    allocation between living enemies.  Enumerating hit-count compositions
    proves a useful interval without pretending that one particular target
    was selected.  The game has at most a handful of living enemies and these
    cards have few packets, so the complete enumeration is tiny.
    """

    targets = list(targets or [])
    total_hits = max(0, int(hits or 0)) * max(1, int(repeat_count or 1))
    damage_per_hit = max(0, int(getattr(card, "damage", 0) or 0))
    if not targets or total_hits <= 0 or damage_per_hit <= 0:
        return {"minimum": 0, "maximum": 0}
    modifiers = combat_predictor.attack_relic_modifiers(game)
    losses = []

    def visit(target_index, remaining, allocation):
        if target_index == len(targets) - 1:
            counts = allocation + [remaining]
            total = 0
            for monster, count in zip(targets, counts):
                if count <= 0:
                    continue
                total += min(
                    max(0, int(getattr(monster, "current_hp", 0) or 0)),
                    combat_predictor.attack_hp_loss(
                        monster,
                        damage_per_hit * count,
                        hits=count,
                        **modifiers,
                    ),
                )
            losses.append(total)
            return
        for count in range(remaining + 1):
            visit(target_index + 1, remaining - count, allocation + [count])

    visit(0, total_hits, [])
    return {"minimum": min(losses), "maximum": max(losses)}


def _current_card_attack_prediction(
    before_game,
    available_commands,
    card_id=None,
    card_instance_id=None,
    target_id=None,
):
    """Project damage for the card actually confirmed by this action.

    ``FastCombatPlanner`` keeps a terminal search snapshot in ``decision``.
    A terminal line can contain several cards, however, so using that
    snapshot's ``first_action_enemy_hp_loss`` for every continuation silently
    assigns the first (often a Skill with zero damage) to later Attacks.  The
    authoritative pre-action hand is the only safe source for a per-card
    prediction.  Return ``None`` when the card cannot be resolved rather than
    inventing damage for random/unknown effects.
    """

    if not isinstance(before_game, dict) or before_game.get("room_phase") != "COMBAT":
        return None
    if not card_id and not card_instance_id:
        return None
    try:
        parsed = Game.from_json(
            before_game,
            available_commands if isinstance(available_commands, list) else [],
        )
        hand = list(getattr(parsed, "hand", []) or [])
        card = next(
            (
                item
                for item in hand
                if card_instance_id
                and getattr(item, "uuid", None) == card_instance_id
            ),
            None,
        )
        if card is None and card_id:
            wanted = _token(card_id)
            card = next(
                (
                    item
                    for item in hand
                    if _token(getattr(item, "card_id", "")) == wanted
                ),
                None,
            )
        if card is None:
            return _CURRENT_ATTACK_UNRESOLVED
        if str(getattr(getattr(card, "type", None), "name", "")) != "ATTACK":
            return _CURRENT_CARD_NON_ATTACK
        card_token = _token(getattr(card, "card_id", ""))
        if card_token == "fiendfire":
            # Fiend Fire's packet is the number of other cards still in hand
            # when it resolves.  The parsed card's static damage field cannot
            # prove that value; ordered turn search can.
            return _CURRENT_ATTACK_UNRESOLVED

        raw_monsters = ((before_game.get("combat_state") or {}).get("monsters") or [])
        living = list(combat_predictor.living_monsters(parsed))
        if target_id:
            raw_target_index = next(
                (
                    index for index, item in enumerate(raw_monsters)
                    if item.get("enemy_instance_id") == target_id
                ),
                None,
            )
            if raw_target_index is None:
                return None
            # Raw bridge enemy objects do not consistently serialize
            # ``monster_index``.  The action binding is nevertheless based on
            # the exact list slot, so use that slot first and only fall back
            # to the normalized id/index pair for older frames.
            all_monsters = list(getattr(parsed, "monsters", []) or [])
            targets = (
                [all_monsters[raw_target_index]]
                if raw_target_index < len(all_monsters)
                and all_monsters[raw_target_index] in living
                else []
            )
            if not targets:
                raw_target = raw_monsters[raw_target_index]
                target_key = (
                    _token(raw_target.get("id")),
                    int(raw_target.get("monster_index") or 0),
                )
                targets = [
                    monster
                    for monster in living
                    if (
                        _token(getattr(monster, "monster_id", "")),
                        int(getattr(monster, "monster_index", 0) or 0),
                    ) == target_key
                ]
        else:
            # Untargeted Attacks (Whirlwind, Dagger Spray, etc.) resolve on
            # every living enemy in the current authoritative frame.
            targets = living
        if not targets:
            return _CURRENT_ATTACK_UNRESOLVED
        # Some attacks depend on the chosen enemy.  Bane, for example, only
        # queues its second hit when that exact target is already Poisoned as
        # the card starts resolving.  Resolve the protocol target before the
        # profile so telemetry uses the same facts as ordered combat search.
        profile_target = targets[0] if len(targets) == 1 else None
        raw_damage, hits = combat_predictor.card_attack_profile(
            parsed,
            card,
            target=profile_target,
        )
        raw_damage = max(0, int(raw_damage or 0))
        hits = max(0, int(hits or 0))
        random_multi_target = bool(
            not target_id
            and len(targets) > 1
            and card_token in {"swordboomerang", "ripandtear"}
        )
        # A zero-hit Barrage is deterministic.  By contrast, a positive-
        # damage card whose profile returns zero is deliberately signalling
        # unresolved random target allocation (Sword Boomerang with several
        # living enemies).  Never replace that with a stale turn-search
        # prediction.
        if hits <= 0:
            return 0
        if raw_damage <= 0 and not random_multi_target:
            return (
                _CURRENT_ATTACK_UNRESOLVED
                if int(getattr(card, "damage", 0) or 0) > 0
                else 0
            )

        repeat_count = 1
        player = getattr(parsed, "player", None)
        if combat_predictor.power_amount(
            player, "Double Tap", "DoubleTapPower"
        ) > 0:
            repeat_count += 1
        if combat_predictor.power_amount(
            player, "Echo Form", "EchoFormPower"
        ) > 0:
            repeat_count += 1
        if combat_predictor.power_amount(
            player, "Duplication", "DuplicationPower"
        ) > 0:
            # DuplicationPower.amount counts future eligible cards; each
            # eligible card still receives exactly one extra resolution.
            repeat_count += 1
        if repeat_count > 1:
            # Repeated card resolutions are ordered game actions, not one
            # larger damage packet.  The first copy can consume Pen Nib or
            # Vigor, apply Vulnerable/change stance, trigger Curl Up,
            # Malleable or Mode Shift, mutate the card (Rampage/Claw), or
            # enter a deferred selection such as Headbutt.  A static
            # multiplication therefore cannot prove even apparently simple
            # cases.  The caller uses FastCombatPlanner's ordered
            # ``first_action_*`` result whenever a repeat is actually live.
            # A repeated plain attack still cannot be collapsed into one
            # deterministic packet: the first resolution may consume a
            # charge or otherwise mutate combat state.  For the basic Strike
            # family, however, publish a conservative aggregate bound (no
            # damage through all living HP) instead of making the audit
            # incomparable without claiming an exact duplicated packet.
            if card_token in {"strike", "striker"}:
                maximum = sum(
                    max(0, int(getattr(monster, "current_hp", 0) or 0))
                    for monster in targets
                )
                if maximum > 0:
                    return {"minimum": 0, "maximum": maximum}
            return _CURRENT_ATTACK_UNRESOLVED
        if random_multi_target:
            return _random_attack_hp_loss_bounds(
                parsed, card, targets, hits, repeat_count=1,
            )
        # ``attack_hp_loss`` models the ordered block/power interaction, but
        # it intentionally reports uncapped damage for planning.  A trace
        # prediction is compared with the observable HP delta, which can
        # never exceed the target's HP at action start.  Cap every bound
        # target independently so ordinary overkill is not reported as a
        # mechanics contradiction.
        return sum(
            min(
                max(0, int(getattr(monster, "current_hp", 0) or 0)),
                (
                    combat_predictor.card_attack_hp_loss(
                        parsed, card, monster, target=profile_target,
                    )
                    if repeat_count == 1
                    else combat_predictor.attack_hp_loss(
                        monster,
                        raw_damage * repeat_count,
                        hits=hits * repeat_count,
                        **combat_predictor.attack_relic_modifiers(parsed),
                    )
                ),
            )
            for monster in targets
        )
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        # Trace generation must never turn an otherwise confirmed action into
        # an operational failure merely because an older bridge omitted a
        # card/monster field.  Mark the current attack unresolved so the
        # caller cannot substitute a stale turn-search prediction.
        return _CURRENT_ATTACK_UNRESOLVED


def _exact_postcombat_hp_loss(before_game, outcome):
    """Reconstruct gross HP loss hidden by deterministic settlement healing.

    Stable protocol frames expose only the net HP delta.  End-turn
    Regeneration plus Burning Blood can therefore turn two points of incoming
    damage into a visible +5 delta.  This helper is deliberately narrow:
    capped healing or any unresolved end-of-combat healer remains unknown
    rather than being guessed.
    """

    if not isinstance(before_game, dict) or not isinstance(outcome, dict):
        return None
    room_phase_after = str(outcome.get("room_phase_after") or "").upper()
    phase_after = str(outcome.get("phase_after") or "").upper()
    if (
        room_phase_after == "COMBAT"
        or phase_after == "COMBAT"
        or phase_after.startswith("COMBAT_TURN_")
    ):
        return None
    if str(outcome.get("screen_type_after") or "").upper() == "GAME_OVER":
        return None
    before_hp = before_game.get("current_hp")
    max_hp = before_game.get("max_hp")
    net_delta = outcome.get("player_hp_delta")
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (before_hp, max_hp, net_delta)
    ):
        return None
    after_hp = before_hp + net_delta
    if before_hp <= 0 or max_hp <= 0 or after_hp <= 0 or after_hp >= max_hp:
        # At the cap, several different gross-loss values have the same
        # stable after frame and cannot be independently distinguished.
        return None
    relic_ids = {
        _token(relic.get("id") or relic.get("name"))
        for relic in before_game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "mark of the bloom" in relic_ids or "markofthebloom" in relic_ids:
        return None
    has_meat_on_the_bone = bool(
        {"meat on the bone", "meatonthebone"} & relic_ids
    )
    if "black blood" in relic_ids or "blackblood" in relic_ids:
        healing = 12
    elif "burning blood" in relic_ids or "burningblood" in relic_ids:
        healing = 6
    else:
        return None
    player = ((before_game.get("combat_state") or {}).get("player") or {})
    powers = [
        power for power in player.get("powers") or []
        if isinstance(power, dict)
    ]
    power_ids = {
        _token(power.get("id") or power.get("name")) for power in powers
    }
    if power_ids & {"self repair", "selfrepair"}:
        return None
    regeneration = []
    for power in powers:
        if _token(power.get("id") or power.get("name")) not in {
            "regeneration", "regenerationpower",
        }:
            continue
        amount = power.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return None
        regeneration.append(amount)
    if len(regeneration) > 1:
        return None
    healing += sum(regeneration)
    if "magic flower" in relic_ids or "magicflower" in relic_ids:
        healing = (healing * 3 + 1) // 2
    gross_loss = before_hp + healing - after_hp
    if gross_loss < 0 or gross_loss > before_hp:
        return None
    if has_meat_on_the_bone and (before_hp - gross_loss) * 2 <= max_hp:
        return None
    return gross_loss


def _planned_first_card_matches_current(
    decision, *, card_id=None, card_instance_id=None,
):
    """Bind an ordered-search first action to the submitted card."""

    planned = (decision or {}).get("planned_sequence") or []
    first = (
        planned[0]
        if planned and isinstance(planned[0], dict)
        else {
            "card_id": (decision or {}).get("card_id"),
            "card_instance_id": (decision or {}).get(
                "card_instance_id"
            ),
        }
    )
    planned_instance = (
        first.get("card_instance_id") or first.get("card_uuid")
        or first.get("uuid")
    )
    if card_instance_id and planned_instance:
        return str(card_instance_id) == str(planned_instance)
    planned_card = first.get("card_id") or first.get("id")
    return bool(
        card_id and planned_card
        and _token(card_id) == _token(planned_card)
    )


def _planned_first_target_matches_current(
    decision, before_game, *, target_id=None,
):
    """Bind the ordered-search first target to the submitted enemy."""

    planned = (decision or {}).get("planned_sequence") or []
    first = (
        planned[0]
        if planned and isinstance(planned[0], dict)
        else {"target_key": (decision or {}).get("target_key")}
    )
    planned_target = first.get("target_key")
    if planned_target is None:
        return target_id is None
    if (
        not target_id
        or not isinstance(planned_target, (list, tuple))
        or len(planned_target) != 2
        or not isinstance(planned_target[1], int)
        or isinstance(planned_target[1], bool)
        or not isinstance(before_game, dict)
    ):
        return False
    monsters = ((before_game.get("combat_state") or {}).get("monsters") or [])
    matches = [
        (index, monster)
        for index, monster in enumerate(monsters)
        if isinstance(monster, dict)
        and monster.get("enemy_instance_id") == target_id
    ]
    if len(matches) != 1:
        return False
    list_index, monster = matches[0]
    monster_index = monster.get("monster_index")
    if not isinstance(monster_index, int) or isinstance(monster_index, bool):
        monster_index = list_index
    return (
        _token(planned_target[0])
        == _token(monster.get("id") or monster.get("name"))
        and planned_target[1] == monster_index
    )


def _bound_first_action_search_damage(search, before_game):
    """Return an exact or conservative bounded first-action HP projection."""

    search = search if isinstance(search, dict) else {}
    exact = search.get("first_action_enemy_hp_loss")
    if not isinstance(exact, (int, float)) or isinstance(exact, bool):
        return None
    exact = max(0, exact)
    expected = search.get("first_action_expected_enemy_hp_loss")
    if not (
        search.get("first_action_enemy_hp_loss_is_expected")
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and expected > exact
    ):
        return exact
    living_hp = sum(
        max(0, int(monster.get("current_hp") or 0))
        for monster in (
            ((before_game or {}).get("combat_state") or {}).get("monsters") or []
        )
        if isinstance(monster, dict)
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    )
    return {
        "minimum": min(living_hp, exact),
        "maximum": max(min(living_hp, exact), living_hp),
        "expected": min(living_hp, max(0, expected)),
    }


def _bound_first_action_decision_damage(decision, before_game):
    """Read an explicitly bound first-action projection from a decision."""

    decision = decision if isinstance(decision, dict) else {}
    search = dict(decision.get("search") or {})
    for field in (
        "first_action_enemy_hp_loss",
        "first_action_expected_enemy_hp_loss",
        "first_action_enemy_hp_loss_is_expected",
    ):
        if search.get(field) is None and decision.get(field) is not None:
            search[field] = decision[field]
    return _bound_first_action_search_damage(search, before_game)


def _current_offering_letter_opener_prediction(
    before_game, available_commands, *, card_id=None,
    card_instance_id=None,
):
    """Return exact non-Attack damage for an emergency Offering play.

    Offering itself deals no enemy damage.  Letter Opener is therefore the
    only immediate packet when it is ready, unless Charon's Ashes adds an
    exhaust packet; that mixed case deliberately remains unresolved.
    """

    if (
        not isinstance(before_game, dict)
        or before_game.get("room_phase") != "COMBAT"
    ):
        return None
    try:
        parsed = Game.from_json(
            before_game,
            available_commands if isinstance(available_commands, list) else [],
        )
        hand = list(getattr(parsed, "hand", []) or [])
        card = next((
            item for item in hand
            if card_instance_id
            and getattr(item, "uuid", None) == card_instance_id
        ), None)
        if card is None and card_id:
            wanted = _token(card_id)
            card = next((
                item for item in hand
                if _token(getattr(item, "card_id", "")) == wanted
            ), None)
        if card is None or _token(getattr(card, "card_id", "")) != "offering":
            return None
        if combat_predictor.relic_ids(parsed) & {
            "charons ashes", "charon's ashes", "charonsashes",
        }:
            return None
        packet = combat_predictor.letter_opener_damage(parsed)
        if packet <= 0:
            return 0
        return sum(
            min(
                max(0, int(getattr(monster, "current_hp", 0) or 0)),
                combat_predictor.attack_hp_loss(
                    monster, packet, vulnerable_eligible=False
                ),
            )
            for monster in combat_predictor.living_monsters(parsed)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _exact_fairy_revival_hp_loss(before_game, after_game, outcome):
    """Recover gross damage hidden by an automatic Fairy Potion revive."""

    if not all(
        isinstance(value, dict)
        for value in (before_game, after_game, outcome)
    ):
        return None
    before_potions = before_game.get("potions") or []
    after_potions = after_game.get("potions") or []
    fairy_before = {
        potion.get("potion_instance_id")
        for potion in before_potions
        if isinstance(potion, dict)
        and _token(potion.get("id")) in {
            "fairypotion", "fairyinabottle", "fairy",
        }
        and potion.get("potion_instance_id")
    }
    fairy_after = {
        potion.get("potion_instance_id")
        for potion in after_potions
        if isinstance(potion, dict)
        and _token(potion.get("id")) in {
            "fairypotion", "fairyinabottle", "fairy",
        }
        and potion.get("potion_instance_id")
    }
    if not fairy_before or not (fairy_before - fairy_after):
        return None
    try:
        parsed = Game.from_json(before_game, [])
        revive_healing = combat_predictor.fairy_in_a_bottle_healing(
            parsed
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    hp_before = before_game.get("current_hp")
    hp_delta = outcome.get("player_hp_delta")
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (hp_before, hp_delta, revive_healing)
    ) or revive_healing <= 0:
        return None
    player = ((before_game.get("combat_state") or {}).get("player") or {})
    if any(
        _token(power.get("id") or power.get("name"))
        in {"regeneration", "regenerationpower"}
        and int(power.get("amount") or 0) > 0
        for power in player.get("powers") or []
        if isinstance(power, dict)
    ):
        # The stable delta merges two heals. Leave that combination unknown
        # until the trace carries both ordered healing settlements.
        return None
    gross_loss = revive_healing - hp_delta
    if gross_loss < 0:
        return None
    return gross_loss


def _juggernaut_block_gain_contract(
    before_game, card_id=None, card_instance_id=None, resolution_count=1,
):
    """Return exact, immediately queued Block-gain triggers for Juggernaut.

    Damage telemetry normally predicts only the played card's attack packets.
    Juggernaut is different: the same PLAY transition can deal one random
    packet for every independently queued GainBlockAction (the card itself,
    Rage, After Image, or Ornamental Fan).  Keep this helper deliberately
    narrow.  Complex mass-exhaust cards remain unclaimed instead of receiving
    an arbitrary wide damage interval.
    """

    if not isinstance(before_game, dict):
        return None
    combat = before_game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    player = combat.get("player")
    player = player if isinstance(player, dict) else {}

    def power_amount(*ids):
        wanted = {_normalized_game_id(value) for value in ids}
        return sum(
            max(0, int(power.get("amount") or 0))
            for power in player.get("powers") or []
            if isinstance(power, dict)
            and _normalized_game_id(power.get("id") or power.get("name"))
            in wanted
        )

    juggernaut = power_amount("Juggernaut", "JuggernautPower")
    if juggernaut <= 0:
        return None
    hand = [card for card in combat.get("hand") or [] if isinstance(card, dict)]
    card = next((
        value for value in hand
        if card_instance_id
        and value.get("card_instance_id") == card_instance_id
    ), None)
    if card is None and card_id:
        wanted = _normalized_game_id(card_id)
        matches = [
            value for value in hand
            if _normalized_game_id(value.get("id") or value.get("name")) == wanted
        ]
        card = matches[0] if len(matches) == 1 else None
    if card is None:
        return None
    if (
        type(resolution_count) is not int
        or resolution_count < 1
        or resolution_count > 3
    ):
        return None
    card_token = _normalized_game_id(card.get("id") or card.get("name"))
    if card_token in {"secondwind", "fiendfire", "seversoul"}:
        return None
    card_type = str(card.get("type") or "").upper()
    attack = card_type == "ATTACK"
    triggers = 0
    sources = []
    if int(card.get("base_block") or -1) >= 0 or card_token == "entrench":
        triggers += resolution_count
        sources.append({"source": "card_block", "count": resolution_count})
    rage = power_amount("Rage", "RagePower")
    if attack and rage > 0:
        triggers += resolution_count
        sources.append({"source": "rage", "count": resolution_count})
    after_image = power_amount("After Image", "AfterImagePower")
    if after_image > 0:
        triggers += 1
        sources.append({"source": "after_image", "count": 1})
    feel_no_pain = power_amount("Feel No Pain", "FeelNoPainPower")
    if card.get("exhausts") is True and feel_no_pain > 0:
        triggers += 1
        sources.append({"source": "feel_no_pain", "count": 1})
    fan = next((
        relic for relic in before_game.get("relics") or []
        if isinstance(relic, dict)
        and _normalized_game_id(relic.get("id") or relic.get("name"))
        == "ornamentalfan"
    ), None)
    if attack and fan is not None:
        counter = fan.get("counter")
        if type(counter) is not int or counter not in {0, 1, 2}:
            return None
        fan_triggers = (counter + resolution_count) // 3
        if fan_triggers:
            triggers += fan_triggers
            sources.append({"source": "ornamental_fan", "count": fan_triggers})
    return {
        "damage_per_trigger": juggernaut,
        "trigger_count": triggers,
        "sources": sources,
        "card_instance_id": card.get("card_instance_id"),
    }


def damage_model_context(
    action,
    decision,
    outcome,
    before_game=None,
    after_game=None,
    available_commands=None,
    card_id=None,
    card_instance_id=None,
    target_id=None,
):
    """Normalize predicted/observed HP deltas for per-turn auditing."""

    decision = decision if isinstance(decision, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    search = decision.get("search") or {}
    predicted_hero = None
    predicted_hero_bounds = None
    juggernaut_contract = None
    prediction_basis = "unknown"
    deferred_attack_resolution = bool(
        action == "play"
        and str(outcome.get("phase_after") or "").upper()
        in {"GRID", "HAND_SELECT"}
        and isinstance(search.get("first_action_resolution_count"), int)
        and not isinstance(search.get("first_action_resolution_count"), bool)
        and search.get("first_action_resolution_count") > 1
    )
    if action == "play":
        # Resolve the current confirmed card before consulting any turn-level
        # search fields.  The latter describe the *first* action of a plan and
        # are stale for terminal-plan continuations after a Skill or setup
        # card.  ``card_damage`` remains a useful planner fallback when the
        # bridge frame cannot be parsed locally.
        search_projection = search.get("first_action_enemy_hp_loss")
        terminal_plan_continuation = (
            decision.get("reason") == "terminal_plan_continuation"
        )
        fresh_terminal_continuation = bool(
            terminal_plan_continuation
            and decision.get("plan_binding") == "terminal_combat_end"
            and _planned_first_card_matches_current(
                decision,
                card_id=card_id,
                card_instance_id=card_instance_id,
            )
            and _planned_first_target_matches_current(
                decision, before_game, target_id=target_id,
            )
        )
        current_projection = None
        repeated_search_resolution = bool(
            isinstance(search.get("first_action_resolution_count"), int)
            and not isinstance(
                search.get("first_action_resolution_count"), bool
            )
            and search.get("first_action_resolution_count") > 1
            and not terminal_plan_continuation
            and decision.get("reason") not in {
                "zero_loss_attack_progress",
                "no_incoming_vulnerable_setup",
            }
        )
        # A repeated resolution needs the ordered search because target Block,
        # Slow, Malleable and on-hit state can change between copies.  Every
        # ordinary single-resolution action is instead rebound to the card
        # that the controller actually submitted.  This is essential when a
        # safe prefix override replaces the search's original first card.
        if not repeated_search_resolution:
            current_projection = _current_card_attack_prediction(
                before_game,
                available_commands,
                card_id=card_id,
                card_instance_id=card_instance_id,
                target_id=target_id,
            )
            if current_projection is not _CURRENT_CARD_NON_ATTACK:
                fallback = decision.get("first_action_enemy_hp_loss")
                if fallback is not None:
                    search_projection = fallback
        ordinary_first_action = (
            (not terminal_plan_continuation or fresh_terminal_continuation)
            and isinstance(search_projection, (int, float))
            and not isinstance(search_projection, bool)
        )
        bound_search_projection = None
        stale_serialized_copy_power = bool(
            search.get("first_action_resolution_count") == 1
            and any(
                _token(power.get("id") or power.get("name")) in {
                    "double tap", "doubletappower",
                    "echo form", "echoformpower",
                    "duplication", "duplicationpower",
                }
                and int(power.get("amount") or 0) > 0
                for power in (
                    (((before_game or {}).get("combat_state") or {}).get(
                        "player"
                    ) or {}).get("powers") or []
                )
                if isinstance(power, dict)
            )
        )
        if (
            ordinary_first_action
            and _planned_first_card_matches_current(
                decision,
                card_id=card_id,
                card_instance_id=card_instance_id,
            )
            and (
                current_projection is _CURRENT_CARD_NON_ATTACK
                or _planned_first_target_matches_current(
                    decision, before_game, target_id=target_id,
                )
            )
        ):
            bound_search_projection = _bound_first_action_decision_damage(
                decision, before_game,
            )
        if deferred_attack_resolution:
            # Some repeated attacks open one or more card-selection screens
            # between copies (Double Tap + Headbutt is the live example).  The
            # immediate PLAY transition contains only the first hit, so neither
            # side of the prediction pair is settled yet.
            prediction_basis = "deferred_card_resolution"
        elif repeated_search_resolution and ordinary_first_action:
            # Ordered search simulates state changes between copies, including
            # Malleable Block.  Prefer that exact first-action result over a
            # static multiplication of the current card packet.
            repeated_projection = _bound_first_action_search_damage(
                search, before_game,
            )
            if isinstance(repeated_projection, dict):
                predicted_hero_bounds = repeated_projection
                prediction_basis = (
                    "turn_search_current_first_action_hp_loss_bounds"
                )
            else:
                predicted_hero = search_projection
                prediction_basis = "turn_search_current_first_action_hp_loss"
        else:
            if current_projection is _CURRENT_CARD_NON_ATTACK:
                if bound_search_projection is not None:
                    if isinstance(bound_search_projection, dict):
                        predicted_hero_bounds = bound_search_projection
                        prediction_basis = (
                            "turn_search_bound_non_attack_"
                            "first_action_hp_loss_bounds"
                        )
                    else:
                        predicted_hero = bound_search_projection
                        prediction_basis = (
                            "turn_search_bound_non_attack_first_action_hp_loss"
                        )
                else:
                    predicted_hero = (
                        _current_offering_letter_opener_prediction(
                            before_game,
                            available_commands,
                            card_id=card_id,
                            card_instance_id=card_instance_id,
                        )
                    )
                    prediction_basis = (
                        "current_offering_letter_opener_exact_hp_loss"
                        if predicted_hero is not None
                        else "current_card_non_attack"
                    )
            elif current_projection is _CURRENT_ATTACK_UNRESOLVED:
                if bound_search_projection is not None:
                    if isinstance(bound_search_projection, dict):
                        predicted_hero_bounds = bound_search_projection
                        prediction_basis = (
                            "turn_search_bound_current_"
                            "first_action_hp_loss_bounds"
                        )
                    else:
                        predicted_hero = bound_search_projection
                        prediction_basis = (
                            "turn_search_bound_current_first_action_hp_loss"
                        )
                elif ordinary_first_action:
                    predicted_hero = search_projection
                    prediction_basis = (
                        "turn_search_current_first_action_hp_loss"
                    )
                else:
                    prediction_basis = "current_card_attack_unresolved"
            elif bound_search_projection is not None and (
                isinstance(bound_search_projection, dict)
                or (
                    isinstance(current_projection, (int, float))
                    and not isinstance(current_projection, bool)
                    and (
                        bound_search_projection > current_projection
                        or (
                            bound_search_projection < current_projection
                            and stale_serialized_copy_power
                        )
                    )
                )
            ):
                if isinstance(bound_search_projection, dict):
                    predicted_hero_bounds = bound_search_projection
                    prediction_basis = (
                        "turn_search_bound_current_first_action_hp_loss_bounds"
                    )
                else:
                    predicted_hero = bound_search_projection
                    prediction_basis = (
                        "turn_search_bound_current_first_action_hp_loss"
                    )
            elif (
                isinstance(current_projection, dict)
                and type(current_projection.get("minimum")) in {int, float}
                and type(current_projection.get("maximum")) in {int, float}
            ):
                predicted_hero_bounds = current_projection
                prediction_basis = "current_card_random_target_hp_loss_bounds"
            elif current_projection is not None:
                predicted_hero = current_projection
                prediction_basis = "current_card_final_target_hp_projection"
            elif "card_damage" in decision:
                predicted_hero = decision.get("card_damage")
                prediction_basis = "decision_card_damage"
            else:
                predicted_hero = search_projection
                if predicted_hero is not None:
                    prediction_basis = "turn_search_first_action_fallback"
    elif action == "potion":
        predicted_hero = decision.get("potion_immediate_enemy_hp_loss")
        if predicted_hero is not None:
            prediction_basis = "direct_potion_exact_hp_loss"
        direct_bounds = decision.get(
            "potion_immediate_enemy_hp_loss_bounds"
        )
        if (
            predicted_hero is None
            and isinstance(direct_bounds, dict)
            and type(direct_bounds.get("minimum")) in {int, float}
            and type(direct_bounds.get("maximum")) in {int, float}
            and 0 <= direct_bounds["minimum"] <= direct_bounds["maximum"]
        ):
            predicted_hero_bounds = {
                "minimum": direct_bounds["minimum"],
                "maximum": direct_bounds["maximum"],
            }
            prediction_basis = "direct_potion_hp_loss_bounds"
        if predicted_hero is None:
            predicted_hero = search.get("first_action_enemy_hp_loss")
        if predicted_hero is None:
            predicted_hero = decision.get("first_action_enemy_hp_loss")
        if predicted_hero is None and "card_damage" in decision:
            predicted_hero = decision.get("card_damage")
        if predicted_hero is not None and prediction_basis == "unknown":
            prediction_basis = "potion_or_turn_search_projection"
        if (
            (
                isinstance(predicted_hero, (int, float))
                and not isinstance(predicted_hero, bool)
                or predicted_hero_bounds is not None
            )
            and isinstance(before_game, dict)
        ):
            raw_monsters = (
                (before_game.get("combat_state") or {}).get("monsters") or []
            )
            living = [
                monster for monster in raw_monsters
                if isinstance(monster, dict)
                and int(monster.get("current_hp") or 0) > 0
                and monster.get("is_gone") is not True
                and monster.get("half_dead") is not True
            ]
            aoe_potion = (
                _token(decision.get("potion_id")) == "explosivepotion"
            )
            exact_aoe = (
                prediction_basis == "direct_potion_exact_hp_loss"
                and aoe_potion
            )
            bound = living if aoe_potion or not target_id else [
                monster for monster in living
                if monster.get("enemy_instance_id") == target_id
            ]
            if bound and predicted_hero_bounds is not None:
                hp_cap = sum(
                    int(monster.get("current_hp") or 0) for monster in bound
                )
                predicted_hero_bounds = {
                    "minimum": min(
                        hp_cap, predicted_hero_bounds["minimum"]
                    ),
                    "maximum": min(
                        hp_cap, predicted_hero_bounds["maximum"]
                    ),
                }
                prediction_basis = (
                    "direct_potion_aoe_hp_loss_bounds"
                    if aoe_potion
                    else "direct_potion_target_hp_loss_bounds"
                )
            elif bound:
                # Observable HP loss is target-HP capped even when the
                # deterministic potion packet itself overkills.
                predicted_hero = min(
                    predicted_hero,
                    sum(int(monster.get("current_hp") or 0) for monster in bound),
                )
                if prediction_basis == "direct_potion_exact_hp_loss":
                    prediction_basis = (
                        "direct_potion_exact_aoe_hp_loss"
                        if exact_aoe
                        else "direct_potion_exact_target_hp_loss"
                    )
    if action == "play" and not deferred_attack_resolution:
        resolution_count = search.get("first_action_resolution_count", 1)
        juggernaut_contract = _juggernaut_block_gain_contract(
            before_game,
            card_id=card_id,
            card_instance_id=card_instance_id,
            resolution_count=resolution_count,
        )
        if (
            isinstance(juggernaut_contract, dict)
            and juggernaut_contract["trigger_count"] > 0
        ):
            if predicted_hero_bounds is not None:
                base_minimum = predicted_hero_bounds["minimum"]
                base_maximum = predicted_hero_bounds["maximum"]
            elif isinstance(predicted_hero, (int, float)) and not isinstance(
                predicted_hero, bool
            ):
                base_minimum = base_maximum = predicted_hero
            elif prediction_basis == "current_card_non_attack":
                base_minimum = base_maximum = 0
            else:
                base_minimum = base_maximum = None
            if base_minimum is not None and base_maximum is not None:
                living_hp = sum(
                    max(0, int(monster.get("current_hp") or 0))
                    for monster in (
                        (before_game or {}).get("combat_state") or {}
                    ).get("monsters") or []
                    if isinstance(monster, dict)
                    and monster.get("is_gone") is not True
                    and monster.get("half_dead") is not True
                )
                predicted_hero_bounds = {
                    "minimum": min(living_hp, base_minimum),
                    "maximum": min(
                        living_hp,
                        base_maximum
                        + juggernaut_contract["trigger_count"]
                        * juggernaut_contract["damage_per_trigger"],
                    ),
                    "base_minimum": base_minimum,
                    "base_maximum": base_maximum,
                }
                predicted_hero = None
                prediction_basis = (
                    "current_card_with_juggernaut_block_gain_bounds"
                )
    predicted_enemy = None
    if action == "end":
        total = decision.get("projected_hp_loss_before")
        if total is None:
            total = decision.get("projected_hp_loss")
        if total is not None:
            # The authoritative END receipt spans the enemy turn and the
            # following player-turn start.  Use the predictor's complete
            # lifecycle total so Brutality and similar start-of-turn self
            # loss are compared with the same HP delta as the receipt.
            predicted_enemy = max(0, int(total))
        else:
            attack = decision.get("projected_attack_hp_loss")
            if attack is None:
                attack = decision.get("projected_attack_hp_loss_before")
            end_turn = decision.get("projected_end_turn_hp_loss")
            if end_turn is None:
                end_turn = decision.get("projected_end_turn_hp_loss_before")
            next_turn_start = decision.get(
                "projected_next_turn_start_hp_loss"
            )
            if next_turn_start is None:
                next_turn_start = decision.get(
                    "projected_next_turn_start_hp_loss_before"
                )
            if (
                attack is not None
                or end_turn is not None
                or next_turn_start is not None
            ):
                predicted_enemy = (
                    max(0, int(attack or 0))
                    + max(0, int(end_turn or 0))
                    + max(0, int(next_turn_start or 0))
                )
    actual_hero = outcome.get("enemy_hp_loss") if action in {"play", "potion"} else None
    actual_enemy = outcome.get("player_hp_loss") if action == "end" else None
    enemy_basis = "end_turn_player_hp_delta"
    if action == "end":
        fairy_loss = _exact_fairy_revival_hp_loss(
            before_game, after_game, outcome
        )
        postcombat_loss = _exact_postcombat_hp_loss(before_game, outcome)
        if fairy_loss is not None:
            actual_enemy = fairy_loss
            enemy_basis = (
                "end_turn_total_hp_loss_before_fairy_revival"
            )
        elif postcombat_loss is not None:
            actual_enemy = postcombat_loss
            enemy_basis = (
                "end_turn_total_hp_loss_before_postcombat_healing"
            )
    model = {
        "hero_to_monsters_basis": "sum(monster_final_hp_delta)",
        "hero_to_monsters_prediction_basis": prediction_basis,
        "monsters_to_hero_basis": enemy_basis,
        "observed_player_hp_delta": outcome.get("player_hp_delta"),
        "observed_player_hp_gain": outcome.get("player_hp_gain"),
        "observed_enemy_hp_before_total": outcome.get(
            "enemy_hp_before_total"
        ),
        "observed_enemy_hp_after_total": outcome.get(
            "enemy_hp_after_total"
        ),
    }
    if (
        prediction_basis == "current_card_with_juggernaut_block_gain_bounds"
        and isinstance(juggernaut_contract, dict)
        and isinstance(predicted_hero_bounds, dict)
    ):
        model["juggernaut_block_gain_contract"] = juggernaut_contract
        model["juggernaut_base_predicted_min"] = predicted_hero_bounds[
            "base_minimum"
        ]
        model["juggernaut_base_predicted_max"] = predicted_hero_bounds[
            "base_maximum"
        ]
    if deferred_attack_resolution:
        model["hero_to_monsters_settlement"] = "deferred_card_resolution"
    # Only publish a prediction pair for the direction this action can
    # actually resolve.  Emitting two explicit ``None`` values made the
    # independent oracle classify every irrelevant direction as an eligible
    # unknown even though no prediction had been asserted.
    if action == "play" and prediction_basis == "current_card_non_attack":
        if (
            isinstance(actual_hero, (int, float))
            and not isinstance(actual_hero, bool)
            and actual_hero != 0
        ):
            model.update({
                "hero_to_monsters_predicted": None,
                "hero_to_monsters_actual": actual_hero,
            })
    elif action == "play" and not deferred_attack_resolution:
        if predicted_hero_bounds is not None:
            model.update({
                "hero_to_monsters_predicted_min": (
                    predicted_hero_bounds["minimum"]
                ),
                "hero_to_monsters_predicted_max": (
                    predicted_hero_bounds["maximum"]
                ),
                "hero_to_monsters_actual": actual_hero,
            })
            if isinstance(predicted_hero_bounds.get("expected"), (int, float)):
                model["hero_to_monsters_predicted_expected"] = (
                    predicted_hero_bounds["expected"]
                )
        else:
            model.update({
                "hero_to_monsters_predicted": predicted_hero,
                "hero_to_monsters_actual": actual_hero,
            })
    elif action == "potion" and not deferred_attack_resolution:
        # A non-damaging potion has no hero-to-monsters prediction surface.
        # Keep a non-zero unpredicted observation eligible so the oracle still
        # fails closed when a damaging potion is missing a projection.
        if (
            predicted_hero_bounds is not None
            and isinstance(actual_hero, (int, float))
            and not isinstance(actual_hero, bool)
        ):
            model.update({
                "hero_to_monsters_predicted_min": (
                    predicted_hero_bounds["minimum"]
                ),
                "hero_to_monsters_predicted_max": (
                    predicted_hero_bounds["maximum"]
                ),
                "hero_to_monsters_actual": actual_hero,
            })
        elif (
            isinstance(predicted_hero, (int, float))
            and not isinstance(predicted_hero, bool)
            and isinstance(actual_hero, (int, float))
            and not isinstance(actual_hero, bool)
        ) or (
            not isinstance(predicted_hero, (int, float))
            and isinstance(actual_hero, (int, float))
            and not isinstance(actual_hero, bool)
            and actual_hero != 0
        ):
            model.update({
                "hero_to_monsters_predicted": predicted_hero,
                "hero_to_monsters_actual": actual_hero,
            })
    if action == "end":
        model.update({
            "monsters_to_hero_predicted": predicted_enemy,
            "monsters_to_hero_actual": actual_enemy,
        })
    return model


def action_true_combat_end_prediction(
    decision,
    *,
    current_action_enemy_hp_loss=None,
    living_enemy_hp_total=None,
):
    """Return a current-action terminal claim, not a whole-plan claim.

    Planner ``search.true_combat_end`` describes the complete searched line.
    The first action can therefore leave enemies alive while a later planned
    action kills them.  Only expose an action-level claim when the remaining
    bound sequence contains exactly one card.  Ambiguous/older planner
    payloads remain plan evidence and are deliberately not promoted.
    """

    decision = decision if isinstance(decision, dict) else {}
    search = decision.get("search")
    search = search if isinstance(search, dict) else {}
    if search.get("true_combat_end") is not True:
        return False
    planned = decision.get("planned_sequence")
    if isinstance(planned, list):
        single_action_plan = len(planned) == 1
    else:
        combo = search.get("combo_card_uuids")
        single_action_plan = isinstance(combo, list) and len(combo) == 1
    if not single_action_plan:
        return False
    # A one-card plan may still rely on poison, Combust, or another END-turn
    # trigger after that card.  Promote the whole-plan claim to an immediate
    # action claim only when the independently projected current action alone
    # accounts for every living enemy HP point in the pre-action frame.
    if (
        not isinstance(current_action_enemy_hp_loss, (int, float))
        or isinstance(current_action_enemy_hp_loss, bool)
        or not isinstance(living_enemy_hp_total, (int, float))
        or isinstance(living_enemy_hp_total, bool)
        or living_enemy_hp_total <= 0
    ):
        return False
    return current_action_enemy_hp_loss >= living_enemy_hp_total


def combat_legal_actions(game, available_commands=None):
    """Expand authoritative combat legality into stable semantic actions."""

    game = game if isinstance(game, dict) else {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    energy = max(0, int(player.get("energy") or 0))
    living = [
        monster for monster in combat.get("monsters") or []
        if isinstance(monster, dict)
        and int(monster.get("current_hp") or 0) > 0
        and not monster.get("is_gone")
        and not monster.get("half_dead")
    ]
    actions = []
    for card in combat.get("hand") or []:
        if not isinstance(card, dict) or card.get("is_playable") is not True:
            continue
        try:
            raw_cost = int(card.get("cost"))
        except (TypeError, ValueError):
            continue
        cost = energy if raw_cost == -1 else max(0, raw_cost)
        if cost > energy:
            continue
        base = {
            "action": "play",
            "card_instance_id": card.get("card_instance_id"),
            "card_id": card.get("id"),
            "cost": cost,
            "legal": True,
        }
        if card.get("has_target"):
            actions.extend({
                **base,
                "enemy_instance_id": monster.get("enemy_instance_id"),
                "choice_id": (
                    f'play:{card.get("card_instance_id")}:'
                    f'{monster.get("enemy_instance_id")}'
                ),
            } for monster in living)
        else:
            actions.append({
                **base,
                "choice_id": f'play:{card.get("card_instance_id")}',
            })
    for potion in game.get("potions") or []:
        if not isinstance(potion, dict) or potion.get("can_use") is not True:
            continue
        base = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": potion.get("potion_instance_id"),
            "potion_id": potion.get("id"),
            "legal": True,
        }
        if potion.get("requires_target"):
            actions.extend({
                **base,
                "enemy_instance_id": monster.get("enemy_instance_id"),
                "choice_id": (
                    f'potion:{potion.get("potion_instance_id")}:'
                    f'{monster.get("enemy_instance_id")}'
                ),
            } for monster in living)
        else:
            actions.append({
                **base,
                "choice_id": f'potion:{potion.get("potion_instance_id")}',
            })
    commands = {
        str(command or "").strip().casefold()
        for command in available_commands or []
    }
    if commands & {"end", "end_turn"}:
        actions.append({
            "action": "end",
            "target_id": "action:end",
            "choice_id": "action:end",
            "legal": True,
        })
    return actions


def _combat_action_selected(candidate, payload):
    if candidate.get("action") != payload.get("action"):
        return False
    for key in (
        "card_instance_id", "potion_instance_id", "enemy_instance_id",
        "operation", "target_id",
    ):
        if candidate.get(key) is not None and candidate.get(key) != payload.get(key):
            return False
    return True


def combat_trace_encounter_serial(game, attempt_id, state_seq):
    """Return a stable per-encounter serial and observe room boundaries.

    Act/floor/room_type alone is insufficient for multi-stage event fights:
    both Colosseum combats share all three fields.  ``compact_action`` calls
    this for every recorded action, so a non-combat room frame can close the
    prior encounter before the next combat begins.  The first authoritative
    state sequence is then a stable, trace-local discriminator.
    """

    tracker_key = str(attempt_id or run_id(game) or "unbound")
    if str(game.get("room_phase") or "").upper() != "COMBAT":
        _COMBAT_TRACE_ENCOUNTERS.pop(tracker_key, None)
        return None
    scope = (
        game.get("class"), game.get("ascension_level"), game.get("seed"),
        game.get("act"), game.get("floor"), game.get("room_type"),
    )
    tracked = _COMBAT_TRACE_ENCOUNTERS.get(tracker_key)
    if not isinstance(tracked, dict) or tracked.get("scope") != scope:
        tracked = {"scope": scope, "serial": int(state_seq)}
        _COMBAT_TRACE_ENCOUNTERS[tracker_key] = tracked
    return tracked["serial"]


def combat_trace_context(
    game,
    phase=None,
    attempt_id=None,
    available_commands=None,
    payload=None,
    decision=None,
    encounter_serial=None,
):
    """Return stable, replay-oriented fields for one combat decision.

    Enemy HP, hand contents, turn number, and even monster-list membership all
    change during a fight.  Slime splits and summoned enemies may be inserted
    before the original (dead-but-retained) monster, so no live list position
    is a stable encounter anchor.  Bind the identifier only to immutable run
    and room fields; the trace auditor resets its scope at non-combat decisions
    and separately detects a turn regression.
    """

    combat = game.get("combat_state") or {}
    monsters = combat.get("monsters") or []
    identity = {
        "attempt_id": attempt_id,
        "class": game.get("class"),
        "ascension_level": game.get("ascension_level"),
        "seed": game.get("seed"),
        "act": game.get("act"),
        "floor": game.get("floor"),
        "room_type": game.get("room_type"),
    }
    if encounter_serial is not None:
        identity["encounter_serial"] = encounter_serial
    digest = hashlib.sha256(json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]
    turn = combat.get("turn")
    if turn is None:
        match = re.fullmatch(r"COMBAT_TURN_(\d+)", str(phase or ""))
        turn = int(match.group(1)) if match else None
    player = combat.get("player") or {}
    legal_actions = combat_legal_actions(game, available_commands)
    payload = payload if isinstance(payload, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    return {
        "turn": turn,
        "combat_id": f"combat:{digest}",
        "audit_oracle_version": AUDIT_ORACLE_VERSION,
        "trace_capabilities": [
            "ordered_hand", "player_state", "monster_state", "potions",
            "next_turn_start_hp_loss", "end_turn_healing",
            "fairy_revival",
            "post_action_damage", "turn_damage_model", "relic_model_coverage",
            "power_model_coverage", "complete_combat_piles",
            "relic_counters", "legal_actions", "production_candidates",
            "authoritative_state_snapshots", "observable_block_delta",
            "stasis_card_uuid_binding", "card_reward_screen_count",
        ],
        "hand_before": [
            compact_audit_value(card) for card in combat.get("hand") or []
        ],
        "draw_pile_before": [
            compact_audit_value(card) for card in combat.get("draw_pile") or []
        ],
        "discard_pile_before": [
            compact_audit_value(card) for card in combat.get("discard_pile") or []
        ],
        "exhaust_pile_before": [
            compact_audit_value(card) for card in combat.get("exhaust_pile") or []
        ],
        "limbo_before": [
            compact_audit_value(card) for card in combat.get("limbo") or []
        ],
        "player_before": {
            "current_hp": player.get("current_hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
        **{key: player[key] for key in ("max_orbs", "orb_slots", "facing_left")
           if key in player},
            "powers": [
                {
                    "id": power.get("id"),
                    "name": power.get("name"),
                    "amount": power.get("amount"),
                }
                for power in player.get("powers") or []
            ],
            "orbs": [
                {
                    "id": orb.get("id"),
                    "passive_amount": orb.get("passive_amount"),
                    "evoke_amount": orb.get("evoke_amount"),
                }
                for orb in player.get("orbs") or []
            ],
        },
        "monsters_before": [
            {
                "enemy_instance_id": monster.get("enemy_instance_id"),
                "id": monster.get("id"),
                "monster_index": monster.get("monster_index"),
                "current_hp": monster.get("current_hp"),
                "max_hp": monster.get("max_hp"),
                "block": monster.get("block"),
                "intent": monster.get("intent"),
        **{key: monster[key] for key in (
            "move_id", "last_move_id", "second_last_move_id",
            "move_base_damage", "is_escaping", "miscBool", "miscInt",
        ) if key in monster},
                "move_adjusted_damage": monster.get("move_adjusted_damage"),
                "move_hits": monster.get("move_hits"),
                "is_gone": monster.get("is_gone"),
                "half_dead": monster.get("half_dead"),
                "powers": [
                    {
                        "id": power.get("id"),
                        "name": power.get("name"),
                        "amount": power.get("amount"),
                        **(
                            {"card": compact_audit_value(power["card"])}
                            if isinstance(power.get("card"), dict) else {}
                        ),
                    }
                    for power in monster.get("powers") or []
                ],
            }
            for monster in monsters
        ],
        "potions_before": [
            {
                "potion_instance_id": potion.get("potion_instance_id"),
                "id": potion.get("id"),
                "can_use": potion.get("can_use"),
                "requires_target": potion.get("requires_target"),
            }
            for potion in game.get("potions") or []
        ],
        "relic_ids_before": [
            relic.get("id") for relic in game.get("relics") or []
        ],
        "relics_before": [
            {
                "id": relic.get("id"),
                "name": relic.get("name"),
                "counter": relic.get("counter"),
            }
            for relic in game.get("relics") or []
        ],
        "legal_actions_before": legal_actions,
        "production_candidates_before": [
            {
                **candidate,
                "selected": _combat_action_selected(candidate, payload),
                "production_score": (
                    decision.get("plan_score")
                    if _combat_action_selected(candidate, payload)
                    else None
                ),
                "score_evidence": (
                    "planner_selected_score"
                    if _combat_action_selected(candidate, payload)
                    else "planner_did_not_expose_counterfactual_score"
                ),
            }
            for candidate in legal_actions
        ],
        "candidates_before": [
            {
                **candidate,
                "selected": _combat_action_selected(candidate, payload),
                "production_score": (
                    decision.get("plan_score")
                    if _combat_action_selected(candidate, payload)
                    else None
                ),
                "counterfactual": {
                    "evidence_level": "unable_to_determine",
                    "predicted_survives": None,
                    "predicted_hp_loss": None,
                    "basis": "production planner did not expose this branch",
                },
            }
            for candidate in legal_actions
        ],
        "model_advice": {
            "status": "not_applicable",
            "reason": "combat policy is local and has no remote model channel",
        },
        "relic_model_coverage": combat_predictor.relic_model_coverage(game),
        # Keep the raw protocol ids together with their current coverage
        # classification.  ``player_before``/``monsters_before`` remain the
        # replay source of truth, allowing future audit versions to reclassify
        # an old trace after the registry or an exact handler is improved.
        "power_model_coverage": combat_predictor.power_model_coverage(game),
    }


def is_game_over(state):
    return ((state.get("game_state") or {}).get("screen_type")) == "GAME_OVER"


def projected_doomed_enemy_ids(parsed_game, raw_monsters):
    doomed_indexes = {
        monster.monster_index
        for monster in combat_predictor.projected_doomed_monsters(parsed_game)
    }
    return [
        raw_monsters[index].get("enemy_instance_id")
        for index in sorted(doomed_indexes)
        if 0 <= index < len(raw_monsters)
    ]


def resynchronize_protocol(
    state=None,
    original_payload=None,
    run_context=None,
    reason="protocol_error",
):
    """Issue only bounded, read-only STATE requests to recover transport state."""

    last_timeout = None
    for attempt in range(1, MAX_STATE_RESYNC_ATTEMPTS + 1):
        payload = {"id": str(uuid.uuid4()), "action": "state"}
        append_protocol_event(
            "state_resync_started",
            state,
            original_payload,
            run_context=run_context,
            reason=reason,
            resync_attempt=attempt,
            state_request_id=payload["id"],
        )
        try:
            receipt = stsctl.send_payload(
                payload, timeout=STATE_RESYNC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            last_timeout = exc
            append_protocol_event(
                "state_resync_timeout",
                state,
                original_payload,
                run_context=run_context,
                reason=reason,
                resync_attempt=attempt,
                state_request_id=payload["id"],
            )
            continue
        if not receipt.get("success"):
            append_protocol_event(
                "state_resync_failed",
                state,
                original_payload,
                receipt,
                run_context,
                reason=reason,
                resync_attempt=attempt,
                state_request_id=payload["id"],
            )
            raise SafetyError(f"protocol resynchronization failed: {receipt}")
        append_protocol_event(
            "state_resync_succeeded",
            state,
            original_payload,
            receipt,
            run_context,
            reason=reason,
            resync_attempt=attempt,
            state_request_id=payload["id"],
        )
        return receipt
    raise SafetyError(
        f"protocol resynchronization timed out after "
        f"{MAX_STATE_RESYNC_ATTEMPTS} read-only attempts"
    ) from last_timeout


def transient_combat_resolution_frame(before, after):
    """Identify a stale combat snapshot emitted while a kill is settling.

    CommunicationMod can briefly retain the defeated monster, hand, block,
    and energy after combat-end healing has already fired.  Its serialized
    turn counter falls back to one during that frame.  Treating the snapshot
    as actionable can send a real card command into an already-finished
    combat.  A genuine new player turn refills energy, so require both the
    counter regression and no refill as well as positive healing.
    """

    before_game = before.get("game_state") or {}
    after_game = after.get("game_state") or {}
    if (
        before_game.get("room_phase") != "COMBAT"
        or after_game.get("room_phase") != "COMBAT"
    ):
        return False
    before_combat = before_game.get("combat_state") or {}
    after_combat = after_game.get("combat_state") or {}
    before_turn = before_combat.get("turn")
    after_turn = after_combat.get("turn")
    before_energy = (before_combat.get("player") or {}).get("energy")
    after_energy = (after_combat.get("player") or {}).get("energy")
    before_hp = before_game.get("current_hp")
    after_hp = after_game.get("current_hp")
    return (
        type(before_turn) is int
        and type(after_turn) is int
        and after_turn < before_turn
        and type(before_energy) is int
        and type(after_energy) is int
        and after_energy <= before_energy
        and type(before_hp) is int
        and type(after_hp) is int
        and after_hp > before_hp
    )


def settle_transient_combat_resolution(before, after, run_context=None):
    """Advance only animation frames until a stale combat snapshot clears."""

    original_after_seq = after.get("state_seq")
    settle_attempts = 0
    while transient_combat_resolution_frame(before, after):
        settle_attempts += 1
        if settle_attempts > MAX_TRANSITION_SETTLE_ATTEMPTS:
            raise SafetyError(
                "combat-resolution transition remained stale after bounded "
                "waits; refusing to issue another gameplay action"
            )
        wait_payload = stsctl.bound_payload(after, "wait", frames=1)
        outcome = send_payload_exactly_once(wait_payload, after, run_context)
        if isinstance(outcome, MissingReceiptOutcome):
            settled = outcome.state
            verify_missing_receipt_effect(after, wait_payload, settled, [])
        else:
            receipt = outcome
            if not receipt.get("success"):
                raise SafetyError(
                    f"combat-resolution wait failed: {receipt}"
                )
            settled = stsctl.load_state()
            if receipt.get("accepted_state_seq") != after.get("state_seq"):
                raise SafetyError(
                    "combat-resolution wait accepted a different sequence"
                )
            if receipt.get("result_state_seq") != settled.get("state_seq"):
                raise SafetyError(
                    "combat-resolution wait result sequence differs from "
                    "latest state"
                )
            verify_effect(after, wait_payload, settled, [])
        validate_bound_context(settled, run_context or {})
        after = settled

    if settle_attempts:
        append_trace({
            "record_type": "transition_settle",
            "time": time.time(),
            "policy_version": POLICY_VERSION,
            "strategy_revision": STRATEGY_REVISION,
            "decision_hash": DECISION_HASH,
            "controller_hash": CONTROLLER_HASH,
            "attempt_id": (run_context or {}).get("attempt_id"),
            "run_id": (run_context or {}).get("run_id"),
            "seed": (run_context or {}).get("seed"),
            "character": (run_context or {}).get("character"),
            "ascension_level": (run_context or {}).get("ascension_level"),
            "run_type": (run_context or {}).get("run_type"),
            "selection_id": (run_context or {}).get("selection_id"),
            "selection_digest": (run_context or {}).get(
                "selection_digest"
            ),
            "state_seq": after.get("state_seq"),
            "kind": "stale_combat_resolution",
            "settle_attempts": settle_attempts,
            "original_after_seq": original_after_seq,
            "settled_after_seq": after.get("state_seq"),
            "settled_phase": after.get("phase"),
        })
    return after


def potion_resolution_pending(after, payload):
    """Whether the exact used/discarded potion is still settling."""

    if not isinstance(payload, dict) or payload.get("action") != "potion":
        return False
    potion_id = payload.get("potion_instance_id")
    if not isinstance(potion_id, str) or not potion_id:
        return False
    return any(
        isinstance(item, dict)
        and item.get("potion_instance_id") == potion_id
        for item in (after.get("game_state") or {}).get("potions") or []
    )


def _entropic_brew_effect_settled(before, after, payload):
    """Accept a completed Entropic Brew when its source slot is stale.

    CommunicationMod can publish generated potions and an actionable frame
    before replacing the consumed source object in its original slot. Require
    the exact source, a stable frame, and a concrete replacement of at least
    one previously empty slot before accepting this boundary.
    """

    if (
        not isinstance(payload, dict)
        or payload.get("action") != "potion"
        or str(payload.get("operation") or "").casefold() != "use"
    ):
        return False
    source_id = payload.get("potion_instance_id")
    if not isinstance(source_id, str) or not source_id:
        return False
    before_game = before.get("game_state") or {}
    source = next(
        (
            item for item in before_game.get("potions") or []
            if isinstance(item, dict)
            and item.get("potion_instance_id") == source_id
        ),
        None,
    )
    if _normalized_game_id((source or {}).get("id")) != "entropicbrew":
        return False
    after_game = after.get("game_state") or {}
    if (
        after.get("ready_for_command") is not True
        or after_game.get("current_action")
        or not isinstance(after.get("state_seq"), int)
        or not isinstance(before.get("state_seq"), int)
        or after["state_seq"] <= before["state_seq"]
    ):
        return False
    after_source = next(
        (
            item for item in after_game.get("potions") or []
            if isinstance(item, dict)
            and item.get("potion_instance_id") == source_id
        ),
        None,
    )
    if after_source is None:
        return False
    before_by_slot = {
        item.get("slot"): item
        for item in before_game.get("potions") or []
        if isinstance(item, dict) and isinstance(item.get("slot"), int)
    }
    after_by_slot = {
        item.get("slot"): item
        for item in after_game.get("potions") or []
        if isinstance(item, dict) and isinstance(item.get("slot"), int)
    }
    generated = 0
    for slot, old_item in before_by_slot.items():
        if _normalized_game_id(old_item.get("id")) != "potionslot":
            continue
        new_item = after_by_slot.get(slot)
        if (
            isinstance(new_item, dict)
            and _normalized_game_id(new_item.get("id")) != "potionslot"
            and new_item.get("potion_instance_id")
            != old_item.get("potion_instance_id")
        ):
            generated += 1
    return generated >= 1


def smoke_bomb_escape_pending(before, after, payload):
    """Whether a consumed Smoke Bomb is still resolving its room escape.

    The potion leaves its slot before the escape animation completes.  During
    that window CommunicationMod can expose an apparently actionable combat
    snapshot, so slot removal alone is not a sufficient settlement boundary.
    """

    if (
        not isinstance(payload, dict)
        or payload.get("action") != "potion"
        or str(payload.get("operation") or "").casefold() != "use"
    ):
        return False
    potion_instance_id = payload.get("potion_instance_id")
    if not isinstance(potion_instance_id, str) or not potion_instance_id:
        return False
    before_game = before.get("game_state") or {}
    source = next(
        (
            item for item in before_game.get("potions") or []
            if isinstance(item, dict)
            and item.get("potion_instance_id") == potion_instance_id
        ),
        None,
    )
    source_id = "".join(
        character for character in str((source or {}).get("id") or "").casefold()
        if character.isalnum()
    )
    if source_id != "smokebomb":
        return False
    after_game = after.get("game_state") or {}
    # A consumed Smoke Bomb can leave the room-phase field stale for one or
    # more bridge frames.  If the frame is explicitly authoritative and
    # actionable, with no queued combat action, there is no remaining escape
    # transition to wait for.  Treating this state as pending causes a false
    # bounded-wait P0 and strands an otherwise recoverable run.
    if (
        after.get("ready_for_command") is True
        and not after_game.get("current_action")
    ):
        return False
    return (
        before_game.get("room_phase") == "COMBAT"
        and after_game.get("room_phase") == "COMBAT"
    )


def settle_potion_resolution(before, after, payload, run_context=None):
    """Wait until the exact accepted potion and its queued effect settle.

    CommunicationMod may acknowledge an Entropic Brew as soon as its random
    potions appear, one frame before the source brew is removed. Only bound
    WAIT commands are sent; the potion action is never replayed. Smoke Bomb
    additionally waits for the room to leave COMBAT because its slot clears
    before the escape animation reaches an authoritative terminal boundary.
    """

    entropic_stale_source_settled = _entropic_brew_effect_settled(
        before, after, payload
    )
    if not (
        (
            potion_resolution_pending(after, payload)
            and not entropic_stale_source_settled
        )
        or smoke_bomb_escape_pending(before, after, payload)
    ):
        if entropic_stale_source_settled:
            append_trace({
                "record_type": "potion_resolution_settle",
                "time": time.time(),
                "policy_version": POLICY_VERSION,
                "strategy_revision": STRATEGY_REVISION,
                "decision_hash": DECISION_HASH,
                "controller_hash": CONTROLLER_HASH,
                "attempt_id": (run_context or {}).get("attempt_id"),
                "run_id": (run_context or {}).get("run_id"),
                "seed": (run_context or {}).get("seed"),
                "character": (run_context or {}).get("character"),
                "ascension_level": (run_context or {}).get("ascension_level"),
                "run_type": (run_context or {}).get("run_type"),
                "selection_id": (run_context or {}).get("selection_id"),
                "selection_digest": (run_context or {}).get(
                    "selection_digest"
                ),
                "state_seq": after.get("state_seq"),
                "kind": "entropic_brew_effect_settled_with_stale_source",
                "settle_attempts": 0,
                "original_after_seq": after.get("state_seq"),
                "settled_after_seq": after.get("state_seq"),
                "settled_phase": after.get("phase"),
            })
        return after
    original_after_seq = after.get("state_seq")
    settle_attempts = 0
    waited_for_smoke_bomb_escape = False
    while (
        (
            potion_resolution_pending(after, payload)
            and not _entropic_brew_effect_settled(before, after, payload)
        )
        or smoke_bomb_escape_pending(before, after, payload)
    ):
        waited_for_smoke_bomb_escape = (
            waited_for_smoke_bomb_escape
            or smoke_bomb_escape_pending(before, after, payload)
        )
        settle_attempts += 1
        if settle_attempts > MAX_POTION_SETTLE_ATTEMPTS:
            raise SafetyError(
                "potion resolution retained the consumed instance or queued "
                "escape after bounded waits"
            )
        wait_payload = stsctl.bound_payload(after, "wait", frames=1)
        outcome = send_payload_exactly_once(wait_payload, after, run_context)
        if isinstance(outcome, MissingReceiptOutcome):
            settled = outcome.state
            verify_missing_receipt_effect(after, wait_payload, settled, [])
        else:
            receipt = outcome
            if not receipt.get("success"):
                raise SafetyError(
                    f"potion-resolution wait failed: {receipt}"
                )
            settled = stsctl.load_state()
            if receipt.get("accepted_state_seq") != after.get("state_seq"):
                raise SafetyError(
                    "potion-resolution wait accepted a different sequence"
                )
            if receipt.get("result_state_seq") != settled.get("state_seq"):
                raise SafetyError(
                    "potion-resolution wait result sequence differs from "
                    "latest state"
                )
            verify_effect(after, wait_payload, settled, [])
        validate_bound_context(settled, run_context or {})
        after = settled

    append_trace({
        "record_type": "potion_resolution_settle",
        "time": time.time(),
        "policy_version": POLICY_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "attempt_id": (run_context or {}).get("attempt_id"),
        "run_id": (run_context or {}).get("run_id"),
        "seed": (run_context or {}).get("seed"),
        "character": (run_context or {}).get("character"),
        "ascension_level": (run_context or {}).get("ascension_level"),
        "run_type": (run_context or {}).get("run_type"),
        "selection_id": (run_context or {}).get("selection_id"),
        "selection_digest": (run_context or {}).get("selection_digest"),
        "state_seq": after.get("state_seq"),
        "kind": (
            "smoke_bomb_escape_pending"
            if waited_for_smoke_bomb_escape
            else "consumed_potion_removal_pending"
        ),
        "potion_instance_id": payload.get("potion_instance_id"),
        "settle_attempts": settle_attempts,
        "original_after_seq": original_after_seq,
        "settled_after_seq": after.get("state_seq"),
        "settled_phase": after.get("phase"),
    })
    return after


def _rebound_sweeping_beam_same_turn_redraw(before, after, payload):
    """Prove the played Sweeping Beam resolved and was redrawn by Rebound.

    Rebound can place Sweeping Beam on top of the draw pile before its queued
    draw resolves, returning the exact played instance to hand in the same turn.
    Treat that as a completed play only when independent state deltas bind the
    consumed Rebound power and observed attack damage.  Every other same-turn
    hand-presence case remains fail-closed.
    """

    if not isinstance(payload, dict) or payload.get("action") != "play":
        return False
    card_instance_id = payload.get("card_instance_id")
    if not isinstance(card_instance_id, str) or not card_instance_id:
        return False
    before_game = before.get("game_state") or {}
    after_game = after.get("game_state") or {}
    if (
        before_game.get("room_phase") != "COMBAT"
        or after_game.get("room_phase") != "COMBAT"
        or before.get("phase") != after.get("phase")
        or not before.get("decision_id")
        or before.get("decision_id") == after.get("decision_id")
    ):
        return False
    before_combat = before_game.get("combat_state") or {}
    after_combat = after_game.get("combat_state") or {}
    if before_combat.get("turn") != after_combat.get("turn"):
        return False

    before_cards = [
        card for card in before_combat.get("hand") or []
        if isinstance(card, dict)
        and card.get("card_instance_id") == card_instance_id
    ]
    after_occurrences = []
    for pile_name in (
        "hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo",
    ):
        for card in after_combat.get(pile_name) or []:
            if (
                isinstance(card, dict)
                and card.get("card_instance_id") == card_instance_id
            ):
                after_occurrences.append((pile_name, card))
    if (
        len(before_cards) != 1
        or _normalized_game_id(before_cards[0].get("id")) != "sweepingbeam"
        or len(after_occurrences) != 1
        or after_occurrences[0][0] != "hand"
        or _normalized_game_id(after_occurrences[0][1].get("id"))
        != "sweepingbeam"
        or after_occurrences[0][1].get("upgrades")
        != before_cards[0].get("upgrades")
    ):
        return False

    def rebound_amount(combat):
        player = combat.get("player")
        player = player if isinstance(player, dict) else {}
        matching = [
            power for power in player.get("powers") or []
            if isinstance(power, dict)
            and _normalized_game_id(power.get("id") or power.get("name"))
            in {"rebound", "reboundpower"}
        ]
        if not matching:
            return 0
        if len(matching) != 1:
            return None
        amount = matching[0].get("amount")
        if type(amount) is not int or amount < 0:
            return None
        return amount

    before_rebound = rebound_amount(before_combat)
    after_rebound = rebound_amount(after_combat)
    if (
        before_rebound is None or before_rebound < 1
        or after_rebound != before_rebound - 1
    ):
        return False

    before_monsters = {
        monster.get("enemy_instance_id"): monster
        for monster in before_combat.get("monsters") or []
        if isinstance(monster, dict)
        and isinstance(monster.get("enemy_instance_id"), str)
    }
    after_monsters = {
        monster.get("enemy_instance_id"): monster
        for monster in after_combat.get("monsters") or []
        if isinstance(monster, dict)
        and isinstance(monster.get("enemy_instance_id"), str)
    }
    if not before_monsters or not set(before_monsters) <= set(after_monsters):
        return False
    observed_damage = 0
    for enemy_id, monster_before in before_monsters.items():
        monster_after = after_monsters[enemy_id]
        before_hp = monster_before.get("current_hp")
        before_block = monster_before.get("block")
        after_hp = monster_after.get("current_hp")
        after_block = monster_after.get("block")
        if not all(
            type(value) is int and value >= 0
            for value in (before_hp, before_block, after_hp, after_block)
        ):
            return False
        observed_damage += max(
            0, before_hp + before_block - after_hp - after_block
        )
    return observed_damage > 0


def played_card_resolution_pending(before, after, payload):
    """Return whether a same-turn card effect is still settling.

    CommunicationMod can publish the state-sequence advance for a card before
    the card's asynchronous exhaust/discard callbacks have finished.  In that
    narrow window the card is still serialized in ``hand`` even though the
    game has already accepted and started resolving the play.  Keep the
    ordinary verifier strict; the run loop may wait for this one exact
    postcondition instead of treating the transient frame as a failed play.
    """

    if not isinstance(payload, dict) or payload.get("action") != "play":
        return False
    before_game = before.get("game_state") or {}
    after_game = after.get("game_state") or {}
    if (
        before_game.get("room_phase") != "COMBAT"
        or after_game.get("room_phase") != "COMBAT"
    ):
        return False
    before_combat = before_game.get("combat_state") or {}
    after_combat = after_game.get("combat_state") or {}
    if before_combat.get("turn") != after_combat.get("turn"):
        return False
    return (
        card_in_hand(after, payload.get("card_instance_id"))
        and not _rebound_sweeping_beam_same_turn_redraw(
            before, after, payload
        )
    )


def _latest_played_card_settlement_state(before, after, payload):
    """Read the bridge's latest frame before declaring a settle timeout.

    A successful one-frame wait can race the CommunicationMod card callback:
    the receipt advances ``state_seq`` while the local ``after`` object still
    contains the played card.  The bridge may publish the same sequence again
    after the callback moves that card to a pile.  Accept only a fresh frame
    that proves the exact instance has left the hand; otherwise keep the
    existing fail-closed timeout.
    """

    for attempt in range(3):
        try:
            latest = stsctl.load_state()
        except (OSError, ValueError, KeyError, TypeError):
            latest = None
        if (
            isinstance(latest, dict)
            and not played_card_resolution_pending(before, latest, payload)
        ):
            return latest
        if attempt < 2:
            time.sleep(0.05)
    return None


def settle_played_card_resolution(before, after, payload, run_context=None):
    """Wait for a bounded, same-turn card-resolution frame to settle.

    This is deliberately narrower than a generic retry: it sends only
    protocol-bound ``wait`` reads, never replays the card, and exits as soon
    as the exact played instance leaves the hand, combat ends, or the turn
    changes (the existing verifier permits a legitimate redraw after a turn
    transition).  A card that remains in hand after the bound is still a hard
    safety error, preserving fail-closed behavior for a real no-op or an
    unsupported retain mechanic.
    """

    if not played_card_resolution_pending(before, after, payload):
        return after
    original_after_seq = after.get("state_seq")
    settle_attempts = 0
    while played_card_resolution_pending(before, after, payload):
        settle_attempts += 1
        if settle_attempts > MAX_PLAYED_CARD_SETTLE_ATTEMPTS:
            latest = _latest_played_card_settlement_state(
                before, after, payload
            )
            if latest is not None:
                after = latest
                break
            raise SafetyError(
                "played card resolution remained in hand after bounded waits"
            )
        wait_payload = stsctl.bound_payload(after, "wait", frames=1)
        outcome = send_payload_exactly_once(wait_payload, after, run_context)
        if isinstance(outcome, MissingReceiptOutcome):
            settled = outcome.state
            verify_missing_receipt_effect(after, wait_payload, settled, [])
        else:
            receipt = outcome
            if not receipt.get("success"):
                raise SafetyError(
                    f"played-card resolution wait failed: {receipt}"
                )
            settled = stsctl.load_state()
            if receipt.get("accepted_state_seq") != after.get("state_seq"):
                raise SafetyError(
                    "played-card resolution wait accepted a different sequence"
                )
            if receipt.get("result_state_seq") != settled.get("state_seq"):
                raise SafetyError(
                    "played-card resolution wait result sequence differs from "
                    "latest state"
                )
            verify_effect(after, wait_payload, settled, [])
        validate_bound_context(settled, run_context or {})
        after = settled

    append_trace({
        "record_type": "card_resolution_settle",
        "time": time.time(),
        "policy_version": POLICY_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "attempt_id": (run_context or {}).get("attempt_id"),
        "run_id": (run_context or {}).get("run_id"),
        "seed": (run_context or {}).get("seed"),
        "character": (run_context or {}).get("character"),
        "ascension_level": (run_context or {}).get("ascension_level"),
        "run_type": (run_context or {}).get("run_type"),
        "selection_id": (run_context or {}).get("selection_id"),
        "selection_digest": (run_context or {}).get("selection_digest"),
        "state_seq": after.get("state_seq"),
        "kind": "played_card_resolution_pending",
        "card_instance_id": payload.get("card_instance_id"),
        "settle_attempts": settle_attempts,
        "original_after_seq": original_after_seq,
        "settled_after_seq": after.get("state_seq"),
        "settled_phase": after.get("phase"),
    })
    return after


def _decision_binding_advanced(before, after, payload=None):
    """Return whether a timed-out action produced authoritative progress.

    A newer frame alone is insufficient: STATE frames also advance state_seq.
    Re-deciding is safe only after the decision or phase has changed, proving
    that the old state-changing request cannot still target the same binding.
    """

    before_seq = before.get("state_seq")
    after_seq = after.get("state_seq")
    if (
        type(before_seq) is int
        and type(after_seq) is int
        and after_seq > before_seq
        and (
            after.get("decision_id") != before.get("decision_id")
            or after.get("phase") != before.get("phase")
        )
    ):
        return True

    # A HAND_SELECT CHOOSE can be accepted by the bridge before its
    # confirmation surface becomes ready.  The phase and decision id may be
    # unchanged, so the normal binding test misses the only durable proof:
    # the exact requested card moved from the unselected hand into the
    # authoritative selected list.  This is safe to re-plan from and never
    # replays the timed-out write.
    if (
        not isinstance(payload, dict)
        or str(payload.get("action") or "").lower() != "choose"
        or before.get("phase") != "HAND_SELECT"
        or after.get("phase") != "HAND_SELECT"
        or type(before_seq) is not int
        or type(after_seq) is not int
        or after_seq <= before_seq
    ):
        return False
    option = next(
        (
            item for item in before.get("options") or []
            if item.get("option_id") == payload.get("option_id")
        ),
        None,
    )
    target_id = ((option or {}).get("target") or {}).get("card_instance_id")
    if not isinstance(target_id, str) or not target_id:
        return False
    before_screen = (before.get("game_state") or {}).get("screen_state") or {}
    after_screen = (after.get("game_state") or {}).get("screen_state") or {}
    before_selected = {
        item.get("card_instance_id")
        for item in (before_screen.get("selected_cards") or before_screen.get("selected") or [])
        if isinstance(item, dict)
    }
    after_selected = {
        item.get("card_instance_id")
        for item in (after_screen.get("selected_cards") or after_screen.get("selected") or [])
        if isinstance(item, dict)
    }
    return target_id not in before_selected and target_id in after_selected


def bind_attempt_payload(payload, state, run_context):
    """Bind one in-run command to the controller's frozen attempt identity."""

    if not isinstance(payload, dict):
        raise SafetyError("command payload must be an object")
    action = str(payload.get("action") or "").lower()
    if action == "state" or state.get("in_game") is not True:
        return payload
    if not isinstance(run_context, dict):
        raise SafetyError("in-game command requires a frozen run context")
    for field in ATTEMPT_BINDING_FIELDS:
        if field not in run_context:
            raise SafetyError(f"run context is missing command binding: {field}")
        expected = run_context[field]
        if field in payload and (
            type(payload[field]) is not type(expected)
            or payload[field] != expected
        ):
            raise SafetyError(f"command attempt binding mismatch: {field}")
        if field in state and (
            type(state[field]) is not type(expected)
            or state[field] != expected
        ):
            raise SafetyError(f"active state attempt binding mismatch: {field}")
        payload[field] = expected
    return payload


def validate_receipt_attempt_binding(receipt, payload, state):
    """Require an in-run receipt to echo the exact requested attempt."""

    action = str(payload.get("action") or "").lower()
    if action == "state" or state.get("in_game") is not True:
        return receipt
    if not isinstance(receipt, dict):
        raise SafetyError("action receipt must be an object")
    for field in ATTEMPT_BINDING_FIELDS:
        expected = payload.get(field)
        observed = receipt.get(field)
        if type(observed) is not type(expected) or observed != expected:
            raise SafetyError(f"action receipt attempt binding mismatch: {field}")
    return receipt


def send_payload_exactly_once(payload, state, run_context=None):
    """Send one action without ever replaying an ambiguously timed-out write.

    ``MissingReceiptOutcome`` means STATE proved that the authoritative
    decision binding moved.  The caller must verify the original action's
    effect against that recovered state before recording it as confirmed.  An
    unchanged binding fails closed.
    """

    payload = bind_attempt_payload(payload, state, run_context)
    receipt_timeout_seconds = (
        HAND_SELECT_ACTION_RECEIPT_TIMEOUT_SECONDS
        if (
            str(payload.get("action") or "").lower() == "choose"
            and state.get("phase") == "HAND_SELECT"
        )
        else ACTION_RECEIPT_TIMEOUT_SECONDS
    )
    try:
        receipt = stsctl.send_payload(
            payload, timeout=receipt_timeout_seconds
        )
        return validate_receipt_attempt_binding(receipt, payload, state)
    except TimeoutError:
        append_protocol_event(
            "receipt_timeout",
            state,
            payload,
            run_context=run_context,
            timeout_seconds=receipt_timeout_seconds,
        )

    try:
        receipt = stsctl.wait_for_receipt(
            payload["id"], timeout=LATE_RECEIPT_RECHECK_SECONDS
        )
    except TimeoutError:
        append_protocol_event(
            "late_receipt_unresolved",
            state,
            payload,
            run_context=run_context,
            timeout_seconds=LATE_RECEIPT_RECHECK_SECONDS,
        )
    else:
        append_protocol_event(
            "late_receipt_recovered",
            state,
            payload,
            receipt,
            run_context,
        )
        return validate_receipt_attempt_binding(receipt, payload, state)

    # CommunicationMod can publish the next authoritative frame before it
    # manages to finalize the receipt for the preceding write.  In that case a
    # read-only STATE command is itself blocked by the still-pending write and
    # would only create a false P0 resynchronization timeout.  The state file
    # is already the bridge's authoritative output, so accept the transition
    # only when its decision binding advanced; the caller still runs the
    # action-specific missing-receipt effect verifier before recording it.
    try:
        refreshed = stsctl.load_state()
    except (OSError, ValueError, KeyError, TypeError):
        refreshed = None
    if (
        isinstance(refreshed, dict)
        and _decision_binding_advanced(state, refreshed, payload)
    ):
        append_protocol_event(
            "timeout_state_advanced",
            refreshed,
            payload,
            run_context=run_context,
            original_state_seq=state.get("state_seq"),
            original_decision_id=state.get("decision_id"),
            original_phase=state.get("phase"),
            recovery="direct_authoritative_state_probe",
        )
        return MissingReceiptOutcome(refreshed)

    resynchronize_protocol(
        state,
        payload,
        run_context,
        reason="action_receipt_timeout",
    )
    refreshed = stsctl.load_state()
    if not _decision_binding_advanced(state, refreshed, payload):
        append_protocol_event(
            "ambiguous_timeout",
            refreshed,
            payload,
            run_context=run_context,
            original_state_seq=state.get("state_seq"),
            original_decision_id=state.get("decision_id"),
            original_phase=state.get("phase"),
        )
        raise SafetyError(
            "action receipt remained ambiguous after STATE resynchronization; "
            "refusing to replay the state-changing request"
        )
    append_protocol_event(
        "timeout_state_advanced",
        refreshed,
        payload,
        run_context=run_context,
        original_state_seq=state.get("state_seq"),
        original_decision_id=state.get("decision_id"),
        original_phase=state.get("phase"),
    )
    return MissingReceiptOutcome(refreshed)


def compact_action(
    state,
    payload,
    receipt,
    after,
    decision=None,
    run_context=None,
    receipt_missing=False,
    state_effect_verified=False,
):
    run_context = run_context if isinstance(run_context, dict) else {}
    has_receipt = isinstance(receipt, dict)
    receipt = receipt if has_receipt else {}
    requested_target_id = receipt.get("requested_target_id")
    if not has_receipt:
        requested_target_id = (
            payload.get("option_id")
            or payload.get("card_instance_id")
            or payload.get("potion_instance_id")
            or payload.get("target_id")
            or ("action:wait" if payload.get("action") == "wait" else None)
        )
    before_game = state.get("game_state") or {}
    game = after.get("game_state") or {}
    encounter_serial = combat_trace_encounter_serial(
        before_game, run_context.get("attempt_id"), state.get("state_seq")
    )
    strategic_noncombat_surface = has_strategic_noncombat_surface(
        state, payload
    )
    record = {
        "record_type": "decision",
        "decision_schema_version": 2,
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "audit_projection_version": AUDIT_PROJECTION_VERSION,
        "time": time.time(),
        "policy_version": run_context.get("policy_version", POLICY_VERSION),
        "strategy_revision": STRATEGY_REVISION,
        "strategy_hash": STRATEGY_HASH,
        "decision_hash": run_context.get("decision_hash", DECISION_HASH),
        "performance_hash": PERFORMANCE_HASH,
        "controller_hash": run_context.get("controller_hash", CONTROLLER_HASH),
        "macro_policy": MACRO_POLICY_PROFILE,
        "attempt_id": run_context.get("attempt_id"),
        "goal_mode": run_context.get("goal_mode"),
        "run_id": run_context.get("run_id", run_id(before_game)),
        "seed": run_context.get("seed", before_game.get("seed")),
        "character": run_context.get("character", before_game.get("class")),
        "ascension_level": run_context.get(
            "ascension_level", before_game.get("ascension_level")
        ),
        "run_type": run_context.get("run_type"),
        "selection_id": run_context.get("selection_id"),
        "selection_digest": run_context.get("selection_digest"),
        "before_seq": state["state_seq"],
        "after_seq": after["state_seq"],
        "phase": state["phase"],
        "action": payload["action"],
        "potion_operation": (
            payload.get("operation")
            if payload.get("action") == "potion" else None
        ),
        "requested_target_id": requested_target_id,
        "resolved_target_id": receipt.get("resolved_target_id"),
        "act": game.get("act"),
        "floor": game.get("floor"),
        "hp_before": before_game.get("current_hp"),
        "hp_after": game.get("current_hp"),
        "decision": decision or {},
        "authoritative_state_before": authoritative_state_snapshot(state),
        "authoritative_state_after": authoritative_state_snapshot(after),
        "decision_outcome": {
            "hp_delta": (
                None
                if before_game.get("current_hp") is None or game.get("current_hp") is None
                else int(game.get("current_hp")) - int(before_game.get("current_hp"))
            ),
            "gold_delta": (
                None
                if before_game.get("gold") is None or game.get("gold") is None
                else int(game.get("gold")) - int(before_game.get("gold"))
            ),
            "keys_before": {
                "ruby": before_game.get("has_ruby_key"),
                "emerald": before_game.get("has_emerald_key"),
                "sapphire": before_game.get("has_sapphire_key"),
            },
            "keys_after": {
                "ruby": game.get("has_ruby_key"),
                "emerald": game.get("has_emerald_key"),
                "sapphire": game.get("has_sapphire_key"),
            },
            "phase_before": state.get("phase"),
            "phase_after": after.get("phase"),
            "room_phase_before": before_game.get("room_phase"),
            "room_phase_after": game.get("room_phase"),
            "room_type_before": before_game.get("room_type"),
            "room_type_after": game.get("room_type"),
            "screen_type_before": before_game.get("screen_type"),
            "screen_type_after": game.get("screen_type"),
            "act_before": before_game.get("act"),
            "act_after": game.get("act"),
            "floor_before": before_game.get("floor"),
            "floor_after": game.get("floor"),
        },
    }
    if receipt_missing:
        record.update({
            "receipt_missing": True,
            "state_effect_verified": bool(state_effect_verified),
        })
    if (
        before_game.get("room_phase") != "COMBAT"
        or strategic_noncombat_surface
    ):
        before_inventory = observable_inventory_snapshot(before_game)
        after_inventory = observable_inventory_snapshot(game)
        record["observable_state_before"] = before_inventory
        record["observable_state_after"] = after_inventory
        deck_counts = {}
        for card in before_game.get("deck") or []:
            key = f'{card.get("id")}+{int(card.get("upgrades") or 0)}'
            deck_counts[key] = deck_counts.get(key, 0) + 1
        record["decision_context"] = {
            "gold": before_game.get("gold"),
            "max_hp": before_game.get("max_hp"),
            "act_boss": before_game.get("act_boss"),
            "deck_counts": deck_counts,
            "relic_ids": [item.get("id") for item in before_game.get("relics") or []],
            "potion_ids": [item.get("id") for item in before_game.get("potions") or []],
            "keys": {
                "ruby": before_game.get("has_ruby_key"),
                "emerald": before_game.get("has_emerald_key"),
                "sapphire": before_game.get("has_sapphire_key"),
            },
            "observable_state": before_inventory,
        }
        inventory_delta = observable_inventory_delta(
            before_inventory, after_inventory
        )
        record["decision_outcome"].update(inventory_delta)
        observed_acquisition = selected_shop_observable_acquisition(
            state, payload, inventory_delta
        )
        if observed_acquisition is None:
            observed_acquisition = selected_event_card_package_acquisition(
                state, payload, inventory_delta
            )
        if observed_acquisition is not None:
            record["decision_outcome"]["observable_delta"] = (
                observed_acquisition
            )
    combat_choice_transition = combat_choice_transition_claim(state, after)
    if combat_choice_transition is not None:
        record["decision_outcome"]["combat_choice_transition"] = (
            combat_choice_transition
        )
    before_combat = before_game.get("combat_state") or {}
    if strategic_noncombat_surface:
        options = state.get("options") or []
        record["available_options_before"] = [compact_option(item) for item in options]
        record["available_commands_before"] = list(
            state.get("available_commands") or []
        )
        resource_options = _resource_preparation_options(state, payload)
        if resource_options:
            record["decision_surface_kind"] = "resource_preparation"
            record["parent_choice_surface_pending"] = True
            record["resource_preparation_options_before"] = [
                compact_option(item) for item in resource_options
            ]
        record["legal_choices_before"] = canonical_legal_choices(
            state, payload, decision or {}
        )
        selected_choice_ids = [
            row.get("choice_id")
            for row in record["legal_choices_before"]
            if row.get("selected") is True
        ]
        record["selected_choice_ids"] = selected_choice_ids
        record["final_choice_ids"] = list(selected_choice_ids)
        if payload.get("action") == "choose":
            chosen = next(
                (
                    item for item in options
                    if item.get("option_id") == payload.get("option_id")
                ),
                None,
            )
            record["chosen_option_before"] = compact_option(chosen or {})
        elif payload.get("action") in {"return", "proceed"}:
            record["chosen_option_before"] = {
                "option_id": f'action:{payload.get("action")}',
                "choice_index": None,
                "label": payload.get("action"),
                "target": {
                    "kind": "protocol_action",
                    "action": payload.get("action"),
                },
            }
        elif payload.get("action") == "potion" and resource_options:
            chosen = next(
                (
                    item for item in resource_options
                    if str((item.get("target") or {}).get("potion_instance_id"))
                    == str(payload.get("potion_instance_id"))
                    and str((item.get("target") or {}).get("operation"))
                    == str(payload.get("operation"))
                ),
                None,
            )
            record["chosen_option_before"] = compact_option(chosen or {})
        record["authoritative_choice_settlement"] = (
            authoritative_choice_settlement(record)
        )
    if payload.get("action") == "play":
        card_id = payload.get("card_instance_id")
        card = next((item for item in before_combat.get("hand") or [] if item.get("card_instance_id") == card_id), None)
        record["card_instance_id"] = card_id
        record["card_id"] = (card or {}).get("id")
    if payload.get("action") == "potion":
        potion_id = payload.get("potion_instance_id")
        potion = next(
            (item for item in before_game.get("potions") or [] if item.get("potion_instance_id") == potion_id),
            None,
        )
        record["potion_instance_id"] = potion_id
        record["potion_operation"] = payload.get("operation")
        record["potion"] = compact_audit_value(potion or {})
    target_id = payload.get("enemy_instance_id")
    if target_id:
        target = next((item for item in before_combat.get("monsters") or [] if item.get("enemy_instance_id") == target_id), None)
        record["enemy_instance_id"] = target_id
        if target is not None:
            record["target_before"] = {
                "id": target.get("id"),
                "index": target.get("monster_index"),
                "hp": target.get("current_hp"),
                "block": target.get("block"),
                "powers": target.get("powers") or [],
            }
    if before_game.get("room_phase") == "COMBAT":
        if payload.get("action") in {"play", "potion", "end"}:
            record.update(combat_trace_context(
                before_game,
                phase=state.get("phase"),
                attempt_id=(run_context or {}).get("attempt_id"),
                available_commands=state.get("available_commands") or [],
                payload=payload,
                decision=decision or {},
                encounter_serial=encounter_serial,
            ))
            record["decision_outcome"].update(
                combat_post_action_context(
                    before_game, game, payload.get("action")
                )
            )
        parsed_before = Game.from_json(before_game, state.get("available_commands") or [])
        raw_monsters_before = before_combat.get("monsters") or []
        raw_player_before = before_combat.get("player") or {}
        player_block_before = max(
            0, int(raw_player_before.get("block") or 0)
        )
        relic_tokens = {
            "".join(
                character
                for character in str(item.get("id") or "").lower()
                if character.isalnum()
            )
            for item in before_game.get("relics") or []
        }
        before_combat_ends = (
            not combat_predictor.living_monsters(parsed_before)
            or combat_predictor.safe_to_wait_for_passive_kills(parsed_before)
        )
        before_turn_outcome = combat_predictor.projected_turn_outcome(
            parsed_before,
            combat_ends_before_next_turn=before_combat_ends,
        )
        end_search = (
            (decision or {}).get("search") or {}
            if payload.get("action") == "end"
            else {}
        )

        def exact_end_value(field, fallback):
            value = end_search.get(field)
            return (
                value
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                else fallback
            )

        record.update(
            {
                "energy_before": raw_player_before.get("energy"),
                "player_block_before": player_block_before,
                "orichalcum_active_before": (
                    player_block_before == 0
                    and "orichalcum" in relic_tokens
                ),
                "projected_incoming_before": combat_predictor.incoming_damage(parsed_before),
                "projected_block_before": combat_predictor.projected_player_block(parsed_before),
                # Components must come from the same ordered lifecycle.  A
                # Burn can consume block before the enemy attacks, so adding
                # two independently projected snapshots is not composable.
                "projected_attack_hp_loss_before": exact_end_value(
                    "projected_attack_hp_loss",
                    before_turn_outcome.attack_hp_loss,
                ),
                "projected_end_turn_hp_loss_before": exact_end_value(
                    "projected_end_turn_hp_loss",
                    before_turn_outcome.end_turn_hp_loss,
                ),
                "projected_end_turn_healing_before": (
                    combat_predictor.projected_end_turn_healing(
                        parsed_before,
                        preceding_hp_loss=before_turn_outcome.end_turn_hp_loss,
                    )
                ),
                "projected_next_turn_start_hp_loss_before": exact_end_value(
                    "projected_next_turn_start_hp_loss",
                    before_turn_outcome.next_turn_start_hp_loss,
                ),
                "projected_hp_loss_before": exact_end_value(
                    "projected_hp_loss",
                    before_turn_outcome.total_hp_loss,
                ),
                "projected_fairy_revive_consumed_before": (
                    end_search.get(
                        "fairy_revive_consumed",
                        getattr(
                            before_turn_outcome,
                            "fairy_revive_consumed",
                            False,
                        ),
                    )
                ),
                "projected_fairy_revive_healing_before": (
                    end_search.get(
                        "fairy_revive_healing",
                        getattr(
                            before_turn_outcome,
                            "fairy_revive_healing",
                            0,
                        ),
                    )
                ),
                "projected_player_hp_after_turn_before": (
                    end_search.get(
                        "projected_player_hp_after_turn",
                        getattr(
                            before_turn_outcome, "final_player_hp", None
                        ),
                    )
                ),
                "projected_player_hp_delta_before": (
                    end_search.get(
                        "projected_player_hp_delta",
                        getattr(
                            before_turn_outcome, "player_hp_delta", None
                        ),
                    )
                ),
                "doomed_enemy_ids_before": projected_doomed_enemy_ids(
                    parsed_before, raw_monsters_before
                ),
            }
        )
    if game.get("room_phase") == "COMBAT":
        parsed = Game.from_json(game, after.get("available_commands") or [])
        raw_monsters = ((game.get("combat_state") or {}).get("monsters") or [])
        combat_ends = (
            not combat_predictor.living_monsters(parsed)
            or combat_predictor.safe_to_wait_for_passive_kills(parsed)
        )
        turn_outcome = combat_predictor.projected_turn_outcome(
            parsed,
            combat_ends_before_next_turn=combat_ends,
        )
        record.update(
            {
                "doomed_enemy_ids_after": projected_doomed_enemy_ids(
                    parsed, raw_monsters
                ),
                "projected_incoming": combat_predictor.incoming_damage(parsed),
                "projected_block": combat_predictor.projected_player_block(parsed),
                "projected_attack_hp_loss": turn_outcome.attack_hp_loss,
                "projected_end_turn_hp_loss": turn_outcome.end_turn_hp_loss,
                "projected_next_turn_start_hp_loss": turn_outcome.next_turn_start_hp_loss,
                "projected_hp_loss": turn_outcome.total_hp_loss,
                "projected_fairy_revive_consumed": (
                    getattr(
                        turn_outcome, "fairy_revive_consumed", False
                    )
                ),
                "projected_fairy_revive_healing": (
                    getattr(
                        turn_outcome, "fairy_revive_healing", 0
                    )
                ),
                "projected_player_hp_after_turn": (
                    getattr(turn_outcome, "final_player_hp", None)
                ),
                "projected_player_hp_delta": (
                    getattr(turn_outcome, "player_hp_delta", None)
                ),
            }
        )
    if before_game.get("room_phase") == "COMBAT" and payload.get("action") in {
        "play", "potion", "end"
    }:
        record["plan_true_combat_end_predicted"] = bool(
            ((record.get("decision") or {}).get("search") or {}).get(
                "true_combat_end"
            )
        )
        record["damage_model"] = damage_model_context(
            payload.get("action"),
            {
                **(record.get("decision") or {}),
                "projected_attack_hp_loss_before": record.get(
                    "projected_attack_hp_loss_before"
                ),
                "projected_end_turn_hp_loss_before": record.get(
                    "projected_end_turn_hp_loss_before"
                ),
                "projected_next_turn_start_hp_loss_before": record.get(
                    "projected_next_turn_start_hp_loss_before"
                ),
                "projected_hp_loss_before": record.get(
                    "projected_hp_loss_before"
                ),
            },
            record.get("decision_outcome") or {},
            before_game=before_game,
            after_game=game,
            available_commands=state.get("available_commands") or [],
            card_id=record.get("card_id"),
            card_instance_id=record.get("card_instance_id"),
            target_id=record.get("enemy_instance_id"),
        )
        record["action_true_combat_end_predicted"] = (
            action_true_combat_end_prediction(
                record.get("decision") or {},
                current_action_enemy_hp_loss=(
                    record["damage_model"].get(
                        "hero_to_monsters_predicted"
                    )
                ),
                living_enemy_hp_total=sum(
                    max(0, int(monster.get("current_hp") or 0))
                    for monster in raw_monsters_before
                    if isinstance(monster, dict)
                    and int(monster.get("current_hp") or 0) > 0
                    and monster.get("is_gone") is not True
                    and monster.get("half_dead") is not True
                ),
            )
        )
        if payload.get("action") == "end":
            record["end_turn_resources"] = end_turn_resource_context(
                before_combat,
                record.get("decision") or {},
            )
    return record


def ensure_single_terminal_trace(result):
    """Append or validate the sole authoritative per-attempt terminal row."""

    trace_path = attempt_trace_path(result.get("attempt_id"))
    terminals = []
    if trace_path.exists():
        try:
            with trace_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SafetyError(
                            f"attempt trace line {line_number} is malformed"
                        ) from exc
                    if (
                        isinstance(record, dict)
                        and record.get("record_type") == "terminal_result"
                    ):
                        terminals.append(record)
        except OSError as exc:
            raise SafetyError("attempt terminal trace is unreadable") from exc
    if len(terminals) > 1:
        raise SafetyError("attempt trace contains duplicate terminal results")
    if len(terminals) == 1:
        terminal = terminals[0]
        mismatches = [
            key for key, value in result.items()
            if terminal.get(key) != value
        ]
        if mismatches:
            raise SafetyError(
                "existing terminal trace differs from result: "
                + ",".join(sorted(mismatches))
            )
        return False
    append_trace({
        **result,
        "record_type": "terminal_result",
        "time": time.time(),
    })
    return True


def final_result(state, actions, run_context=None, termination_kind="game_over"):
    if not isinstance(run_context, dict):
        raise SafetyError("final result requires a bound run context")
    if termination_kind == "game_over" and not is_game_over(state):
        raise SafetyError("game_over result requires an authoritative GAME_OVER state")
    validate_bound_context(state, run_context)
    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    victory_values = [
        source[field]
        for source, field in ((game, "run_victory"), (screen, "victory"))
        if field in source
    ]
    if not victory_values or any(type(value) is not bool for value in victory_values):
        raise SafetyError("terminal victory flag is missing or invalid")
    if len(set(victory_values)) != 1:
        raise SafetyError("terminal victory flags conflict")
    if "heart_defeated" not in game or type(game["heart_defeated"]) is not bool:
        raise SafetyError("terminal heart_defeated flag is missing or invalid")
    victory = victory_values[0]
    heart_defeated = game["heart_defeated"]
    if heart_defeated and not victory:
        raise SafetyError("Heart defeat cannot be recorded without victory")
    keys = {}
    for key_name, field in (
        ("ruby", "has_ruby_key"),
        ("emerald", "has_emerald_key"),
        ("sapphire", "has_sapphire_key"),
    ):
        value = game.get(field, False)
        if type(value) is not bool:
            raise SafetyError(f"terminal {field} flag is invalid")
        keys[key_name] = value
    observed_max_act = game.get("act")
    isolated_trace = attempt_trace_path(run_context.get("attempt_id"))
    if isolated_trace.exists():
        try:
            with isolated_trace.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record_act = json.loads(line).get("act")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if type(record_act) is int:
                        observed_max_act = max(
                            int(observed_max_act or 0), record_act
                        )
        except OSError as exc:
            raise SafetyError("isolated attempt trace is unreadable") from exc
    if type(observed_max_act) is not int:
        raise SafetyError("terminal act is missing or invalid")
    validate_terminal_state_binding(state, run_context)
    run_context = run_context_lib.bind_terminal_state(
        RUN_CONTEXT_PATH,
        state,
        run_context,
        DECISION_HASH,
        CONTROLLER_HASH,
    )
    result = {
        "schema_version": 2,
        "policy_version": POLICY_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "strategy_hash": STRATEGY_HASH,
        "decision_hash": DECISION_HASH,
        "performance_hash": PERFORMANCE_HASH,
        "controller_hash": CONTROLLER_HASH,
        "macro_policy": MACRO_POLICY_PROFILE,
        "attempt_id": (run_context or {}).get("attempt_id"),
        "goal_mode": (run_context or {}).get("goal_mode"),
        "progression_at_start": (run_context or {}).get("progression_at_start"),
        "selection": (run_context or {}).get("selection"),
        "selection_id": (run_context or {}).get("selection_id"),
        "selection_digest": (run_context or {}).get("selection_digest"),
        "run_type": (run_context or {}).get("run_type"),
        "termination_kind": termination_kind,
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "run_id": run_id(game),
        "seed": game.get("seed"),
        "state_seq": state.get("state_seq"),
        "terminal_state_seq": state.get("state_seq"),
        "class": game.get("class"),
        "character": game.get("class"),
        "ascension_level": game.get("ascension_level"),
        "act": game.get("act"),
        "observed_max_act": observed_max_act,
        "floor": game.get("floor"),
        "current_hp": game.get("current_hp"),
        "max_hp": game.get("max_hp"),
        "victory": victory,
        "heart_defeated": heart_defeated,
        "keys": keys,
        "deck": game.get("deck") or [],
        "relics": game.get("relics") or [],
        "potions": game.get("potions") or [],
        "actions": actions,
    }
    if RESULT_PATH.exists():
        try:
            previous_result = json.loads(
                RESULT_PATH.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyError("existing run result is unreadable") from exc
        if (
            isinstance(previous_result, dict)
            and previous_result.get("attempt_id") == result.get("attempt_id")
            and previous_result != result
        ):
            raise SafetyError(
                "existing run result differs for the same attempt"
            )
    stsctl.atomic_write_json(RESULT_PATH, result)
    append_result_history({**result, "record_type": "terminal_result"})
    ensure_single_terminal_trace(result)
    persist_attempt_snapshots(result)
    persist_automatic_run_audit(result)
    return result


def active_error_context():
    """Return context only when it belongs to the current run and binaries."""

    try:
        state = stsctl.load_state()
    except Exception:
        return _validated_cached_error_context()
    if not state.get("in_game") or is_game_over(state):
        return {}
    candidates = []
    try:
        disk_context = run_context_lib.load_context(RUN_CONTEXT_PATH) or {}
    except Exception:
        disk_context = {}
    if disk_context:
        candidates.append(disk_context)
    if isinstance(_LAST_RUN_CONTEXT, dict) and _LAST_RUN_CONTEXT not in candidates:
        candidates.append(_LAST_RUN_CONTEXT)
    for candidate in candidates:
        try:
            validated = validate_bound_context(state, candidate)
            remember_authoritative_state(state, validated)
            return validated
        except (run_context_lib.RunContextError, SafetyError):
            continue
    return {}


def persist_operational_result(error, active_context, validation=False):
    """Persist an operational result only for a verified live attempt."""

    if validation or not active_context:
        return False
    if RESULT_PATH.exists():
        try:
            existing = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and (
            existing.get("termination_kind") in {
                "game_over", "operational_error"
            }
            or (
                "error" not in existing
                and "heart_defeated" in existing
                and "victory" in existing
            )
        ) and (
            existing.get("attempt_id")
            and existing.get("attempt_id") == active_context.get("attempt_id")
        ):
            return False
    if (
        not isinstance(error, dict)
        or error.get("schema_version") != 2
        or error.get("termination_kind") != "operational_error"
        or error.get("attempt_id") != active_context.get("attempt_id")
    ):
        raise SafetyError(
            "operational result does not match its bound schema-v2 attempt"
        )
    stsctl.atomic_write_json(RESULT_PATH, error)
    append_result_history({**error, "record_type": "terminal_result"})
    return True


def _run(max_actions, validation=False, cache_warmup=True):
    global _ACTIVE_MACRO_ADVISOR
    reset_authoritative_error_fallback()
    initial = stsctl.load_state()
    game_state = initial.get("game_state") or {}
    if not initial.get("in_game"):
        raise SafetyError("autoplay expects an already-started run")
    if initial.get("protocol_version") != 2:
        raise SafetyError("unsupported or missing protocol_version")
    if initial.get("ready_for_command") is not True:
        raise SafetyError("authoritative state is not ready for a controller")
    if stsctl.COMMAND_PATH.exists():
        raise SafetyError("a pending command exists before controller start")
    if game_state.get("screen_type") == "GAME_OVER":
        raise SafetyError(
            "controller cannot attach after GAME_OVER; the prior session "
            "requires an abnormal-exit audit"
        )
    run_context = run_context_lib.load_or_create_context(
        initial,
        DECISION_HASH,
        CONTROLLER_HASH,
        RUN_CONTEXT_PATH,
        selection_path=PENDING_SELECTION_PATH,
    )
    remember_authoritative_state(initial, run_context)
    player_class = PlayerClass[game_state["class"]]

    model_trace_sink = append_trace
    model_trace_path = Path(TRACE_PATH)
    model_isolated_path = attempt_trace_path(
        run_context.get("attempt_id"), trace_path=model_trace_path
    )

    def append_model_record(record):
        model_trace_sink({
            "time": time.time(),
            "decision_schema_version": 2,
            "policy_version": POLICY_VERSION,
            "strategy_revision": STRATEGY_REVISION,
            "decision_hash": DECISION_HASH,
            "controller_hash": CONTROLLER_HASH,
            "attempt_id": run_context.get("attempt_id"),
            "run_id": run_context.get("run_id"),
            "seed": run_context.get("seed"),
            "character": run_context.get("character"),
            "ascension_level": run_context.get("ascension_level"),
            "run_type": run_context.get("run_type"),
            "selection_id": run_context.get("selection_id"),
            "selection_digest": run_context.get("selection_digest"),
            "state_seq": record.get(
                "before_seq", record.get("state_seq", initial.get("state_seq"))
            ),
            **record,
        }, trace_path=model_trace_path, isolated_path=model_isolated_path)

    macro_advisor = DeepSeekMacroAdvisor(
        MACRO_CONFIG,
        PROMPT_ASSEMBLER,
        callback=append_model_record,
    )
    if _ACTIVE_MACRO_ADVISOR is not None:
        macro_advisor.close(wait=True)
        raise SafetyError("a macro advisor is already active in this controller")
    _ACTIVE_MACRO_ADVISOR = macro_advisor
    agent = SimpleAgent(
        player_class,
        goal_mode=run_context["goal_mode"],
        macro_advisor=macro_advisor,
    )
    append_trace({
        "record_type": "controller_start",
        "decision_schema_version": 2,
        "time": time.time(),
        "policy_version": POLICY_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "strategy_hash": STRATEGY_HASH,
        "decision_hash": DECISION_HASH,
        "performance_hash": PERFORMANCE_HASH,
        "controller_hash": CONTROLLER_HASH,
        "macro_policy": MACRO_POLICY_PROFILE,
        "state_seq": initial.get("state_seq"),
        **run_context,
    })
    if cache_warmup and not validation:
        macro_advisor.prewarm(player_class.name)
    # Route risk is evaluated dynamically by the shared agent for every class.
    last_seq = initial["state_seq"] - 1
    actions = 0
    pending_selection = []
    consecutive_protocol_resyncs = 0
    initializing_resyncs = 0
    hand_select_settle_polls = 0
    recent_action_signatures = deque(maxlen=32)
    stable_wait_signature = None
    stable_wait_count = 0
    barricade_liveness_tracker = None

    while actions < max_actions:
        state = stsctl.load_state()
        if state.get("protocol_version") != 2:
            raise SafetyError("unsupported or missing protocol_version")
        validate_bound_context(state, run_context)
        remember_authoritative_state(state, run_context)
        if state["state_seq"] < last_seq:
            raise SafetyError("state sequence regressed")
        last_seq = state["state_seq"]
        game_json = state.get("game_state") or {}
        if game_json.get("screen_type") == "GAME_OVER":
            close_active_macro_advisor()
            result = final_result(
                state,
                trace_action_count(run_context.get("attempt_id")),
                run_context,
            )
            # The controller supervisor consumes this single terminal line as
            # UTF-8 evidence. Windows consoles may encode non-ASCII stdout
            # with the active code page (for example cp936), which makes an
            # otherwise valid game-over receipt look corrupt. Escape
            # non-ASCII here; trace/artifact files still retain their UTF-8
            # human-readable form.
            print(json.dumps(result, ensure_ascii=True))
            return 0
        # Combat creation is asynchronous: CommunicationMod can expose the
        # new room and a HAND_SELECT surface before the monster intent has
        # been rolled.  The bridge intentionally marks that frame
        # non-actionable and only permits STATE/WAIT.  Never ask the planner
        # for a gameplay action from that frame; use bounded read-only
        # resyncs until the authoritative combat turn is ready.
        if (
            state.get("phase") == "COMBAT_INITIALIZING"
            and state.get("ready_for_command") is not True
        ):
            initializing_resyncs += 1
            if initializing_resyncs > MAX_COMBAT_INITIALIZATION_RESYNCS:
                raise SafetyError(
                    "combat initialization did not become actionable after "
                    f"{MAX_COMBAT_INITIALIZATION_RESYNCS} resyncs"
                )
            resynchronize_protocol(
                state,
                run_context=run_context,
                reason="combat_initializing",
            )
            continue
        initializing_resyncs = 0
        # A completed CHOOSE receipt proves selection, not readiness for the
        # next gameplay action. Ask for fresh read-only frames while settling;
        # polling the local file alone can leave the bridge waiting for input.
        if (
            state.get("phase") == "HAND_SELECT"
            and state.get("ready_for_command") is not True
        ):
            hand_select_settle_polls += 1
            if hand_select_settle_polls > MAX_HAND_SELECT_SETTLE_POLLS:
                raise SafetyError(
                    "HAND_SELECT did not become actionable after "
                    f"{MAX_HAND_SELECT_SETTLE_POLLS} polls"
                )
            time.sleep(HAND_SELECT_SETTLE_POLL_SECONDS)
            resynchronize_protocol(
                state, run_context=run_context, reason="hand_select_settling",
            )
            continue
        hand_select_settle_polls = 0
        barricade_liveness_tracker = advance_barricade_liveness_guard(
            barricade_liveness_tracker, state
        )
        game = Game.from_json(game_json, state.get("available_commands") or [])
        bind_shop_protocol_surface(game, state)
        if hasattr(agent, "set_macro_protocol_context"):
            agent.set_macro_protocol_context({
                "attempt_id": run_context.get("attempt_id"),
                "run_id": run_context.get("run_id"),
                "state_seq": state.get("state_seq"),
                "decision_id": state.get("decision_id"),
                "phase": state.get("phase"),
            })
        action = None
        payload = overlay_recovery_payload(state)
        if payload is None:
            action = agent.get_next_action_in_game(game)
            if action is None:
                payload = stsctl.bound_payload(state, "wait", frames=1)
            else:
                payload = payload_for_action(state, game, action)

        selected_this_action = None
        if payload.get("action") == "choose" and state.get("phase") in {"GRID", "HAND_SELECT"}:
            selected_option = next(item for item in state.get("options") or [] if item["option_id"] == payload["option_id"])
            selected_id = (selected_option.get("target") or {}).get("card_instance_id")
            if selected_id:
                old_card = deck_card(state, selected_id)
                if old_card is None:
                    old_card = next(
                        (item for item in ((((state.get("game_state") or {}).get("combat_state") or {}).get("hand")) or []) if item.get("card_instance_id") == selected_id),
                        {"card_instance_id": selected_id},
                    )
                selected_this_action = dict(old_card)
                selected_this_action["_selection_action"] = before_game_action(state)
                screen = (state.get("game_state") or {}).get("screen_state") or {}
                relic_ids = {item.get("id") for item in (state.get("game_state") or {}).get("relics") or []}
                if state.get("phase") == "GRID" and "Astrolabe" in relic_ids and screen.get("num_cards") == 3:
                    selected_this_action["_selection_action"] = "AstrolabeTransform"

        outcome = send_payload_exactly_once(payload, state, run_context)
        receipt_missing = isinstance(outcome, MissingReceiptOutcome)
        if receipt_missing:
            receipt = None
            after = outcome.state
        else:
            receipt = outcome
            if receipt.get("status") == "rejected" and "stale state" in str(receipt.get("error")):
                append_protocol_event(
                    "stale_state_rejected",
                    state,
                    payload,
                    receipt,
                    run_context,
                    recovery="terminal_operational_error",
                )
                raise SafetyError("stale command rejected by the bridge")
            if recoverable_protocol_error(receipt):
                consecutive_protocol_resyncs += 1
                append_protocol_event(
                    "recoverable_protocol_error",
                    state,
                    payload,
                    receipt,
                    run_context,
                    recovery="state_resync",
                    consecutive_errors=consecutive_protocol_resyncs,
                )
                if consecutive_protocol_resyncs > MAX_RECOVERABLE_PROTOCOL_ERRORS:
                    raise SafetyError(f"repeated protocol errors: {receipt}")
                resynchronize_protocol(
                    state,
                    payload,
                    run_context,
                    reason="recoverable_protocol_error",
                )
                continue
            if not receipt.get("success"):
                failed_state = stsctl.load_state()
                if is_game_over(failed_state):
                    append_protocol_event(
                        "failed_receipt_at_game_over",
                        state,
                        payload,
                        receipt,
                        run_context,
                    )
                    close_active_macro_advisor()
                    final_result(
                        failed_state,
                        trace_action_count(run_context.get("attempt_id")),
                        run_context,
                        termination_kind="operational_error",
                    )
                    raise SafetyError(
                        "action receipt failed at authoritative GAME_OVER"
                    )
                raise SafetyError(f"action failed: {receipt}")
            if receipt.get("requested_target_id") != receipt.get("resolved_target_id"):
                raise SafetyError("requested and resolved targets differ")
            after = stsctl.load_state()
            if receipt.get("accepted_state_seq") != state["state_seq"]:
                raise SafetyError("accepted sequence differs from request state")
            if receipt.get("result_state_seq") != after.get("state_seq"):
                raise SafetyError("receipt result sequence differs from latest state")
        if payload.get("action") in {"play", "potion", "end"}:
            after = settle_transient_combat_resolution(
                state, after, run_context
            )
        if payload.get("action") == "potion":
            after = settle_potion_resolution(
                state, after, payload, run_context
            )
        if payload.get("action") == "play":
            after = settle_played_card_resolution(
                state, after, payload, run_context
            )
        consecutive_protocol_resyncs = 0
        validate_bound_context(after, run_context)
        remember_authoritative_state(after, run_context)
        effective_pending = pending_selection + ([selected_this_action] if selected_this_action is not None else [])
        try:
            verifier = (
                verify_missing_receipt_effect
                if receipt_missing
                else verify_effect
            )
            verifier(state, payload, after, effective_pending)
        except Exception:
            if receipt_missing:
                append_protocol_event(
                    "missing_receipt_effect_verification_failed",
                    state,
                    payload,
                    run_context=run_context,
                    recovered_state_seq=after.get("state_seq"),
                )
            raise
        if receipt_missing:
            append_protocol_event(
                "missing_receipt_effect_verified",
                state,
                payload,
                run_context=run_context,
                recovered_state_seq=after.get("state_seq"),
            )
        record_confirmed_effect(agent, game, state, payload)
        if selected_this_action is not None:
            pending_selection.append(selected_this_action)
        if payload.get("action") == "proceed" and state.get("phase") in {"GRID", "HAND_SELECT"}:
            pending_selection = []
        elif after.get("phase") not in {"GRID", "HAND_SELECT"}:
            pending_selection = []
        if game_json.get("room_phase") == "COMBAT" and payload.get("action") in {"play", "potion", "end"}:
            decision = agent.combat_planner.last_decision
        else:
            decision = agent.last_noncombat_decision
        decision_record = compact_action(
            state, payload, receipt, after, decision, run_context=run_context,
            receipt_missing=receipt_missing,
            state_effect_verified=receipt_missing,
        )
        append_trace(decision_record)
        # Persist a compact, independent replay corpus only after the bound
        # action has succeeded and its effect has been verified.  This is a
        # small append for non-combat choices, never part of the decision or
        # combat-frame hot path.
        persist_decision_case(
            decision_record,
            state=state,
            payload=payload,
            run_context=run_context,
        )
        if payload.get("action") == "wait":
            wait_signature = (
                state.get("phase"),
                state.get("decision_id"),
                game_json.get("screen_name"),
            )
            if wait_signature == stable_wait_signature:
                stable_wait_count += 1
            else:
                stable_wait_signature = wait_signature
                stable_wait_count = 1
            if stable_wait_count >= 120:
                raise SafetyError(
                    "no semantic progress after 120 waits: "
                    f"phase={state.get('phase')} screen={game_json.get('screen_name')}"
                )
        else:
            stable_wait_signature = None
            stable_wait_count = 0
            recent_action_signatures.append(action_cycle_signature(state, payload))
            cycle = repeating_action_cycle(recent_action_signatures)
            if cycle is not None:
                raise SafetyError(
                    "repeating successful action cycle detected: "
                    + " -> ".join(
                        f"{item[3]}:{item[5]}:{item[6] or item[10] or ''}"
                        for item in cycle
                    )
                )
        last_seq = after["state_seq"]
        actions += 1
    if validation:
        state = stsctl.load_state()
        game = state.get("game_state") or {}
        close_active_macro_advisor()
        print(json.dumps({
            "validation": True,
            "policy_version": POLICY_VERSION,
            "decision_hash": DECISION_HASH,
            "controller_hash": CONTROLLER_HASH,
            "macro_policy": MACRO_POLICY_PROFILE,
            "attempt_id": run_context.get("attempt_id"),
            "goal_mode": run_context.get("goal_mode"),
            "actions": actions,
            "state_seq": state.get("state_seq"),
            "act": game.get("act"),
            "floor": game.get("floor"),
            "hp": game.get("current_hp"),
            "phase": state.get("phase"),
        }, ensure_ascii=False))
        return 0
    close_active_macro_advisor()
    raise SafetyError(f"run exceeded {max_actions} atomic actions")


def run(max_actions, validation=False, cache_warmup=True):
    """Run one controller and synchronously drain every background callback."""

    try:
        return _run(
            max_actions,
            validation=validation,
            cache_warmup=cache_warmup,
        )
    finally:
        close_active_macro_advisor()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-actions", type=int, default=5000)
    parser.add_argument("--validation-actions", type=int)
    args = parser.parse_args()
    lease_acquired = False
    try:
        with ControllerLease():
            lease_acquired = True
            if args.validation_actions is not None:
                raise SystemExit(run(args.validation_actions, validation=True))
            raise SystemExit(run(args.max_actions))
    except Exception as exc:
        close_active_macro_advisor()
        if not lease_acquired:
            print(json.dumps({
                "schema_version": 2,
                "policy_version": POLICY_VERSION,
                "termination_kind": "controller_rejected",
                "error": type(exc).__name__,
                "message": str(exc),
            }, ensure_ascii=False), file=sys.stderr)
            raise
        active_context = active_error_context()
        current_state_seq = operational_error_state_sequence(active_context)
        error = {
            "schema_version": 2,
            "policy_version": POLICY_VERSION,
            "strategy_revision": STRATEGY_REVISION,
            "strategy_hash": STRATEGY_HASH,
            "decision_hash": DECISION_HASH,
            "performance_hash": PERFORMANCE_HASH,
            "controller_hash": CONTROLLER_HASH,
            "macro_policy": MACRO_POLICY_PROFILE,
            "attempt_id": active_context.get("attempt_id"),
            "goal_mode": active_context.get("goal_mode"),
            "progression_at_start": active_context.get("progression_at_start"),
            "selection": active_context.get("selection"),
            "selection_id": active_context.get("selection_id"),
            "selection_digest": active_context.get("selection_digest"),
            "run_id": active_context.get("run_id"),
            "seed": active_context.get("seed"),
            "run_type": active_context.get("run_type"),
            "termination_kind": "operational_error",
            "class": active_context.get("character"),
            "character": active_context.get("character"),
            "ascension_level": active_context.get("ascension_level"),
            "victory": False,
            "heart_defeated": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "state_seq": current_state_seq,
            "terminal_state_seq": current_state_seq,
            "last_authoritative_state": operational_error_state_snapshot(
                active_context
            ),
        }
        # A bounded validation is diagnostic and must never replace the
        # authoritative result of an autonomous run.  Its stderr/trace still
        # preserve the complete failure for review.
        persisted = persist_operational_result(
            error,
            active_context,
            validation=args.validation_actions is not None,
        )
        if persisted:
            append_trace({
                "record_type": "terminal_result",
                "time": time.time(),
                **error,
            })
            persist_automatic_run_audit(error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
