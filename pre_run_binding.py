"""Strict selector binding for MAIN_MENU START and audited RESUME commands."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

import campaign_selector
import cohort_report
import cohort_review
import freeze_manifest


POLICY_VERSION = "fast-policy-v5"
PROTOCOL_VERSION = 2
PRE_RUN_BINDING_VERSION = 1
START_ACCEPTANCE_RECORD_TYPE = "start_acceptance"
START_ACCEPTANCE_ROOT = Path("logs") / "start-acceptances"
_START_ACCEPTANCE_FIELDS = {
    "schema_version", "record_type", "policy_version",
    "pre_run_binding_version", "request_id", "accepted_state_seq",
    "decision_id", "phase", "selection_id", "selection_digest",
    "decision_hash", "controller_hash", "character",
    "ascension_level", "run_type", "target_id", "selection",
    "start_payload", "accepted_receipt", "created_at",
}
_BRIDGE_INSTANCE_FIELDS = {
    "schema_version",
    "protocol_version",
    "instance_token",
    "bridge_pid",
    "parent_java_pid",
    "launch_id",
    "bridge_sha256",
    "runtime_source_digest",
    "runtime_source_file_count",
    "started_at",
}


class PreRunBindingError(ValueError):
    pass


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _same_typed_tree(actual, expected):
    """Compare a JSON tree without allowing bool/int type aliases."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _same_typed_tree(actual[key], expected[key])
            for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_typed_tree(left, right)
            for left, right in zip(actual, expected)
        )
    return actual == expected


def selection_digest(selection):
    try:
        return campaign_selector.selection_digest(selection)
    except ValueError as exc:
        raise PreRunBindingError(str(exc)) from exc


