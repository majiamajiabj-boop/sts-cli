"""Fail-closed orchestration for one audited standard-A0 Heart attempt.

The controller deliberately starts *after* this module has selected a
character and obtained an authoritative START receipt.  Conversely, a
terminal GAME_OVER screen is left untouched until the outer runner has
recorded a clear terminal -> audit -> controller_exit chain.

This module is intentionally orchestration-only.  Character selection,
pre-run binding, controller execution, and post-run evidence validation stay
owned by their existing modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
import uuid
from pathlib import Path

import autoplay_runner
import campaign_selector
import freeze_manifest
import launch_game
import pre_run_binding
import stsctl


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 2
PROTOCOL_VERSION = 2
POLICY_VERSION = "fast-policy-v5"

STATE_NAME = "state.json"
COMMAND_NAME = "command.json"
RECEIPT_NAME = "action-receipt.json"
SELECTION_NAME = "next-run-selection.json"
HISTORY_NAME = "run-history.jsonl"
RESULT_NAME = "run-result.json"
FREEZE_MANIFEST_NAME = "freeze-manifest.json"
COHORT_REVIEW_NAME = "cohort-review.json"
CONTROLLER_LOCK_NAME = ".autoplay-controller.lock"
RUNNER_LOCK_NAME = ".autoplay-runner.lock"
ORCHESTRATOR_LOCK_NAME = ".campaign-attempt.lock"
MENU_TRANSITION_NAME = "menu-transition.json"
MAINTENANCE_RESOLUTION_NAME = "maintenance-resolution.json"
MAINTENANCE_RESTART_INTENT_NAME = "maintenance-restart-intent.json"
MAINTENANCE_SOURCE_RECONCILIATION_NAME = (
    "maintenance-restart-source-reconciliation.json"
)
MAINTENANCE_RUNTIME_FREEZE_NAME = (
    "maintenance-replacement-runtime-freeze.json"
)
MAINTENANCE_FREEZE_MANIFEST_NAME = "maintenance-freeze-manifest.json"
MAINTENANCE_PRECOMMIT_RECOVERY_NAME = (
    "maintenance-resolution-precommit-recovery.json"
)
START_RECOVERY_NAME = "started-run-recovery.json"
UNCONSUMED_START_RECOVERY_NAME = "proven-unconsumed-start.json"
ABANDONED_ACCEPTED_START_RECOVERY_NAME = "abandoned-accepted-start.json"

_LEGACY_UNCONSUMED_RECOVERY_FIELDS = {
    "schema_version",
    "record_type",
    "recovery_status",
    "selection_id",
    "selection_digest",
    "decision_hash",
    "controller_hash",
    "incident_sha256",
    "command_sha256",
    "state_seq",
    "decision_id",
    "phase",
    "dead_processes",
    "command",
    "selection",
    "receipt_absent",
    "acceptance_absent",
    "safe_to_retry_after_new_freeze",
    "created_at",
}
_CURRENT_UNCONSUMED_RECOVERY_FIELDS = (
    _LEGACY_UNCONSUMED_RECOVERY_FIELDS
    | {
        "start_was_resent",
        "incident_dispatch_status",
        "pre_start_state_probe",
        "pre_dispatch_runtime",
    }
)
_LEGACY_UNCONSUMED_INCIDENT_FIELDS = {
    "schema_version",
    "record_type",
    "incident_status",
    "policy_version",
    "selection_id",
    "selection_digest",
    "start_acceptance_sha256",
    "decision_hash",
    "controller_hash",
    "character",
    "ascension_level",
    "run_type",
    "start_dispatched",
    "start_committed",
    "selection_must_be_retained",
    "dispatch_status",
    "runner_invoked",
    "pending_selection_present",
    "start_payload",
    "start_receipt",
    "observed_started_state",
    "context_binding",
    "error_class",
    "error",
    "created_at",
}
_CURRENT_UNCONSUMED_INCIDENT_FIELDS = (
    _LEGACY_UNCONSUMED_INCIDENT_FIELDS
    | {"pre_start_state_probe", "pre_dispatch_runtime"}
)

# This is the complete active-attempt identity required by bridge.py.  Keep
# it explicit here: a shorter projection must never be accepted as a bound
# terminal action.
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

_BASE_RECEIPT_FIELDS = {
    "request_id",
    "success",
    "status",
    "error",
    "accepted_state_seq",
    "result_state_seq",
    "requested_target_id",
    "resolved_target_id",
}
_START_RECEIPT_FIELDS = _BASE_RECEIPT_FIELDS | {
    "selection_digest", "start_acceptance_sha256",
}
_PROCEED_RECEIPT_FIELDS = _BASE_RECEIPT_FIELDS | set(
    ATTEMPT_BINDING_FIELDS
)
_PROCEED_PAYLOAD_FIELDS = {
    "id",
    "action",
    "expected_seq",
    "decision_id",
    "phase",
    "target_id",
} | set(ATTEMPT_BINDING_FIELDS)


class CampaignAttemptError(RuntimeError):
    """Raised whenever one single-attempt invariant cannot be proved."""


class HeartVictoryHeld(CampaignAttemptError):
    """A valid Heart victory must remain on its terminal evidence screen."""


class OSLease:
    """Small cross-platform, non-blocking exclusive file lease."""

    def __init__(self, path, label=None):
        self.path = Path(path)
        self.label = str(label or self.path.name)
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

                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise CampaignAttemptError(
                f"{self.label} lease is already owned"
            ) from exc
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle = self.handle
        self.handle = None
        if handle is None:
            return
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


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _same_typed_value(actual, expected):
    return type(actual) is type(expected) and actual == expected


def _same_typed_tree(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _same_typed_tree(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_typed_tree(left, right)
            for left, right in zip(actual, expected)
        )
    return actual == expected


def _read_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CampaignAttemptError(f"{label} is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignAttemptError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CampaignAttemptError(f"{label} is not an object")
    return value


def _write_bytes_once(path, encoded):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            try:
                existing = path.read_bytes()
            except OSError as read_exc:
                raise CampaignAttemptError(
                    f"existing evidence is unreadable: {path}"
                ) from read_exc
            if existing != encoded:
                raise CampaignAttemptError(
                    f"refusing to overwrite existing evidence: {path}"
                ) from exc
    except OSError as exc:
        raise CampaignAttemptError(
            f"immutable evidence could not be written: {path}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_json_once(path, payload):
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _write_bytes_once(path, encoded)


def _default_state_loader(root):
    return lambda: _read_object(Path(root) / STATE_NAME, "authoritative state")


def _default_send_payload(root):
    if Path(root).resolve() != ROOT.resolve():
        raise CampaignAttemptError(
            "a non-default root requires an injected send_payload_fn"
        )
    return stsctl.send_payload


def _probe_runtime_leases(root, lease_class=OSLease):
    """Prove controller and runner leases are simultaneously available."""

    root = Path(root)
    acquired = []
    try:
        for name, label in (
            (CONTROLLER_LOCK_NAME, "autoplay controller"),
            (RUNNER_LOCK_NAME, "autoplay runner"),
        ):
            lease = lease_class(root / name, label)
            lease.__enter__()
            acquired.append(lease)
    finally:
        for lease in reversed(acquired):
            lease.__exit__(None, None, None)


def _validate_menu_state(state, *, minimum_seq=None):
    if not isinstance(state, dict):
        raise CampaignAttemptError("authoritative MAIN_MENU state is invalid")
    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise CampaignAttemptError("MAIN_MENU protocol_version is not 2")
    if state.get("in_game") is not False:
        raise CampaignAttemptError("launch requires in_game=false MAIN_MENU")
    if str(state.get("phase") or "").upper() != "MAIN_MENU":
        raise CampaignAttemptError("launch phase is not MAIN_MENU")
    if state.get("ready_for_command") is not True:
        raise CampaignAttemptError("MAIN_MENU is not ready_for_command")
    sequence = state.get("state_seq")
    if type(sequence) is not int or sequence < 0:
        raise CampaignAttemptError("MAIN_MENU state_seq is invalid")
    if minimum_seq is not None and sequence < minimum_seq:
        raise CampaignAttemptError("MAIN_MENU state_seq regressed")
    if not _nonempty_string(state.get("decision_id")):
        raise CampaignAttemptError("MAIN_MENU decision_id is missing")
    legal = state.get("legal_actions")
    if not isinstance(legal, list) or "start" not in {
        str(item).lower() for item in legal
    }:
        raise CampaignAttemptError("START is not legal at MAIN_MENU")
    for field in (
        "pending_action",
        "pending_command",
        "command_pending",
        "action_pending",
    ):
        if state.get(field):
            raise CampaignAttemptError(
                f"MAIN_MENU reports a pending operation: {field}"
            )
    return state


def _validate_no_pending_receipt(root):
    path = Path(root) / RECEIPT_NAME
    if not path.exists():
        return
    receipt = _read_object(path, "action receipt")
    if receipt.get("status") not in {"succeeded", "failed", "rejected"}:
        raise CampaignAttemptError("an action receipt is still pending")
    if receipt.get("success") is None:
        raise CampaignAttemptError("an action receipt has no final outcome")


def _assert_absent(path, label):
    if Path(path).exists():
        raise CampaignAttemptError(f"{label} already exists")


def _assert_clean_launch_transport(root, *, require_no_selection):
    root = Path(root)
    _assert_absent(root / COMMAND_NAME, "pending command")
    _assert_absent(root / f"{COMMAND_NAME}.tmp", "pending command temporary")
    if require_no_selection:
        _assert_absent(root / SELECTION_NAME, "pending selection")
        _assert_absent(
            root / f"{SELECTION_NAME}.tmp", "pending selection temporary"
        )
    _validate_no_pending_receipt(root)


def _validate_state_probe_receipt(receipt, payload, initial_state):
    """Validate one complete transport-level STATE round trip.

    STATE is deliberately read-only, but it still has to traverse the exact
    bridge -> game -> bridge path.  A live PID alone is not sufficient: a
    wedged bridge can occupy its PID while leaving a stale MAIN_MENU snapshot
    behind.
    """

    if not isinstance(receipt, dict) or set(receipt) != _BASE_RECEIPT_FIELDS:
        raise CampaignAttemptError(
            "pre-START STATE receipt fields are incomplete"
        )
    expected = {
        "request_id": payload["id"],
        "success": True,
        "status": "succeeded",
        "error": None,
        "requested_target_id": "action:state",
        "resolved_target_id": "action:state",
    }
    for field, wanted in expected.items():
        if not _same_typed_value(receipt.get(field), wanted):
            raise CampaignAttemptError(
                f"pre-START STATE receipt {field} mismatch"
            )
    accepted = receipt.get("accepted_state_seq")
    result = receipt.get("result_state_seq")
    initial_sequence = initial_state["state_seq"]
    if type(accepted) is not int or accepted < initial_sequence:
        raise CampaignAttemptError(
            "pre-START STATE receipt accepted_state_seq is stale"
        )
    if type(result) is not int or result <= accepted:
        raise CampaignAttemptError(
            "pre-START STATE receipt has no newer authoritative frame"
        )
    return receipt


def _consume_owned_probe_receipt(root, receipt):
    """Remove only the exact finalized STATE receipt just observed."""

    root = Path(root)
    temporary = root / f"{RECEIPT_NAME}.tmp"
    _assert_absent(temporary, "pre-START STATE receipt temporary")
    path = root / RECEIPT_NAME
    if not path.exists():
        # Injected transports used by tests need not materialize a sidecar.
        return
    observed = _read_object(path, "pre-START STATE receipt")
    if not _same_typed_tree(observed, receipt):
        raise CampaignAttemptError(
            "pre-START STATE receipt changed before consumption"
        )
    path.unlink()


def _remove_owned_probe_command(root, payload):
    """Best-effort cleanup of a read-only probe that was never consumed.

    A real bridge unlinks command.json before resolving it.  Therefore an
    exact STATE payload still present after a sender exception is safe to
    archive-by-value and remove; a changed command is never touched.
    """

    path = Path(root) / COMMAND_NAME
    if not path.exists():
        return False
    observed = _read_object(path, "pre-START STATE command")
    if not _same_typed_tree(observed, payload):
        raise CampaignAttemptError(
            "pre-START STATE command changed after dispatch"
        )
    receipt_path = Path(root) / RECEIPT_NAME
    if receipt_path.exists():
        receipt = _read_object(receipt_path, "pre-START STATE receipt")
        if receipt.get("request_id") == payload["id"]:
            raise CampaignAttemptError(
                "pre-START STATE command coexists with its receipt"
            )
    path.unlink()
    return True


def _fresh_menu_state_round_trip(
    root, initial_state, state_loader, send_payload_fn
):
    """Return a MAIN_MENU frame obtained through a real STATE round trip."""

    initial_state = _validate_menu_state(initial_state)
    probe_payload = stsctl.bound_payload(initial_state, "state")
    if not _nonempty_string(probe_payload.get("id")):
        raise CampaignAttemptError("pre-START STATE request_id is invalid")
    try:
        receipt = send_payload_fn(probe_payload)
    except Exception:
        # STATE cannot mutate the run.  Remove it only when the exact payload
        # is still present, which proves the bridge never consumed it.
        _remove_owned_probe_command(root, probe_payload)
        raise
    _validate_state_probe_receipt(receipt, probe_payload, initial_state)
    _assert_absent(
        Path(root) / COMMAND_NAME,
        "pre-START STATE command after final receipt",
    )
    _assert_absent(
        Path(root) / f"{COMMAND_NAME}.tmp",
        "pre-START STATE command temporary after final receipt",
    )
    fresh_state = _validate_menu_state(
        state_loader(), minimum_seq=initial_state["state_seq"] + 1
    )
    if fresh_state["state_seq"] != receipt["result_state_seq"]:
        raise CampaignAttemptError(
            "pre-START STATE receipt does not bind the observed state_seq"
        )
    _consume_owned_probe_receipt(root, receipt)
    return fresh_state, {
        "request_id": probe_payload["id"],
        "accepted_state_seq": receipt["accepted_state_seq"],
        "result_state_seq": receipt["result_state_seq"],
        "result_decision_id": fresh_state["decision_id"],
        "result_phase": fresh_state["phase"],
        "requested_target_id": receipt["requested_target_id"],
        "resolved_target_id": receipt["resolved_target_id"],
    }


def _validate_start_receipt(receipt, payload, root, selection):
    if not isinstance(receipt, dict):
        raise CampaignAttemptError("START receipt is not an object")
    receipt_fields = frozenset(receipt)
    if receipt_fields not in {
        frozenset(_START_RECEIPT_FIELDS),
        frozenset(_BASE_RECEIPT_FIELDS),
    }:
        raise CampaignAttemptError(
            "START receipt fields are incomplete or unclassified"
        )
    expected = {
        "request_id": payload["id"],
        "success": True,
        "status": "succeeded",
        "error": None,
        "accepted_state_seq": payload["expected_seq"],
        "requested_target_id": payload["target_id"],
        "resolved_target_id": payload["target_id"],
        "selection_digest": payload["selection_digest"],
    }
    for field, wanted in expected.items():
        if not _same_typed_value(receipt.get(field), wanted):
            raise CampaignAttemptError(f"START receipt {field} mismatch")
    result_sequence = receipt.get("result_state_seq")
    if (
        type(result_sequence) is not int
        or result_sequence <= payload["expected_seq"]
    ):
        raise CampaignAttemptError("START receipt result_state_seq is invalid")
    try:
        acceptance = pre_run_binding.load_start_acceptance(
            root, selection, payload=payload
        )
        acceptance_sha256 = pre_run_binding.start_acceptance_sha256(
            acceptance
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(str(exc)) from exc
    if receipt.get("start_acceptance_sha256") != acceptance_sha256:
        raise CampaignAttemptError(
            "START receipt acceptance sidecar digest mismatch"
        )
    return receipt


def _valid_seed(value):
    return type(value) is int or (
        isinstance(value, str) and bool(value.strip())
    )


def _validate_started_state(state, selection, receipt):
    if not isinstance(state, dict):
        raise CampaignAttemptError("post-START state is not an object")
    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise CampaignAttemptError("post-START protocol_version is not 2")
    if state.get("in_game") is not True:
        raise CampaignAttemptError("START did not enter an authoritative run")
    if state.get("ready_for_command") is not True:
        raise CampaignAttemptError("post-START state is not ready_for_command")
    if not _same_typed_value(
        state.get("state_seq"), receipt.get("result_state_seq")
    ):
        raise CampaignAttemptError("START receipt/state sequence mismatch")
    if not _nonempty_string(state.get("decision_id")):
        raise CampaignAttemptError("post-START decision_id is missing")
    if str(state.get("phase") or "").upper() == "MAIN_MENU":
        raise CampaignAttemptError("post-START state remained at MAIN_MENU")
    game = state.get("game_state")
    if not isinstance(game, dict):
        raise CampaignAttemptError("post-START game_state is missing")
    character = selection.get("character")
    if game.get("class") != character:
        raise CampaignAttemptError("START opened the wrong character")
    if type(game.get("ascension_level")) is not int:
        raise CampaignAttemptError("post-START ascension_level is invalid")
    if game.get("ascension_level") != 0:
        raise CampaignAttemptError("START did not open ascension level 0")
    if game.get("is_standard_run") is not True:
        raise CampaignAttemptError("START did not open a standard run")
    if not _valid_seed(game.get("seed")):
        raise CampaignAttemptError("post-START seed is missing or invalid")
    if str(game.get("screen_type") or "").upper() == "GAME_OVER":
        raise CampaignAttemptError("new run is already at GAME_OVER")
    for field in (
        "key_system_unlocked",
        "ironclad_third_act_win",
        "silent_third_act_win",
        "defect_third_act_win",
    ):
        if state.get(field) is not True:
            raise CampaignAttemptError(
                f"post-START Heart progression is not authoritative: {field}"
            )
    # No controller has run yet, so a projected active attempt here could
    # only be a stale context (for example, an exact seed collision).
    stale = [field for field in ATTEMPT_BINDING_FIELDS if field in state]
    if stale:
        raise CampaignAttemptError(
            "post-START state carries a stale attempt binding: "
            + ",".join(stale)
        )
    for field in (
        "pending_action", "pending_command", "command_pending",
        "action_pending",
    ):
        if state.get(field):
            raise CampaignAttemptError(
                f"started-run recovery reports a pending operation: {field}"
            )
    return state


def _remove_owned_selection(selection_path, selection):
    """Remove only the exact one-use selection minted by this invocation."""

    selection_path = Path(selection_path)
    for path in (
        selection_path,
        selection_path.with_suffix(selection_path.suffix + ".tmp"),
    ):
        if not path.exists():
            continue
        current = _read_object(path, "owned pending selection")
        if not _same_typed_tree(current, selection):
            raise CampaignAttemptError(
                "refusing to remove a pending selection not owned by this "
                "attempt invocation"
            )
        try:
            path.unlink()
        except OSError as exc:
            raise CampaignAttemptError(
                "failed to remove this invocation's pending selection"
            ) from exc


def _launch_incident_path(root, selection):
    selection_id = str(selection.get("selection_id") or "")
    component = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()[:24]
    return (
        Path(root)
        / "logs"
        / "blocked-launches"
        / f"selection-{component}"
        / "campaign-launch-incident.json"
    )


def _context_binding_for_incident(root, selection):
    path = Path(root) / "run-context.json"
    if not path.exists():
        return None
    try:
        context = _read_object(path, "incident run context")
    except CampaignAttemptError:
        return None
    expected = {
        "selection_id": selection.get("selection_id"),
        "character": selection.get("character"),
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
        "policy_version": selection.get("policy_version"),
        "ascension_level": 0,
        "run_type": "standard",
    }
    if any(
        not _same_typed_value(context.get(field), wanted)
        for field, wanted in expected.items()
    ):
        return None
    fields = (
        "schema_version",
        *ATTEMPT_BINDING_FIELDS,
        "started_state_seq",
        "terminal_state_seq",
    )
    return {field: context.get(field) for field in fields}


def _write_committed_launch_incident(
    root, selection, transaction, error, *, clock=time.time
):
    receipt = transaction.get("start_receipt")
    state = transaction.get("observed_started_state")
    game = state.get("game_state") if isinstance(state, dict) else {}
    if not isinstance(game, dict):
        game = {}
    state_summary = None
    if isinstance(state, dict):
        state_summary = {
            "protocol_version": state.get("protocol_version"),
            "state_seq": state.get("state_seq"),
            "phase": state.get("phase"),
            "decision_id": state.get("decision_id"),
            "in_game": state.get("in_game"),
            "ready_for_command": state.get("ready_for_command"),
            "character": game.get("class"),
            "ascension_level": game.get("ascension_level"),
            "is_standard_run": game.get("is_standard_run"),
            "seed": game.get("seed"),
            "screen_type": game.get("screen_type"),
        }
    incident = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "campaign_launch_incident",
        "incident_status": "blocked_after_start_dispatch",
        "policy_version": POLICY_VERSION,
        "selection_id": selection.get("selection_id"),
        "selection_digest": pre_run_binding.selection_digest(selection),
        "start_acceptance_sha256": (
            receipt.get("start_acceptance_sha256")
            if isinstance(receipt, dict) else None
        ),
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
        "character": selection.get("character"),
        "ascension_level": selection.get("ascension_level"),
        "run_type": selection.get("run_type"),
        "start_dispatched": transaction.get("start_dispatched") is True,
        "start_committed": transaction.get("start_committed") is True,
        "selection_must_be_retained": True,
        "dispatch_status": transaction.get("dispatch_status"),
        "runner_invoked": transaction.get("runner_invoked") is True,
        "pending_selection_present": (
            Path(root) / SELECTION_NAME
        ).exists(),
        "start_payload": transaction.get("start_payload"),
        "start_receipt": receipt if isinstance(receipt, dict) else None,
        "pre_dispatch_runtime": transaction.get(
            "pre_dispatch_runtime"
        ),
        "pre_start_state_probe": transaction.get(
            "pre_start_state_probe"
        ),
        "observed_started_state": state_summary,
        "context_binding": _context_binding_for_incident(root, selection),
        "error_class": type(error).__name__,
        "error": str(error),
        "created_at": float(clock()),
    }
    _write_json_once(_launch_incident_path(root, selection), incident)
    return incident


def _matching_launch_incident(root, selection):
    expected_path = _launch_incident_path(root, selection).resolve()
    matches = []
    for path in sorted(
        (Path(root) / "logs" / "blocked-launches").glob(
            "*/campaign-launch-incident.json"
        )
    ):
        incident = _read_object(path, "campaign launch incident")
        if incident.get("selection_id") == selection.get("selection_id"):
            matches.append((path.resolve(), incident))
    if len(matches) != 1:
        raise CampaignAttemptError(
            "started-run recovery requires one unique matching launch incident"
        )
    path, incident = matches[0]
    if path != expected_path:
        raise CampaignAttemptError(
            "matching launch incident is stored at an unexpected path"
        )
    expected = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "campaign_launch_incident",
        "policy_version": POLICY_VERSION,
        "selection_id": selection.get("selection_id"),
        "selection_digest": pre_run_binding.selection_digest(selection),
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
        "character": selection.get("character"),
        "ascension_level": selection.get("ascension_level"),
        "run_type": selection.get("run_type"),
        "start_dispatched": True,
    }
    for field, wanted in expected.items():
        if not _same_typed_value(incident.get(field), wanted):
            raise CampaignAttemptError(
                f"started-run incident {field} mismatch"
            )
    created_at = incident.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        raise CampaignAttemptError("started-run incident time is invalid")
    return path, incident


def _validate_incident_start_payload(incident, selection, history):
    payload = incident.get("start_payload")
    if not isinstance(payload, dict):
        raise CampaignAttemptError("launch incident START payload is missing")
    synthetic_menu = {
        "protocol_version": PROTOCOL_VERSION,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": payload.get("expected_seq"),
        "phase": payload.get("phase"),
        "decision_id": payload.get("decision_id"),
    }
    try:
        pre_run_binding.validate_start_payload(
            payload,
            synthetic_menu,
            selection,
            history,
            selection.get("decision_hash"),
            selection.get("controller_hash"),
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(
            f"launch incident START payload is invalid: {exc}"
        ) from exc
    return payload


def _validate_rejected_start_receipt(receipt, payload, menu_state):
    if not isinstance(receipt, dict) or set(receipt) != _BASE_RECEIPT_FIELDS:
        raise CampaignAttemptError(
            "rejected START receipt fields are incomplete"
        )
    expected = {
        "request_id": payload["id"],
        "success": False,
        "status": "rejected",
        "requested_target_id": payload["target_id"],
        "resolved_target_id": None,
        "accepted_state_seq": None,
        "result_state_seq": menu_state["state_seq"],
    }
    for field, wanted in expected.items():
        if not _same_typed_value(receipt.get(field), wanted):
            raise CampaignAttemptError(
                f"rejected START receipt {field} mismatch"
            )
    if not _nonempty_string(receipt.get("error")):
        raise CampaignAttemptError("rejected START receipt error is missing")
    return receipt


def _validate_started_recovery_state(state, selection, incident, payload):
    if not isinstance(state, dict):
        raise CampaignAttemptError("started-run recovery state is invalid")
    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise CampaignAttemptError("started-run recovery protocol is not 2")
    if state.get("in_game") is not True:
        raise CampaignAttemptError("started-run recovery is not in game")
    if state.get("ready_for_command") is not True:
        raise CampaignAttemptError("started-run recovery is not ready")
    if str(state.get("phase") or "").upper() in {"", "MAIN_MENU", "GAME_OVER"}:
        raise CampaignAttemptError("started-run recovery phase is not active")
    sequence = state.get("state_seq")
    if type(sequence) is not int or sequence <= payload["expected_seq"]:
        raise CampaignAttemptError("started-run recovery sequence is invalid")
    if not _nonempty_string(state.get("decision_id")):
        raise CampaignAttemptError("started-run recovery decision_id is missing")
    game = state.get("game_state")
    if not isinstance(game, dict):
        raise CampaignAttemptError("started-run recovery game_state is missing")
    expected_game = {
        "class": selection.get("character"),
        "ascension_level": 0,
        "is_standard_run": True,
    }
    for field, wanted in expected_game.items():
        if not _same_typed_value(game.get(field), wanted):
            raise CampaignAttemptError(
                f"started-run recovery game {field} mismatch"
            )
    if not _valid_seed(game.get("seed")):
        raise CampaignAttemptError("started-run recovery seed is invalid")
    observed = incident.get("observed_started_state")
    if isinstance(observed, dict):
        observed_expected = {
            "character": game.get("class"),
            "ascension_level": game.get("ascension_level"),
            "is_standard_run": game.get("is_standard_run"),
            "seed": game.get("seed"),
        }
        for field, wanted in observed_expected.items():
            prior = observed.get(field)
            if prior is not None and not _same_typed_value(prior, wanted):
                raise CampaignAttemptError(
                    f"started-run incident observed {field} mismatch"
                )
        prior_sequence = observed.get("state_seq")
        if type(prior_sequence) is int and sequence < prior_sequence:
            raise CampaignAttemptError(
                "started-run recovery state sequence regressed"
            )
    stale = [field for field in ATTEMPT_BINDING_FIELDS if field in state]
    if stale:
        raise CampaignAttemptError(
            "started-run recovery state carries an active binding: "
            + ",".join(stale)
        )
    return state


def _started_recovery_record(root, incident_path, incident, selection, state):
    game = state["game_state"]
    base = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "started_run_recovery",
        "recovery_status": "controller_pending",
        "policy_version": POLICY_VERSION,
        "selection_id": selection["selection_id"],
        "selection_digest": pre_run_binding.selection_digest(selection),
        "start_acceptance_sha256": pre_run_binding.start_acceptance_sha256(
            pre_run_binding.load_start_acceptance(
                root, selection, payload=incident.get("start_payload")
            )
        ),
        "decision_hash": selection["decision_hash"],
        "controller_hash": selection["controller_hash"],
        "character": selection["character"],
        "ascension_level": 0,
        "run_type": "standard",
        "seed": game["seed"],
        "state_seq": state["state_seq"],
        "decision_id": state["decision_id"],
        "phase": state["phase"],
        "incident_sha256": hashlib.sha256(
            Path(incident_path).read_bytes()
        ).hexdigest(),
        "start_dispatched": incident.get("start_dispatched") is True,
        "start_committed": incident.get("start_committed") is True,
        "dispatch_status": incident.get("dispatch_status"),
        "start_was_resent": False,
    }
    path = Path(incident_path).parent / START_RECOVERY_NAME
    if path.exists():
        existing = _read_object(path, "started-run recovery")
        for field, wanted in base.items():
            if not _same_typed_value(existing.get(field), wanted):
                raise CampaignAttemptError(
                    f"started-run recovery {field} mismatch"
                )
        return existing
    record = {**base, "created_at": time.time()}
    _write_json_once(path, record)
    return record


def _canonical_object_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_archived_unconsumed_recovery(root, recovery_path):
    """Validate one completed recovery without consulting live runtime state."""

    root = Path(root).resolve()
    recovery_path = Path(recovery_path).resolve()
    record = _read_object(
        recovery_path, "archived unconsumed START recovery"
    )
    fields = set(record)
    if fields == _LEGACY_UNCONSUMED_RECOVERY_FIELDS:
        proof_format = "legacy_schema_2"
    elif fields == _CURRENT_UNCONSUMED_RECOVERY_FIELDS:
        proof_format = "current_schema_2"
    else:
        raise CampaignAttemptError(
            "archived unconsumed START recovery fields are incomplete or "
            "unclassified"
        )

    expected_record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "unconsumed_start_recovery",
        "recovery_status": "proven_unconsumed",
        "receipt_absent": True,
        "acceptance_absent": True,
        "safe_to_retry_after_new_freeze": True,
    }
    for field, wanted in expected_record.items():
        if not _same_typed_value(record.get(field), wanted):
            raise CampaignAttemptError(
                f"archived unconsumed START recovery {field} mismatch"
            )
    created_at = record.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        raise CampaignAttemptError(
            "archived unconsumed START recovery time is invalid"
        )

    selection = record.get("selection")
    if not isinstance(selection, dict):
        raise CampaignAttemptError(
            "archived unconsumed START selection is missing"
        )
    try:
        campaign_selector.validate_selection(
            selection,
            selection.get("decision_hash"),
            selection.get("controller_hash"),
        )
        selection_digest = pre_run_binding.selection_digest(selection)
    except (ValueError, pre_run_binding.PreRunBindingError) as exc:
        raise CampaignAttemptError(
            f"archived unconsumed START selection is invalid: {exc}"
        ) from exc
    selection_expected = {
        "selection_id": selection.get("selection_id"),
        "selection_digest": selection_digest,
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
    }
    for field, wanted in selection_expected.items():
        if not _same_typed_value(record.get(field), wanted):
            raise CampaignAttemptError(
                f"archived unconsumed START recovery {field} mismatch"
            )

    command = record.get("command")
    if not isinstance(command, dict) or not _nonempty_string(command.get("id")):
        raise CampaignAttemptError(
            "archived unconsumed START command is invalid"
        )
    state_seq = record.get("state_seq")
    decision_id = record.get("decision_id")
    phase = record.get("phase")
    if (
        type(state_seq) is not int
        or state_seq < 0
        or not _nonempty_string(decision_id)
        or phase != "MAIN_MENU"
    ):
        raise CampaignAttemptError(
            "archived unconsumed START menu binding is invalid"
        )
    expected_command = {
        "id": command.get("id"),
        "action": "start",
        "policy_version": POLICY_VERSION,
        "expected_seq": state_seq,
        "decision_id": decision_id,
        "phase": phase,
        "pre_run_binding_version": pre_run_binding.PRE_RUN_BINDING_VERSION,
        "selection_id": selection["selection_id"],
        "selection_digest": selection_digest,
        "decision_hash": selection["decision_hash"],
        "controller_hash": selection["controller_hash"],
        "goal_mode": "HEART",
        "player_class": selection["character"],
        "character": selection["character"],
        "ascension_level": 0,
        "run_type": "standard",
        "target_id": f"run:{selection['character']}:a0:standard",
        "eligible_attempt_ids": list(selection["eligible_attempt_ids"]),
    }
    if not _same_typed_tree(command, expected_command):
        raise CampaignAttemptError(
            "archived unconsumed START command binding mismatch"
        )
    if record.get("command_sha256") != _canonical_object_sha256(command):
        raise CampaignAttemptError(
            "archived unconsumed START command digest mismatch"
        )

    incident_path, incident = _matching_launch_incident(root, selection)
    if recovery_path.parent != Path(incident_path).parent:
        raise CampaignAttemptError(
            "archived unconsumed START recovery path mismatch"
        )
    incident_fields = set(incident)
    required_incident_fields = (
        _LEGACY_UNCONSUMED_INCIDENT_FIELDS
        if proof_format == "legacy_schema_2"
        else _CURRENT_UNCONSUMED_INCIDENT_FIELDS
    )
    if incident_fields != required_incident_fields:
        raise CampaignAttemptError(
            "archived unconsumed START incident fields do not match the "
            "recovery proof format"
        )
    if record.get("incident_sha256") != hashlib.sha256(
        Path(incident_path).read_bytes()
    ).hexdigest():
        raise CampaignAttemptError(
            "archived unconsumed START incident digest mismatch"
        )
    strict_incident = {
        "incident_status": "blocked_after_start_dispatch",
        "start_dispatched": True,
        "start_committed": False,
        "selection_must_be_retained": True,
        "dispatch_status": "send_exception_after_dispatch",
        "runner_invoked": False,
        "pending_selection_present": True,
        "start_receipt": None,
        "start_acceptance_sha256": None,
        "observed_started_state": None,
        "context_binding": None,
        "error_class": "TimeoutError",
        "start_payload": command,
    }
    for field, wanted in strict_incident.items():
        if not _same_typed_tree(incident.get(field), wanted):
            raise CampaignAttemptError(
                f"archived unconsumed START incident {field} mismatch"
            )
    if not _nonempty_string(incident.get("error")):
        raise CampaignAttemptError(
            "archived unconsumed START incident error is missing"
        )

    dead_processes = record.get("dead_processes")
    if (
        not isinstance(dead_processes, dict)
        or set(dead_processes) != {"launcher", "java", "bridge"}
        or any(type(value) is not int or value <= 0 for value in dead_processes.values())
        or len(set(dead_processes.values())) != 3
    ):
        raise CampaignAttemptError(
            "archived unconsumed START dead process proof is invalid"
        )

    if proof_format == "current_schema_2":
        if record.get("start_was_resent") is not False:
            raise CampaignAttemptError(
                "archived unconsumed START resend marker mismatch"
            )
        if record.get("incident_dispatch_status") != incident.get(
            "dispatch_status"
        ):
            raise CampaignAttemptError(
                "archived unconsumed START dispatch summary mismatch"
            )
        probe = record.get("pre_start_state_probe")
        runtime = record.get("pre_dispatch_runtime")
        if not _same_typed_tree(probe, incident.get("pre_start_state_probe")):
            raise CampaignAttemptError(
                "archived unconsumed START STATE probe mismatch"
            )
        if not _same_typed_tree(runtime, incident.get("pre_dispatch_runtime")):
            raise CampaignAttemptError(
                "archived unconsumed START runtime proof mismatch"
            )
        probe_expected = {
            "result_state_seq": state_seq,
            "result_decision_id": decision_id,
            "result_phase": phase,
            "requested_target_id": "action:state",
            "resolved_target_id": "action:state",
        }
        if not isinstance(probe, dict) or any(
            not _same_typed_value(probe.get(field), wanted)
            for field, wanted in probe_expected.items()
        ):
            raise CampaignAttemptError(
                "archived unconsumed START STATE probe binding mismatch"
            )
        if (
            not _nonempty_string(probe.get("request_id"))
            or type(probe.get("accepted_state_seq")) is not int
            or probe["accepted_state_seq"] >= probe["result_state_seq"]
        ):
            raise CampaignAttemptError(
                "archived unconsumed START STATE probe sequence is invalid"
            )
        runtime_expected = {
            "launcher": runtime.get("launcher_pid") if isinstance(runtime, dict) else None,
            "java": runtime.get("java_pid") if isinstance(runtime, dict) else None,
            "bridge": runtime.get("bridge_pid") if isinstance(runtime, dict) else None,
        }
        if not isinstance(runtime, dict) or not _same_typed_tree(
            dead_processes, runtime_expected
        ):
            raise CampaignAttemptError(
                "archived unconsumed START runtime process summary mismatch"
            )
        launch_evidence = runtime.get("launch_evidence")
        runtime_fields = (
            "launch_id",
            "launcher_pid",
            "java_pid",
            "bridge_pid",
            "bridge_instance_token",
            "bridge_sha256",
        )
        if not isinstance(launch_evidence, dict) or any(
            not _same_typed_value(
                launch_evidence.get(field), runtime.get(field)
            )
            for field in runtime_fields
        ):
            raise CampaignAttemptError(
                "archived unconsumed START frozen launch evidence mismatch"
            )
    return record


def _reconcile_archived_unconsumed_start(root, recovery_paths):
    """Return one immutable completed recovery; never mutate transport files."""

    root = Path(root).resolve()
    paths = [Path(path).resolve() for path in recovery_paths]
    if len(paths) != 1:
        raise CampaignAttemptError(
            "unconsumed START reconciliation requires exactly one recovery"
        )
    for path, label in (
        (root / SELECTION_NAME, "pending selection"),
        (root / f"{SELECTION_NAME}.tmp", "pending selection temporary"),
        (root / COMMAND_NAME, "pending command"),
        (root / f"{COMMAND_NAME}.tmp", "pending command temporary"),
        (root / RECEIPT_NAME, "pending receipt"),
        (root / f"{RECEIPT_NAME}.tmp", "pending receipt temporary"),
        (root / "run-context.json", "run context"),
    ):
        if path.exists():
            raise CampaignAttemptError(
                f"cannot reconcile completed unconsumed START with {label}"
            )
    record = _validate_archived_unconsumed_recovery(root, paths[0])
    acceptance_path = pre_run_binding.start_acceptance_path(
        root, record["selection_id"]
    )
    if acceptance_path.exists():
        raise CampaignAttemptError(
            "cannot reconcile completed unconsumed START with acceptance"
        )
    return record


def recover_proven_unconsumed_start(
    root=ROOT,
    *,
    state_loader=None,
    process_alive_fn=None,
    lease_class=OSLease,
    clock=time.time,
):
    """Archive and clear a START command that no bridge ever consumed."""

    root = Path(root).resolve()
    state_loader = state_loader or _default_state_loader(root)
    alive = process_alive_fn or pre_run_binding.process_is_alive
    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        recovery_paths = sorted(
            (root / "logs" / "blocked-launches").glob(
                f"*/{UNCONSUMED_START_RECOVERY_NAME}"
            )
        )
        if recovery_paths:
            return _reconcile_archived_unconsumed_start(
                root, recovery_paths
            )
        selection_path = root / SELECTION_NAME
        command_path = root / COMMAND_NAME
        selection = _read_object(selection_path, "pending selection")
        command = _read_object(command_path, "unconsumed START command")
        incident_path, incident = _matching_launch_incident(root, selection)
        history = campaign_selector.load_history(root / HISTORY_NAME)
        payload = _validate_incident_start_payload(
            incident, selection, history
        )
        strict_incident = {
            "incident_status": "blocked_after_start_dispatch",
            "start_dispatched": True,
            "start_committed": False,
            "selection_must_be_retained": True,
            "dispatch_status": "send_exception_after_dispatch",
            "runner_invoked": False,
            "pending_selection_present": True,
            "start_receipt": None,
            "start_acceptance_sha256": None,
            "observed_started_state": None,
            "context_binding": None,
            "error_class": "TimeoutError",
        }
        for field, wanted in strict_incident.items():
            if not _same_typed_value(incident.get(field), wanted):
                raise CampaignAttemptError(
                    f"unconsumed START incident {field} mismatch"
                )
        probe = incident.get("pre_start_state_probe")
        if not isinstance(probe, dict):
            raise CampaignAttemptError(
                "unconsumed START incident has no fresh STATE probe"
            )
        probe_expected = {
            "result_state_seq": payload.get("expected_seq"),
            "result_decision_id": payload.get("decision_id"),
            "result_phase": payload.get("phase"),
            "requested_target_id": "action:state",
            "resolved_target_id": "action:state",
        }
        for field, wanted in probe_expected.items():
            if not _same_typed_value(probe.get(field), wanted):
                raise CampaignAttemptError(
                    f"unconsumed START STATE probe {field} mismatch"
                )
        if (
            not _nonempty_string(probe.get("request_id"))
            or type(probe.get("accepted_state_seq")) is not int
            or probe["accepted_state_seq"] >= probe["result_state_seq"]
        ):
            raise CampaignAttemptError(
                "unconsumed START STATE probe sequence is invalid"
            )
        runtime = incident.get("pre_dispatch_runtime")
        if not isinstance(runtime, dict):
            raise CampaignAttemptError(
                "unconsumed START incident runtime proof is missing"
            )
        if not _same_typed_tree(command, payload):
            raise CampaignAttemptError(
                "pending command differs from the incident START payload"
            )
        if (root / RECEIPT_NAME).exists():
            raise CampaignAttemptError(
                "cannot prove START unconsumed while a receipt exists"
            )
        acceptance_path = pre_run_binding.start_acceptance_path(
            root, selection["selection_id"]
        )
        if acceptance_path.exists():
            raise CampaignAttemptError(
                "cannot prove START unconsumed after bridge acceptance"
            )
        _assert_absent(
            root / f"{COMMAND_NAME}.tmp",
            "unconsumed START command temporary",
        )
        _assert_absent(
            root / f"{RECEIPT_NAME}.tmp",
            "unconsumed START receipt temporary",
        )
        _assert_absent(
            root / f"{SELECTION_NAME}.tmp",
            "unconsumed START selection temporary",
        )
        if _context_binding_for_incident(root, selection) is not None:
            raise CampaignAttemptError(
                "cannot prove START unconsumed after run-context binding"
            )
        state = _validate_menu_state(state_loader())
        for field, wanted in (
            ("state_seq", payload.get("expected_seq")),
            ("decision_id", payload.get("decision_id")),
            ("phase", payload.get("phase")),
        ):
            if not _same_typed_value(state.get(field), wanted):
                raise CampaignAttemptError(
                    f"authoritative state changed after unconsumed START: {field}"
                )

        launch = _read_object(root / "launch-latest.json", "launch record")
        bridge_instance = _read_object(
            root / "bridge-instance.json", "bridge instance"
        )
        pids = {
            "launcher": launch.get("launcher_pid"),
            "java": launch.get("java_pid"),
            "bridge": bridge_instance.get("bridge_pid"),
        }
        if (
            any(type(pid) is not int or pid <= 0 for pid in pids.values())
            or len(set(pids.values())) != 3
            or bridge_instance.get("parent_java_pid") != pids["java"]
            or bridge_instance.get("launch_id") != launch.get("launch_id")
        ):
            raise CampaignAttemptError(
                "stale launch process identity is incomplete"
            )
        runtime_expected = {
            "launch_id": launch.get("launch_id"),
            "launcher_pid": pids["launcher"],
            "java_pid": pids["java"],
            "bridge_pid": pids["bridge"],
            "bridge_instance_token": bridge_instance.get(
                "instance_token"
            ),
            "bridge_sha256": bridge_instance.get("bridge_sha256"),
        }
        for field, wanted in runtime_expected.items():
            if not _same_typed_value(runtime.get(field), wanted):
                raise CampaignAttemptError(
                    f"unconsumed START runtime {field} mismatch"
                )
        launch_evidence = runtime.get("launch_evidence")
        if (
            not isinstance(launch_evidence, dict)
            or launch_evidence.get("launch_id") != launch.get("launch_id")
            or launch_evidence.get("launcher_pid") != pids["launcher"]
            or launch_evidence.get("java_pid") != pids["java"]
            or launch_evidence.get("bridge_pid") != pids["bridge"]
            or launch_evidence.get("bridge_instance_token")
            != bridge_instance.get("instance_token")
            or launch_evidence.get("bridge_sha256")
            != bridge_instance.get("bridge_sha256")
        ):
            raise CampaignAttemptError(
                "unconsumed START frozen launch evidence mismatch"
            )
        try:
            live = sorted(name for name, pid in pids.items() if alive(pid))
        except Exception as exc:
            raise CampaignAttemptError(
                "failed to prove stale launch process termination"
            ) from exc
        if live:
            raise CampaignAttemptError(
                "cannot clear START while runtime process is alive: "
                + ",".join(live)
            )

        incident_digest = hashlib.sha256(
            Path(incident_path).read_bytes()
        ).hexdigest()
        record_base = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "unconsumed_start_recovery",
            "recovery_status": "proven_unconsumed",
            "selection_id": selection["selection_id"],
            "selection_digest": pre_run_binding.selection_digest(selection),
            "decision_hash": selection["decision_hash"],
            "controller_hash": selection["controller_hash"],
            "incident_sha256": incident_digest,
            "command_sha256": hashlib.sha256(
                json.dumps(
                    command,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "state_seq": state["state_seq"],
            "decision_id": state["decision_id"],
            "phase": state["phase"],
            "dead_processes": pids,
            "command": command,
            "selection": selection,
            "receipt_absent": True,
            "acceptance_absent": True,
            "start_was_resent": False,
            "incident_dispatch_status": incident["dispatch_status"],
            "pre_start_state_probe": probe,
            "pre_dispatch_runtime": runtime,
            "safe_to_retry_after_new_freeze": True,
        }
        recovery_path = Path(incident_path).parent / UNCONSUMED_START_RECOVERY_NAME
        if recovery_path.exists():
            existing = _read_object(
                recovery_path, "unconsumed START recovery"
            )
            for field, wanted in record_base.items():
                if not _same_typed_tree(existing.get(field), wanted):
                    raise CampaignAttemptError(
                        f"unconsumed START recovery {field} mismatch"
                    )
            record = existing
        else:
            record = {**record_base, "created_at": float(clock())}
            _write_json_once(recovery_path, record)

        current_command = _read_object(command_path, "unconsumed START command")
        if not _same_typed_tree(current_command, command):
            raise CampaignAttemptError(
                "unconsumed START command changed before cleanup"
            )
        command_path.unlink()
        _remove_owned_selection(selection_path, selection)
        return record


def recover_abandoned_accepted_start(
    root=ROOT,
    repair_decision_hash=None,
    repair_controller_hash=None,
    *,
    state_loader=None,
    process_alive_fn=None,
    lease_class=OSLease,
    clock=time.time,
):
    """Archive an accepted START whose original runtime died before binding.

    This is deliberately separate from unconsumed-START recovery: an
    acceptance sidecar proves that the bridge consumed the command, so the
    selection cannot be called unconsumed or silently reused.  Recovery is
    allowed only from a newly frozen, live replacement runtime at MAIN_MENU;
    the abandoned selection is permanently ineligible for cohort history.
    """

    root = Path(root).resolve()
    if not _nonempty_string(repair_decision_hash) or not _nonempty_string(
        repair_controller_hash
    ):
        raise CampaignAttemptError("repair hashes must be non-empty")
    state_loader = state_loader or _default_state_loader(root)
    alive = process_alive_fn or pre_run_binding.process_is_alive

    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        selection_path = root / SELECTION_NAME
        selection = _read_object(selection_path, "pending selection")
        if (
            selection.get("decision_hash") == repair_decision_hash
            and selection.get("controller_hash") == repair_controller_hash
        ):
            raise CampaignAttemptError(
                "abandoned START recovery requires a newly frozen repair hash"
            )
        history = campaign_selector.load_history(root / HISTORY_NAME)
        try:
            pre_run_binding.validate_pending_selection(
                selection,
                history,
                selection.get("decision_hash"),
                selection.get("controller_hash"),
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CampaignAttemptError(str(exc)) from exc

        incident_path, incident = _matching_launch_incident(root, selection)
        payload = _validate_incident_start_payload(
            incident, selection, history
        )
        if not (
            incident.get("start_dispatched") is True
            and incident.get("runner_invoked") is False
            and incident.get("selection_must_be_retained") is True
            and incident.get("pending_selection_present") is True
            and incident.get("dispatch_status")
            in {
                "send_exception_after_dispatch",
                "dispatched_receipt_unknown",
                "ambiguous_final_receipt",
                "authoritatively_succeeded",
            }
        ):
            raise CampaignAttemptError(
                "launch incident does not authorize accepted START abandonment"
            )
        if _context_binding_for_incident(root, selection) is not None:
            raise CampaignAttemptError(
                "accepted START already has an active run-context binding"
            )
        try:
            acceptance = pre_run_binding.load_start_acceptance(
                root, selection, payload=payload
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CampaignAttemptError(str(exc)) from exc
        acceptance_sha256 = pre_run_binding.start_acceptance_sha256(
            acceptance
        )
        incident_acceptance_sha256 = incident.get(
            "start_acceptance_sha256"
        )
        if incident_acceptance_sha256 not in {None, acceptance_sha256}:
            raise CampaignAttemptError(
                "launch incident acceptance sidecar mismatch"
            )

        for path, label in (
            (root / COMMAND_NAME, "pending command"),
            (root / f"{COMMAND_NAME}.tmp", "pending command temporary"),
            (root / RECEIPT_NAME, "pending receipt"),
            (root / f"{RECEIPT_NAME}.tmp", "pending receipt temporary"),
            (root / f"{SELECTION_NAME}.tmp", "pending selection temporary"),
        ):
            _assert_absent(path, label)

        old_runtime = incident.get("pre_dispatch_runtime")
        if not isinstance(old_runtime, dict):
            raise CampaignAttemptError(
                "abandoned START incident runtime proof is missing"
            )
        old_pids = {
            "launcher": old_runtime.get("launcher_pid"),
            "java": old_runtime.get("java_pid"),
            "bridge": old_runtime.get("bridge_pid"),
        }
        old_launch = old_runtime.get("launch_evidence")
        if (
            any(type(pid) is not int or pid <= 0 for pid in old_pids.values())
            or len(set(old_pids.values())) != 3
            or not isinstance(old_launch, dict)
            or old_launch.get("launch_id") != old_runtime.get("launch_id")
            or old_launch.get("launcher_pid") != old_pids["launcher"]
            or old_launch.get("java_pid") != old_pids["java"]
            or old_launch.get("bridge_pid") != old_pids["bridge"]
        ):
            raise CampaignAttemptError(
                "abandoned START original runtime identity is incomplete"
            )
        try:
            old_live = sorted(
                name for name, pid in old_pids.items() if alive(pid)
            )
        except Exception as exc:
            raise CampaignAttemptError(
                "failed to prove abandoned START runtime termination"
            ) from exc
        if old_live:
            raise CampaignAttemptError(
                "cannot abandon START while original runtime is alive: "
                + ",".join(old_live)
            )

        menu = _validate_menu_state(state_loader())
        try:
            prerequisite = campaign_selector.validate_launch_prerequisites(
                history,
                repair_decision_hash,
                repair_controller_hash,
                root,
                freeze_manifest_path=root / FREEZE_MANIFEST_NAME,
                cohort_review_path=root / COHORT_REVIEW_NAME,
            )
            replacement_runtime = (
                pre_run_binding.validate_pre_dispatch_runtime(
                    menu,
                    root,
                    prerequisite["manifest"],
                    process_alive_fn=alive,
                )
            )
        except (
            campaign_selector.HistoryValidationError,
            pre_run_binding.PreRunBindingError,
            ValueError,
        ) as exc:
            raise CampaignAttemptError(str(exc)) from exc
        replacement_pids = {
            replacement_runtime["launcher_pid"],
            replacement_runtime["java_pid"],
            replacement_runtime["bridge_pid"],
        }
        if (
            replacement_runtime.get("launch_id") == old_runtime.get("launch_id")
            or replacement_pids.intersection(old_pids.values())
        ):
            raise CampaignAttemptError(
                "replacement runtime is not distinct from abandoned runtime"
            )

        manifest_bytes = (root / FREEZE_MANIFEST_NAME).read_bytes()
        record_base = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "abandoned_accepted_start_recovery",
            "recovery_status": "abandoned_after_dead_runtime_repair",
            "eligible_for_cohort": False,
            "selection_id": selection["selection_id"],
            "selection_digest": pre_run_binding.selection_digest(selection),
            "decision_hash": selection["decision_hash"],
            "controller_hash": selection["controller_hash"],
            "repair_decision_hash": repair_decision_hash,
            "repair_controller_hash": repair_controller_hash,
            "incident_sha256": hashlib.sha256(
                incident_path.read_bytes()
            ).hexdigest(),
            "start_acceptance_sha256": acceptance_sha256,
            "freeze_manifest_sha256": hashlib.sha256(
                manifest_bytes
            ).hexdigest(),
            "freeze_source_digest": prerequisite["manifest"].get(
                "source_digest"
            ),
            "dead_original_processes": old_pids,
            "replacement_runtime": replacement_runtime,
            "menu_state": {
                "state_seq": menu["state_seq"],
                "decision_id": menu["decision_id"],
                "phase": menu["phase"],
            },
            "selection": selection,
            "start_was_resent": False,
            "safe_to_retry_after_new_freeze": True,
        }
        recovery_path = (
            incident_path.parent / ABANDONED_ACCEPTED_START_RECOVERY_NAME
        )
        if recovery_path.exists():
            record = _read_object(
                recovery_path, "abandoned accepted START recovery"
            )
            for field, wanted in record_base.items():
                if not _same_typed_tree(record.get(field), wanted):
                    raise CampaignAttemptError(
                        f"abandoned START recovery {field} mismatch"
                    )
        else:
            record = {**record_base, "created_at": float(clock())}
            _write_json_once(recovery_path, record)

        _remove_owned_selection(selection_path, selection)
        return record


def recover_started_attempt(
    root=ROOT,
    decision_hash=None,
    controller_hash=None,
    *,
    max_actions=5000,
    state_loader=None,
    runner_fn=None,
    lease_class=OSLease,
):
    """Attach one controller after an ambiguous START without resending it."""

    root = Path(root).resolve()
    if not _nonempty_string(decision_hash) or not _nonempty_string(
        controller_hash
    ):
        raise CampaignAttemptError("recovery hashes must be non-empty")
    if type(max_actions) is not int or max_actions < 1:
        raise CampaignAttemptError("max_actions must be a positive integer")
    state_loader = state_loader or _default_state_loader(root)
    runner_fn = runner_fn or autoplay_runner.run_controller
    selection_path = root / SELECTION_NAME

    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        selection = _read_object(selection_path, "pending selection")
        _assert_absent(
            root / f"{SELECTION_NAME}.tmp",
            "pending recovery selection temporary",
        )
        if (
            selection.get("decision_hash") != decision_hash
            or selection.get("controller_hash") != controller_hash
        ):
            raise CampaignAttemptError("pending recovery selection hash mismatch")
        history = campaign_selector.load_history(root / HISTORY_NAME)
        try:
            pre_run_binding.validate_pending_selection(
                selection, history, decision_hash, controller_hash
            )
            pre_run_binding.validate_release_checkpoint(
                selection,
                history,
                root,
                manifest_path=root / FREEZE_MANIFEST_NAME,
                cohort_review_path=root / COHORT_REVIEW_NAME,
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CampaignAttemptError(str(exc)) from exc
        incident_path, incident = _matching_launch_incident(root, selection)
        payload = _validate_incident_start_payload(
            incident, selection, history
        )
        state = state_loader()

        if (
            isinstance(state, dict)
            and state.get("in_game") is False
            and str(state.get("phase") or "").upper() == "MAIN_MENU"
        ):
            menu = _validate_menu_state(state)
            if incident.get("dispatch_status") != "authoritatively_rejected":
                raise CampaignAttemptError(
                    "ambiguous START cannot be cleaned from MAIN_MENU"
                )
            _validate_rejected_start_receipt(
                incident.get("start_receipt"), payload, menu
            )
            _remove_owned_selection(selection_path, selection)
            record = {
                "schema_version": SCHEMA_VERSION,
                "operation": "recover_started_attempt",
                "recovery_status": "authoritative_rejection_cleaned",
                "selection_id": selection["selection_id"],
                "selection_digest": pre_run_binding.selection_digest(
                    selection
                ),
                "decision_hash": decision_hash,
                "controller_hash": controller_hash,
                "incident_sha256": hashlib.sha256(
                    incident_path.read_bytes()
                ).hexdigest(),
                "start_was_resent": False,
            }
            _write_json_once(
                incident_path.parent / START_RECOVERY_NAME, record
            )
            return record

        active = _validate_started_recovery_state(
            state, selection, incident, payload
        )
        try:
            acceptance = pre_run_binding.load_start_acceptance(
                root, selection, payload=payload
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CampaignAttemptError(str(exc)) from exc
        acceptance_sha256 = pre_run_binding.start_acceptance_sha256(
            acceptance
        )
        incident_receipt = incident.get("start_receipt")
        if isinstance(incident_receipt, dict) and (
            incident_receipt.get("success") is True
            and incident_receipt.get("status") == "succeeded"
        ):
            _validate_start_receipt(
                incident_receipt, payload, root, selection
            )
        incident_acceptance_sha = incident.get(
            "start_acceptance_sha256"
        )
        if incident_acceptance_sha not in {None, acceptance_sha256}:
            raise CampaignAttemptError(
                "started-run incident acceptance sidecar mismatch"
            )
        if incident.get("dispatch_status") == "authoritatively_rejected":
            raise CampaignAttemptError(
                "rejected START incident conflicts with an active run"
            )
        if not (
            incident.get("start_committed") is True
            or incident.get("dispatch_status") in {
                "dispatched_receipt_unknown",
                "send_exception_after_dispatch",
                "ambiguous_final_receipt",
                "authoritatively_succeeded",
            }
        ):
            raise CampaignAttemptError(
                "launch incident does not authorize active-run recovery"
            )

        context_path = root / "run-context.json"
        if context_path.exists():
            context = _read_object(context_path, "existing run context")
            game = active["game_state"]
            active_run_id = f"{game.get('class')}:0:{game.get('seed')}"
            if context.get("run_id") == active_run_id:
                raise CampaignAttemptError(
                    "an active run context already owns this recovered run"
                )
        _assert_absent(root / COMMAND_NAME, "pending command")
        _assert_absent(root / f"{COMMAND_NAME}.tmp", "pending command temporary")
        _validate_no_pending_receipt(root)
        _probe_runtime_leases(root, lease_class)
        recovery = _started_recovery_record(
            root, incident_path, incident, selection, active
        )
        try:
            pre_run_binding.validate_accepted_start_checkpoint(
                root, expected_selection=selection
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CampaignAttemptError(str(exc)) from exc
        controller_exit = runner_fn(root=root, max_actions=max_actions)
        if not isinstance(controller_exit, dict):
            raise CampaignAttemptError(
                "recovered autoplay runner returned no controller exit"
            )
        if selection_path.exists():
            raise CampaignAttemptError(
                "recovered controller did not consume the pending selection"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "recover_started_attempt",
            "decision_hash": decision_hash,
            "controller_hash": controller_hash,
            "selection": selection,
            "recovery": recovery,
            "controller_exit": controller_exit,
            "start_was_resent": False,
        }


def _complete_launch_after_selection(
    root,
    decision_hash,
    controller_hash,
    *,
    max_actions,
    state_loader,
    send_payload_fn,
    runner_fn,
    request_id_factory,
    lease_class,
    history,
    selection,
    prerequisite,
    transaction,
):
    selection_path = root / SELECTION_NAME
    persisted_selection = _read_object(selection_path, "pending selection")
    if not _same_typed_tree(persisted_selection, selection):
        raise CampaignAttemptError(
            "persisted selection differs from selector output"
        )

    current_history = campaign_selector.load_history(root / HISTORY_NAME)
    if not _same_typed_tree(current_history, history):
        raise CampaignAttemptError(
            "run history changed after selection was generated"
        )
    menu_state = _validate_menu_state(state_loader())
    _assert_clean_launch_transport(root, require_no_selection=False)
    current_selection = _read_object(selection_path, "pending selection")
    if not _same_typed_tree(current_selection, persisted_selection):
        raise CampaignAttemptError("pending selection changed before START")
    _probe_runtime_leases(root, lease_class)

    # Re-run the pre-run release checkpoint against the persisted selection
    # immediately before binding START.  This is separate from the selector's
    # initial freeze/review gate and from the bridge's acceptance-time check.
    pre_run_checkpoint = pre_run_binding.validate_release_checkpoint(
        persisted_selection,
        current_history,
        root,
        manifest_path=root / FREEZE_MANIFEST_NAME,
        cohort_review_path=root / COHORT_REVIEW_NAME,
    )
    try:
        runtime_before_probe = pre_run_binding.validate_pre_dispatch_runtime(
            menu_state,
            root,
            pre_run_checkpoint["manifest"],
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(str(exc)) from exc

    fresh_menu_state, state_probe = _fresh_menu_state_round_trip(
        root, menu_state, state_loader, send_payload_fn
    )
    current_selection = _read_object(selection_path, "pending selection")
    if not _same_typed_tree(current_selection, persisted_selection):
        raise CampaignAttemptError(
            "pending selection changed during pre-START STATE probe"
        )
    current_history = campaign_selector.load_history(root / HISTORY_NAME)
    if not _same_typed_tree(current_history, history):
        raise CampaignAttemptError(
            "run history changed during pre-START STATE probe"
        )
    try:
        runtime_after_probe = pre_run_binding.validate_pre_dispatch_runtime(
            fresh_menu_state,
            root,
            pre_run_checkpoint["manifest"],
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(str(exc)) from exc
    if not _same_typed_tree(runtime_after_probe, runtime_before_probe):
        raise CampaignAttemptError(
            "frozen runtime identity changed during pre-START STATE probe"
        )
    transaction["pre_dispatch_runtime"] = runtime_after_probe
    transaction["pre_start_state_probe"] = state_probe

    request_id = request_id_factory()
    if not _nonempty_string(request_id):
        raise CampaignAttemptError("START request_id is invalid")
    # START construction is intentionally delegated in full.  Do not add or
    # remove fields around this call.
    start_payload = pre_run_binding.build_start_payload(
        fresh_menu_state,
        persisted_selection,
        current_history,
        decision_hash,
        controller_hash,
        request_id=request_id,
    )
    transaction["start_payload"] = start_payload
    transaction["start_dispatched"] = True
    transaction["selection_must_be_retained"] = True
    transaction["dispatch_status"] = "dispatched_receipt_unknown"
    try:
        start_receipt = send_payload_fn(start_payload)
    except Exception:
        transaction["dispatch_status"] = "send_exception_after_dispatch"
        raise
    transaction["start_receipt"] = start_receipt
    if (
        isinstance(start_receipt, dict)
        and start_receipt.get("request_id") == start_payload["id"]
        and start_receipt.get("status") == "rejected"
        and start_receipt.get("success") is False
    ):
        # A bridge rejection is the one authoritative post-dispatch outcome
        # that proves START was never emitted to the game.
        transaction["selection_must_be_retained"] = False
        transaction["dispatch_status"] = "authoritatively_rejected"
    elif (
        isinstance(start_receipt, dict)
        and start_receipt.get("request_id") == start_payload["id"]
        and start_receipt.get("status") == "succeeded"
        and start_receipt.get("success") is True
    ):
        transaction["start_committed"] = True
        transaction["dispatch_status"] = "authoritatively_succeeded"
    else:
        transaction["dispatch_status"] = "ambiguous_final_receipt"
    _validate_start_receipt(
        start_receipt, start_payload, root, persisted_selection
    )

    observed_started_state = state_loader()
    transaction["observed_started_state"] = observed_started_state
    started_state = _validate_started_state(
        observed_started_state, persisted_selection, start_receipt
    )
    _assert_absent(root / COMMAND_NAME, "pending command after START")
    _assert_absent(
        root / f"{COMMAND_NAME}.tmp",
        "pending command temporary after START",
    )
    current_selection = _read_object(selection_path, "pending selection")
    if not _same_typed_tree(current_selection, persisted_selection):
        raise CampaignAttemptError(
            "pending selection was consumed before controller ownership"
        )
    _probe_runtime_leases(root, lease_class)

    # Reconsume the accepted selection, history and complete runtime freeze
    # after START has reached an active state. The runner repeats this gate at
    # the actual subprocess boundary.
    try:
        pre_run_binding.validate_accepted_start_checkpoint(
            root, expected_selection=persisted_selection
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(str(exc)) from exc

    # There is exactly one call site for the controller supervisor in the
    # launch state machine.
    transaction["runner_invoked"] = True
    controller_exit = runner_fn(root=root, max_actions=max_actions)
    if not isinstance(controller_exit, dict):
        raise CampaignAttemptError(
            "autoplay runner did not return a controller exit object"
        )
    if selection_path.exists():
        raise CampaignAttemptError(
            "autoplay runner returned without consuming its exact selection"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "launch_attempt",
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "selection": persisted_selection,
        "start_payload": start_payload,
        "start_receipt": start_receipt,
        "started_state": {
            "protocol_version": started_state.get("protocol_version"),
            "state_seq": started_state.get("state_seq"),
            "phase": started_state.get("phase"),
            "decision_id": started_state.get("decision_id"),
            "character": (
                started_state.get("game_state") or {}
            ).get("class"),
            "ascension_level": (
                started_state.get("game_state") or {}
            ).get("ascension_level"),
            "run_type": "standard",
            "seed": (started_state.get("game_state") or {}).get("seed"),
        },
        "release_checkpoint": {
            "review_required": prerequisite.get("review") is not None,
            "source_digest": (
                prerequisite.get("manifest") or {}
            ).get("source_digest"),
            "pre_run_review_required": (
                pre_run_checkpoint.get("review") is not None
            ),
        },
        "pre_dispatch_runtime": runtime_after_probe,
        "pre_start_state_probe": state_probe,
        "controller_exit": controller_exit,
    }


def _launch_attempt_impl(
    root,
    decision_hash,
    controller_hash,
    *,
    max_actions,
    state_loader,
    send_payload_fn,
    runner_fn,
    selector_rng,
    validation_batch,
    p0_only_batch,
    request_id_factory,
    lease_class,
):
    root = Path(root).resolve()
    if not _nonempty_string(decision_hash):
        raise CampaignAttemptError("decision_hash must be non-empty")
    if not _nonempty_string(controller_hash):
        raise CampaignAttemptError("controller_hash must be non-empty")
    if type(max_actions) is not int or max_actions < 1:
        raise CampaignAttemptError("max_actions must be a positive integer")

    state_loader = state_loader or _default_state_loader(root)
    send_payload_fn = send_payload_fn or _default_send_payload(root)
    runner_fn = runner_fn or autoplay_runner.run_controller
    request_id_factory = request_id_factory or (
        lambda: str(uuid.uuid4())
    )
    history_path = root / HISTORY_NAME
    selection_path = root / SELECTION_NAME

    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        _validate_menu_state(state_loader())
        _assert_clean_launch_transport(root, require_no_selection=True)
        _probe_runtime_leases(root, lease_class)

        history = campaign_selector.load_history(history_path)
        prerequisite = campaign_selector.validate_launch_prerequisites(
            history,
            decision_hash,
            controller_hash,
            root,
            freeze_manifest_path=root / FREEZE_MANIFEST_NAME,
            cohort_review_path=root / COHORT_REVIEW_NAME,
            p0_only_batch=p0_only_batch,
        )

        # Gate execution may be expensive.  Re-prove the live launch surface
        # before minting the one-use selection.
        _validate_menu_state(state_loader())
        _assert_clean_launch_transport(root, require_no_selection=True)
        _probe_runtime_leases(root, lease_class)

        selection = campaign_selector.select_character(
            history,
            decision_hash,
            rng=selector_rng,
            controller_hash=controller_hash,
            validation_batch=validation_batch,
            p0_only_batch=p0_only_batch,
        )
        transaction = {
            "start_dispatched": False,
            "start_committed": False,
            "selection_must_be_retained": False,
            "runner_invoked": False,
        }
        try:
            campaign_selector.write_selection(selection_path, selection)
            return _complete_launch_after_selection(
                root,
                decision_hash,
                controller_hash,
                max_actions=max_actions,
                state_loader=state_loader,
                send_payload_fn=send_payload_fn,
                runner_fn=runner_fn,
                request_id_factory=request_id_factory,
                lease_class=lease_class,
                history=history,
                selection=selection,
                prerequisite=prerequisite,
                transaction=transaction,
            )
        except Exception as exc:
            if transaction.get("selection_must_be_retained"):
                try:
                    _write_committed_launch_incident(
                        root, selection, transaction, exc
                    )
                except Exception as incident_exc:
                    raise CampaignAttemptError(
                        "attempt failed after START dispatch and its bound "
                        f"launch incident could not be written: {incident_exc}"
                    ) from exc
            else:
                try:
                    _remove_owned_selection(selection_path, selection)
                except Exception as cleanup_exc:
                    raise CampaignAttemptError(
                        "attempt failed before START acceptance and its owned "
                        f"selection could not be removed: {cleanup_exc}"
                    ) from exc
            raise


def launch_attempt(
    root=ROOT,
    decision_hash=None,
    controller_hash=None,
    *,
    max_actions=5000,
    state_loader=None,
    send_payload_fn=None,
    runner_fn=None,
    selector_rng=None,
    validation_batch=False,
    p0_only_batch=False,
    request_id_factory=None,
    lease_class=OSLease,
):
    """Select, START, and supervise exactly one attempt."""

    try:
        return _launch_attempt_impl(
            root,
            decision_hash,
            controller_hash,
            max_actions=max_actions,
            state_loader=state_loader,
            send_payload_fn=send_payload_fn,
            runner_fn=runner_fn,
            selector_rng=selector_rng,
            validation_batch=validation_batch,
            p0_only_batch=p0_only_batch,
            request_id_factory=request_id_factory,
            lease_class=lease_class,
        )
    except CampaignAttemptError:
        raise
    except Exception as exc:
        raise CampaignAttemptError(f"attempt launch failed: {exc}") from exc


def _validated_terminal_result(root):
    result = _read_object(Path(root) / RESULT_NAME, "authoritative run result")
    try:
        full_binding = autoplay_runner._binding_from_result(result)
    except autoplay_runner.RunnerError as exc:
        raise CampaignAttemptError(str(exc)) from exc
    expected = {
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
    }
    for field, wanted in expected.items():
        if not _same_typed_value(result.get(field), wanted):
            raise CampaignAttemptError(
                f"run result {field} is not authoritative"
            )
    if not _same_typed_value(
        result.get("state_seq"), result.get("terminal_state_seq")
    ):
        raise CampaignAttemptError("run result terminal sequence mismatch")
    for field in ("victory", "heart_defeated"):
        if type(result.get(field)) is not bool:
            raise CampaignAttemptError(f"run result {field} is not boolean")
    if result.get("heart_defeated") and not result.get("victory"):
        raise CampaignAttemptError("Heart defeat is not paired with victory")
    binding = {
        field: full_binding[field] for field in ATTEMPT_BINDING_FIELDS
    }
    return result, binding


def _validate_clean_controller_exit(exit_record, result):
    error = campaign_selector._controller_exit_validation_error(
        exit_record, result
    )
    if error is not None:
        raise CampaignAttemptError(
            f"controller exit is not clear and bound: {error}"
        )
    return exit_record


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_object(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_nonclear_audit(audit, result):
    if not isinstance(audit, dict):
        raise CampaignAttemptError("maintenance audit is not an object")
    expected = {
        "record_type": campaign_selector.AUDIT_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "terminal_state_seq": result["terminal_state_seq"],
    }
    for field, wanted in expected.items():
        if not _same_typed_value(audit.get(field), wanted):
            raise CampaignAttemptError(
                f"maintenance audit binding mismatch: {field}"
            )
    if audit.get("termination_kind") not in {None, "game_over"}:
        raise CampaignAttemptError(
            "maintenance audit termination_kind mismatch"
        )
    if audit.get("audit_status") not in {
        "clear",
        "issues",
        "inconclusive",
    }:
        raise CampaignAttemptError("maintenance audit status is invalid")
    if type(audit.get("release_gate_passed")) is not bool:
        raise CampaignAttemptError(
            "maintenance audit release gate is not explicit"
        )
    if type(audit.get("issue_count")) is not int or audit["issue_count"] < 0:
        raise CampaignAttemptError("maintenance audit issue_count is invalid")
    return audit


def _validate_observed_controller_exit(exit_record, result, attempt_dir):
    """Prove the child exited, without claiming its attempt was eligible."""

    if not isinstance(exit_record, dict):
        raise CampaignAttemptError(
            "maintenance controller exit is not an object"
        )
    expected = {
        "record_type": campaign_selector.CONTROLLER_EXIT_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "terminal_state_seq": result["terminal_state_seq"],
    }
    for field, wanted in expected.items():
        if not _same_typed_value(exit_record.get(field), wanted):
            raise CampaignAttemptError(
                f"maintenance controller exit binding mismatch: {field}"
            )
    if exit_record.get("controller_exit_status") not in {"clear", "issues"}:
        raise CampaignAttemptError(
            "maintenance controller exit has no final status"
        )
    if type(exit_record.get("exit_code")) is not int:
        raise CampaignAttemptError(
            "maintenance controller exit_code is invalid"
        )
    if exit_record.get("controller_exit_status") == "issues":
        audit = exit_record.get("controller_exit_audit")
        if (
            not isinstance(audit, dict)
            or audit.get("release_gate_passed") is not False
            or audit.get("audit_status") != "issues"
        ):
            raise CampaignAttemptError(
                "nonclear controller exit lacks its blocking audit"
            )
    sidecar = _read_object(
        Path(attempt_dir) / "controller-exit.json",
        "controller exit sidecar",
    )
    if not _same_typed_tree(sidecar, exit_record):
        raise CampaignAttemptError(
            "controller exit sidecar/history mismatch"
        )
    try:
        stdout = (Path(attempt_dir) / "controller.stdout.log").read_bytes()
        stderr = (Path(attempt_dir) / "controller.stderr.log").read_bytes()
    except OSError as exc:
        raise CampaignAttemptError(
            "controller exit stream evidence is missing"
        ) from exc
    observed = autoplay_runner._output_summary(
        stdout, stderr, exit_record["exit_code"]
    )
    for field, wanted in observed.items():
        if not _same_typed_value(exit_record.get(field), wanted):
            raise CampaignAttemptError(
                f"controller exit stream evidence mismatch: {field}"
            )
    return exit_record


def _maintenance_triplet(root, result):
    records = campaign_selector.load_history(Path(root) / HISTORY_NAME)
    attempt_id = result["attempt_id"]
    matching = [
        (index, record)
        for index, record in enumerate(records)
        if isinstance(record, dict) and record.get("attempt_id") == attempt_id
    ]
    terminals = [
        (index, record)
        for index, record in matching
        if record.get("record_type") == campaign_selector.TERMINAL_RECORD_TYPE
    ]
    audits = [
        (index, record)
        for index, record in matching
        if record.get("record_type") == campaign_selector.AUDIT_RECORD_TYPE
    ]
    exits = [
        (index, record)
        for index, record in matching
        if record.get("record_type")
        == campaign_selector.CONTROLLER_EXIT_RECORD_TYPE
    ]
    if len(terminals) != 1:
        raise CampaignAttemptError(
            "maintenance requires one unique terminal history record"
        )
    if len(audits) != 1:
        raise CampaignAttemptError(
            "maintenance requires one unique audit history record"
        )
    if len(exits) != 1:
        raise CampaignAttemptError(
            "maintenance requires one unique controller exit history record"
        )
    terminal_index, terminal = terminals[0]
    audit_index, audit = audits[0]
    exit_index, exit_record = exits[0]
    if not (terminal_index < audit_index < exit_index):
        raise CampaignAttemptError(
            "maintenance terminal/audit/controller exit order is invalid"
        )
    expected_terminal = {
        **result,
        "record_type": campaign_selector.TERMINAL_RECORD_TYPE,
    }
    if not _same_typed_tree(terminal, expected_terminal):
        raise CampaignAttemptError(
            "maintenance terminal history differs from run-result"
        )
    _validate_nonclear_audit(audit, result)
    attempt_dir = autoplay_runner.attempt_directory(root, attempt_id)
    _validate_observed_controller_exit(exit_record, result, attempt_dir)
    return {
        "records": records,
        "terminal": terminal,
        "audit": audit,
        "controller_exit": exit_record,
        "indices": [terminal_index, audit_index, exit_index],
        "attempt_dir": attempt_dir,
    }


def _finite_positive_timestamp(value, label):
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise CampaignAttemptError(f"{label} is invalid")
    return float(value)


def _validated_original_runtime_anchor(
    root,
    exit_record,
    *,
    process_alive_fn,
    require_running,
):
    """Bind a terminal attempt to its immutable launch and bridge snapshot."""

    root = Path(root).resolve()
    launch_id = exit_record.get("freeze_launch_id")
    start_value = exit_record.get("freeze_launch_start_record_path")
    bridge_token = exit_record.get("freeze_bridge_instance_token")
    source_digest = exit_record.get("freeze_source_digest")
    if (
        not _nonempty_string(launch_id)
        or exit_record.get("launch_id") != launch_id
        or exit_record.get("freeze_bridge_launch_id") != launch_id
        or not _nonempty_string(start_value)
        or not _nonempty_string(bridge_token)
        or not _valid_sha256(source_digest)
    ):
        raise CampaignAttemptError(
            "maintenance restart old launch identity is incomplete"
        )
    pids = {
        "launcher": exit_record.get("freeze_launcher_pid"),
        "java": exit_record.get("freeze_java_pid"),
        "bridge": exit_record.get("freeze_bridge_pid"),
    }
    if (
        any(type(pid) is not int or pid <= 0 for pid in pids.values())
        or len(set(pids.values())) != 3
    ):
        raise CampaignAttemptError(
            "maintenance restart old process identity is invalid"
        )
    bridge_started_at = _finite_positive_timestamp(
        exit_record.get("freeze_bridge_started_at"),
        "maintenance restart old bridge_started_at",
    )
    freeze_generated_at = _finite_positive_timestamp(
        exit_record.get("freeze_generated_at"),
        "maintenance restart old freeze_generated_at",
    )
    controller_observed_at = _finite_positive_timestamp(
        exit_record.get("observed_at"),
        "maintenance restart controller observed_at",
    )
    if not (
        bridge_started_at <= freeze_generated_at <= controller_observed_at
    ):
        raise CampaignAttemptError(
            "maintenance restart old runtime timestamps are unordered"
        )

    start_path = (root / start_value).resolve()
    expected_start_path = (
        root / "logs" / "launches" / f"{launch_id}.start.json"
    ).resolve()
    if start_path != expected_start_path:
        raise CampaignAttemptError(
            "maintenance restart old launch start path is not canonical"
        )
    start_record = _read_object(start_path, "old immutable launch start")
    start_sha256 = _sha256_object(start_record)
    if (
        start_sha256 != exit_record.get("freeze_launch_start_record_sha256")
        or start_sha256 != exit_record.get("freeze_launch_record_sha256")
        or start_record.get("launch_id") != launch_id
        or start_record.get("launcher_pid") != pids["launcher"]
        or start_record.get("java_pid") != pids["java"]
        or start_record.get("launch_start_record_path") != start_value
    ):
        raise CampaignAttemptError(
            "maintenance restart old immutable launch record mismatch"
        )
    launch_started_at = _finite_positive_timestamp(
        start_record.get("started_at"),
        "maintenance restart old launch started_at",
    )
    if launch_started_at > bridge_started_at:
        raise CampaignAttemptError(
            "maintenance restart old bridge predates its launch"
        )

    bridge_instance = _read_object(
        root / "bridge-instance.json", "old bridge instance"
    )
    bridge_instance_sha256 = _sha256_object(bridge_instance)
    if (
        bridge_instance_sha256
        != exit_record.get("freeze_bridge_instance_sha256")
        or bridge_instance.get("instance_token") != bridge_token
        or bridge_instance.get("launch_id") != launch_id
        or bridge_instance.get("bridge_pid") != pids["bridge"]
        or bridge_instance.get("parent_java_pid") != pids["java"]
        or bridge_instance.get("bridge_sha256")
        != exit_record.get("freeze_bridge_sha256")
        or bridge_instance.get("runtime_source_digest") != source_digest
        or float(bridge_instance.get("started_at", 0)) != bridge_started_at
    ):
        raise CampaignAttemptError(
            "maintenance restart old bridge instance mismatch"
        )
    if require_running:
        current_launch = _read_object(
            root / "launch-latest.json", "running old launch"
        )
        if not _same_typed_tree(current_launch, start_record):
            raise CampaignAttemptError(
                "maintenance restart old launch is no longer the running launch"
            )

    alive = process_alive_fn or pre_run_binding.process_is_alive
    try:
        statuses = {name: alive(pid) for name, pid in pids.items()}
    except Exception as exc:
        raise CampaignAttemptError(
            "maintenance restart old process probe failed"
        ) from exc
    if any(type(status) is not bool for status in statuses.values()):
        raise CampaignAttemptError(
            "maintenance restart old process probe is not boolean"
        )
    if require_running and (
        statuses["launcher"] is not True or statuses["java"] is not True
    ):
        raise CampaignAttemptError(
            "maintenance restart requires the old launcher and Java process"
        )
    return {
        "launch_id": launch_id,
        "launch_start_record_path": start_value,
        "launch_start_record_sha256": start_sha256,
        "launch_started_at": launch_started_at,
        "launcher_pid": pids["launcher"],
        "java_pid": pids["java"],
        "bridge_pid": pids["bridge"],
        "bridge_instance_token": bridge_token,
        "bridge_instance_sha256": bridge_instance_sha256,
        "bridge_sha256": bridge_instance["bridge_sha256"],
        "bridge_started_at": bridge_started_at,
        "freeze_source_digest": source_digest,
        "freeze_generated_at": freeze_generated_at,
        "controller_observed_at": controller_observed_at,
        "process_alive": statuses,
    }


def _maintenance_restart_intent_fields():
    return {
        "schema_version",
        "record_type",
        "intent_status",
        "eligible_for_cohort",
        "terminal_state_seq",
        "terminal_decision_id",
        "terminal_phase",
        "run_result_sha256",
        "terminal_state_sha256",
        "terminal_record_sha256",
        "audit_record_sha256",
        "controller_exit_record_sha256",
        "maintenance_decision_hash",
        "maintenance_controller_hash",
        "target_source_digest",
        "target_source_file_count",
        "old_runtime",
        "created_at",
    } | set(ATTEMPT_BINDING_FIELDS)


def _maintenance_restart_handoff_fields():
    return {
        "schema_version",
        "record_type",
        "handoff_status",
        "old_launch_id",
        "old_launch_start_record_path",
        "old_launch_start_record_sha256",
        "restart_intent_record_path",
        "restart_intent_sha256",
        "target_source_digest",
        "target_source_file_count",
        "created_at",
    }


def _build_or_validate_restart_handoff(root, attempt_dir, intent):
    root = Path(root).resolve()
    old_runtime = intent["old_runtime"]
    intent_path = (
        Path(attempt_dir) / MAINTENANCE_RESTART_INTENT_NAME
    ).resolve()
    try:
        intent_relative = intent_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise CampaignAttemptError(
            "maintenance restart intent path is not canonical"
        ) from exc
    handoff_path = launch_game.maintenance_restart_handoff_path(
        root, old_runtime["launch_id"]
    ).resolve()
    handoff = {
        "schema_version": 1,
        "record_type": "maintenance_restart_handoff",
        "handoff_status": "prepared_before_old_runtime_exit",
        "old_launch_id": old_runtime["launch_id"],
        "old_launch_start_record_path": old_runtime[
            "launch_start_record_path"
        ],
        "old_launch_start_record_sha256": old_runtime[
            "launch_start_record_sha256"
        ],
        "restart_intent_record_path": intent_relative,
        "restart_intent_sha256": _sha256_object(intent),
        "target_source_digest": intent["target_source_digest"],
        "target_source_file_count": intent["target_source_file_count"],
        "created_at": intent["created_at"],
    }
    if set(handoff) != _maintenance_restart_handoff_fields():
        raise CampaignAttemptError(
            "maintenance restart handoff construction is incomplete"
        )
    if handoff_path.exists():
        existing = _read_object(
            handoff_path, "maintenance restart handoff"
        )
        if not _same_typed_tree(existing, handoff):
            raise CampaignAttemptError(
                "maintenance restart handoff changed after preparation"
            )
        return existing
    _write_json_once(handoff_path, handoff)
    return handoff


def prepare_maintenance_restart(
    root=ROOT,
    *,
    maintenance_decision_hash,
    maintenance_controller_hash,
    state_loader=None,
    process_alive_fn=None,
    lease_class=OSLease,
    clock=time.time,
):
    """Freeze an intent while the exact failed GAME_OVER launch still exists."""

    root = Path(root).resolve()
    if not _nonempty_string(maintenance_decision_hash):
        raise CampaignAttemptError(
            "maintenance_decision_hash must be non-empty"
        )
    if not _nonempty_string(maintenance_controller_hash):
        raise CampaignAttemptError(
            "maintenance_controller_hash must be non-empty"
        )
    state_loader = state_loader or _default_state_loader(root)
    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        result, binding = _validated_terminal_result(root)
        if (
            maintenance_decision_hash == result["decision_hash"]
            and maintenance_controller_hash == result["controller_hash"]
        ):
            raise CampaignAttemptError(
                "maintenance restart requires a repaired hash"
            )
        triplet = _maintenance_triplet(root, result)
        current_sources = freeze_manifest.source_snapshot(root)
        current_source_digest = freeze_manifest.snapshot_digest(current_sources)
        old_source_digest = triplet["controller_exit"].get(
            "freeze_source_digest"
        )
        nonclear_attempt = (
            triplet["audit"].get("release_gate_passed") is False
            and triplet["controller_exit"].get("controller_exit_status")
            == "issues"
        )
        clear_audit_repair = (
            result.get("heart_defeated") is False
            and triplet["audit"].get("audit_status") == "clear"
            and triplet["audit"].get("release_gate_passed") is True
            and triplet["controller_exit"].get("controller_exit_status")
            == "clear"
            and isinstance(old_source_digest, str)
            and current_source_digest != old_source_digest
        )
        if not (nonclear_attempt or clear_audit_repair):
            raise CampaignAttemptError(
                "maintenance restart intent requires a nonclear attempt"
            )
        saved_terminal = _validate_terminal_state(
            _read_object(
                triplet["attempt_dir"] / "terminal-state.json",
                "attempt terminal state",
            ),
            result,
            binding,
        )
        current_terminal = _validate_terminal_state(
            state_loader(), result, binding
        )
        if not _same_typed_tree(current_terminal, saved_terminal):
            raise CampaignAttemptError(
                "maintenance restart current GAME_OVER differs from evidence"
            )
        _assert_clean_launch_transport(root, require_no_selection=True)
        _probe_runtime_leases(root, lease_class)
        old_runtime = _validated_original_runtime_anchor(
            root,
            triplet["controller_exit"],
            process_alive_fn=process_alive_fn,
            require_running=True,
        )
        sources = current_sources
        base = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "maintenance_restart_intent",
            "intent_status": "prepared_before_runtime_restart",
            "eligible_for_cohort": False,
            **binding,
            "terminal_state_seq": result["terminal_state_seq"],
            "terminal_decision_id": saved_terminal["decision_id"],
            "terminal_phase": saved_terminal["phase"],
            "run_result_sha256": _sha256_object(result),
            "terminal_state_sha256": _sha256_object(saved_terminal),
            "terminal_record_sha256": _sha256_object(triplet["terminal"]),
            "audit_record_sha256": _sha256_object(triplet["audit"]),
            "controller_exit_record_sha256": _sha256_object(
                triplet["controller_exit"]
            ),
            "maintenance_decision_hash": maintenance_decision_hash,
            "maintenance_controller_hash": maintenance_controller_hash,
            "target_source_digest": freeze_manifest.snapshot_digest(sources),
            "target_source_file_count": len(sources),
            "old_runtime": old_runtime,
        }
        path = triplet["attempt_dir"] / MAINTENANCE_RESTART_INTENT_NAME
        if path.exists():
            existing = _read_object(path, "maintenance restart intent")
            if set(existing) != _maintenance_restart_intent_fields():
                raise CampaignAttemptError(
                    "maintenance restart intent fields are invalid"
                )
            for field, wanted in base.items():
                if not _same_typed_tree(existing.get(field), wanted):
                    raise CampaignAttemptError(
                        f"maintenance restart intent mismatch: {field}"
                    )
            existing_created_at = _finite_positive_timestamp(
                existing.get("created_at"),
                "maintenance restart intent created_at",
            )
            if existing_created_at <= old_runtime["controller_observed_at"]:
                raise CampaignAttemptError(
                    "maintenance restart intent predates controller exit"
                )
            _build_or_validate_restart_handoff(
                root, triplet["attempt_dir"], existing
            )
            return existing
        created_at = _finite_positive_timestamp(
            clock(), "maintenance restart intent created_at"
        )
        if created_at <= old_runtime["controller_observed_at"]:
            raise CampaignAttemptError(
                "maintenance restart intent does not follow controller exit"
            )
        intent = {**base, "created_at": created_at}
        _write_json_once(path, intent)
        _build_or_validate_restart_handoff(
            root, triplet["attempt_dir"], intent
        )
        return intent


def _validate_p0_only_batch_triplet(root, result):
    """Authorize continuation only for complete, explicit non-P0 issues."""

    triplet = _maintenance_triplet(root, result)
    assessment = {
        "terminal": triplet["terminal"],
        "audit": triplet["audit"],
        "controller_exit": triplet["controller_exit"],
        "valid": False,
        "reason": "audit_status",
    }
    error = campaign_selector.p0_only_batch_assessment_error(assessment)
    if error is not None:
        raise CampaignAttemptError(
            f"P0-only batch cannot release this attempt: {error}"
        )
    return triplet


def _maintenance_resolution_fields():
    return {
        "schema_version",
        "record_type",
        "resolution_kind",
        "resolution_status",
        "eligible_for_cohort",
        "original_findings_preserved",
        "terminal_state_seq",
        "original_audit_status",
        "original_release_gate_passed",
        "original_controller_exit_status",
        "original_exit_code",
        "maintenance_decision_hash",
        "maintenance_controller_hash",
        "freeze_manifest_record_path",
        "freeze_manifest_sha256",
        "freeze_source_digest",
        "freeze_generated_at",
        "history_sha256",
        "history_byte_length",
        "terminal_record_sha256",
        "audit_record_sha256",
        "controller_exit_record_sha256",
        "history_indices",
        "created_at",
    } | set(ATTEMPT_BINDING_FIELDS)


def _maintenance_precommit_recovery_fields():
    return {
        "schema_version",
        "record_type",
        "recovery_status",
        "attempt_id",
        "freeze_manifest_original_path",
        "freeze_manifest_archive_path",
        "freeze_manifest_sha256",
        "maintenance_resolution_original_path",
        "maintenance_resolution_archive_path",
        "maintenance_resolution_sha256",
        "menu_transition_absent",
        "created_at",
    }


def _resolution_matches_base(resolution, base):
    if (
        not isinstance(resolution, dict)
        or set(resolution) != _maintenance_resolution_fields()
    ):
        return False
    if any(
        not _same_typed_tree(resolution.get(field), wanted)
        for field, wanted in base.items()
    ):
        return False
    created_at = resolution.get("created_at")
    return (
        type(created_at) in {int, float}
        and math.isfinite(float(created_at))
        and float(created_at) > 0
    )


def _preserve_immutable_file(original, archive, expected_sha256):
    original = Path(original)
    archive = Path(archive)
    if archive.exists():
        try:
            archived = archive.read_bytes()
        except OSError as exc:
            raise CampaignAttemptError(
                f"archived maintenance evidence is unreadable: {archive}"
            ) from exc
        if hashlib.sha256(archived).hexdigest() != expected_sha256:
            raise CampaignAttemptError(
                f"archived maintenance evidence changed: {archive}"
            )
        return
    try:
        os.link(original, archive)
    except FileExistsError:
        return _preserve_immutable_file(
            original, archive, expected_sha256
        )
    except OSError as exc:
        raise CampaignAttemptError(
            f"maintenance evidence could not be archived: {original}"
        ) from exc
    try:
        archived = archive.read_bytes()
    except OSError as exc:
        raise CampaignAttemptError(
            f"archived maintenance evidence is unreadable: {archive}"
        ) from exc
    if hashlib.sha256(archived).hexdigest() != expected_sha256:
        raise CampaignAttemptError(
            f"archived maintenance evidence changed: {archive}"
        )


def _unlink_preserved_precommit(path, expected_sha256):
    path = Path(path)
    if not path.exists():
        return
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise CampaignAttemptError(
            f"uncommitted maintenance evidence is unreadable: {path}"
        ) from exc
    if hashlib.sha256(current).hexdigest() != expected_sha256:
        # A later successful retry has already installed a new canonical
        # record.  The archived predecessor must remain, but the new record
        # must not be removed while replaying the recovery sidecar.
        return
    try:
        path.unlink()
    except OSError as exc:
        raise CampaignAttemptError(
            f"uncommitted maintenance evidence could not be retired: {path}"
        ) from exc


def _recover_uncommitted_maintenance_evidence(
    root,
    triplet,
    binding,
    base,
    manifest_bytes,
    *,
    allow_recovery,
    clock,
):
    """Preserve and retire a failed pre-transition evidence pair."""

    root = Path(root).resolve()
    attempt_dir = Path(triplet["attempt_dir"]).resolve()
    manifest_path = attempt_dir / MAINTENANCE_FREEZE_MANIFEST_NAME
    resolution_path = attempt_dir / MAINTENANCE_RESOLUTION_NAME
    transition_path = attempt_dir / MENU_TRANSITION_NAME
    recovery_path = attempt_dir / MAINTENANCE_PRECOMMIT_RECOVERY_NAME

    if recovery_path.exists():
        recovery = _read_object(
            recovery_path, "maintenance precommit recovery"
        )
        if set(recovery) != _maintenance_precommit_recovery_fields():
            raise CampaignAttemptError(
                "maintenance precommit recovery fields are invalid"
            )
        expected_paths = {
            "freeze_manifest_original_path": (
                manifest_path.relative_to(root).as_posix()
            ),
            "maintenance_resolution_original_path": (
                resolution_path.relative_to(root).as_posix()
            ),
        }
        for field, wanted in {
            "schema_version": SCHEMA_VERSION,
            "record_type": "maintenance_resolution_precommit_recovery",
            "recovery_status": "archived_uncommitted_resolution",
            "attempt_id": binding["attempt_id"],
            "menu_transition_absent": True,
            **expected_paths,
        }.items():
            if not _same_typed_value(recovery.get(field), wanted):
                raise CampaignAttemptError(
                    f"maintenance precommit recovery mismatch: {field}"
                )
        _finite_positive_timestamp(
            recovery.get("created_at"),
            "maintenance precommit recovery created_at",
        )
        manifest_digest = recovery.get("freeze_manifest_sha256")
        resolution_digest = recovery.get(
            "maintenance_resolution_sha256"
        )
        if not all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(
                character in "0123456789abcdef"
                for character in digest
            )
            for digest in (manifest_digest, resolution_digest)
        ):
            raise CampaignAttemptError(
                "maintenance precommit recovery digest is invalid"
            )
        expected_manifest_archive = attempt_dir / (
            "maintenance-freeze-manifest.uncommitted-"
            f"{manifest_digest}.json"
        )
        expected_resolution_archive = attempt_dir / (
            "maintenance-resolution.uncommitted-"
            f"{resolution_digest}.json"
        )
        archive_paths = {
            "freeze_manifest_archive_path": (
                expected_manifest_archive.relative_to(root).as_posix()
            ),
            "maintenance_resolution_archive_path": (
                expected_resolution_archive.relative_to(root).as_posix()
            ),
        }
        for field, wanted in archive_paths.items():
            if recovery.get(field) != wanted:
                raise CampaignAttemptError(
                    f"maintenance precommit recovery mismatch: {field}"
                )
        archive_pairs = (
            (
                manifest_path,
                expected_manifest_archive,
                manifest_digest,
            ),
            (
                resolution_path,
                expected_resolution_archive,
                resolution_digest,
            ),
        )
        for _original, archive, digest in archive_pairs:
            try:
                archived = archive.read_bytes()
            except OSError as exc:
                raise CampaignAttemptError(
                    "maintenance precommit archive is unreadable"
                ) from exc
            if hashlib.sha256(archived).hexdigest() != digest:
                raise CampaignAttemptError(
                    "maintenance precommit archive changed"
                )
        if not transition_path.exists():
            for original, _archive, digest in archive_pairs:
                _unlink_preserved_precommit(original, digest)

    manifest_raw = (
        manifest_path.read_bytes() if manifest_path.exists() else None
    )
    resolution = (
        _read_object(resolution_path, "maintenance resolution")
        if resolution_path.exists()
        else None
    )
    if (
        manifest_raw in {None, manifest_bytes}
        and (
            resolution is None
            or _resolution_matches_base(resolution, base)
        )
    ):
        return
    if not allow_recovery:
        raise CampaignAttemptError(
            "refusing to overwrite existing maintenance evidence"
        )
    if transition_path.exists():
        raise CampaignAttemptError(
            "committed menu transition forbids maintenance evidence recovery"
        )
    if manifest_raw is None or resolution is None:
        raise CampaignAttemptError(
            "uncommitted maintenance evidence pair is incomplete"
        )
    if (
        set(resolution) != _maintenance_resolution_fields()
        or not _nonempty_string(resolution.get("maintenance_decision_hash"))
        or not _nonempty_string(resolution.get("maintenance_controller_hash"))
    ):
        raise CampaignAttemptError(
            "uncommitted maintenance resolution is invalid"
        )
    mutable = {
        "maintenance_decision_hash",
        "maintenance_controller_hash",
        "freeze_manifest_sha256",
        "freeze_source_digest",
        "freeze_generated_at",
    }
    for field, wanted in base.items():
        if field not in mutable and not _same_typed_tree(
            resolution.get(field), wanted
        ):
            raise CampaignAttemptError(
                f"uncommitted maintenance resolution mismatch: {field}"
            )
    _finite_positive_timestamp(
        resolution.get("created_at"),
        "uncommitted maintenance resolution created_at",
    )
    try:
        old_manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignAttemptError(
            "uncommitted maintenance freeze is unreadable"
        ) from exc
    old_manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    old_sources = (
        old_manifest.get("sources")
        if isinstance(old_manifest, dict)
        else None
    )
    if (
        not isinstance(old_sources, dict)
        or old_manifest.get("release_gate_passed") is not True
        or old_manifest.get("decision_hash")
        != resolution["maintenance_decision_hash"]
        or old_manifest.get("controller_hash")
        != resolution["maintenance_controller_hash"]
        or old_manifest.get("source_file_count") != len(old_sources)
        or old_manifest.get("source_digest")
        != freeze_manifest.snapshot_digest(old_sources)
        or resolution.get("freeze_manifest_sha256")
        != old_manifest_sha256
        or resolution.get("freeze_source_digest")
        != old_manifest.get("source_digest")
        or resolution.get("freeze_generated_at")
        != old_manifest.get("generated_at")
    ):
        raise CampaignAttemptError(
            "uncommitted maintenance freeze binding is invalid"
        )
    resolution_raw = resolution_path.read_bytes()
    resolution_sha256 = hashlib.sha256(resolution_raw).hexdigest()
    manifest_archive = attempt_dir / (
        "maintenance-freeze-manifest.uncommitted-"
        f"{old_manifest_sha256}.json"
    )
    resolution_archive = attempt_dir / (
        "maintenance-resolution.uncommitted-"
        f"{resolution_sha256}.json"
    )
    _preserve_immutable_file(
        manifest_path, manifest_archive, old_manifest_sha256
    )
    _preserve_immutable_file(
        resolution_path, resolution_archive, resolution_sha256
    )
    if recovery_path.exists():
        recovery_path = attempt_dir / (
            "maintenance-resolution-precommit-recovery-"
            f"{resolution_sha256}.json"
        )
    recovery_base = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "maintenance_resolution_precommit_recovery",
        "recovery_status": "archived_uncommitted_resolution",
        "attempt_id": binding["attempt_id"],
        "freeze_manifest_original_path": (
            manifest_path.relative_to(root).as_posix()
        ),
        "freeze_manifest_archive_path": (
            manifest_archive.relative_to(root).as_posix()
        ),
        "freeze_manifest_sha256": old_manifest_sha256,
        "maintenance_resolution_original_path": (
            resolution_path.relative_to(root).as_posix()
        ),
        "maintenance_resolution_archive_path": (
            resolution_archive.relative_to(root).as_posix()
        ),
        "maintenance_resolution_sha256": resolution_sha256,
        "menu_transition_absent": True,
    }
    if recovery_path.exists():
        recovery = _read_object(
            recovery_path, "maintenance precommit recovery"
        )
        if set(recovery) != _maintenance_precommit_recovery_fields():
            raise CampaignAttemptError(
                "maintenance precommit recovery fields are invalid"
            )
        for field, wanted in recovery_base.items():
            if not _same_typed_value(recovery.get(field), wanted):
                raise CampaignAttemptError(
                    f"maintenance precommit recovery mismatch: {field}"
                )
        _finite_positive_timestamp(
            recovery.get("created_at"),
            "maintenance precommit recovery created_at",
        )
    else:
        recovery = {
            **recovery_base,
            "created_at": _finite_positive_timestamp(
                clock(), "maintenance precommit recovery created_at"
            ),
        }
        _write_json_once(recovery_path, recovery)
    _unlink_preserved_precommit(manifest_path, old_manifest_sha256)
    _unlink_preserved_precommit(resolution_path, resolution_sha256)


def _build_or_validate_maintenance_resolution(
    root,
    result,
    binding,
    *,
    maintenance_decision_hash,
    maintenance_controller_hash,
    clock,
    allow_uncommitted_recovery=False,
):
    if not _nonempty_string(maintenance_decision_hash):
        raise CampaignAttemptError(
            "maintenance_decision_hash must be non-empty"
        )
    if not _nonempty_string(maintenance_controller_hash):
        raise CampaignAttemptError(
            "maintenance_controller_hash must be non-empty"
        )
    if (
        maintenance_decision_hash == result["decision_hash"]
        and maintenance_controller_hash == result["controller_hash"]
    ):
        raise CampaignAttemptError(
            "maintenance requires a newly frozen repair/controller hash"
        )
    triplet = _maintenance_triplet(root, result)
    prerequisite = campaign_selector.validate_launch_prerequisites(
        triplet["records"],
        maintenance_decision_hash,
        maintenance_controller_hash,
        root,
        freeze_manifest_path=Path(root) / FREEZE_MANIFEST_NAME,
        cohort_review_path=Path(root) / COHORT_REVIEW_NAME,
    )
    manifest_path = Path(root) / FREEZE_MANIFEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        history_bytes = (Path(root) / HISTORY_NAME).read_bytes()
    except OSError as exc:
        raise CampaignAttemptError(
            "maintenance gate evidence is unreadable"
        ) from exc
    manifest = prerequisite.get("manifest")
    manifest_snapshot_path = (
        Path(triplet["attempt_dir"]) / MAINTENANCE_FREEZE_MANIFEST_NAME
    ).resolve()
    try:
        manifest_snapshot_relative = manifest_snapshot_path.relative_to(
            Path(root).resolve()
        ).as_posix()
    except ValueError as exc:
        raise CampaignAttemptError(
            "maintenance freeze snapshot path is not canonical"
        ) from exc
    base = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "maintenance_resolution",
        "resolution_kind": "terminal_screen_release_after_repair",
        "resolution_status": "maintenance_gate_passed",
        "eligible_for_cohort": False,
        "original_findings_preserved": True,
        **binding,
        "terminal_state_seq": result["terminal_state_seq"],
        "original_audit_status": triplet["audit"].get("audit_status"),
        "original_release_gate_passed": triplet["audit"].get(
            "release_gate_passed"
        ),
        "original_controller_exit_status": triplet[
            "controller_exit"
        ].get("controller_exit_status"),
        "original_exit_code": triplet["controller_exit"].get("exit_code"),
        "maintenance_decision_hash": maintenance_decision_hash,
        "maintenance_controller_hash": maintenance_controller_hash,
        "freeze_manifest_record_path": manifest_snapshot_relative,
        "freeze_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "freeze_source_digest": (manifest or {}).get("source_digest"),
        "freeze_generated_at": (manifest or {}).get("generated_at"),
        "history_sha256": hashlib.sha256(history_bytes).hexdigest(),
        "history_byte_length": len(history_bytes),
        "terminal_record_sha256": _sha256_object(triplet["terminal"]),
        "audit_record_sha256": _sha256_object(triplet["audit"]),
        "controller_exit_record_sha256": _sha256_object(
            triplet["controller_exit"]
        ),
        "history_indices": triplet["indices"],
    }
    path = Path(triplet["attempt_dir"]) / MAINTENANCE_RESOLUTION_NAME
    _recover_uncommitted_maintenance_evidence(
        root,
        triplet,
        binding,
        base,
        manifest_bytes,
        allow_recovery=allow_uncommitted_recovery,
        clock=clock,
    )
    _write_bytes_once(manifest_snapshot_path, manifest_bytes)
    if path.exists():
        existing = _read_object(path, "maintenance resolution")
        if set(existing) != _maintenance_resolution_fields():
            raise CampaignAttemptError(
                "maintenance resolution fields are invalid"
            )
        for field, wanted in base.items():
            if not _same_typed_tree(existing.get(field), wanted):
                raise CampaignAttemptError(
                    f"maintenance resolution mismatch: {field}"
                )
        created_at = existing.get("created_at")
        if (
            type(created_at) not in {int, float}
            or not math.isfinite(float(created_at))
            or created_at <= 0
        ):
            raise CampaignAttemptError(
                "maintenance resolution created_at is invalid"
            )
        return existing
    resolution = {**base, "created_at": float(clock())}
    if set(resolution) != _maintenance_resolution_fields():
        raise CampaignAttemptError(
            "maintenance resolution construction is incomplete"
        )
    _write_json_once(path, resolution)
    return resolution


def _old_runtime_fields():
    return {
        "launch_id",
        "launch_start_record_path",
        "launch_start_record_sha256",
        "launch_started_at",
        "launcher_pid",
        "java_pid",
        "bridge_pid",
        "bridge_instance_token",
        "bridge_instance_sha256",
        "bridge_sha256",
        "bridge_started_at",
        "freeze_source_digest",
        "freeze_generated_at",
        "controller_observed_at",
        "process_alive",
    }


def _load_valid_maintenance_restart_intent(
    root, result, binding, triplet, maintenance_resolution, manifest
):
    path = (
        Path(triplet["attempt_dir"]) / MAINTENANCE_RESTART_INTENT_NAME
    )
    intent = _read_object(path, "maintenance restart intent")
    if set(intent) != _maintenance_restart_intent_fields():
        raise CampaignAttemptError(
            "maintenance restart intent fields are invalid"
        )
    expected = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "maintenance_restart_intent",
        "intent_status": "prepared_before_runtime_restart",
        "eligible_for_cohort": False,
        **binding,
        "terminal_state_seq": result["terminal_state_seq"],
        "maintenance_decision_hash": intent.get(
            "maintenance_decision_hash"
        ),
        "maintenance_controller_hash": intent.get(
            "maintenance_controller_hash"
        ),
        "target_source_digest": intent.get("target_source_digest"),
        "target_source_file_count": intent.get("target_source_file_count"),
        "run_result_sha256": _sha256_object(result),
        "terminal_record_sha256": _sha256_object(triplet["terminal"]),
        "audit_record_sha256": _sha256_object(triplet["audit"]),
        "controller_exit_record_sha256": _sha256_object(
            triplet["controller_exit"]
        ),
    }
    terminal_state = _validate_terminal_state(
        _read_object(
            Path(triplet["attempt_dir"]) / "terminal-state.json",
            "attempt terminal state",
        ),
        result,
        binding,
    )
    expected.update({
        "terminal_decision_id": terminal_state["decision_id"],
        "terminal_phase": terminal_state["phase"],
        "terminal_state_sha256": _sha256_object(terminal_state),
    })
    for field, wanted in expected.items():
        if not _same_typed_tree(intent.get(field), wanted):
            raise CampaignAttemptError(
                f"maintenance restart intent mismatch: {field}"
            )
    manifest_sources = manifest.get("sources")
    manifest_digest = manifest.get("source_digest")
    intent_digest = intent.get("target_source_digest")
    intent_count = intent.get("target_source_file_count")
    runtime_migration_used = False
    if (
        intent_digest != manifest_digest
        or intent_count != manifest.get("source_file_count")
    ):
        launch_evidence = manifest.get("launch_evidence")
        migration = (
            launch_evidence.get("runtime_source_migration")
            if isinstance(launch_evidence, dict)
            else None
        )
        direct_migration_valid = (
            isinstance(manifest_sources, dict)
            and isinstance(migration, dict)
            and migration.get("previous_source_digest") == intent_digest
            and migration.get("previous_source_file_count") == intent_count
            and migration.get("current_source_digest") == manifest_digest
            and migration.get("current_source_file_count")
            == manifest.get("source_file_count")
            and freeze_manifest._validate_runtime_source_migration(
                migration, intent_digest, intent_count, manifest_sources
            )
        )
        if not direct_migration_valid:
            _build_or_validate_maintenance_source_reconciliation(
                root, intent, manifest, triplet["attempt_dir"]
            )
        runtime_migration_used = True
    if (
        (
            intent.get("maintenance_decision_hash")
            != maintenance_resolution["maintenance_decision_hash"]
            or intent.get("maintenance_controller_hash")
            != maintenance_resolution["maintenance_controller_hash"]
        )
        and not runtime_migration_used
    ):
        raise CampaignAttemptError(
            "maintenance restart intent mismatch: maintenance_decision_hash"
        )
    created_at = _finite_positive_timestamp(
        intent.get("created_at"), "maintenance restart intent created_at"
    )
    old = intent.get("old_runtime")
    if not isinstance(old, dict) or set(old) != _old_runtime_fields():
        raise CampaignAttemptError(
            "maintenance restart intent old runtime fields are invalid"
        )
    exit_record = triplet["controller_exit"]
    old_expected = {
        "launch_id": exit_record.get("freeze_launch_id"),
        "launch_start_record_path": exit_record.get(
            "freeze_launch_start_record_path"
        ),
        "launch_start_record_sha256": exit_record.get(
            "freeze_launch_start_record_sha256"
        ),
        "launcher_pid": exit_record.get("freeze_launcher_pid"),
        "java_pid": exit_record.get("freeze_java_pid"),
        "bridge_pid": exit_record.get("freeze_bridge_pid"),
        "bridge_instance_token": exit_record.get(
            "freeze_bridge_instance_token"
        ),
        "bridge_instance_sha256": exit_record.get(
            "freeze_bridge_instance_sha256"
        ),
        "bridge_sha256": exit_record.get("freeze_bridge_sha256"),
        "bridge_started_at": exit_record.get("freeze_bridge_started_at"),
        "freeze_source_digest": exit_record.get("freeze_source_digest"),
        "freeze_generated_at": exit_record.get("freeze_generated_at"),
        "controller_observed_at": exit_record.get("observed_at"),
    }
    for field, wanted in old_expected.items():
        if not _same_typed_value(old.get(field), wanted):
            raise CampaignAttemptError(
                f"maintenance restart old runtime mismatch: {field}"
            )
    start_path = (Path(root) / old["launch_start_record_path"]).resolve()
    start_record = _read_object(start_path, "old immutable launch start")
    if (
        _sha256_object(start_record) != old["launch_start_record_sha256"]
        or start_record.get("launch_id") != old["launch_id"]
        or start_record.get("launcher_pid") != old["launcher_pid"]
        or start_record.get("java_pid") != old["java_pid"]
    ):
        raise CampaignAttemptError(
            "maintenance restart intent old launch record changed"
        )
    launch_started_at = _finite_positive_timestamp(
        start_record.get("started_at"),
        "maintenance restart old launch started_at",
    )
    if launch_started_at != old.get("launch_started_at"):
        raise CampaignAttemptError(
            "maintenance restart old launch timestamp mismatch"
        )
    statuses = old.get("process_alive")
    if (
        not isinstance(statuses, dict)
        or set(statuses) != {"launcher", "java", "bridge"}
        or any(type(value) is not bool for value in statuses.values())
        or statuses["launcher"] is not True
        or statuses["java"] is not True
    ):
        raise CampaignAttemptError(
            "maintenance restart intent did not observe the old launch"
        )
    observed_at = _finite_positive_timestamp(
        old.get("controller_observed_at"),
        "maintenance restart controller observed_at",
    )
    if created_at <= observed_at:
        raise CampaignAttemptError(
            "maintenance restart intent predates controller exit"
        )
    return intent


def _maintenance_source_reconciliation_fields():
    return {
        "schema_version",
        "record_type",
        "reconciliation_status",
        "attempt_id",
        "restart_intent_sha256",
        "old_launch_id",
        "replacement_launch_id",
        "target_source_digest",
        "target_source_file_count",
        "bridge_runtime_source_digest",
        "bridge_runtime_source_file_count",
        "freeze_source_digest",
        "freeze_source_file_count",
        "maintenance_decision_hash",
        "maintenance_controller_hash",
        "old_bridge_sha256",
        "replacement_bridge_sha256",
        "communication_mod_jar_sha256",
        "freeze_manifest_sha256",
        "created_at",
    }


def _runtime_to_freeze_source_chain(manifest, bridge):
    sources = manifest.get("sources")
    launch_evidence = manifest.get("launch_evidence")
    if not isinstance(sources, dict) or not isinstance(launch_evidence, dict):
        return None
    runtime_digest = bridge.get("runtime_source_digest")
    runtime_count = bridge.get("runtime_source_file_count")
    manifest_digest = manifest.get("source_digest")
    manifest_count = manifest.get("source_file_count")
    if (
        launch_evidence.get("bridge_runtime_source_digest") != runtime_digest
        or launch_evidence.get("bridge_runtime_source_file_count")
        != runtime_count
    ):
        return None
    migration = launch_evidence.get("runtime_source_migration")
    if runtime_digest == manifest_digest and runtime_count == manifest_count:
        return None if migration is not None else ""
    if not (
        isinstance(migration, dict)
        and migration.get("previous_source_digest") == runtime_digest
        and migration.get("previous_source_file_count") == runtime_count
        and migration.get("current_source_digest") == manifest_digest
        and migration.get("current_source_file_count") == manifest_count
        and freeze_manifest._validate_runtime_source_migration(
            migration, runtime_digest, runtime_count, sources
        )
    ):
        return None
    return _sha256_object(migration)


def _build_or_validate_maintenance_source_reconciliation(
    root, intent, manifest, attempt_dir, *, clock=time.time, create=True
):
    """Bridge an immutable intent to an attested replacement runtime.

    Creation is allowed only while the root freeze still exactly describes
    the running replacement bridge and retains the intent's policy hashes.
    That freeze is archived once.  A later audited source migration may then
    move the same bridge runtime to a newer fully tested freeze without
    weakening or rewriting this first-leg attestation.
    """

    root = Path(root).resolve()
    attempt_dir = Path(attempt_dir).resolve()
    launch = _read_object(root / "launch-latest.json", "replacement launch")
    bridge = _read_object(
        root / "bridge-instance.json", "replacement bridge instance"
    )
    old_runtime = intent.get("old_runtime")
    if not isinstance(old_runtime, dict):
        raise CampaignAttemptError(
            "maintenance restart source reconciliation lacks old runtime"
        )
    old_start_path = (root / old_runtime["launch_start_record_path"]).resolve()
    try:
        old_start_path.relative_to(root)
    except ValueError as exc:
        raise CampaignAttemptError(
            "maintenance restart old launch path is not canonical"
        ) from exc
    old_start = _read_object(old_start_path, "maintenance old launch")
    if _sha256_object(old_start) != old_runtime["launch_start_record_sha256"]:
        raise CampaignAttemptError(
            "maintenance restart old launch changed before reconciliation"
        )

    migration_sha256 = _runtime_to_freeze_source_chain(manifest, bridge)
    if migration_sha256 is None:
        raise CampaignAttemptError(
            "replacement runtime does not have a validated source chain"
        )
    jar_hashes = {
        old_start.get("root_communication_mod_jar_sha256"),
        old_start.get("installed_communication_mod_jar_sha256"),
        launch.get("root_communication_mod_jar_sha256"),
        launch.get("installed_communication_mod_jar_sha256"),
    }
    if (
        len(jar_hashes) != 1
        or not all(_valid_sha256(value) for value in jar_hashes)
        or bridge.get("bridge_sha256") != old_runtime.get("bridge_sha256")
        or launch.get("launch_id") == old_runtime.get("launch_id")
        or bridge.get("launch_id") != launch.get("launch_id")
        or manifest.get("release_gate_passed") is not True
        or intent.get("target_source_file_count")
        != bridge.get("runtime_source_file_count")
        or manifest.get("source_file_count")
        != bridge.get("runtime_source_file_count")
    ):
        raise CampaignAttemptError(
            "maintenance restart source drift changed a runtime identity"
        )
    path = attempt_dir / MAINTENANCE_SOURCE_RECONCILIATION_NAME
    archive_path = attempt_dir / MAINTENANCE_RUNTIME_FREEZE_NAME
    if path.exists():
        try:
            archive_bytes = archive_path.read_bytes()
            attestation_manifest = json.loads(archive_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CampaignAttemptError(
                "maintenance replacement runtime freeze is unreadable"
            ) from exc
    else:
        if not create:
            raise CampaignAttemptError(
                "maintenance restart source reconciliation is missing"
            )
        if (
            migration_sha256 != ""
            or manifest.get("source_digest")
            != bridge.get("runtime_source_digest")
            or manifest.get("source_file_count")
            != bridge.get("runtime_source_file_count")
            or manifest.get("decision_hash")
            != intent.get("maintenance_decision_hash")
            or manifest.get("controller_hash")
            != intent.get("maintenance_controller_hash")
        ):
            raise CampaignAttemptError(
                "maintenance replacement runtime lost its exact attestation"
            )
        freeze_path = root / FREEZE_MANIFEST_NAME
        try:
            archive_bytes = freeze_path.read_bytes()
            attestation_manifest = json.loads(
                archive_bytes.decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CampaignAttemptError(
                "maintenance restart freeze is unreadable"
            ) from exc
        _write_bytes_once(archive_path, archive_bytes)
    if (
        not isinstance(attestation_manifest, dict)
        or attestation_manifest.get("release_gate_passed") is not True
        or attestation_manifest.get("source_digest")
        != bridge.get("runtime_source_digest")
        or attestation_manifest.get("source_file_count")
        != bridge.get("runtime_source_file_count")
        or attestation_manifest.get("decision_hash")
        != intent.get("maintenance_decision_hash")
        or attestation_manifest.get("controller_hash")
        != intent.get("maintenance_controller_hash")
    ):
        raise CampaignAttemptError(
            "maintenance replacement runtime attestation is invalid"
        )
    freeze_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    base = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "maintenance_restart_source_reconciliation",
        "reconciliation_status": "runtime_identity_preserved",
        "attempt_id": intent["attempt_id"],
        "restart_intent_sha256": _sha256_object(intent),
        "old_launch_id": old_runtime["launch_id"],
        "replacement_launch_id": launch["launch_id"],
        "target_source_digest": intent["target_source_digest"],
        "target_source_file_count": intent["target_source_file_count"],
        "bridge_runtime_source_digest": bridge["runtime_source_digest"],
        "bridge_runtime_source_file_count": bridge[
            "runtime_source_file_count"
        ],
        "freeze_source_digest": attestation_manifest["source_digest"],
        "freeze_source_file_count": attestation_manifest[
            "source_file_count"
        ],
        "maintenance_decision_hash": intent["maintenance_decision_hash"],
        "maintenance_controller_hash": intent[
            "maintenance_controller_hash"
        ],
        "old_bridge_sha256": old_runtime["bridge_sha256"],
        "replacement_bridge_sha256": bridge["bridge_sha256"],
        "communication_mod_jar_sha256": next(iter(jar_hashes)),
        "freeze_manifest_sha256": freeze_sha256,
    }
    if path.exists():
        existing = _read_object(
            path, "maintenance restart source reconciliation"
        )
        if set(existing) != _maintenance_source_reconciliation_fields():
            raise CampaignAttemptError(
                "maintenance restart source reconciliation fields are invalid"
            )
        for field, wanted in base.items():
            if not _same_typed_tree(existing.get(field), wanted):
                raise CampaignAttemptError(
                    "maintenance restart source reconciliation changed: "
                    + field
                )
        _finite_positive_timestamp(
            existing.get("created_at"),
            "maintenance restart source reconciliation created_at",
        )
        return existing
    reconciliation = {
        **base,
        "created_at": _finite_positive_timestamp(
            clock(), "maintenance restart source reconciliation created_at"
        ),
    }
    if set(reconciliation) != _maintenance_source_reconciliation_fields():
        raise CampaignAttemptError(
            "maintenance restart source reconciliation is incomplete"
        )
    _write_json_once(path, reconciliation)
    return reconciliation


def _validate_old_launch_exit(root, intent, *, process_alive_fn):
    old = intent["old_runtime"]
    root = Path(root).resolve()
    start_record = _read_object(
        root / old["launch_start_record_path"],
        "old immutable launch start",
    )
    exit_path = (
        root / "logs" / "launches" / f"{old['launch_id']}.exit.json"
    ).resolve()
    exit_record = _read_object(exit_path, "old immutable launch exit")
    exit_relative = exit_path.relative_to(root).as_posix()
    expected_fields = set(start_record) | {
        "launch_exit_record_path",
        "finished_at",
        "exit_code",
    }
    if set(exit_record) != expected_fields:
        raise CampaignAttemptError(
            "maintenance restart old launch exit fields are invalid"
        )
    for field, wanted in start_record.items():
        if not _same_typed_tree(exit_record.get(field), wanted):
            raise CampaignAttemptError(
                f"maintenance restart old launch exit mismatch: {field}"
            )
    if exit_record.get("launch_exit_record_path") != exit_relative:
        raise CampaignAttemptError(
            "maintenance restart old launch exit path mismatch"
        )
    finished_at = _finite_positive_timestamp(
        exit_record.get("finished_at"),
        "maintenance restart old launch finished_at",
    )
    if finished_at <= _finite_positive_timestamp(
        intent.get("created_at"), "maintenance restart intent created_at"
    ):
        raise CampaignAttemptError(
            "maintenance restart old launch did not exit after the intent"
        )
    if type(exit_record.get("exit_code")) is not int:
        raise CampaignAttemptError(
            "maintenance restart old launch exit_code is invalid"
        )
    alive = process_alive_fn or pre_run_binding.process_is_alive
    old_pids = {
        old["launcher_pid"], old["java_pid"], old["bridge_pid"]
    }
    try:
        still_alive = sorted(pid for pid in old_pids if alive(pid) is True)
    except Exception as exc:
        raise CampaignAttemptError(
            "maintenance restart old process exit probe failed"
        ) from exc
    if still_alive:
        raise CampaignAttemptError(
            "maintenance restart old processes are still alive: "
            + ",".join(str(pid) for pid in still_alive)
        )
    return {
        "record_path": exit_relative,
        "record_sha256": _sha256_object(exit_record),
        "finished_at": finished_at,
        "exit_code": exit_record["exit_code"],
    }


def _prevalidate_maintenance_restart(
    root,
    result,
    binding,
    *,
    maintenance_decision_hash,
    maintenance_controller_hash,
    process_alive_fn,
):
    """Validate restart-only evidence before committing a resolution."""

    triplet = _maintenance_triplet(root, result)
    prerequisite = campaign_selector.validate_launch_prerequisites(
        triplet["records"],
        maintenance_decision_hash,
        maintenance_controller_hash,
        root,
        freeze_manifest_path=Path(root) / FREEZE_MANIFEST_NAME,
        cohort_review_path=Path(root) / COHORT_REVIEW_NAME,
    )
    manifest = prerequisite.get("manifest")
    if not isinstance(manifest, dict):
        raise CampaignAttemptError(
            "maintenance restart freeze manifest is missing"
        )
    provisional_resolution = {
        "maintenance_decision_hash": maintenance_decision_hash,
        "maintenance_controller_hash": maintenance_controller_hash,
    }
    intent = _load_valid_maintenance_restart_intent(
        root,
        result,
        binding,
        triplet,
        provisional_resolution,
        manifest,
    )
    _validate_old_launch_exit(
        root, intent, process_alive_fn=process_alive_fn
    )
    return intent


def _validate_terminal_state(state, result, binding):
    if not isinstance(state, dict):
        raise CampaignAttemptError("terminal state is not an object")
    expected = {
        "protocol_version": PROTOCOL_VERSION,
        "in_game": True,
        "ready_for_command": True,
        "phase": "GAME_OVER",
        "state_seq": result["terminal_state_seq"],
        "terminal_state_seq": result["terminal_state_seq"],
    }
    for field, wanted in expected.items():
        if not _same_typed_value(state.get(field), wanted):
            raise CampaignAttemptError(f"terminal state {field} mismatch")
    if not _nonempty_string(state.get("decision_id")):
        raise CampaignAttemptError("terminal decision_id is missing")
    legal = state.get("legal_actions")
    if not isinstance(legal, list) or "proceed" not in {
        str(item).lower() for item in legal
    }:
        raise CampaignAttemptError("PROCEED is not legal at GAME_OVER")
    for field, wanted in binding.items():
        if not _same_typed_value(state.get(field), wanted):
            raise CampaignAttemptError(
                f"terminal state attempt binding mismatch: {field}"
            )
    game = state.get("game_state")
    if not isinstance(game, dict):
        raise CampaignAttemptError("terminal game_state is missing")
    game_expected = {
        "screen_type": "GAME_OVER",
        "class": result["character"],
        "ascension_level": 0,
        "seed": result["seed"],
        "is_standard_run": True,
        "run_victory": result["victory"],
        "heart_defeated": result["heart_defeated"],
    }
    for field, wanted in game_expected.items():
        if not _same_typed_value(game.get(field), wanted):
            raise CampaignAttemptError(
                f"terminal game_state {field} mismatch"
            )
    screen = game.get("screen_state")
    if (
        not isinstance(screen, dict)
        or not _same_typed_value(
            screen.get("victory"), result["victory"]
        )
    ):
        raise CampaignAttemptError(
            "terminal game_state screen_state.victory mismatch"
        )
    return state


def _build_proceed_payload(terminal_state, binding, request_id):
    if not _nonempty_string(request_id):
        raise CampaignAttemptError("PROCEED request_id is invalid")
    payload = {
        "id": request_id,
        "action": "proceed",
        "policy_version": POLICY_VERSION,
        "expected_seq": terminal_state["state_seq"],
        "decision_id": terminal_state["decision_id"],
        "phase": terminal_state["phase"],
        "target_id": "action:proceed",
        **binding,
    }
    if set(payload) != _PROCEED_PAYLOAD_FIELDS:
        raise CampaignAttemptError("PROCEED payload binding is incomplete")
    return payload


def _validate_proceed_payload(payload, terminal_state, binding):
    if not isinstance(payload, dict) or set(payload) != _PROCEED_PAYLOAD_FIELDS:
        raise CampaignAttemptError("PROCEED payload fields are invalid")
    expected = _build_proceed_payload(
        terminal_state, binding, payload.get("id")
    )
    if not _same_typed_tree(payload, expected):
        raise CampaignAttemptError("PROCEED payload binding mismatch")
    return payload


def _validate_proceed_receipt(receipt, payload):
    if not isinstance(receipt, dict):
        raise CampaignAttemptError("PROCEED receipt is not an object")
    if set(receipt) != _PROCEED_RECEIPT_FIELDS:
        raise CampaignAttemptError(
            "PROCEED receipt fields are incomplete or unclassified"
        )
    expected = {
        "request_id": payload["id"],
        "success": True,
        "status": "succeeded",
        "error": None,
        "accepted_state_seq": payload["expected_seq"],
        "requested_target_id": "action:proceed",
        "resolved_target_id": "action:proceed",
        **{
            field: payload[field] for field in ATTEMPT_BINDING_FIELDS
        },
    }
    for field, wanted in expected.items():
        if not _same_typed_value(receipt.get(field), wanted):
            raise CampaignAttemptError(
                f"PROCEED receipt {field} mismatch"
            )
    result_sequence = receipt.get("result_state_seq")
    if (
        type(result_sequence) is not int
        or result_sequence <= payload["expected_seq"]
    ):
        raise CampaignAttemptError(
            "PROCEED receipt result_state_seq is invalid"
        )
    return receipt


def _validate_returned_menu(state, *, minimum_seq, exact_seq=None):
    _validate_menu_state(state, minimum_seq=minimum_seq)
    if exact_seq is not None and not _same_typed_value(
        state.get("state_seq"), exact_seq
    ):
        raise CampaignAttemptError("PROCEED receipt/menu sequence mismatch")
    stale = [
        field
        for field in (*ATTEMPT_BINDING_FIELDS, "terminal_state_seq")
        if field in state
    ]
    if stale:
        raise CampaignAttemptError(
            "MAIN_MENU retained terminal attempt binding: "
            + ",".join(stale)
        )
    return state


def _load_restart_manifest(root, maintenance_resolution):
    root = Path(root).resolve()
    attempt_id = maintenance_resolution.get("attempt_id")
    if not _nonempty_string(attempt_id):
        raise CampaignAttemptError(
            "maintenance restart freeze snapshot attempt is invalid"
        )
    expected_path = (
        autoplay_runner.attempt_directory(root, attempt_id)
        / MAINTENANCE_FREEZE_MANIFEST_NAME
    ).resolve()
    path_value = maintenance_resolution.get("freeze_manifest_record_path")
    path = (root / str(path_value)).resolve()
    if (
        not isinstance(path_value, str)
        or not path_value
        or path != expected_path
        or path_value != path.relative_to(root).as_posix()
    ):
        raise CampaignAttemptError(
            "maintenance restart freeze snapshot path is not canonical"
        )
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignAttemptError(
            "maintenance restart freeze manifest is unreadable"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or hashlib.sha256(raw).hexdigest()
        != maintenance_resolution.get("freeze_manifest_sha256")
        or manifest.get("source_digest")
        != maintenance_resolution.get("freeze_source_digest")
        or manifest.get("generated_at")
        != maintenance_resolution.get("freeze_generated_at")
    ):
        raise CampaignAttemptError(
            "maintenance restart freeze manifest changed after validation"
        )
    return manifest


def _restart_predecessor_fields():
    return {
        "schema_version",
        "record_type",
        "predecessor_status",
        "lineage_depth",
        "new_launch_id",
        "new_launcher_pid",
        "new_launch_started_at",
        "new_launch_log_path",
        "old_launch_id",
        "old_launch_start_record_path",
        "old_launch_start_record_sha256",
        "old_launch_exit_record_path",
        "old_launch_exit_record_sha256",
        "restart_handoff_record_path",
        "restart_handoff_record_sha256",
        "restart_intent_record_path",
        "restart_intent_sha256",
        "lineage_parent_record_path",
        "lineage_parent_record_sha256",
        "lineage_parent_start_record_path",
        "lineage_parent_start_record_sha256",
        "lineage_parent_exit_record_path",
        "lineage_parent_exit_record_sha256",
        "lineage_parent_launch_id",
        "lineage_parent_started_at",
        "lineage_parent_finished_at",
    }


def _validate_restart_predecessor(
    root, runtime, launch, intent, old_exit
):
    root = Path(root).resolve()
    old_runtime = intent["old_runtime"]
    intent_path = (
        autoplay_runner.attempt_directory(root, intent["attempt_id"])
        / MAINTENANCE_RESTART_INTENT_NAME
    ).resolve()
    loaded_intent = _read_object(
        intent_path, "maintenance restart intent"
    )
    if not _same_typed_tree(loaded_intent, intent):
        raise CampaignAttemptError(
            "maintenance restart intent changed before replacement launch"
        )
    intent_relative = intent_path.relative_to(root).as_posix()
    intent_sha256 = _sha256_object(intent)

    handoff_path = launch_game.maintenance_restart_handoff_path(
        root, old_runtime["launch_id"]
    ).resolve()
    handoff = _read_object(
        handoff_path, "maintenance restart handoff"
    )
    expected_handoff = {
        "schema_version": 1,
        "record_type": "maintenance_restart_handoff",
        "handoff_status": "prepared_before_old_runtime_exit",
        "old_launch_id": old_runtime["launch_id"],
        "old_launch_start_record_path": old_runtime[
            "launch_start_record_path"
        ],
        "old_launch_start_record_sha256": old_runtime[
            "launch_start_record_sha256"
        ],
        "restart_intent_record_path": intent_relative,
        "restart_intent_sha256": intent_sha256,
        "target_source_digest": intent["target_source_digest"],
        "target_source_file_count": intent["target_source_file_count"],
        "created_at": intent["created_at"],
    }
    if (
        set(handoff) != _maintenance_restart_handoff_fields()
        or not _same_typed_tree(handoff, expected_handoff)
    ):
        raise CampaignAttemptError(
            "maintenance restart handoff does not bind the intent"
        )
    handoff_relative = handoff_path.relative_to(root).as_posix()
    handoff_sha256 = _sha256_object(handoff)

    predecessor_value = launch.get("restart_predecessor_record_path")
    predecessor_sha256 = launch.get(
        "restart_predecessor_record_sha256"
    )
    predecessor_path = launch_game.restart_predecessor_path(
        root, runtime.get("launch_id")
    ).resolve()
    expected_predecessor_value = predecessor_path.relative_to(root).as_posix()
    if (
        predecessor_value != expected_predecessor_value
        or not _valid_sha256(predecessor_sha256)
    ):
        raise CampaignAttemptError(
            "replacement launch does not anchor its restart predecessor"
        )
    predecessor = _read_object(
        predecessor_path, "replacement restart predecessor"
    )
    expected = {
        "schema_version": 2,
        "record_type": "restart_predecessor",
        "predecessor_status": "observed_before_new_runtime_launch",
        "new_launch_id": runtime.get("launch_id"),
        "new_launcher_pid": runtime.get("launcher_pid"),
        "new_launch_started_at": launch.get("started_at"),
        "new_launch_log_path": launch.get("launch_log_path"),
        "old_launch_id": old_runtime["launch_id"],
        "old_launch_start_record_path": old_runtime[
            "launch_start_record_path"
        ],
        "old_launch_start_record_sha256": old_runtime[
            "launch_start_record_sha256"
        ],
        "old_launch_exit_record_path": old_exit["record_path"],
        "old_launch_exit_record_sha256": old_exit["record_sha256"],
        "restart_handoff_record_path": handoff_relative,
        "restart_handoff_record_sha256": handoff_sha256,
        "restart_intent_record_path": intent_relative,
        "restart_intent_sha256": intent_sha256,
    }
    if set(predecessor) != _restart_predecessor_fields():
        raise CampaignAttemptError(
            "replacement launch predecessor evidence is invalid"
        )
    for field, wanted in expected.items():
        if not _same_typed_tree(predecessor.get(field), wanted):
            raise CampaignAttemptError(
                f"replacement launch predecessor mismatch: {field}"
            )
    if _sha256_object(predecessor) != predecessor_sha256:
        raise CampaignAttemptError(
            "replacement launch predecessor changed after launch"
        )
    lineage_root = launch_game._restart_lineage_root_fields(predecessor)
    try:
        lineage = launch_game._validate_restart_lineage_record(
            root,
            predecessor_path,
            lineage_root,
            expected_sha256=predecessor_sha256,
        )
    except launch_game.LaunchError as exc:
        raise CampaignAttemptError(str(exc)) from exc
    if not _same_typed_tree(lineage["start"]["record"], launch):
        raise CampaignAttemptError(
            "replacement launch start differs from lineage anchor"
        )
    return {
        "record_path": predecessor_path.relative_to(root).as_posix(),
        "record_sha256": predecessor_sha256,
        "handoff_record_path": handoff_relative,
        "handoff_record_sha256": handoff_sha256,
        "restart_intent_record_path": intent_relative,
        "restart_intent_sha256": intent_sha256,
        "old_launch_exit_record_path": old_exit["record_path"],
        "old_launch_exit_record_sha256": old_exit["record_sha256"],
    }


def _replacement_runtime_evidence(
    root,
    menu_state,
    manifest,
    intent,
    old_exit,
    *,
    process_alive_fn,
):
    try:
        runtime = pre_run_binding.validate_pre_dispatch_runtime(
            menu_state,
            root,
            manifest,
            process_alive_fn=process_alive_fn,
        )
    except pre_run_binding.PreRunBindingError as exc:
        raise CampaignAttemptError(str(exc)) from exc
    launch = _read_object(
        Path(root) / "launch-latest.json", "replacement running launch"
    )
    bridge = _read_object(
        Path(root) / "bridge-instance.json", "replacement bridge instance"
    )
    new_pids = {
        runtime.get("launcher_pid"),
        runtime.get("java_pid"),
        runtime.get("bridge_pid"),
    }
    old_runtime = intent["old_runtime"]
    old_pids = {
        old_runtime["launcher_pid"],
        old_runtime["java_pid"],
        old_runtime["bridge_pid"],
    }
    if (
        runtime.get("launch_id") == old_runtime["launch_id"]
        or runtime.get("bridge_instance_token")
        == old_runtime["bridge_instance_token"]
        or new_pids & old_pids
    ):
        raise CampaignAttemptError(
            "maintenance restart replacement runtime reused old identity"
        )
    launch_started_at = _finite_positive_timestamp(
        launch.get("started_at"),
        "maintenance restart replacement launch started_at",
    )
    bridge_started_at = _finite_positive_timestamp(
        bridge.get("started_at"),
        "maintenance restart replacement bridge started_at",
    )
    manifest_generated_at = _finite_positive_timestamp(
        manifest.get("generated_at"),
        "maintenance restart replacement freeze generated_at",
    )
    if not (
        old_exit["finished_at"] < launch_started_at
        <= bridge_started_at <= manifest_generated_at
    ):
        raise CampaignAttemptError(
            "maintenance restart replacement runtime timestamps are unordered"
        )
    if not _replacement_runtime_sources_match(
        runtime,
        manifest,
        intent,
        root=root,
        attempt_dir=autoplay_runner.attempt_directory(
            root, intent["attempt_id"]
        ),
    ):
        raise CampaignAttemptError(
            "maintenance restart replacement did not load exact target sources"
        )
    predecessor = _validate_restart_predecessor(
        root, runtime, launch, intent, old_exit
    )
    return {
        **runtime,
        "launch_started_at": launch_started_at,
        "bridge_started_at": bridge_started_at,
        "restart_predecessor": predecessor,
    }


def _replacement_runtime_sources_match(
    runtime, manifest, intent, *, root=None, attempt_dir=None
):
    launch_evidence = runtime.get("launch_evidence")
    if not isinstance(launch_evidence, dict):
        return False
    migration = launch_evidence.get("runtime_source_migration")
    intent_digest = intent.get("target_source_digest")
    intent_count = intent.get("target_source_file_count")
    manifest_digest = manifest.get("source_digest")
    manifest_count = manifest.get("source_file_count")
    runtime_digest = runtime.get("bridge_runtime_source_digest")
    runtime_count = runtime.get("bridge_runtime_source_file_count")
    if migration is None:
        if (
            runtime_digest == intent_digest == manifest_digest
            and runtime_count == intent_count == manifest_count
        ):
            return True
        if root is None or attempt_dir is None:
            return False
    sources = manifest.get("sources")
    if (
        isinstance(sources, dict)
        and isinstance(migration, dict)
        and runtime_digest == manifest_digest
        and runtime_count == manifest_count
        and migration.get("previous_source_digest") == intent_digest
        and migration.get("previous_source_file_count") == intent_count
        and migration.get("current_source_digest") == manifest_digest
        and migration.get("current_source_file_count") == manifest_count
        and freeze_manifest._validate_runtime_source_migration(
            migration, intent_digest, intent_count, sources
        )
    ):
        return True
    if root is None or attempt_dir is None:
        return False
    try:
        reconciliation = _build_or_validate_maintenance_source_reconciliation(
            root, intent, manifest, attempt_dir, create=False
        )
    except CampaignAttemptError:
        return False
    return (
        reconciliation.get("bridge_runtime_source_digest") == runtime_digest
        and reconciliation.get("bridge_runtime_source_file_count")
        == runtime_count
    )


def _restart_evidence_fields():
    return {
        "schema_version",
        "restart_intent_sha256",
        "old_launch_exit",
        "replacement_runtime",
        "freeze_manifest_sha256",
        "freeze_source_digest",
        "freeze_generated_at",
        "initial_menu_state_seq",
        "initial_menu_decision_id",
        "state_probe",
    }


def _validate_restart_transition_created_at(
    value, *, intent, old_exit, maintenance_resolution
):
    created_at = _finite_positive_timestamp(
        value, "restart menu transition created_at"
    )
    intent_created_at = _finite_positive_timestamp(
        intent.get("created_at"), "maintenance restart intent created_at"
    )
    old_finished_at = _finite_positive_timestamp(
        old_exit.get("finished_at"), "maintenance restart old launch finished_at"
    )
    freeze_generated_at = _finite_positive_timestamp(
        maintenance_resolution.get("freeze_generated_at"),
        "maintenance restart freeze generated_at",
    )
    resolution_created_at = _finite_positive_timestamp(
        maintenance_resolution.get("created_at"),
        "maintenance restart resolution created_at",
    )
    if created_at <= intent_created_at or created_at <= old_finished_at:
        raise CampaignAttemptError(
            "restart menu transition predates the completed old-runtime exit"
        )
    if created_at < freeze_generated_at:
        raise CampaignAttemptError(
            "restart menu transition predates the replacement freeze"
        )
    if created_at < resolution_created_at:
        raise CampaignAttemptError(
            "restart menu transition predates its maintenance authorization"
        )
    return created_at


def _build_verified_restart_transition(
    root,
    result,
    binding,
    terminal_state,
    current_menu,
    maintenance_resolution,
    *,
    state_loader,
    send_payload_fn,
    process_alive_fn,
    lease_class,
    clock,
):
    current_menu = _validate_returned_menu(
        current_menu, minimum_seq=result["terminal_state_seq"] + 1
    )
    triplet = _maintenance_triplet(root, result)
    manifest = _load_restart_manifest(root, maintenance_resolution)
    intent = _load_valid_maintenance_restart_intent(
        root,
        result,
        binding,
        triplet,
        maintenance_resolution,
        manifest,
    )
    old_exit = _validate_old_launch_exit(
        root, intent, process_alive_fn=process_alive_fn
    )
    _assert_clean_launch_transport(root, require_no_selection=True)
    _probe_runtime_leases(root, lease_class)
    before = _replacement_runtime_evidence(
        root,
        current_menu,
        manifest,
        intent,
        old_exit,
        process_alive_fn=process_alive_fn,
    )
    fresh_menu, state_probe = _fresh_menu_state_round_trip(
        root, current_menu, state_loader, send_payload_fn
    )
    fresh_menu = _validate_returned_menu(
        fresh_menu,
        minimum_seq=current_menu["state_seq"] + 1,
        exact_seq=state_probe["result_state_seq"],
    )
    after = _replacement_runtime_evidence(
        root,
        fresh_menu,
        manifest,
        intent,
        old_exit,
        process_alive_fn=process_alive_fn,
    )
    if not _same_typed_tree(after, before):
        raise CampaignAttemptError(
            "maintenance restart runtime changed during STATE probe"
        )
    evidence = {
        "schema_version": 1,
        "restart_intent_sha256": _sha256_object(intent),
        "old_launch_exit": old_exit,
        "replacement_runtime": after,
        "freeze_manifest_sha256": maintenance_resolution[
            "freeze_manifest_sha256"
        ],
        "freeze_source_digest": maintenance_resolution[
            "freeze_source_digest"
        ],
        "freeze_generated_at": maintenance_resolution[
            "freeze_generated_at"
        ],
        "initial_menu_state_seq": current_menu["state_seq"],
        "initial_menu_decision_id": current_menu["decision_id"],
        "state_probe": state_probe,
    }
    if set(evidence) != _restart_evidence_fields():
        raise CampaignAttemptError(
            "maintenance restart evidence construction is incomplete"
        )
    created_at = _validate_restart_transition_created_at(
        clock(),
        intent=intent,
        old_exit=old_exit,
        maintenance_resolution=maintenance_resolution,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "menu_transition",
        "transition_kind": "game_over_to_main_menu_via_runtime_restart",
        "transition_status": "clear",
        "eligible_for_cohort": False,
        **binding,
        "terminal_state_seq": result["terminal_state_seq"],
        "terminal_decision_id": terminal_state["decision_id"],
        "terminal_phase": terminal_state["phase"],
        "menu_state_seq": fresh_menu["state_seq"],
        "menu_decision_id": fresh_menu["decision_id"],
        "authorization_kind": "maintenance_resolution",
        "maintenance_resolution_sha256": _sha256_object(
            maintenance_resolution
        ),
        "restart_evidence": evidence,
        "created_at": created_at,
    }


def _build_transition_record(
    result,
    binding,
    terminal_state,
    menu_state,
    payload,
    receipt,
    *,
    recovered,
    authorization_kind,
    maintenance_resolution,
    clock,
):
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "menu_transition",
        "transition_kind": "game_over_to_main_menu",
        "transition_status": "clear",
        **binding,
        "terminal_state_seq": result["terminal_state_seq"],
        "terminal_decision_id": terminal_state["decision_id"],
        "terminal_phase": terminal_state["phase"],
        "menu_state_seq": menu_state["state_seq"],
        "menu_decision_id": menu_state["decision_id"],
        "payload": payload,
        "receipt": receipt,
        "recovered_from_receipt": bool(recovered),
        "authorization_kind": authorization_kind,
        "maintenance_resolution_sha256": (
            _sha256_object(maintenance_resolution)
            if maintenance_resolution is not None
            else None
        ),
        "created_at": float(clock()),
    }


def _transition_record_fields():
    return {
        "schema_version",
        "record_type",
        "transition_kind",
        "transition_status",
        "terminal_state_seq",
        "terminal_decision_id",
        "terminal_phase",
        "menu_state_seq",
        "menu_decision_id",
        "payload",
        "receipt",
        "recovered_from_receipt",
        "authorization_kind",
        "maintenance_resolution_sha256",
        "created_at",
    } | set(ATTEMPT_BINDING_FIELDS)


def _restart_transition_record_fields():
    return {
        "schema_version",
        "record_type",
        "transition_kind",
        "transition_status",
        "eligible_for_cohort",
        "terminal_state_seq",
        "terminal_decision_id",
        "terminal_phase",
        "menu_state_seq",
        "menu_decision_id",
        "authorization_kind",
        "maintenance_resolution_sha256",
        "restart_evidence",
        "created_at",
    } | set(ATTEMPT_BINDING_FIELDS)


def _validate_restart_transition_record(
    record,
    result,
    binding,
    terminal_state,
    current_menu,
    *,
    root,
    maintenance_resolution,
    process_alive_fn,
):
    if (
        not isinstance(record, dict)
        or set(record) != _restart_transition_record_fields()
    ):
        raise CampaignAttemptError(
            "restart menu transition sidecar fields are invalid"
        )
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "menu_transition",
        "transition_kind": "game_over_to_main_menu_via_runtime_restart",
        "transition_status": "clear",
        "eligible_for_cohort": False,
        "terminal_state_seq": result["terminal_state_seq"],
        "terminal_decision_id": terminal_state["decision_id"],
        "terminal_phase": terminal_state["phase"],
        "authorization_kind": "maintenance_resolution",
        "maintenance_resolution_sha256": _sha256_object(
            maintenance_resolution
        ),
    }
    for field, wanted in {**binding, **envelope}.items():
        if not _same_typed_value(record.get(field), wanted):
            raise CampaignAttemptError(
                f"restart menu transition sidecar {field} mismatch"
            )
    created_at = _finite_positive_timestamp(
        record.get("created_at"), "restart menu transition created_at"
    )
    _validate_returned_menu(
        current_menu, minimum_seq=record.get("menu_state_seq")
    )
    if current_menu.get("decision_id") != record.get("menu_decision_id"):
        raise CampaignAttemptError(
            "current MAIN_MENU differs from restart transition sidecar"
        )
    evidence = record.get("restart_evidence")
    if not isinstance(evidence, dict) or set(evidence) != _restart_evidence_fields():
        raise CampaignAttemptError(
            "restart menu transition evidence fields are invalid"
        )
    triplet = _maintenance_triplet(root, result)
    manifest = _load_restart_manifest(root, maintenance_resolution)
    intent = _load_valid_maintenance_restart_intent(
        root,
        result,
        binding,
        triplet,
        maintenance_resolution,
        manifest,
    )
    old_exit = _validate_old_launch_exit(
        root, intent, process_alive_fn=process_alive_fn
    )
    _validate_restart_transition_created_at(
        created_at,
        intent=intent,
        old_exit=old_exit,
        maintenance_resolution=maintenance_resolution,
    )
    replacement = _replacement_runtime_evidence(
        root,
        current_menu,
        manifest,
        intent,
        old_exit,
        process_alive_fn=process_alive_fn,
    )
    expected_evidence = {
        "schema_version": 1,
        "restart_intent_sha256": _sha256_object(intent),
        "old_launch_exit": old_exit,
        "replacement_runtime": replacement,
        "freeze_manifest_sha256": maintenance_resolution[
            "freeze_manifest_sha256"
        ],
        "freeze_source_digest": maintenance_resolution[
            "freeze_source_digest"
        ],
        "freeze_generated_at": maintenance_resolution[
            "freeze_generated_at"
        ],
    }
    for field, wanted in expected_evidence.items():
        if not _same_typed_tree(evidence.get(field), wanted):
            raise CampaignAttemptError(
                f"restart menu transition evidence mismatch: {field}"
            )
    initial_seq = evidence.get("initial_menu_state_seq")
    initial_decision = evidence.get("initial_menu_decision_id")
    probe = evidence.get("state_probe")
    probe_fields = {
        "request_id",
        "accepted_state_seq",
        "result_state_seq",
        "result_decision_id",
        "result_phase",
        "requested_target_id",
        "resolved_target_id",
    }
    if (
        type(initial_seq) is not int
        or initial_seq <= result["terminal_state_seq"]
        or not _nonempty_string(initial_decision)
        or not isinstance(probe, dict)
        or set(probe) != probe_fields
        or not _nonempty_string(probe.get("request_id"))
        or type(probe.get("accepted_state_seq")) is not int
        or probe.get("accepted_state_seq") < initial_seq
        or probe.get("result_state_seq") != record.get("menu_state_seq")
        or probe.get("result_state_seq") <= probe.get("accepted_state_seq")
        or probe.get("result_decision_id") != record.get("menu_decision_id")
        or str(probe.get("result_phase") or "").upper() != "MAIN_MENU"
        or probe.get("requested_target_id") != "action:state"
        or probe.get("resolved_target_id") != "action:state"
    ):
        raise CampaignAttemptError(
            "restart menu transition STATE probe is invalid"
        )
    return record


def _validate_transition_record(
    record,
    result,
    binding,
    terminal_state,
    current_menu,
    *,
    root,
    authorization_kind,
    maintenance_resolution,
    process_alive_fn,
):
    if (
        isinstance(record, dict)
        and record.get("transition_kind")
        == "game_over_to_main_menu_via_runtime_restart"
    ):
        if (
            authorization_kind != "maintenance_resolution"
            or maintenance_resolution is None
        ):
            raise CampaignAttemptError(
                "restart menu transition lacks maintenance authorization"
            )
        return _validate_restart_transition_record(
            record,
            result,
            binding,
            terminal_state,
            current_menu,
            root=root,
            maintenance_resolution=maintenance_resolution,
            process_alive_fn=process_alive_fn,
        )
    if not isinstance(record, dict) or set(record) != _transition_record_fields():
        raise CampaignAttemptError("menu transition sidecar fields are invalid")
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "menu_transition",
        "transition_kind": "game_over_to_main_menu",
        "transition_status": "clear",
        "terminal_state_seq": result["terminal_state_seq"],
        "terminal_decision_id": terminal_state["decision_id"],
        "terminal_phase": terminal_state["phase"],
        "authorization_kind": authorization_kind,
        "maintenance_resolution_sha256": (
            _sha256_object(maintenance_resolution)
            if maintenance_resolution is not None
            else None
        ),
    }
    for field, wanted in {**binding, **envelope}.items():
        if not _same_typed_value(record.get(field), wanted):
            raise CampaignAttemptError(
                f"menu transition sidecar {field} mismatch"
            )
    if type(record.get("recovered_from_receipt")) is not bool:
        raise CampaignAttemptError(
            "menu transition recovery marker is invalid"
        )
    created_at = record.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        raise CampaignAttemptError("menu transition created_at is invalid")
    _validate_proceed_payload(record.get("payload"), terminal_state, binding)
    _validate_proceed_receipt(record.get("receipt"), record["payload"])
    if not _same_typed_value(
        record.get("receipt", {}).get("result_state_seq"),
        record.get("menu_state_seq"),
    ):
        raise CampaignAttemptError(
            "menu transition receipt/menu_state_seq mismatch"
        )
    _validate_returned_menu(
        current_menu,
        minimum_seq=record["menu_state_seq"],
    )
    if current_menu.get("decision_id") != record.get("menu_decision_id"):
        raise CampaignAttemptError(
            "current MAIN_MENU differs from transition sidecar"
        )
    return record


def _load_optional_receipt(receipt_loader):
    try:
        return receipt_loader()
    except FileNotFoundError:
        return None
    except CampaignAttemptError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


def _running_launch_id(root):
    root = Path(root).resolve()
    launch = _read_object(root / "launch-latest.json", "running launch")
    launch_id = launch.get("launch_id")
    start_value = launch.get("launch_start_record_path")
    if (
        not _nonempty_string(launch_id)
        or not _nonempty_string(start_value)
        or "finished_at" in launch
        or "exit_code" in launch
    ):
        raise CampaignAttemptError("running launch identity is invalid")
    start_path = (root / start_value).resolve()
    expected = (
        root / launch_game.LAUNCH_DIRECTORY
        / f"{launch_id}.start.json"
    ).resolve()
    if start_path != expected:
        raise CampaignAttemptError(
            "running launch start path is not canonical"
        )
    start = _read_object(start_path, "running immutable launch start")
    if not _same_typed_tree(start, launch):
        raise CampaignAttemptError(
            "running launch differs from immutable start evidence"
        )
    return launch_id


def _current_runtime_is_original(
    root, controller_exit, *, process_alive_fn
):
    old_launch_id = controller_exit.get("freeze_launch_id")
    if _running_launch_id(root) != old_launch_id:
        return False
    _validated_original_runtime_anchor(
        root,
        controller_exit,
        process_alive_fn=process_alive_fn,
        require_running=True,
    )
    return True


def _validate_terminal_leftover_receipt(
    receipt, terminal_state, binding
):
    if receipt is None:
        return None
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _PROCEED_RECEIPT_FIELDS
    ):
        raise CampaignAttemptError(
            "replacement runtime leftover receipt fields are invalid"
        )
    requested = receipt.get("requested_target_id")
    resolved = receipt.get("resolved_target_id")
    expected = {
        "success": True,
        "status": "succeeded",
        "error": None,
        "result_state_seq": terminal_state["state_seq"],
        **binding,
    }
    for field, wanted in expected.items():
        if not _same_typed_value(receipt.get(field), wanted):
            raise CampaignAttemptError(
                f"replacement runtime leftover receipt {field} mismatch"
            )
    accepted = receipt.get("accepted_state_seq")
    if (
        not _nonempty_string(receipt.get("request_id"))
        or not _nonempty_string(requested)
        or requested != resolved
        or requested == "action:proceed"
        or type(accepted) is not int
        or accepted >= terminal_state["state_seq"]
    ):
        raise CampaignAttemptError(
            "replacement runtime leftover receipt is not a bound terminal action"
        )
    return receipt


def _return_to_main_menu_impl(
    root,
    *,
    state_loader,
    send_payload_fn,
    receipt_loader,
    reconcile_fn,
    request_id_factory,
    lease_class,
    process_alive_fn,
    clock,
    maintenance_decision_hash,
    maintenance_controller_hash,
    p0_only_batch,
):
    root = Path(root).resolve()
    state_loader = state_loader or _default_state_loader(root)
    send_payload_fn = send_payload_fn or _default_send_payload(root)
    receipt_loader = receipt_loader or (
        lambda: _read_object(root / RECEIPT_NAME, "action receipt")
    )
    reconcile_fn = reconcile_fn or autoplay_runner._reconcile_completed_receipt
    request_id_factory = request_id_factory or (
        lambda: str(uuid.uuid4())
    )

    with lease_class(
        root / ORCHESTRATOR_LOCK_NAME, "campaign attempt orchestrator"
    ):
        result, binding = _validated_terminal_result(root)

        clean_error = None
        controller_exit = None
        preloaded_current = None
        try:
            controller_exit = reconcile_fn(root)
            if controller_exit is None:
                raise CampaignAttemptError(
                    "terminal attempt has no complete controller exit chain"
                )
            _validate_clean_controller_exit(controller_exit, result)
            authorization_kind = "clear_triple"
            maintenance_resolution = None
        except Exception as exc:
            clean_error = exc
            if (
                p0_only_batch
                and maintenance_decision_hash is None
                and maintenance_controller_hash is None
            ):
                _validate_p0_only_batch_triplet(root, result)
                authorization_kind = "p0_only_validation"
                maintenance_resolution = None
            elif (
                maintenance_decision_hash is None
                and maintenance_controller_hash is None
            ):
                # Preserve the most specific authoritative-state diagnostic
                # when both the audit chain and the visible GAME_OVER frame
                # are bad.  The strict audit gate still blocks a valid frame,
                # but a forged/mismatched terminal must not be hidden behind
                # the more general non-clear message.
                failure_state = state_loader()
                if not (
                    isinstance(failure_state, dict)
                    and failure_state.get("in_game") is False
                    and str(failure_state.get("phase") or "").upper()
                    == "MAIN_MENU"
                ):
                    _validate_terminal_state(failure_state, result, binding)
                raise CampaignAttemptError(
                    "nonclear audit blocks GAME_OVER return until an explicit "
                    "newly frozen maintenance repair is supplied"
                ) from exc
            elif (
                maintenance_decision_hash is None
                or maintenance_controller_hash is None
            ):
                raise CampaignAttemptError(
                    "maintenance requires both repaired decision and "
                    "controller hashes"
                ) from exc
            else:
                preloaded_current = state_loader()
                replacement_runtime = (
                    isinstance(preloaded_current, dict)
                    and preloaded_current.get("in_game") is False
                    and str(
                        preloaded_current.get("phase") or ""
                    ).upper() == "MAIN_MENU"
                )
                if replacement_runtime:
                    _prevalidate_maintenance_restart(
                        root,
                        result,
                        binding,
                        maintenance_decision_hash=(
                            maintenance_decision_hash
                        ),
                        maintenance_controller_hash=(
                            maintenance_controller_hash
                        ),
                        process_alive_fn=process_alive_fn,
                    )
                maintenance_resolution = (
                    _build_or_validate_maintenance_resolution(
                        root,
                        result,
                        binding,
                        maintenance_decision_hash=maintenance_decision_hash,
                        maintenance_controller_hash=maintenance_controller_hash,
                        clock=clock,
                        allow_uncommitted_recovery=replacement_runtime,
                    )
                )
                authorization_kind = "maintenance_resolution"

        # A fully reconciled Heart victory (including the runner's mandatory
        # final cohort review) is the acceptance candidate and must remain on
        # its authoritative GAME_OVER frame.  A non-clear Heart candidate is
        # different: after an explicit, newly-frozen maintenance repair it
        # must be possible to preserve the failed evidence, leave the screen,
        # and obtain a fresh qualifying victory.
        if (
            result.get("heart_defeated") is True
            and authorization_kind == "clear_triple"
        ):
            raise HeartVictoryHeld(
                "refusing to leave the authoritative clear Heart victory"
            )

        attempt_dir = autoplay_runner.attempt_directory(
            root, result["attempt_id"]
        )
        terminal_state = _validate_terminal_state(
            _read_object(
                attempt_dir / "terminal-state.json",
                "attempt terminal state",
            ),
            result,
            binding,
        )
        sidecar_path = attempt_dir / MENU_TRANSITION_NAME
        current = (
            preloaded_current
            if preloaded_current is not None
            else state_loader()
        )

        if sidecar_path.exists():
            current_menu = _validate_returned_menu(
                current, minimum_seq=result["terminal_state_seq"] + 1
            )
            sidecar = _read_object(
                sidecar_path, "menu transition sidecar"
            )
            return _validate_transition_record(
                sidecar,
                result,
                binding,
                terminal_state,
                current_menu,
                root=root,
                authorization_kind=authorization_kind,
                maintenance_resolution=maintenance_resolution,
                process_alive_fn=process_alive_fn,
            )

        # If the process stopped after the authoritative receipt but before
        # the sidecar write, recover only from that exact ten-field receipt.
        if (
            isinstance(current, dict)
            and current.get("in_game") is False
            and str(current.get("phase") or "").upper() == "MAIN_MENU"
        ):
            receipt = _load_optional_receipt(receipt_loader)
            references_proceed = (
                isinstance(receipt, dict)
                and "action:proceed"
                in {
                    receipt.get("requested_target_id"),
                    receipt.get("resolved_target_id"),
                }
            )
            runtime_anchor_exit = controller_exit
            if not isinstance(runtime_anchor_exit, dict):
                runtime_anchor_exit = _maintenance_triplet(
                    root, result
                )["controller_exit"]
            original_runtime = _current_runtime_is_original(
                root,
                runtime_anchor_exit,
                process_alive_fn=process_alive_fn,
            )
            restart_intent_path = (
                attempt_dir / MAINTENANCE_RESTART_INTENT_NAME
            )
            if not original_runtime:
                if references_proceed:
                    raise CampaignAttemptError(
                        "replacement runtime cannot recover an old PROCEED receipt"
                    )
                if (
                    authorization_kind != "maintenance_resolution"
                    or not restart_intent_path.exists()
                ):
                    raise CampaignAttemptError(
                        "replacement runtime lacks maintenance restart evidence"
                    )
                _validate_terminal_leftover_receipt(
                    receipt, terminal_state, binding
                )
                record = _build_verified_restart_transition(
                    root,
                    result,
                    binding,
                    terminal_state,
                    current,
                    maintenance_resolution,
                    state_loader=state_loader,
                    send_payload_fn=send_payload_fn,
                    process_alive_fn=process_alive_fn,
                    lease_class=lease_class,
                    clock=clock,
                )
                _write_json_once(sidecar_path, record)
                return record
            if receipt is None:
                raise CampaignAttemptError(
                    "original runtime PROCEED receipt is missing"
                )
            request_id = receipt.get("request_id") if isinstance(
                receipt, dict
            ) else None
            payload = _build_proceed_payload(
                terminal_state, binding, request_id
            )
            _validate_proceed_receipt(receipt, payload)
            menu_state = _validate_returned_menu(
                current,
                minimum_seq=receipt["result_state_seq"],
            )
            # The receipt's result frame, not a later polling frame, is the
            # transition boundary persisted by the sidecar.
            observed_menu = dict(menu_state)
            observed_menu["state_seq"] = receipt["result_state_seq"]
            record = _build_transition_record(
                result,
                binding,
                terminal_state,
                observed_menu,
                payload,
                receipt,
                recovered=True,
                authorization_kind=authorization_kind,
                maintenance_resolution=maintenance_resolution,
                clock=clock,
            )
            _write_json_once(sidecar_path, record)
            return record

        _validate_terminal_state(current, result, binding)
        _assert_clean_launch_transport(root, require_no_selection=True)
        _probe_runtime_leases(root, lease_class)
        request_id = request_id_factory()
        payload = _build_proceed_payload(
            current, binding, request_id
        )
        receipt = send_payload_fn(payload)
        _validate_proceed_receipt(receipt, payload)
        menu_state = _validate_returned_menu(
            state_loader(),
            minimum_seq=receipt["result_state_seq"],
            exact_seq=receipt["result_state_seq"],
        )
        _assert_absent(root / COMMAND_NAME, "pending command after PROCEED")
        _assert_absent(
            root / f"{COMMAND_NAME}.tmp",
            "pending command temporary after PROCEED",
        )
        _assert_absent(root / SELECTION_NAME, "pending selection after terminal")
        record = _build_transition_record(
            result,
            binding,
            current,
            menu_state,
            payload,
            receipt,
            recovered=False,
            authorization_kind=authorization_kind,
            maintenance_resolution=maintenance_resolution,
            clock=clock,
        )
        _write_json_once(sidecar_path, record)
        return record


def return_to_main_menu(
    root=ROOT,
    *,
    state_loader=None,
    send_payload_fn=None,
    receipt_loader=None,
    reconcile_fn=None,
    request_id_factory=None,
    lease_class=OSLease,
    process_alive_fn=None,
    clock=time.time,
    maintenance_decision_hash=None,
    maintenance_controller_hash=None,
    p0_only_batch=False,
):
    """Leave GAME_OVER only after a fully clear, non-Heart attempt."""

    try:
        return _return_to_main_menu_impl(
            root,
            state_loader=state_loader,
            send_payload_fn=send_payload_fn,
            receipt_loader=receipt_loader,
            reconcile_fn=reconcile_fn,
            request_id_factory=request_id_factory,
            lease_class=lease_class,
            process_alive_fn=process_alive_fn,
            clock=clock,
            maintenance_decision_hash=maintenance_decision_hash,
            maintenance_controller_hash=maintenance_controller_hash,
            p0_only_batch=p0_only_batch,
        )
    except CampaignAttemptError:
        raise
    except Exception as exc:
        raise CampaignAttemptError(
            f"return-to-main-menu failed: {exc}"
        ) from exc


def run_attempt(
    root=ROOT,
    decision_hash=None,
    controller_hash=None,
    *,
    max_actions=5000,
    state_loader=None,
    send_payload_fn=None,
    runner_fn=None,
    selector_rng=None,
    validation_batch=False,
    p0_only_batch=False,
    request_id_factory=None,
    receipt_loader=None,
    reconcile_fn=None,
    lease_class=OSLease,
    clock=time.time,
):
    """Run one attempt and return to menu when the batch permits it.

    A clear Heart victory remains held for the normal release-oriented run.
    An explicitly requested P0-only batch may continue after a structurally
    complete non-P0 audit whose protocol and mechanics dimensions are clear,
    including a Heart victory, through the guarded
    ``p0_only_validation`` transition.
    """

    launch = launch_attempt(
        root,
        decision_hash,
        controller_hash,
        max_actions=max_actions,
        state_loader=state_loader,
        send_payload_fn=send_payload_fn,
        runner_fn=runner_fn,
        selector_rng=selector_rng,
        validation_batch=validation_batch,
        p0_only_batch=p0_only_batch,
        request_id_factory=request_id_factory,
        lease_class=lease_class,
    )
    exit_record = launch["controller_exit"]
    result, _ = _validated_terminal_result(Path(root).resolve())
    if result.get("heart_defeated") is True and not p0_only_batch:
        return {
            **launch,
            "operation": "run_attempt",
            "menu_transition": None,
            "return_status": "held_heart_victory",
        }
    transition = return_to_main_menu(
        root,
        state_loader=state_loader,
        send_payload_fn=send_payload_fn,
        receipt_loader=receipt_loader,
        reconcile_fn=reconcile_fn,
        request_id_factory=request_id_factory,
        lease_class=lease_class,
        clock=clock,
        p0_only_batch=p0_only_batch,
    )
    return {
        **launch,
        "operation": "run_attempt",
        "menu_transition": transition,
        "return_status": (
            "clear"
            if exit_record.get("controller_exit_status") == "clear"
            else "cohort_nonclear"
        ),
    }


def _add_launch_arguments(parser):
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--controller-hash", required=True)
    parser.add_argument("--max-actions", type=int, default=5000)
    parser.add_argument(
        "--validation-batch",
        action="store_true",
        help="run one explicit post-cohort six-run validation batch",
    )
    parser.add_argument(
        "--p0-only-batch",
        action="store_true",
        help=(
            "continue an explicit test matrix after complete non-P0 audits "
            "with clear protocol/mechanics; P0, protocol, mechanics, and "
            "operational evidence still stop"
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one strictly bound audited campaign attempt"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    _add_launch_arguments(run_parser)
    launch_parser = subparsers.add_parser("launch")
    _add_launch_arguments(launch_parser)
    recovery_parser = subparsers.add_parser("recover-started")
    _add_launch_arguments(recovery_parser)
    unconsumed_parser = subparsers.add_parser("recover-unconsumed-start")
    unconsumed_parser.add_argument("--root", type=Path, default=ROOT)
    abandoned_parser = subparsers.add_parser(
        "recover-abandoned-accepted-start"
    )
    abandoned_parser.add_argument("--root", type=Path, default=ROOT)
    abandoned_parser.add_argument("--decision-hash", required=True)
    abandoned_parser.add_argument("--controller-hash", required=True)
    return_parser = subparsers.add_parser("return-to-main-menu")
    return_parser.add_argument("--root", type=Path, default=ROOT)
    maintenance_parser = subparsers.add_parser(
        "maintenance-return-to-main-menu"
    )
    maintenance_parser.add_argument("--root", type=Path, default=ROOT)
    maintenance_parser.add_argument("--decision-hash", required=True)
    maintenance_parser.add_argument("--controller-hash", required=True)
    restart_intent_parser = subparsers.add_parser(
        "prepare-maintenance-restart"
    )
    restart_intent_parser.add_argument("--root", type=Path, default=ROOT)
    restart_intent_parser.add_argument("--decision-hash", required=True)
    restart_intent_parser.add_argument("--controller-hash", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            value = run_attempt(
                args.root,
                args.decision_hash,
                args.controller_hash,
                max_actions=args.max_actions,
                validation_batch=args.validation_batch,
                p0_only_batch=args.p0_only_batch,
            )
        elif args.command == "launch":
            value = launch_attempt(
                args.root,
                args.decision_hash,
                args.controller_hash,
                max_actions=args.max_actions,
                validation_batch=args.validation_batch,
                p0_only_batch=args.p0_only_batch,
            )
        elif args.command == "recover-started":
            value = recover_started_attempt(
                args.root,
                args.decision_hash,
                args.controller_hash,
                max_actions=args.max_actions,
            )
        elif args.command == "recover-unconsumed-start":
            value = recover_proven_unconsumed_start(args.root)
        elif args.command == "recover-abandoned-accepted-start":
            value = recover_abandoned_accepted_start(
                args.root,
                args.decision_hash,
                args.controller_hash,
            )
        elif args.command == "return-to-main-menu":
            value = return_to_main_menu(args.root)
        elif args.command == "maintenance-return-to-main-menu":
            value = return_to_main_menu(
                args.root,
                maintenance_decision_hash=args.decision_hash,
                maintenance_controller_hash=args.controller_hash,
            )
        else:
            value = prepare_maintenance_restart(
                args.root,
                maintenance_decision_hash=args.decision_hash,
                maintenance_controller_hash=args.controller_hash,
            )
    except CampaignAttemptError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    if (
        args.command == "run"
        and value.get("return_status") == "blocked_invalid_controller_exit"
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