def _object_sha256(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def start_acceptance_path(root, selection_id):
    if not isinstance(selection_id, str) or not selection_id.strip():
        raise PreRunBindingError("selection_id is missing")
    component = hashlib.sha256(
        selection_id.encode("utf-8")
    ).hexdigest()[:32]
    return Path(root).resolve() / START_ACCEPTANCE_ROOT / component / (
        "start-acceptance.json"
    )


def start_acceptance_sha256(record):
    if not isinstance(record, dict):
        raise PreRunBindingError("START acceptance is not an object")
    return _object_sha256(record)


def _write_json_once_idempotent(path, payload):
    """Atomically install one immutable record, accepting only exact replay."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _load_object(path, "START acceptance")
        if existing != payload:
            raise PreRunBindingError(
                "START acceptance already exists with different evidence"
            )
        return existing
    encoded = _canonical(payload).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _load_object(path, "START acceptance")
            if existing != payload:
                raise PreRunBindingError(
                    "START acceptance raced with different evidence"
                )
            return existing
        return payload
    except OSError as exc:
        raise PreRunBindingError(
            "START acceptance could not be persisted atomically"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_start_acceptance_record(
    payload, state, selection, *, created_at=None
):
    if not isinstance(payload, dict):
        raise PreRunBindingError("START payload is not an object")
    sequence, decision_id, phase = _state_binding(state)
    digest = selection_digest(selection)
    if payload.get("selection_digest") != digest:
        raise PreRunBindingError("START selection digest binding mismatch")
    accepted_receipt = {
        "request_id": payload.get("id"),
        "success": None,
        "status": "accepted",
        "error": None,
        "accepted_state_seq": sequence,
        "result_state_seq": None,
        "requested_target_id": payload.get("target_id"),
        "resolved_target_id": payload.get("target_id"),
        "selection_digest": digest,
    }
    return {
        "schema_version": 2,
        "record_type": START_ACCEPTANCE_RECORD_TYPE,
        "policy_version": POLICY_VERSION,
        "pre_run_binding_version": PRE_RUN_BINDING_VERSION,
        "request_id": payload.get("id"),
        "accepted_state_seq": sequence,
        "decision_id": decision_id,
        "phase": phase,
        "selection_id": selection.get("selection_id"),
        "selection_digest": digest,
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
        "character": selection.get("character"),
        "ascension_level": selection.get("ascension_level"),
        "run_type": selection.get("run_type"),
        "target_id": payload.get("target_id"),
        "selection": selection,
        "start_payload": payload,
        "accepted_receipt": accepted_receipt,
        "created_at": time.time() if created_at is None else created_at,
    }


def validate_start_acceptance(record, selection, *, payload=None):
    if not isinstance(record, dict) or set(record) != _START_ACCEPTANCE_FIELDS:
        raise PreRunBindingError(
            "START acceptance fields are incomplete or unclassified"
        )
    digest = selection_digest(selection)
    expected = {
        "schema_version": 2,
        "record_type": START_ACCEPTANCE_RECORD_TYPE,
        "policy_version": POLICY_VERSION,
        "pre_run_binding_version": PRE_RUN_BINDING_VERSION,
        "selection_id": selection.get("selection_id"),
        "selection_digest": digest,
        "decision_hash": selection.get("decision_hash"),
        "controller_hash": selection.get("controller_hash"),
        "character": selection.get("character"),
        "ascension_level": selection.get("ascension_level"),
        "run_type": selection.get("run_type"),
        "selection": selection,
    }
    for field, wanted in expected.items():
        observed = record.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise PreRunBindingError(
                f"START acceptance {field} mismatch"
            )
    created_at = record.get("created_at")
    if (
        type(created_at) not in {int, float}
        or not math.isfinite(float(created_at))
        or created_at <= 0
    ):
        raise PreRunBindingError("START acceptance created_at is invalid")
    if type(record.get("accepted_state_seq")) is not int:
        raise PreRunBindingError(
            "START acceptance accepted_state_seq is invalid"
        )
    for field in ("request_id", "decision_id", "phase", "target_id"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise PreRunBindingError(f"START acceptance {field} is missing")
    stored_payload = record.get("start_payload")
    if not isinstance(stored_payload, dict):
        raise PreRunBindingError("START acceptance payload is missing")
    if payload is not None and stored_payload != payload:
        raise PreRunBindingError("START acceptance payload mismatch")
    if (
        stored_payload.get("selection_digest") != digest
        or stored_payload.get("selection_id") != selection.get("selection_id")
        or stored_payload.get("id") != record.get("request_id")
        or stored_payload.get("expected_seq")
        != record.get("accepted_state_seq")
        or stored_payload.get("decision_id") != record.get("decision_id")
        or stored_payload.get("phase") != record.get("phase")
        or stored_payload.get("target_id") != record.get("target_id")
    ):
        raise PreRunBindingError(
            "START acceptance payload binding is inconsistent"
        )
    expected_receipt = {
        "request_id": record.get("request_id"),
        "success": None,
        "status": "accepted",
        "error": None,
        "accepted_state_seq": record.get("accepted_state_seq"),
        "result_state_seq": None,
        "requested_target_id": record.get("target_id"),
        "resolved_target_id": record.get("target_id"),
        "selection_digest": digest,
    }
    if record.get("accepted_receipt") != expected_receipt:
        raise PreRunBindingError(
            "START acceptance receipt binding is inconsistent"
        )
    return record


def load_start_acceptance(root, selection, *, payload=None):
    path = start_acceptance_path(root, selection.get("selection_id"))
    record = _load_object(path, "START acceptance")
    return validate_start_acceptance(record, selection, payload=payload)


def _load_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreRunBindingError(f"{label} is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PreRunBindingError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise PreRunBindingError(f"{label} is not an object")
    return value


def _valid_sha256(value):
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise PreRunBindingError(
            f"runtime provenance artifact is unreadable: {path}"
        ) from exc
    return digest.hexdigest()


def process_is_alive(pid):
    """Read-only liveness probe used by the START acceptance gate."""

    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as exc:
            return getattr(exc, "winerror", None) == 5
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError):
        return False


def validate_runtime_provenance(
    state,
    root,
    manifest,
    *,
    launch_path=None,
    bridge_instance_path=None,
    process_alive_fn=None,
    current_pid_fn=None,
    current_parent_pid_fn=None,
):
    """Prove START is reaching the exact frozen live Java/bridge pair."""

    _state_binding(state)
    root = Path(root).resolve()
    launch_path = Path(launch_path or root / "launch-latest.json")
    bridge_instance_path = Path(
        bridge_instance_path or root / "bridge-instance.json"
    )
    if launch_path.resolve() != (root / "launch-latest.json").resolve():
        raise PreRunBindingError("launch record path escaped repository root")
    if bridge_instance_path.resolve() != (
        root / "bridge-instance.json"
    ).resolve():
        raise PreRunBindingError(
            "bridge instance path escaped repository root"
        )
    alive = process_alive_fn or process_is_alive
    current_pid = (current_pid_fn or os.getpid)()
    current_parent_pid = (current_parent_pid_fn or os.getppid)()
    launch = _load_object(launch_path, "launch record")
    # capture_launch_evidence independently rehashes both JARs, validates the
    # immutable launch path and proves no bridge stderr bytes were appended
    # after this launch's recorded prefix.
    try:
        current_launch = freeze_manifest.capture_launch_evidence(root)
    except freeze_manifest.FreezeManifestError as exc:
        raise PreRunBindingError(str(exc)) from exc
    frozen_launch = manifest.get("launch_evidence")
    if (
        not isinstance(frozen_launch, dict)
        or frozen_launch != current_launch
    ):
        raise PreRunBindingError(
            "running launch differs from the frozen launch evidence"
        )
    if "finished_at" in launch or "exit_code" in launch:
        raise PreRunBindingError("launcher already finished")
    if (
        launch.get("launch_id") != frozen_launch.get("launch_id")
        or launch.get("launcher_pid")
        != frozen_launch.get("launcher_pid")
        or launch.get("java_pid") != frozen_launch.get("java_pid")
    ):
        raise PreRunBindingError("launch identity differs from freeze")

    bridge_instance = _load_object(
        bridge_instance_path, "bridge instance"
    )
    if set(bridge_instance) != _BRIDGE_INSTANCE_FIELDS:
        raise PreRunBindingError(
            "bridge instance schema-v2 envelope is incomplete"
        )
    expected = {
        "schema_version": 2,
        "protocol_version": PROTOCOL_VERSION,
        "parent_java_pid": launch.get("java_pid"),
        "launch_id": launch.get("launch_id"),
    }
    for field, wanted in expected.items():
        observed = bridge_instance.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise PreRunBindingError(
                f"bridge instance {field} mismatch"
            )
    for field in ("instance_token",):
        if (
            not isinstance(bridge_instance.get(field), str)
            or not bridge_instance[field].strip()
        ):
            raise PreRunBindingError(f"bridge instance {field} is missing")
    bridge_pid = bridge_instance.get("bridge_pid")
    launcher_pid = launch.get("launcher_pid")
    java_pid = launch.get("java_pid")
    if (
        type(bridge_pid) is not int
        or bridge_pid <= 0
        or type(launcher_pid) is not int
        or launcher_pid <= 0
        or launcher_pid in {bridge_pid, java_pid}
        or bridge_pid == java_pid
        or bridge_pid != current_pid
        or java_pid != current_parent_pid
    ):
        raise PreRunBindingError("live bridge/Java parent process chain is invalid")
    started_at = bridge_instance.get("started_at")
    if (
        type(started_at) not in {int, float}
        or not math.isfinite(float(started_at))
        or float(started_at) < float(launch.get("started_at", 0))
    ):
        raise PreRunBindingError(
            "bridge instance start time precedes its Java launch"
        )
    try:
        launcher_alive = alive(launcher_pid)
        java_alive = alive(java_pid)
        bridge_alive = alive(bridge_pid)
    except Exception as exc:
        raise PreRunBindingError("runtime process probe failed") from exc
    if (
        launcher_alive is not True
        or java_alive is not True
        or bridge_alive is not True
    ):
        raise PreRunBindingError(
            "launcher, Java, or bridge process is not alive"
        )

    sources = manifest.get("sources")
    bridge_source = sources.get("bridge.py") if isinstance(sources, dict) else None
    jar_source = (
        sources.get("CommunicationMod.jar")
        if isinstance(sources, dict) else None
    )
    bridge_hash = bridge_instance.get("bridge_sha256")
    runtime_source_digest = bridge_instance.get("runtime_source_digest")
    runtime_source_file_count = bridge_instance.get(
        "runtime_source_file_count"
    )
    if (
        not _valid_sha256(bridge_hash)
        or not isinstance(bridge_source, dict)
        or bridge_source.get("sha256") != bridge_hash
        or _file_sha256(root / "bridge.py") != bridge_hash
        or not _valid_sha256(runtime_source_digest)
        or runtime_source_digest != manifest.get("source_digest")
        or type(runtime_source_file_count) is not int
        or runtime_source_file_count != manifest.get("source_file_count")
    ):
        raise PreRunBindingError(
            "live bridge runtime sources differ from freeze"
        )
    jar_hash = frozen_launch.get(
        "root_communication_mod_jar_sha256"
    )
    if (
        not _valid_sha256(jar_hash)
        or not isinstance(jar_source, dict)
        or jar_source.get("sha256") != jar_hash
        or frozen_launch.get(
            "installed_communication_mod_jar_sha256"
        ) != jar_hash
    ):
        raise PreRunBindingError(
            "CommunicationMod.jar differs from freeze"
        )
    return {
        "launch": launch,
        "launch_evidence": current_launch,
        "bridge_instance": bridge_instance,
    }


def validate_pre_dispatch_runtime(
    state,
    root,
    manifest,
    *,
    process_alive_fn=None,
):
    """Prove the frozen launcher/Java/bridge chain is alive before START.

    The bridge repeats the stronger parent/current-process proof when it
    consumes START.  This controller-side check closes the dead-bridge gap:
    a stale MAIN_MENU file must never be enough to place a command on disk.
    """

    _state_binding(state)
    root = Path(root).resolve()
    try:
        current_launch = freeze_manifest.capture_launch_evidence(root)
    except freeze_manifest.FreezeManifestError as exc:
        raise PreRunBindingError(str(exc)) from exc
    frozen_launch = manifest.get("launch_evidence")
    if not isinstance(frozen_launch, dict) or current_launch != frozen_launch:
        raise PreRunBindingError(
            "pre-dispatch launch differs from frozen runtime evidence"
        )
    launch = _load_object(root / "launch-latest.json", "launch record")
    bridge_instance = _load_object(
        root / "bridge-instance.json", "bridge instance"
    )
    if set(bridge_instance) != _BRIDGE_INSTANCE_FIELDS:
        raise PreRunBindingError(
            "pre-dispatch bridge instance envelope is incomplete"
        )
    launcher_pid = launch.get("launcher_pid")
    java_pid = launch.get("java_pid")
    bridge_pid = bridge_instance.get("bridge_pid")
    if (
        type(launcher_pid) is not int
        or type(java_pid) is not int
        or type(bridge_pid) is not int
        or min(launcher_pid, java_pid, bridge_pid) <= 0
        or len({launcher_pid, java_pid, bridge_pid}) != 3
        or bridge_instance.get("parent_java_pid") != java_pid
        or bridge_instance.get("launch_id") != launch.get("launch_id")
    ):
        raise PreRunBindingError(
            "pre-dispatch launcher/Java/bridge identity is invalid"
        )
    alive = process_alive_fn or process_is_alive
    try:
        statuses = {
            "launcher": alive(launcher_pid),
            "java": alive(java_pid),
            "bridge": alive(bridge_pid),
        }
    except Exception as exc:
        raise PreRunBindingError(
            "pre-dispatch runtime process probe failed"
        ) from exc
    dead = sorted(name for name, status in statuses.items() if status is not True)
    if dead:
        raise PreRunBindingError(
            "pre-dispatch runtime process is not alive: " + ",".join(dead)
        )
    return {
        "launch_evidence": current_launch,
        "launch_id": launch.get("launch_id"),
        "launcher_pid": launcher_pid,
        "java_pid": java_pid,
        "bridge_pid": bridge_pid,
        "bridge_instance_token": bridge_instance.get("instance_token"),
        "bridge_sha256": bridge_instance.get("bridge_sha256"),
        "bridge_runtime_source_digest": bridge_instance.get(
            "runtime_source_digest"
        ),
        "bridge_runtime_source_file_count": bridge_instance.get(
            "runtime_source_file_count"
        ),
    }


def _state_binding(state):
    if not isinstance(state, dict):
        raise PreRunBindingError("state must be an object")
    if state.get("protocol_version") != PROTOCOL_VERSION:
        raise PreRunBindingError("MAIN_MENU protocol_version must be 2")
    if state.get("in_game") is not False:
        raise PreRunBindingError("START requires an authoritative MAIN_MENU")
    if str(state.get("phase") or "").upper() != "MAIN_MENU":
        raise PreRunBindingError("START phase is not MAIN_MENU")
    if state.get("ready_for_command") is not True:
        raise PreRunBindingError("MAIN_MENU is not ready_for_command")
    sequence = state.get("state_seq")
    decision_id = state.get("decision_id")
    if type(sequence) is not int or sequence < 0:
        raise PreRunBindingError("MAIN_MENU state_seq is invalid")
    if not isinstance(decision_id, str) or not decision_id:
        raise PreRunBindingError("MAIN_MENU decision_id is invalid")
    return sequence, decision_id, str(state.get("phase"))


def _eligible_ids(history, decision_hash, controller_hash):
    attempts = campaign_selector.eligible_attempts(
        history, decision_hash, controller_hash=controller_hash
    )
    return [item["attempt_id"] for item in attempts]


def validate_pending_selection(
    selection,
    history,
    decision_hash,
    controller_hash,
):
    try:
        campaign_selector.require_resolved_attempt_history(
            history,
            decision_hash,
            controller_hash,
            p0_only_batch=bool(selection.get("p0_only_batch", False)),
        )
    except campaign_selector.HistoryValidationError as exc:
        raise PreRunBindingError(str(exc)) from exc
    eligible_ids = _eligible_ids(history, decision_hash, controller_hash)
    try:
        campaign_selector.validate_selection(
            selection,
            decision_hash,
            controller_hash,
            eligible_attempt_ids=eligible_ids,
        )
    except ValueError as exc:
        raise PreRunBindingError(str(exc)) from exc
    selection_id = selection["selection_id"]
    reused_by = sorted({
        record.get("attempt_id")
        for record in history
        if isinstance(record, dict)
        and record.get("selection_id") == selection_id
        and record.get("attempt_id")
    })
    if reused_by:
        raise PreRunBindingError(
            "selection_id was already consumed by attempt(s): "
            + ",".join(reused_by)
        )
    return selection


def validate_release_checkpoint(
    selection,
    history,
    root,
    *,
    manifest_path,
    cohort_review_path,
):
    """Prove hash freeze and mandatory cohort review before START."""

    root = Path(root)
    try:
        manifest = freeze_manifest.load_validated_manifest(
            manifest_path,
            root,
            selection.get("decision_hash"),
            selection.get("controller_hash"),
        )
    except freeze_manifest.FreezeManifestError as exc:
        raise PreRunBindingError(str(exc)) from exc
    cohort = cohort_report.build_cohort_report(
        history, selection["decision_hash"]
    )
    requirement = cohort_review.review_requirement(cohort)
    if requirement is not None:
        review = _load_object(cohort_review_path, "cohort review")
        if not cohort_review.validate_review_gate(
            review, cohort, root / "logs" / "attempts"
        ):
            raise PreRunBindingError(
                f"mandatory {requirement} review gate is not clear"
            )
    return {"manifest": manifest, "cohort": cohort, "review": requirement}


def validate_accepted_start_checkpoint(root, *, expected_selection=None):
    """Reconsume every mutable START prerequisite immediately before child.

    START acceptance proves the bridge checked one exact snapshot, but the
    controller is created later by a different process. Re-read the pending
    selection and append-only history, revalidate the complete freeze
    (including launch and bridge evidence), and reload the immutable
    acceptance sidecar. Campaign orchestration and the runner both call this.
    """

    root = Path(root).resolve()
    selection = _load_object(
        root / "next-run-selection.json", "pending accepted selection"
    )
    if (
        expected_selection is not None
        and not _same_typed_tree(selection, expected_selection)
    ):
        raise PreRunBindingError(
            "pending accepted selection changed before controller spawn"
        )
    try:
        history = campaign_selector.load_history(root / "run-history.jsonl")
    except campaign_selector.HistoryValidationError as exc:
        raise PreRunBindingError(str(exc)) from exc
    decision_hash = selection.get("decision_hash")
    controller_hash = selection.get("controller_hash")
    validate_pending_selection(
        selection, history, decision_hash, controller_hash
    )
    checkpoint = validate_release_checkpoint(
        selection,
        history,
        root,
        manifest_path=root / "freeze-manifest.json",
        cohort_review_path=root / "cohort-review.json",
    )
    acceptance = load_start_acceptance(root, selection)
    return {
        "selection": selection,
        "selection_digest": selection_digest(selection),
        "history_record_count": len(history),
        "acceptance": acceptance,
        "acceptance_sha256": start_acceptance_sha256(acceptance),
        "checkpoint": checkpoint,
    }


def build_start_payload(
    state,
    selection,
    history,
    decision_hash,
    controller_hash,
    *,
    request_id,
):
    sequence, decision_id, phase = _state_binding(state)
    selection = validate_pending_selection(
        selection, history, decision_hash, controller_hash
    )
    character = selection["character"]
    target_id = f"run:{character}:a0:standard"
    return {
        "id": str(request_id),
        "action": "start",
        "policy_version": POLICY_VERSION,
        "expected_seq": sequence,
        "decision_id": decision_id,
        "phase": phase,
        "pre_run_binding_version": PRE_RUN_BINDING_VERSION,
        "selection_id": selection["selection_id"],
        "selection_digest": selection_digest(selection),
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "goal_mode": "HEART",
        "player_class": character,
        "character": character,
        "ascension_level": 0,
        "run_type": "standard",
        "target_id": target_id,
        "eligible_attempt_ids": list(selection["eligible_attempt_ids"]),
    }


def validate_start_payload(
    payload,
    state,
    selection,
    history,
    decision_hash,
    controller_hash,
):
    expected = build_start_payload(
        state,
        selection,
        history,
        decision_hash,
        controller_hash,
        request_id=payload.get("id") if isinstance(payload, dict) else None,
    )
    if not isinstance(payload, dict):
        raise PreRunBindingError("START payload is not an object")
    for field, wanted in expected.items():
        observed = payload.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise PreRunBindingError(f"START {field} binding mismatch")
    extras = set(payload) - set(expected)
    if extras:
        raise PreRunBindingError(
            "START payload contains unclassified fields: "
            + ",".join(sorted(extras))
        )
    return payload


def load_and_validate_start_payload(
    payload,
    state,
    *,
    selection_path,
    history_path,
    manifest_path=None,
    cohort_review_path=None,
    launch_path=None,
    bridge_instance_path=None,
    process_alive_fn=None,
    current_pid_fn=None,
    current_parent_pid_fn=None,
):
    selection = _load_object(selection_path, "pending selection")
    try:
        history = campaign_selector.load_history(history_path)
    except campaign_selector.HistoryValidationError as exc:
        raise PreRunBindingError(str(exc)) from exc
    validated = validate_start_payload(
        payload,
        state,
        selection,
        history,
        selection.get("decision_hash"),
        selection.get("controller_hash"),
    )
    root = Path(selection_path).resolve().parent
    checkpoint = validate_release_checkpoint(
        selection,
        history,
        root,
        manifest_path=(
            root / "freeze-manifest.json"
            if manifest_path is None else manifest_path
        ),
        cohort_review_path=(
            root / "cohort-review.json"
            if cohort_review_path is None else cohort_review_path
        ),
    )
    validate_runtime_provenance(
        state,
        root,
        checkpoint["manifest"],
        launch_path=launch_path,
        bridge_instance_path=bridge_instance_path,
        process_alive_fn=process_alive_fn,
        current_pid_fn=current_pid_fn,
        current_parent_pid_fn=current_parent_pid_fn,
    )
    return validated


def accept_start_payload(
    payload,
    state,
    *,
    selection_path,
    history_path,
    manifest_path=None,
    cohort_review_path=None,
    launch_path=None,
    bridge_instance_path=None,
    process_alive_fn=None,
    current_pid_fn=None,
    current_parent_pid_fn=None,
    created_at=None,
):
    """Persist the immutable evidence for the exact START acceptance.

    The complete acceptance gate is deliberately re-run here immediately
    before the bridge emits START.  This closes the race between command
    resolution and acceptance and makes recovery consume the same evidence.
    """

    load_and_validate_start_payload(
        payload,
        state,
        selection_path=selection_path,
        history_path=history_path,
        manifest_path=manifest_path,
        cohort_review_path=cohort_review_path,
        launch_path=launch_path,
        bridge_instance_path=bridge_instance_path,
        process_alive_fn=process_alive_fn,
        current_pid_fn=current_pid_fn,
        current_parent_pid_fn=current_parent_pid_fn,
    )
    selection = _load_object(selection_path, "pending selection")
    record = build_start_acceptance_record(
        payload, state, selection, created_at=created_at
    )
    root = Path(selection_path).resolve().parent
    path = start_acceptance_path(root, selection.get("selection_id"))
    persisted = _write_json_once_idempotent(path, record)
    validate_start_acceptance(persisted, selection, payload=payload)
    return {
        "path": path,
        "record": persisted,
        "sha256": start_acceptance_sha256(persisted),
    }


def build_resume_payload(state, context, *, request_id):
    sequence, decision_id, phase = _state_binding(state)
    if not isinstance(context, dict) or context.get("schema_version") != 2:
        raise PreRunBindingError("RESUME requires a schema-v2 run context")
    if context.get("terminal_state_seq") is not None:
        raise PreRunBindingError("a terminal attempt must never be resumed")
    required = {
        "policy_version": POLICY_VERSION,
        "goal_mode": "HEART",
        "ascension_level": 0,
        "run_type": "standard",
    }
    for field, wanted in required.items():
        if context.get(field) != wanted:
            raise PreRunBindingError(f"RESUME context {field} mismatch")
    for field in (
        "attempt_id", "run_id", "decision_hash", "controller_hash",
        "selection_id", "character",
    ):
        if not isinstance(context.get(field), str) or not context[field]:
            raise PreRunBindingError(f"RESUME context {field} is missing")
    character = context["character"]
    target_id = f"resume:{character}:autosave"
    return {
        "id": str(request_id),
        "action": "resume",
        "policy_version": POLICY_VERSION,
        "expected_seq": sequence,
        "decision_id": decision_id,
        "phase": phase,
        "pre_run_binding_version": PRE_RUN_BINDING_VERSION,
        "attempt_id": context["attempt_id"],
        "run_id": context["run_id"],
        "seed": context.get("seed"),
        "decision_hash": context["decision_hash"],
        "controller_hash": context["controller_hash"],
        "selection_id": context["selection_id"],
        "goal_mode": "HEART",
        "player_class": character,
        "character": character,
        "ascension_level": 0,
        "run_type": "standard",
        "target_id": target_id,
    }


def validate_resume_payload(payload, state, context):
    expected = build_resume_payload(
        state,
        context,
        request_id=payload.get("id") if isinstance(payload, dict) else None,
    )
    if not isinstance(payload, dict):
        raise PreRunBindingError("RESUME payload is not an object")
    for field, wanted in expected.items():
        observed = payload.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise PreRunBindingError(f"RESUME {field} binding mismatch")
    return payload


def load_and_validate_resume_payload(
    payload,
    state,
    *,
    context_path,
    manifest_path=None,
):
    context = _load_object(context_path, "run context")
    root = Path(context_path).resolve().parent
    try:
        freeze_manifest.load_validated_manifest(
            root / "freeze-manifest.json"
            if manifest_path is None else manifest_path,
            root,
            context.get("decision_hash"),
            context.get("controller_hash"),
        )
    except freeze_manifest.FreezeManifestError as exc:
        raise PreRunBindingError(str(exc)) from exc
    return validate_resume_payload(payload, state, context)
