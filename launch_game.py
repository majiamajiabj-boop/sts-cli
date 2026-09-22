import json
import hashlib
import math
import os
import subprocess
import time
import uuid
import sys
from pathlib import Path

import freeze_manifest
from assistant_paths import installation, java_property


ROOT = Path(__file__).resolve().parent
_INSTALLATION = installation()
GAME_DIR = _INSTALLATION.game
JAVA = GAME_DIR / "jre" / "bin" / "java.exe"
MOD_THE_SPIRE = _INSTALLATION.mts
BASE_MOD = _INSTALLATION.basemod
COMMUNICATION_MOD = GAME_DIR / "mods" / "CommunicationMod.jar"
ROOT_COMMUNICATION_MOD = ROOT / "CommunicationMod.jar"
BRIDGE_STDERR = GAME_DIR / "communication_mod_errors.log"
LATEST_LAUNCH_PATH = ROOT / "launch-latest.json"
SCHEMA_VERSION = 2
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
LAUNCH_DIRECTORY = Path("logs") / "launches"
ACTIVE_LEASE_DIRECTORY = ".active-launch"
MAINTENANCE_RESTART_HANDOFF_SUFFIX = ".maintenance-restart-handoff.json"
RESTART_PREDECESSOR_SUFFIX = ".restart-predecessor.json"
INTERRUPTED_EXIT_RECOVERY_SUFFIX = ".exit-recovery.json"
# This value is evidence that the supervisor did not observe Java's native
# exit code.  It is never presented as the Java exit code: the immutable
# recovery sidecar proves why this reserved value was used.
INTERRUPTED_EXIT_CODE = -2147483648
# A distinct reserved value records a supervisor that proved Java failed during
# ModTheSpire bootstrap before a protocol bridge could become usable.  It is
# never treated as a native Java exit code; the immutable recovery sidecar
# below proves the exact startup failure and dead bound processes.
FAILED_STARTUP_EXIT_CODE = -2147483647
# A third reserved value records an intentionally interrupted runtime that had
# already proved bridge readiness.  It is kept distinct from the maintenance
# sentinel because this recovery does not depend on a prepared handoff.
INTERRUPTED_RUNTIME_EXIT_CODE = -2147483646
INTERRUPTED_EXIT_RECOVERY_ENV = "STS_RECOVER_INTERRUPTED_MAINTENANCE_EXIT"
TERMINAL_RECORD_TYPE = "terminal_result"
AUDIT_RECORD_TYPE = "run_audit"
CONTROLLER_EXIT_RECORD_TYPE = "controller_exit"

_BASE_LAUNCH_START_FIELDS = {
    "schema_version",
    "launch_id",
    "started_at",
    "launch_log_path",
    "launch_start_record_path",
    "launcher_pid",
    "java_pid",
    "command",
    "game_dir",
    "root_communication_mod_jar_sha256",
    "installed_communication_mod_jar_sha256",
    "bridge_stderr_path",
    "bridge_stderr_start_offset",
    "bridge_stderr_start_sha256",
}
_LAUNCH_START_FIELDS = _BASE_LAUNCH_START_FIELDS | {
    "restart_predecessor_record_path",
    "restart_predecessor_record_sha256",
}
_INTERRUPTED_EXIT_RECOVERY_FIELDS = {
    "schema_version",
    "record_type",
    "recovery_status",
    "launch_id",
    "launch_start_record_path",
    "launch_start_record_sha256",
    "maintenance_handoff_record_path",
    "maintenance_handoff_record_sha256",
    "maintenance_intent_record_path",
    "maintenance_intent_record_sha256",
    "launcher_pid",
    "java_pid",
    "bridge_pid",
    "launcher_alive",
    "java_alive",
    "bridge_alive",
    "exit_code_sentinel",
    "recovered_at",
}
_FAILED_STARTUP_EXIT_RECOVERY_FIELDS = {
    "schema_version",
    "record_type",
    "recovery_status",
    "launch_id",
    "launch_start_record_path",
    "launch_start_record_sha256",
    "launch_log_path",
    "launch_log_sha256",
    "failure_marker",
    "launcher_pid",
    "java_pid",
    "launcher_alive",
    "java_alive",
    "exit_code_sentinel",
    "recovered_at",
}
_INTERRUPTED_RUNTIME_EXIT_RECOVERY_FIELDS = {
    "schema_version",
    "record_type",
    "recovery_status",
    "launch_id",
    "launch_start_record_path",
    "launch_start_record_sha256",
    "launch_log_path",
    "launch_log_sha256",
    "bridge_instance_sha256",
    "bridge_sha256",
    "launcher_pid",
    "java_pid",
    "bridge_pid",
    "parent_java_pid",
    "launcher_alive",
    "java_alive",
    "bridge_alive",
    "exit_code_sentinel",
    "recovered_at",
}
_STARTUP_FAILURE_MARKERS = (
    ("mod_the_spire_zip_error", b"java.util.zip.ZipException:"),
    ("java_main_class_error", b"Error: Could not find or load main class"),
    ("java_main_thread_error", b"Exception in thread \"main\""),
    ("java_no_class_def_found", b"NoClassDefFoundError:"),
    ("java_class_not_found", b"ClassNotFoundException:"),
)

JAVA_LOCALAPPDATA_DIRECTORY = ".java-localappdata"
CACHED_PYTHON = Path(sys.executable)
BRIDGE_RUNTIME_PROBE_TIMEOUT_SECONDS = 3.0

class LaunchError(RuntimeError):
    """Raised before launch when runtime provenance cannot be proved."""


def _java_property_value(path):
    """Encode a Windows path for java.util.Properties."""

    return java_property(Path(path).resolve())


def _bridge_config_paths(localappdata):
    localappdata = Path(localappdata).resolve()
    return (
        localappdata / "ModTheSpire" / "CommunicationMod" / "config.properties",
        localappdata / "CommunicationMod" / "config.properties",
    )


def _write_bridge_config(path, *, python_path, bridge_path):
    """Write an idempotent CommunicationMod command configuration."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = f"{_java_property_value(python_path)} {_java_property_value(bridge_path)}"
    desired = {
        "command": command,
        "commandJson": java_property(json.dumps([
            Path(python_path).resolve().as_posix(),
            Path(bridge_path).resolve().as_posix(),
        ], ensure_ascii=False)),
        "maxInitializationTimeout": "10",
        "runAtGameStart": "true",
        "verbose": "false",
    }
    existing = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith(("#", "!"))
                or "=" not in stripped
            ):
                continue
            key, value = stripped.split("=", 1)
            existing[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LaunchError(f"CommunicationMod config is unreadable: {path}") from exc
    existing.update(desired)
    lines = [f"{key}={existing[key]}" for key in sorted(existing)]
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise LaunchError(f"CommunicationMod config is not writable: {path}") from exc


def _probe_python(
    path, *, runner=subprocess.run,
    timeout=BRIDGE_RUNTIME_PROBE_TIMEOUT_SECONDS,
):
    """Prove the exact interpreter can be executed by this launch context."""

    path = Path(path).resolve()
    if not path.is_file():
        raise LaunchError(f"bridge Python runtime is missing: {path}")
    try:
        result = runner(
            [str(path), "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except PermissionError as exc:
        raise LaunchError(
            f"bridge Python runtime cannot be executed (access denied): {path}"
        ) from exc
    except FileNotFoundError as exc:
        raise LaunchError(f"bridge Python runtime disappeared: {path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchError(f"bridge Python runtime probe timed out: {path}") from exc
    except OSError as exc:
        raise LaunchError(f"bridge Python runtime probe failed: {path}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().replace("\n", " ")
        suffix = f": {detail[:240]}" if detail else ""
        raise LaunchError(
            f"bridge Python runtime exited with {result.returncode}: {path}{suffix}"
        )
    return path


def _select_bridge_runtime(root, *, probe_fn=_probe_python):
    """Choose a runnable interpreter before Java/CommunicationMod starts."""

    root = Path(root).resolve()
    candidates = (
        root / "runtime" / "python.exe",
        root / ".java-python-runtime" / "python.exe",
        CACHED_PYTHON,
    )
    errors = []
    seen = set()
    for candidate in candidates:
        candidate = Path(candidate).resolve()
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        try:
            return probe_fn(candidate)
        except LaunchError as exc:
            errors.append(str(exc))
    if errors:
        raise LaunchError(
            "no executable bridge Python runtime is available; "
            + " | ".join(errors)
        )
    raise LaunchError(
        "no bridge Python runtime found; expected .java-python-runtime/python.exe "
        "or the bundled cached Python"
    )


def prepare_bridge_runtime(root):
    """Prepare a writable CommunicationMod config and return its environment.

    Test harnesses that do not contain bridge.py intentionally skip this
    real-runtime preparation; production launches always contain it.
    """

    root = Path(root).resolve()
    bridge_path = root / "bridge.py"
    if not bridge_path.is_file():
        return None, None
    try:
        bridge_path = bridge_path.resolve(strict=True)
    except OSError as exc:
        raise LaunchError(f"bridge entrypoint is unreadable: {bridge_path}") from exc
    python_path = _select_bridge_runtime(root)
    localappdata = root / JAVA_LOCALAPPDATA_DIRECTORY
    for config_path in _bridge_config_paths(localappdata):
        _write_bridge_config(
            config_path, python_path=python_path, bridge_path=bridge_path
        )
    return python_path, localappdata.resolve()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise LaunchError(f"runtime artifact is unreadable: {path}") from exc
    return digest.hexdigest()


def _startup_failure_marker(path):
    """Return a narrow bootstrap marker, or None for an unproven failure."""

    try:
        contents = Path(path).read_bytes()
    except OSError as exc:
        raise LaunchError(f"startup failure log is unreadable: {path}") from exc
    # These markers are emitted by the ModTheSpire/Java bootstrap before the
    # CommunicationMod bridge can publish a usable state.  Requiring the
    # bootstrap banner prevents an arbitrary in-game stack trace from being
    # mistaken for a launch failure.
    if b"Begin patching..." not in contents:
        return None
    for name, marker in _STARTUP_FAILURE_MARKERS:
        if marker in contents:
            return name
    return None


def verify_communication_mod_jars(root_jar, installed_jar):
    root_hash = sha256_file(root_jar)
    installed_hash = sha256_file(installed_jar)
    if root_hash != installed_hash:
        raise LaunchError(
            "repository and installed CommunicationMod.jar hashes differ"
        )
    return root_hash, installed_hash


def stderr_start_snapshot(path):
    path = Path(path)
    if not path.exists():
        return 0, EMPTY_SHA256
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise LaunchError(
            f"bridge stderr is unreadable before launch: {path}"
        ) from exc
    return size, digest.hexdigest()


def _write_latest_launch(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _canonical_json_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_sha256(payload):
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_json_object(path, label, *, missing_ok=False):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LaunchError(f"{label} is missing")
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"{label} is not an object")
    return value


def _read_history_records(raw, label):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LaunchError(f"{label} is unreadable") from exc
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LaunchError(
                f"{label} line {line_number} is invalid"
            ) from exc
        if not isinstance(record, dict):
            raise LaunchError(
                f"{label} line {line_number} is not an object"
            )
        records.append(record)
    return records


def maintenance_restart_handoff_path(root, launch_id):
    return (
        Path(root) / LAUNCH_DIRECTORY
        / f"{launch_id}{MAINTENANCE_RESTART_HANDOFF_SUFFIX}"
    )


def restart_predecessor_path(root, launch_id):
    return (
        Path(root) / LAUNCH_DIRECTORY
        / f"{launch_id}{RESTART_PREDECESSOR_SUFFIX}"
    )


def interrupted_exit_recovery_path(root, launch_id):
    return (
        Path(root) / LAUNCH_DIRECTORY
        / f"{launch_id}{INTERRUPTED_EXIT_RECOVERY_SUFFIX}"
    )


def failed_startup_exit_recovery_path(root, launch_id):
    return (
        Path(root) / LAUNCH_DIRECTORY
        / f"{launch_id}.failed-startup-recovery.json"
    )


def interrupted_runtime_exit_recovery_path(root, launch_id):
    return (
        Path(root) / LAUNCH_DIRECTORY
        / f"{launch_id}.runtime-exit-recovery.json"
    )


_MAINTENANCE_RESTART_HANDOFF_FIELDS = {
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

_ATTEMPT_BINDING_FIELDS = {
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
}
_OLD_RUNTIME_FIELDS = {
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
_MAINTENANCE_RESTART_INTENT_FIELDS = _ATTEMPT_BINDING_FIELDS | {
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
}

_RESTART_PARENT_FIELDS = {
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

_RESTART_PREDECESSOR_FIELDS = {
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
} | _RESTART_PARENT_FIELDS

_RESTART_TRANSITION_FIELDS = _ATTEMPT_BINDING_FIELDS | {
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
}
_RESTART_TRANSITION_EVIDENCE_FIELDS = {
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
_REPLACEMENT_RUNTIME_FIELDS = {
    "launch_evidence",
    "launch_id",
    "launcher_pid",
    "java_pid",
    "bridge_pid",
    "bridge_instance_token",
    "bridge_sha256",
    "bridge_runtime_source_digest",
    "bridge_runtime_source_file_count",
    "launch_started_at",
    "bridge_started_at",
    "restart_predecessor",
}
_RESTART_PREDECESSOR_SUMMARY_FIELDS = {
    "record_path",
    "record_sha256",
    "handoff_record_path",
    "handoff_record_sha256",
    "restart_intent_record_path",
    "restart_intent_sha256",
    "old_launch_exit_record_path",
    "old_launch_exit_record_sha256",
}
_STATE_PROBE_FIELDS = {
    "request_id",
    "accepted_state_seq",
    "result_state_seq",
    "result_decision_id",
    "result_phase",
    "requested_target_id",
    "resolved_target_id",
}
_LAUNCH_EVIDENCE_FIELDS = {
    "schema_version",
    "launch_record_sha256",
    "launch_start_record_path",
    "launch_start_record_sha256",
    "launch_id",
    "launcher_pid",
    "java_pid",
    "bridge_instance_sha256",
    "bridge_instance_token",
    "bridge_pid",
    "bridge_launch_id",
    "bridge_sha256",
    "bridge_runtime_source_digest",
    "bridge_runtime_source_file_count",
    "runtime_source_migration",
    "bridge_started_at",
    "root_communication_mod_jar_sha256",
    "installed_communication_mod_jar_sha256",
    "bridge_stderr_path",
    "bridge_stderr_start_offset",
    "bridge_stderr_start_sha256",
    "bridge_stderr_current_size",
    "bridge_stderr_current_sha256",
    "bridge_stderr_delta_size",
    "bridge_stderr_delta_sha256",
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
_MAINTENANCE_RESOLUTION_FIELDS = _ATTEMPT_BINDING_FIELDS | {
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
}
_MAINTENANCE_SOURCE_RECONCILIATION_FIELDS = {
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
_FREEZE_MANIFEST_FIELDS = {
    "schema_version",
    "policy_version",
    "decision_hash",
    "controller_hash",
    "generated_at",
    "source_digest",
    "source_file_count",
    "sources",
    "decision_case_replay",
    "decision_case_replay_revalidation",
    "launch_evidence",
    "full_tests",
    "git_diff_check",
    "runtime_artifacts_unchanged",
    "runtime_artifacts_before_sha256",
    "runtime_artifacts_after_sha256",
    "release_gate_passed",
}


def _finite_positive(value, label):
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise LaunchError(f"{label} is invalid")
    return float(value)


def _safe_component(value, label):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(character in value for character in "\\/:")
    ):
        raise LaunchError(f"{label} is invalid")
    return value


def _canonical_relative(root, value, expected, label):
    root = Path(root).resolve()
    expected = Path(expected).resolve()
    try:
        expected_relative = expected.relative_to(root).as_posix()
    except ValueError as exc:
        raise LaunchError(f"{label} escaped repository root") from exc
    if not isinstance(value, str) or value != expected_relative:
        raise LaunchError(f"{label} is not canonical")
    if (root / value).resolve() != expected:
        raise LaunchError(f"{label} is not canonical")
    return expected


def _pending_maintenance_restart_launch_id(root):
    root = Path(root).resolve()
    result = _read_json_object(
        root / "run-result.json",
        "pending maintenance run result",
        missing_ok=True,
    )
    if result is None:
        return None
    attempt_id = result.get("attempt_id")
    try:
        _safe_component(attempt_id, "pending maintenance attempt_id")
    except LaunchError:
        return None
    attempt_dir = root / "logs" / "attempts" / attempt_id
    if (attempt_dir / "menu-transition.json").exists():
        return None
    intent_path = attempt_dir / "maintenance-restart-intent.json"
    if not intent_path.exists():
        return None
    intent = _read_json_object(
        intent_path, "pending maintenance restart intent"
    )
    old_runtime = intent.get("old_runtime")
    old_launch_id = (
        old_runtime.get("launch_id")
        if isinstance(old_runtime, dict)
        else None
    )
    if (
        intent.get("record_type") != "maintenance_restart_intent"
        or intent.get("intent_status")
        != "prepared_before_runtime_restart"
        or intent.get("attempt_id") != attempt_id
    ):
        raise LaunchError(
            "pending maintenance restart intent is malformed"
        )
    _safe_component(old_launch_id, "pending maintenance old launch_id")
    handoff_path = maintenance_restart_handoff_path(
        root, old_launch_id
    )
    if not handoff_path.is_file():
        raise LaunchError(
            "pending maintenance restart handoff is missing"
        )
    return old_launch_id


def _validate_maintenance_restart_handoff(root, handoff_path):
    """Validate the launcher's complete restart authorization envelope."""

    root = Path(root).resolve()
    handoff_path = Path(handoff_path).resolve()
    handoff = _read_json_object(
        handoff_path, "maintenance restart handoff"
    )
    old_launch_id = handoff.get("old_launch_id")
    _safe_component(old_launch_id, "maintenance old launch_id")
    _canonical_relative(
        root,
        handoff_path.relative_to(root).as_posix(),
        maintenance_restart_handoff_path(root, old_launch_id),
        "maintenance restart handoff path",
    )
    created_at = _finite_positive(
        handoff.get("created_at"), "maintenance handoff created_at"
    )
    if (
        set(handoff) != _MAINTENANCE_RESTART_HANDOFF_FIELDS
        or handoff.get("schema_version") != 1
        or handoff.get("record_type") != "maintenance_restart_handoff"
        or handoff.get("handoff_status")
        != "prepared_before_old_runtime_exit"
        or not _valid_sha256(
            handoff.get("old_launch_start_record_sha256")
        )
        or not _valid_sha256(handoff.get("restart_intent_sha256"))
        or not _valid_sha256(handoff.get("target_source_digest"))
        or type(handoff.get("target_source_file_count")) is not int
        or handoff["target_source_file_count"] <= 0
    ):
        raise LaunchError("maintenance restart handoff is malformed")

    intent_value = handoff.get("restart_intent_record_path")
    if not isinstance(intent_value, str) or not intent_value:
        raise LaunchError("maintenance restart intent path is invalid")
    intent_path = (root / intent_value).resolve()
    attempts_root = (root / "logs" / "attempts").resolve()
    try:
        intent_parts = intent_path.relative_to(attempts_root).parts
    except ValueError as exc:
        raise LaunchError(
            "maintenance restart intent path escaped attempt root"
        ) from exc
    if (
        len(intent_parts) != 2
        or intent_parts[1] != "maintenance-restart-intent.json"
        or intent_value != intent_path.relative_to(root).as_posix()
    ):
        raise LaunchError("maintenance restart intent path is not canonical")
    attempt_id = _safe_component(
        intent_parts[0], "maintenance restart intent attempt_id"
    )
    intent = _read_json_object(intent_path, "maintenance restart intent")
    if (
        set(intent) != _MAINTENANCE_RESTART_INTENT_FIELDS
        or intent.get("schema_version") != 2
        or intent.get("record_type") != "maintenance_restart_intent"
        or intent.get("intent_status") != "prepared_before_runtime_restart"
        or intent.get("eligible_for_cohort") is not False
        or intent.get("attempt_id") != attempt_id
        or intent.get("terminal_phase") != "GAME_OVER"
        or type(intent.get("terminal_state_seq")) is not int
        or intent["terminal_state_seq"] < 0
        or type(intent.get("target_source_file_count")) is not int
        or intent["target_source_file_count"] <= 0
        or not _valid_sha256(intent.get("target_source_digest"))
        or any(
            not _valid_sha256(intent.get(field))
            for field in (
                "run_result_sha256",
                "terminal_state_sha256",
                "terminal_record_sha256",
                "audit_record_sha256",
                "controller_exit_record_sha256",
            )
        )
        or any(
            not isinstance(intent.get(field), str) or not intent[field]
            for field in (
                "run_id",
                "character",
                "run_type",
                "decision_hash",
                "controller_hash",
                "policy_version",
                "selection_id",
                "selection_digest",
                "terminal_decision_id",
                "maintenance_decision_hash",
                "maintenance_controller_hash",
            )
        )
        or type(intent.get("seed")) is not int
        or type(intent.get("ascension_level")) is not int
        or (
            intent.get("maintenance_decision_hash")
            == intent.get("decision_hash")
            and intent.get("maintenance_controller_hash")
            == intent.get("controller_hash")
        )
    ):
        raise LaunchError("maintenance restart intent is malformed")
    intent_created_at = _finite_positive(
        intent.get("created_at"), "maintenance restart intent created_at"
    )
    if (
        _canonical_sha256(intent) != handoff["restart_intent_sha256"]
        or handoff.get("restart_intent_record_path") != intent_value
        or handoff.get("target_source_digest")
        != intent.get("target_source_digest")
        or handoff.get("target_source_file_count")
        != intent.get("target_source_file_count")
        or created_at != intent_created_at
    ):
        raise LaunchError(
            "maintenance restart handoff does not bind its intent"
        )

    old = intent.get("old_runtime")
    if not isinstance(old, dict) or set(old) != _OLD_RUNTIME_FIELDS:
        raise LaunchError("maintenance restart old runtime is malformed")
    statuses = old.get("process_alive")
    if (
        old.get("launch_id") != old_launch_id
        or old.get("launch_start_record_path")
        != handoff.get("old_launch_start_record_path")
        or old.get("launch_start_record_sha256")
        != handoff.get("old_launch_start_record_sha256")
        or any(
            type(old.get(field)) is not int or old[field] <= 0
            for field in ("launcher_pid", "java_pid", "bridge_pid")
        )
        or len({old["launcher_pid"], old["java_pid"], old["bridge_pid"]})
        != 3
        or not isinstance(statuses, dict)
        or set(statuses) != {"launcher", "java", "bridge"}
        or any(type(value) is not bool for value in statuses.values())
        or statuses["launcher"] is not True
        or statuses["java"] is not True
        or any(
            not _valid_sha256(old.get(field))
            for field in (
                "launch_start_record_sha256",
                "bridge_instance_sha256",
                "bridge_sha256",
                "freeze_source_digest",
            )
        )
        or not isinstance(old.get("bridge_instance_token"), str)
        or not old["bridge_instance_token"]
    ):
        raise LaunchError("maintenance restart old runtime is invalid")
    launch_started_at = _finite_positive(
        old.get("launch_started_at"), "maintenance old launch started_at"
    )
    bridge_started_at = _finite_positive(
        old.get("bridge_started_at"), "maintenance old bridge started_at"
    )
    freeze_generated_at = _finite_positive(
        old.get("freeze_generated_at"), "maintenance old freeze generated_at"
    )
    controller_observed_at = _finite_positive(
        old.get("controller_observed_at"),
        "maintenance old controller observed_at",
    )
    if not (
        launch_started_at
        <= bridge_started_at
        <= freeze_generated_at
        <= controller_observed_at
        < intent_created_at
    ):
        raise LaunchError("maintenance restart timestamps are unordered")
    return handoff, intent


def _restart_lineage_root_fields(record):
    return {
        field: record.get(field)
        for field in (
            "old_launch_id",
            "old_launch_start_record_path",
            "old_launch_start_record_sha256",
            "old_launch_exit_record_path",
            "old_launch_exit_record_sha256",
            "restart_handoff_record_path",
            "restart_handoff_record_sha256",
            "restart_intent_record_path",
            "restart_intent_sha256",
        )
    }


def _validate_launch_start_record(
    root,
    launch_id,
    *,
    require_predecessor,
    allow_legacy=False,
):
    root = Path(root).resolve()
    _safe_component(launch_id, "restart lineage launch_id")
    start_path = (
        root / LAUNCH_DIRECTORY / f"{launch_id}.start.json"
    ).resolve()
    start = _read_json_object(start_path, "restart lineage launch start")
    fields = set(start)
    if fields == _BASE_LAUNCH_START_FIELDS:
        if not allow_legacy or require_predecessor:
            raise LaunchError(
                "restart lineage launch start has obsolete schema"
            )
        predecessor_path = None
        predecessor_sha256 = None
    elif fields == _LAUNCH_START_FIELDS:
        predecessor_value = start.get("restart_predecessor_record_path")
        predecessor_sha256 = start.get(
            "restart_predecessor_record_sha256"
        )
        if predecessor_value is None or predecessor_sha256 is None:
            if predecessor_value is not None or predecessor_sha256 is not None:
                raise LaunchError(
                    "restart lineage launch predecessor anchor is incomplete"
                )
            if require_predecessor:
                raise LaunchError(
                    "restart lineage launch predecessor anchor is missing"
                )
            predecessor_path = None
        else:
            expected_predecessor = restart_predecessor_path(
                root, launch_id
            ).resolve()
            predecessor_path = _canonical_relative(
                root,
                predecessor_value,
                expected_predecessor,
                "restart lineage launch predecessor path",
            )
            if not _valid_sha256(predecessor_sha256):
                raise LaunchError(
                    "restart lineage launch predecessor hash is invalid"
                )
            predecessor = _read_json_object(
                predecessor_path, "restart lineage launch predecessor"
            )
            if _canonical_sha256(predecessor) != predecessor_sha256:
                raise LaunchError(
                    "restart lineage launch predecessor changed"
                )
    else:
        raise LaunchError("restart lineage launch start fields are invalid")

    started_at = _finite_positive(
        start.get("started_at"), "restart lineage launch started_at"
    )
    launcher_pid = start.get("launcher_pid")
    java_pid = start.get("java_pid")
    if (
        start.get("schema_version") != SCHEMA_VERSION
        or start.get("launch_id") != launch_id
        or type(launcher_pid) is not int
        or launcher_pid <= 0
        or type(java_pid) is not int
        or java_pid <= 0
        or launcher_pid == java_pid
        or not _valid_sha256(
            start.get("root_communication_mod_jar_sha256")
        )
        or not _valid_sha256(
            start.get("installed_communication_mod_jar_sha256")
        )
        or start.get("root_communication_mod_jar_sha256")
        != start.get("installed_communication_mod_jar_sha256")
        or type(start.get("bridge_stderr_start_offset")) is not int
        or start["bridge_stderr_start_offset"] < 0
        or not _valid_sha256(start.get("bridge_stderr_start_sha256"))
    ):
        raise LaunchError("restart lineage launch start is invalid")
    _canonical_relative(
        root,
        start.get("launch_start_record_path"),
        start_path,
        "restart lineage launch start path",
    )
    log_value = start.get("launch_log_path")
    if not isinstance(log_value, str) or not log_value:
        raise LaunchError("restart lineage launch log path is invalid")
    log_path = (root / log_value).resolve()
    try:
        log_relative = log_path.relative_to(
            (root / LAUNCH_DIRECTORY).resolve()
        )
    except ValueError as exc:
        raise LaunchError("restart lineage launch log escaped root") from exc
    if (
        log_value != log_path.relative_to(root).as_posix()
        or len(log_relative.parts) != 1
        or log_path.suffix.casefold() != ".log"
        or not log_path.name.startswith("launch-")
        or launch_id not in log_path.stem
        or not log_path.is_file()
    ):
        raise LaunchError("restart lineage launch log is not canonical")
    game_dir_value = start.get("game_dir")
    command = start.get("command")
    if (
        not isinstance(game_dir_value, str)
        or not game_dir_value
        or not Path(game_dir_value).is_absolute()
        or not isinstance(command, list)
        or len(command) != 6
        or any(not isinstance(item, str) or not item for item in command)
        or command[1] != "-jar"
        or command[3:] != [
            "--skip-intro", "--mods", "basemod,CommunicationMod"
        ]
        or Path(command[0]).resolve()
        != (Path(game_dir_value) / "jre" / "bin" / "java.exe").resolve()
        or not isinstance(start.get("bridge_stderr_path"), str)
        or not Path(start["bridge_stderr_path"]).is_absolute()
    ):
        raise LaunchError("restart lineage launch command is invalid")
    return {
        "record": start,
        "path": start_path,
        "sha256": _canonical_sha256(start),
        "started_at": started_at,
        "predecessor_path": predecessor_path,
        "predecessor_sha256": predecessor_sha256,
    }


def _validate_launch_exit_record(root, launch_id, start_evidence):
    root = Path(root).resolve()
    exit_path = (
        root / LAUNCH_DIRECTORY / f"{launch_id}.exit.json"
    ).resolve()
    exit_record = _read_json_object(
        exit_path, "restart lineage launch exit"
    )
    start = start_evidence["record"]
    expected_fields = set(start) | {
        "launch_exit_record_path", "finished_at", "exit_code"
    }
    finished_at = _finite_positive(
        exit_record.get("finished_at"),
        "restart lineage launch finished_at",
    )
    if (
        set(exit_record) != expected_fields
        or any(exit_record.get(key) != value for key, value in start.items())
        or type(exit_record.get("exit_code")) is not int
        or finished_at < start_evidence["started_at"]
    ):
        raise LaunchError("restart lineage launch exit is invalid")
    _canonical_relative(
        root,
        exit_record.get("launch_exit_record_path"),
        exit_path,
        "restart lineage launch exit path",
    )
    if exit_record["exit_code"] == INTERRUPTED_EXIT_CODE:
        _validate_interrupted_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=finished_at,
        )
    elif exit_record["exit_code"] == FAILED_STARTUP_EXIT_CODE:
        _validate_failed_startup_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=finished_at,
        )
    elif exit_record["exit_code"] == INTERRUPTED_RUNTIME_EXIT_CODE:
        _validate_interrupted_runtime_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=finished_at,
        )
    return {
        "record": exit_record,
        "path": exit_path,
        "sha256": _canonical_sha256(exit_record),
        "finished_at": finished_at,
    }


def _validate_failed_startup_exit_recovery(
    root, launch_id, start_evidence, *, expected_finished_at=None,
):
    """Validate immutable proof that a launch died before bridge readiness."""

    root = Path(root).resolve()
    recovery_path = failed_startup_exit_recovery_path(root, launch_id).resolve()
    recovery = _read_json_object(
        recovery_path, "failed startup exit recovery"
    )
    start = start_evidence["record"]
    log_path = (root / start["launch_log_path"]).resolve()
    marker = _startup_failure_marker(log_path)
    recovered_at = _finite_positive(
        recovery.get("recovered_at"), "failed startup recovered_at"
    )
    expected = {
        "schema_version": 1,
        "record_type": "failed_startup_exit_recovery",
        "recovery_status": "bootstrap_failure_after_dead_bound_processes",
        "launch_id": launch_id,
        "launch_start_record_path": (
            start_evidence["path"].relative_to(root).as_posix()
        ),
        "launch_start_record_sha256": start_evidence["sha256"],
        "launch_log_path": start["launch_log_path"],
        "launch_log_sha256": sha256_file(log_path),
        "failure_marker": marker,
        "launcher_pid": start["launcher_pid"],
        "java_pid": start["java_pid"],
        "launcher_alive": False,
        "java_alive": False,
        "exit_code_sentinel": FAILED_STARTUP_EXIT_CODE,
        "recovered_at": recovered_at,
    }
    if (
        marker is None
        or set(recovery) != _FAILED_STARTUP_EXIT_RECOVERY_FIELDS
        or recovery != expected
        or recovered_at < start_evidence["started_at"]
        or (
            expected_finished_at is not None
            and recovered_at != expected_finished_at
        )
    ):
        raise LaunchError("failed startup exit recovery is invalid")
    return {
        "record": recovery,
        "path": recovery_path,
        "sha256": _canonical_sha256(recovery),
        "recovered_at": recovered_at,
    }


def _validate_lineage_root_documents(root, handoff_path):
    root = Path(root).resolve()
    handoff, intent = _validate_maintenance_restart_handoff(
        root, handoff_path
    )
    old_launch_id = handoff["old_launch_id"]
    old_start = _validate_launch_start_record(
        root,
        old_launch_id,
        require_predecessor=False,
        allow_legacy=True,
    )
    if (
        handoff.get("old_launch_start_record_path")
        != old_start["path"].relative_to(root).as_posix()
        or handoff.get("old_launch_start_record_sha256")
        != old_start["sha256"]
    ):
        raise LaunchError("maintenance old launch start changed")
    old_exit = _validate_launch_exit_record(
        root, old_launch_id, old_start
    )
    handoff_relative = Path(handoff_path).resolve().relative_to(root).as_posix()
    intent_path = (root / handoff["restart_intent_record_path"]).resolve()
    return {
        "handoff": handoff,
        "intent": intent,
        "old_start": old_start,
        "old_exit": old_exit,
        "root_fields": {
            "old_launch_id": old_launch_id,
            "old_launch_start_record_path": (
                old_start["path"].relative_to(root).as_posix()
            ),
            "old_launch_start_record_sha256": old_start["sha256"],
            "old_launch_exit_record_path": (
                old_exit["path"].relative_to(root).as_posix()
            ),
            "old_launch_exit_record_sha256": old_exit["sha256"],
            "restart_handoff_record_path": handoff_relative,
            "restart_handoff_record_sha256": _canonical_sha256(handoff),
            "restart_intent_record_path": (
                intent_path.relative_to(root).as_posix()
            ),
            "restart_intent_sha256": _canonical_sha256(intent),
        },
    }


def _validate_restart_lineage_record(
    root,
    path,
    expected_root,
    *,
    expected_sha256=None,
    require_exit=False,
    child_started_at=None,
    seen=None,
):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise LaunchError("restart lineage path escaped root") from exc
    seen = set() if seen is None else set(seen)
    if relative in seen:
        raise LaunchError("restart lineage contains a cycle")
    if len(seen) >= 128:
        raise LaunchError("restart lineage is too deep")
    seen.add(relative)
    record = _read_json_object(path, "restart lineage predecessor")
    launch_id = _safe_component(
        record.get("new_launch_id"), "restart lineage new launch_id"
    )
    expected_path = restart_predecessor_path(root, launch_id).resolve()
    _canonical_relative(
        root, relative, expected_path, "restart lineage predecessor path"
    )
    record_sha256 = _canonical_sha256(record)
    started_at = _finite_positive(
        record.get("new_launch_started_at"),
        "restart lineage predecessor started_at",
    )
    depth = record.get("lineage_depth")
    if (
        set(record) != _RESTART_PREDECESSOR_FIELDS
        or record.get("schema_version") != 2
        or record.get("record_type") != "restart_predecessor"
        or record.get("predecessor_status")
        != "observed_before_new_runtime_launch"
        or type(depth) is not int
        or depth < 0
        or type(record.get("new_launcher_pid")) is not int
        or record["new_launcher_pid"] <= 0
        or not isinstance(record.get("new_launch_log_path"), str)
        or not record["new_launch_log_path"]
        or _restart_lineage_root_fields(record) != expected_root
        or (
            expected_sha256 is not None
            and (
                not _valid_sha256(expected_sha256)
                or record_sha256 != expected_sha256
            )
        )
    ):
        raise LaunchError("restart lineage predecessor is invalid")
    start = _validate_launch_start_record(
        root,
        launch_id,
        require_predecessor=True,
    )
    if (
        start["predecessor_path"] != path
        or start["predecessor_sha256"] != record_sha256
        or start["record"].get("launcher_pid")
        != record.get("new_launcher_pid")
        or start["started_at"] != started_at
        or start["record"].get("launch_log_path")
        != record.get("new_launch_log_path")
    ):
        raise LaunchError(
            "restart lineage predecessor does not bind its launch"
        )
    exit_evidence = None
    if require_exit:
        exit_evidence = _validate_launch_exit_record(
            root, launch_id, start
        )
        if (
            child_started_at is not None
            and exit_evidence["finished_at"] >= child_started_at
        ):
            raise LaunchError(
                "restart lineage launch overlaps its replacement"
            )

    parent_values = {
        field: record.get(field) for field in _RESTART_PARENT_FIELDS
    }
    if depth == 0:
        if any(value is not None for value in parent_values.values()):
            raise LaunchError("restart lineage leaf has a parent binding")
        old_exit_value = expected_root.get("old_launch_exit_record_path")
        old_exit_sha256 = expected_root.get("old_launch_exit_record_sha256")
        old_launch_id = _safe_component(
            expected_root.get("old_launch_id"),
            "restart lineage old launch_id",
        )
        old_exit_path = _canonical_relative(
            root,
            old_exit_value,
            root / LAUNCH_DIRECTORY / f"{old_launch_id}.exit.json",
            "restart lineage old exit path",
        )
        old_exit = _read_json_object(
            old_exit_path, "restart lineage old exit"
        )
        old_finished_at = _finite_positive(
            old_exit.get("finished_at"),
            "restart lineage old finished_at",
        )
        if (
            not _valid_sha256(old_exit_sha256)
            or _canonical_sha256(old_exit) != old_exit_sha256
            or old_finished_at >= started_at
        ):
            raise LaunchError("restart lineage old exit is invalid")
    else:
        if any(value is None for value in parent_values.values()):
            raise LaunchError("restart lineage parent binding is incomplete")
        parent_launch_id = _safe_component(
            record.get("lineage_parent_launch_id"),
            "restart lineage parent launch_id",
        )
        parent_started_at = _finite_positive(
            record.get("lineage_parent_started_at"),
            "restart lineage parent started_at",
        )
        parent_finished_at = _finite_positive(
            record.get("lineage_parent_finished_at"),
            "restart lineage parent finished_at",
        )
        parent_sha256 = record.get("lineage_parent_record_sha256")
        parent_start_sha256 = record.get(
            "lineage_parent_start_record_sha256"
        )
        parent_exit_sha256 = record.get(
            "lineage_parent_exit_record_sha256"
        )
        if (
            not _valid_sha256(parent_sha256)
            or not _valid_sha256(parent_start_sha256)
            or not _valid_sha256(parent_exit_sha256)
            or not (
                parent_started_at <= parent_finished_at < started_at
            )
        ):
            raise LaunchError("restart lineage parent binding is invalid")
        parent_path = _canonical_relative(
            root,
            record.get("lineage_parent_record_path"),
            restart_predecessor_path(root, parent_launch_id),
            "restart lineage parent predecessor path",
        )
        parent_start_path = _canonical_relative(
            root,
            record.get("lineage_parent_start_record_path"),
            root / LAUNCH_DIRECTORY / f"{parent_launch_id}.start.json",
            "restart lineage parent start path",
        )
        parent_exit_path = _canonical_relative(
            root,
            record.get("lineage_parent_exit_record_path"),
            root / LAUNCH_DIRECTORY / f"{parent_launch_id}.exit.json",
            "restart lineage parent exit path",
        )
        parent = _validate_restart_lineage_record(
            root,
            parent_path,
            expected_root,
            expected_sha256=parent_sha256,
            require_exit=True,
            child_started_at=started_at,
            seen=seen,
        )
        if (
            parent["record"].get("lineage_depth") != depth - 1
            or parent["record"].get("new_launch_id") != parent_launch_id
            or parent["start"]["path"] != parent_start_path
            or parent["start"]["sha256"] != parent_start_sha256
            or parent["start"]["started_at"] != parent_started_at
            or parent["exit"]["path"] != parent_exit_path
            or parent["exit"]["sha256"] != parent_exit_sha256
            or parent["exit"]["finished_at"] != parent_finished_at
        ):
            raise LaunchError("restart lineage parent evidence changed")
    return {
        "record": record,
        "path": path,
        "sha256": record_sha256,
        "start": start,
        "exit": exit_evidence,
    }


def _valid_maintenance_source_reconciliation(
    reconciliation,
    attestation_manifest,
    attestation_bytes,
    intent,
    replacement,
    launch_evidence,
    old_start,
):
    old_runtime = intent.get("old_runtime")
    created_at = reconciliation.get("created_at")
    return (
        isinstance(reconciliation, dict)
        and set(reconciliation) == _MAINTENANCE_SOURCE_RECONCILIATION_FIELDS
        and reconciliation.get("schema_version") == 2
        and reconciliation.get("record_type")
        == "maintenance_restart_source_reconciliation"
        and reconciliation.get("reconciliation_status")
        == "runtime_identity_preserved"
        and isinstance(old_runtime, dict)
        and reconciliation.get("attempt_id") == intent.get("attempt_id")
        and reconciliation.get("restart_intent_sha256")
        == _canonical_sha256(intent)
        and reconciliation.get("old_launch_id")
        == old_runtime.get("launch_id")
        and reconciliation.get("replacement_launch_id")
        == replacement.get("launch_id")
        and reconciliation.get("target_source_digest")
        == intent.get("target_source_digest")
        and reconciliation.get("target_source_file_count")
        == intent.get("target_source_file_count")
        and reconciliation.get("bridge_runtime_source_digest")
        == replacement.get("bridge_runtime_source_digest")
        and reconciliation.get("bridge_runtime_source_file_count")
        == replacement.get("bridge_runtime_source_file_count")
        and reconciliation.get("target_source_file_count")
        == reconciliation.get("bridge_runtime_source_file_count")
        and reconciliation.get("maintenance_decision_hash")
        == intent.get("maintenance_decision_hash")
        and reconciliation.get("maintenance_controller_hash")
        == intent.get("maintenance_controller_hash")
        and reconciliation.get("old_bridge_sha256")
        == old_runtime.get("bridge_sha256")
        and reconciliation.get("replacement_bridge_sha256")
        == replacement.get("bridge_sha256")
        and reconciliation.get("old_bridge_sha256")
        == reconciliation.get("replacement_bridge_sha256")
        and isinstance(attestation_manifest, dict)
        and attestation_manifest.get("release_gate_passed") is True
        and attestation_manifest.get("source_digest")
        == reconciliation.get("freeze_source_digest")
        == reconciliation.get("bridge_runtime_source_digest")
        and attestation_manifest.get("source_file_count")
        == reconciliation.get("freeze_source_file_count")
        == reconciliation.get("bridge_runtime_source_file_count")
        and attestation_manifest.get("decision_hash")
        == reconciliation.get("maintenance_decision_hash")
        and attestation_manifest.get("controller_hash")
        == reconciliation.get("maintenance_controller_hash")
        and hashlib.sha256(attestation_bytes).hexdigest()
        == reconciliation.get("freeze_manifest_sha256")
        and reconciliation.get("communication_mod_jar_sha256")
        == old_start.get("root_communication_mod_jar_sha256")
        == old_start.get("installed_communication_mod_jar_sha256")
        == launch_evidence.get("root_communication_mod_jar_sha256")
        == launch_evidence.get("installed_communication_mod_jar_sha256")
        and type(created_at) in {int, float}
        and math.isfinite(float(created_at))
        and created_at > intent.get("created_at", float("inf"))
    )


def _restart_lineage_was_released(root, predecessor):
    root = Path(root).resolve()
    intent = predecessor["root_intent"]
    intent_path = (
        root / predecessor["record"]["restart_intent_record_path"]
    ).resolve()
    transition_path = intent_path.parent / "menu-transition.json"
    if not transition_path.exists():
        return False
    transition = _read_json_object(
        transition_path, "completed restart menu transition"
    )
    evidence = transition.get("restart_evidence")
    replacement = (
        evidence.get("replacement_runtime")
        if isinstance(evidence, dict)
        else None
    )
    restart = (
        replacement.get("restart_predecessor")
        if isinstance(replacement, dict)
        else None
    )
    old_exit = (
        evidence.get("old_launch_exit")
        if isinstance(evidence, dict)
        else None
    )
    launch_evidence = (
        replacement.get("launch_evidence")
        if isinstance(replacement, dict)
        else None
    )
    probe = (
        evidence.get("state_probe")
        if isinstance(evidence, dict)
        else None
    )
    real_old_exit_path = _canonical_relative(
        root,
        predecessor["record"]["old_launch_exit_record_path"],
        root
        / LAUNCH_DIRECTORY
        / f"{predecessor['record']['old_launch_id']}.exit.json",
        "completed restart old exit path",
    )
    real_old_exit = _read_json_object(
        real_old_exit_path, "completed restart old exit"
    )
    bridge_instance = (
        {
            "schema_version": 2,
            "protocol_version": 2,
            "instance_token": replacement.get("bridge_instance_token"),
            "bridge_pid": replacement.get("bridge_pid"),
            "parent_java_pid": replacement.get("java_pid"),
            "launch_id": replacement.get("launch_id"),
            "bridge_sha256": replacement.get("bridge_sha256"),
            "runtime_source_digest": replacement.get(
                "bridge_runtime_source_digest"
            ),
            "runtime_source_file_count": replacement.get(
                "bridge_runtime_source_file_count"
            ),
            "started_at": replacement.get("bridge_started_at"),
        }
        if isinstance(replacement, dict)
        else {}
    )
    resolution_path = intent_path.parent / "maintenance-resolution.json"
    resolution = _read_json_object(
        resolution_path, "completed maintenance resolution"
    )
    history_path = root / "run-history.jsonl"
    try:
        history_bytes = history_path.read_bytes()
    except OSError as exc:
        raise LaunchError(
            "completed maintenance history is unreadable"
        ) from exc
    history_records = _read_history_records(
        history_bytes, "completed maintenance history"
    )
    history_byte_length = resolution.get("history_byte_length")
    valid_history_byte_length = (
        type(history_byte_length) is int
        and 0 < history_byte_length <= len(history_bytes)
    )
    history_snapshot_bytes = (
        history_bytes[:history_byte_length]
        if valid_history_byte_length
        else b""
    )
    history_snapshot_records = _read_history_records(
        history_snapshot_bytes,
        "completed maintenance history snapshot",
    )
    history_indices = resolution.get("history_indices")
    valid_history_indices = (
        isinstance(history_indices, list)
        and len(history_indices) == 3
        and all(type(index) is int for index in history_indices)
        and 0 <= history_indices[0] < history_indices[1] < history_indices[2]
        and history_indices[2] < len(history_records)
    )
    history_triplet = (
        [history_records[index] for index in history_indices]
        if valid_history_indices
        else [None, None, None]
    )
    terminal_record, audit_record, controller_exit_record = history_triplet
    matching_history_indices = []
    for record_type in (
        TERMINAL_RECORD_TYPE,
        AUDIT_RECORD_TYPE,
        CONTROLLER_EXIT_RECORD_TYPE,
    ):
        matches = [
            index
            for index, record in enumerate(history_records)
            if record.get("attempt_id") == intent.get("attempt_id")
            and record.get("record_type") == record_type
        ]
        matching_history_indices.append(matches[0] if len(matches) == 1 else None)
    manifest_path = _canonical_relative(
        root,
        resolution.get("freeze_manifest_record_path"),
        intent_path.parent / "maintenance-freeze-manifest.json",
        "completed maintenance freeze snapshot path",
    )
    manifest = _read_json_object(
        manifest_path, "completed maintenance freeze manifest"
    )
    manifest_sources = manifest.get("sources")
    manifest_source_digest = manifest.get("source_digest")
    intent_source_digest = intent.get("target_source_digest")
    intent_source_file_count = intent.get("target_source_file_count")
    runtime_source_migration = (
        launch_evidence.get("runtime_source_migration")
        if isinstance(launch_evidence, dict)
        else None
    )
    direct_runtime_source_migration_valid = (
        isinstance(manifest_sources, dict)
        and isinstance(runtime_source_migration, dict)
        and runtime_source_migration.get("previous_source_digest")
        == intent_source_digest
        and runtime_source_migration.get("previous_source_file_count")
        == intent_source_file_count
        and runtime_source_migration.get("current_source_digest")
        == manifest_source_digest
        and runtime_source_migration.get("current_source_file_count")
        == manifest.get("source_file_count")
        and freeze_manifest._validate_runtime_source_migration(
            runtime_source_migration,
            intent_source_digest,
            intent_source_file_count,
            manifest_sources,
        )
    )
    reconciliation_path = (
        intent_path.parent
        / "maintenance-restart-source-reconciliation.json"
    )
    attestation_path = (
        intent_path.parent / "maintenance-replacement-runtime-freeze.json"
    )
    source_reconciliation_valid = False
    if reconciliation_path.exists() or attestation_path.exists():
        reconciliation = _read_json_object(
            reconciliation_path,
            "completed maintenance source reconciliation",
        )
        try:
            attestation_bytes = attestation_path.read_bytes()
            attestation_manifest = json.loads(
                attestation_bytes.decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LaunchError(
                "completed maintenance runtime freeze is unreadable"
            ) from exc
        old_runtime = intent.get("old_runtime")
        old_start_path = _canonical_relative(
            root,
            old_runtime.get("launch_start_record_path")
            if isinstance(old_runtime, dict) else None,
            root / LAUNCH_DIRECTORY
            / f"{old_runtime.get('launch_id') if isinstance(old_runtime, dict) else ''}.start.json",
            "completed maintenance old launch path",
        )
        old_start = _read_json_object(
            old_start_path, "completed maintenance old launch"
        )
        source_reconciliation_valid = (
            isinstance(replacement, dict)
            and isinstance(launch_evidence, dict)
            and _canonical_sha256(old_start)
            == old_runtime.get("launch_start_record_sha256")
            and _valid_maintenance_source_reconciliation(
                reconciliation,
                attestation_manifest,
                attestation_bytes,
                intent,
                replacement,
                launch_evidence,
                old_start,
            )
        )
    bridge_to_manifest_migration_valid = (
        source_reconciliation_valid
        and isinstance(manifest_sources, dict)
        and isinstance(runtime_source_migration, dict)
        and runtime_source_migration.get("previous_source_digest")
        == replacement.get("bridge_runtime_source_digest")
        and runtime_source_migration.get("previous_source_file_count")
        == replacement.get("bridge_runtime_source_file_count")
        and runtime_source_migration.get("current_source_digest")
        == manifest_source_digest
        and runtime_source_migration.get("current_source_file_count")
        == manifest.get("source_file_count")
        and freeze_manifest._validate_runtime_source_migration(
            runtime_source_migration,
            replacement.get("bridge_runtime_source_digest"),
            replacement.get("bridge_runtime_source_file_count"),
            manifest_sources,
        )
    )
    reconciled_exact_runtime_valid = (
        source_reconciliation_valid
        and runtime_source_migration is None
        and manifest_source_digest
        == replacement.get("bridge_runtime_source_digest")
        and manifest.get("source_file_count")
        == replacement.get("bridge_runtime_source_file_count")
    )
    runtime_source_migration_valid = (
        direct_runtime_source_migration_valid
        or bridge_to_manifest_migration_valid
        or reconciled_exact_runtime_valid
    )
    full_tests = manifest.get("full_tests")
    diff_check = manifest.get("git_diff_check")
    replay = manifest.get("decision_case_replay")
    replay_revalidation = manifest.get(
        "decision_case_replay_revalidation"
    )
    if (
        set(transition) != _RESTART_TRANSITION_FIELDS
        or transition.get("schema_version") != 2
        or transition.get("record_type") != "menu_transition"
        or transition.get("transition_kind")
        != "game_over_to_main_menu_via_runtime_restart"
        or transition.get("transition_status") != "clear"
        or transition.get("eligible_for_cohort") is not False
        or transition.get("authorization_kind")
        != "maintenance_resolution"
        or set(resolution) != _MAINTENANCE_RESOLUTION_FIELDS
        or resolution.get("schema_version") != 2
        or resolution.get("record_type") != "maintenance_resolution"
        or resolution.get("resolution_kind")
        != "terminal_screen_release_after_repair"
        or resolution.get("resolution_status")
        != "maintenance_gate_passed"
        or resolution.get("eligible_for_cohort") is not False
        or resolution.get("original_findings_preserved") is not True
        or _canonical_sha256(resolution)
        != transition.get("maintenance_resolution_sha256")
        or any(
            resolution.get(field) != intent.get(field)
            for field in _ATTEMPT_BINDING_FIELDS
        )
        or resolution.get("terminal_state_seq")
        != intent.get("terminal_state_seq")
        or (
            resolution.get("maintenance_decision_hash")
            != intent.get("maintenance_decision_hash")
            and not runtime_source_migration_valid
        )
        or (
            resolution.get("maintenance_controller_hash")
            != intent.get("maintenance_controller_hash")
            and not runtime_source_migration_valid
        )
        or (
            resolution.get("maintenance_decision_hash")
            == intent.get("decision_hash")
            and resolution.get("maintenance_controller_hash")
            == intent.get("controller_hash")
        )
        or resolution.get("terminal_record_sha256")
        != intent.get("terminal_record_sha256")
        or resolution.get("audit_record_sha256")
        != intent.get("audit_record_sha256")
        or resolution.get("controller_exit_record_sha256")
        != intent.get("controller_exit_record_sha256")
        or any(
            not _valid_sha256(resolution.get(field))
            for field in (
                "freeze_manifest_sha256",
                "freeze_source_digest",
                "history_sha256",
                "terminal_record_sha256",
                "audit_record_sha256",
                "controller_exit_record_sha256",
            )
        )
        or not valid_history_byte_length
        or hashlib.sha256(history_snapshot_bytes).hexdigest()
        != resolution.get("history_sha256")
        or history_snapshot_records
        != history_records[:len(history_snapshot_records)]
        or not valid_history_indices
        or history_indices[2] >= len(history_snapshot_records)
        or history_indices != matching_history_indices
        or terminal_record.get("record_type") != TERMINAL_RECORD_TYPE
        or audit_record.get("record_type") != AUDIT_RECORD_TYPE
        or controller_exit_record.get("record_type")
        != CONTROLLER_EXIT_RECORD_TYPE
        or any(
            record.get("attempt_id") != intent.get("attempt_id")
            for record in history_triplet
        )
        or any(
            type(record.get(field)) is not type(intent.get(field))
            or record.get(field) != intent.get(field)
            for record in history_triplet
            for field in _ATTEMPT_BINDING_FIELDS
        )
        or _canonical_sha256(terminal_record)
        != intent.get("terminal_record_sha256")
        or _canonical_sha256(audit_record)
        != intent.get("audit_record_sha256")
        or _canonical_sha256(controller_exit_record)
        != intent.get("controller_exit_record_sha256")
        or terminal_record.get("terminal_state_seq")
        != intent.get("terminal_state_seq")
        or audit_record.get("audit_status")
        != resolution.get("original_audit_status")
        or type(audit_record.get("release_gate_passed"))
        is not type(resolution.get("original_release_gate_passed"))
        or audit_record.get("release_gate_passed")
        != resolution.get("original_release_gate_passed")
        or controller_exit_record.get("controller_exit_status")
        != resolution.get("original_controller_exit_status")
        or type(controller_exit_record.get("exit_code"))
        is not type(resolution.get("original_exit_code"))
        or controller_exit_record.get("exit_code")
        != resolution.get("original_exit_code")
        or (
            audit_record.get("release_gate_passed") is not False
            and controller_exit_record.get("controller_exit_status")
            != "issues"
            and not runtime_source_migration_valid
        )
        or set(manifest) != _FREEZE_MANIFEST_FIELDS
        or manifest.get("schema_version") != 1
        or manifest.get("policy_version") != "fast-policy-v5"
        or manifest.get("decision_hash")
        != resolution.get("maintenance_decision_hash")
        or manifest.get("controller_hash")
        != resolution.get("maintenance_controller_hash")
        or (
            (
                manifest.get("decision_hash")
                != intent.get("maintenance_decision_hash")
                or manifest.get("controller_hash")
                != intent.get("maintenance_controller_hash")
            )
            and not runtime_source_migration_valid
        )
        or manifest.get("release_gate_passed") is not True
        or sha256_file(manifest_path)
        != resolution.get("freeze_manifest_sha256")
        or manifest.get("source_digest")
        != resolution.get("freeze_source_digest")
        or (
            (
                manifest.get("source_digest")
                != intent.get("target_source_digest")
                or manifest.get("source_file_count")
                != intent.get("target_source_file_count")
            )
            and not runtime_source_migration_valid
        )
        or manifest.get("generated_at")
        != resolution.get("freeze_generated_at")
        or manifest.get("runtime_artifacts_unchanged") is not True
        or manifest.get("runtime_artifacts_before_sha256")
        != manifest.get("runtime_artifacts_after_sha256")
        or not _valid_sha256(
            manifest.get("runtime_artifacts_before_sha256")
        )
        or not isinstance(manifest.get("sources"), dict)
        or len(manifest["sources"]) != manifest.get("source_file_count")
        or not isinstance(full_tests, dict)
        or full_tests.get("returncode") != 0
        or type(full_tests.get("test_count")) is not int
        or full_tests["test_count"] <= 0
        or not isinstance(diff_check, dict)
        or diff_check.get("returncode") != 0
        or diff_check.get("test_count") is not None
        or not isinstance(replay, dict)
        or replay.get("target_decision_hash")
        != manifest.get("decision_hash")
        or replay.get("status") != "clear"
        or replay.get("release_gate_passed") is not True
        or replay.get("issue_count") != 0
        or replay.get("eligible_unknown_count") != 0
        or not isinstance(replay_revalidation, dict)
        or replay_revalidation.get("target_decision_hash")
        != manifest.get("decision_hash")
        or replay_revalidation.get("trace_prefix_verified") is not True
        or manifest.get("launch_evidence") != launch_evidence
        or any(
            transition.get(field) != intent.get(field)
            for field in _ATTEMPT_BINDING_FIELDS
        )
        or transition.get("terminal_state_seq")
        != intent.get("terminal_state_seq")
        or transition.get("terminal_decision_id")
        != intent.get("terminal_decision_id")
        or transition.get("terminal_phase") != "GAME_OVER"
        or type(transition.get("menu_state_seq")) is not int
        or transition["menu_state_seq"] < 0
        or not isinstance(transition.get("menu_decision_id"), str)
        or not transition["menu_decision_id"]
        or not isinstance(evidence, dict)
        or set(evidence) != _RESTART_TRANSITION_EVIDENCE_FIELDS
        or evidence.get("schema_version") != 1
        or evidence.get("restart_intent_sha256")
        != predecessor["record"]["restart_intent_sha256"]
        or evidence.get("freeze_manifest_sha256")
        != resolution.get("freeze_manifest_sha256")
        or evidence.get("freeze_source_digest")
        != resolution.get("freeze_source_digest")
        or evidence.get("freeze_generated_at")
        != resolution.get("freeze_generated_at")
        or type(evidence.get("initial_menu_state_seq")) is not int
        or evidence["initial_menu_state_seq"] < 0
        or not isinstance(evidence.get("initial_menu_decision_id"), str)
        or not evidence["initial_menu_decision_id"]
        or evidence["initial_menu_state_seq"]
        <= intent.get("terminal_state_seq")
        or not isinstance(probe, dict)
        or set(probe) != _STATE_PROBE_FIELDS
        or not isinstance(probe.get("request_id"), str)
        or not probe["request_id"]
        or type(probe.get("accepted_state_seq")) is not int
        or probe["accepted_state_seq"]
        < evidence["initial_menu_state_seq"]
        or probe.get("result_state_seq")
        != transition.get("menu_state_seq")
        or probe["result_state_seq"] <= probe["accepted_state_seq"]
        or probe.get("result_decision_id")
        != transition.get("menu_decision_id")
        or str(probe.get("result_phase") or "").upper() != "MAIN_MENU"
        or probe.get("requested_target_id") != "action:state"
        or probe.get("resolved_target_id") != "action:state"
        or not isinstance(old_exit, dict)
        or set(old_exit)
        != {"record_path", "record_sha256", "finished_at", "exit_code"}
        or old_exit.get("record_path")
        != predecessor["record"]["old_launch_exit_record_path"]
        or old_exit.get("record_sha256")
        != predecessor["record"]["old_launch_exit_record_sha256"]
        or old_exit.get("finished_at")
        != real_old_exit.get("finished_at")
        or old_exit.get("exit_code") != real_old_exit.get("exit_code")
        or type(old_exit.get("exit_code")) is not int
        or not isinstance(replacement, dict)
        or set(replacement) != _REPLACEMENT_RUNTIME_FIELDS
        or replacement.get("launch_id")
        != predecessor["record"]["new_launch_id"]
        or replacement.get("launcher_pid")
        != predecessor["record"]["new_launcher_pid"]
        or replacement.get("launcher_pid")
        != predecessor["start"]["record"].get("launcher_pid")
        or replacement.get("java_pid")
        != predecessor["start"]["record"].get("java_pid")
        or replacement.get("launch_started_at")
        != predecessor["record"]["new_launch_started_at"]
        or any(
            type(replacement.get(field)) is not int
            or replacement[field] <= 0
            for field in ("launcher_pid", "java_pid", "bridge_pid")
        )
        or len({
            replacement["launcher_pid"],
            replacement["java_pid"],
            replacement["bridge_pid"],
        }) != 3
        or not isinstance(replacement.get("bridge_instance_token"), str)
        or not replacement["bridge_instance_token"]
        or not _valid_sha256(replacement.get("bridge_sha256"))
        or (
            replacement.get("bridge_runtime_source_digest")
            != intent.get("target_source_digest")
            and not source_reconciliation_valid
        )
        or replacement.get("bridge_runtime_source_file_count")
        != intent.get("target_source_file_count")
        or not isinstance(launch_evidence, dict)
        or set(launch_evidence) != _LAUNCH_EVIDENCE_FIELDS
        or launch_evidence.get("schema_version") != 1
        or launch_evidence.get("launch_id")
        != predecessor["record"]["new_launch_id"]
        or launch_evidence.get("launch_start_record_path")
        != predecessor["start"]["path"].relative_to(root).as_posix()
        or launch_evidence.get("launch_start_record_sha256")
        != predecessor["start"]["sha256"]
        or launch_evidence.get("launch_record_sha256")
        != predecessor["start"]["sha256"]
        or launch_evidence.get("launcher_pid")
        != replacement.get("launcher_pid")
        or launch_evidence.get("java_pid") != replacement.get("java_pid")
        or launch_evidence.get("bridge_pid")
        != replacement.get("bridge_pid")
        or launch_evidence.get("bridge_launch_id")
        != replacement.get("launch_id")
        or launch_evidence.get("bridge_instance_token")
        != replacement.get("bridge_instance_token")
        or launch_evidence.get("bridge_sha256")
        != replacement.get("bridge_sha256")
        or launch_evidence.get("bridge_runtime_source_digest")
        != replacement.get("bridge_runtime_source_digest")
        or launch_evidence.get("bridge_runtime_source_file_count")
        != replacement.get("bridge_runtime_source_file_count")
        or (
            launch_evidence.get("runtime_source_migration") is not None
            and not runtime_source_migration_valid
        )
        or launch_evidence.get("bridge_started_at")
        != replacement.get("bridge_started_at")
        or launch_evidence.get("root_communication_mod_jar_sha256")
        != launch_evidence.get("installed_communication_mod_jar_sha256")
        or launch_evidence.get("root_communication_mod_jar_sha256")
        != predecessor["start"]["record"].get(
            "root_communication_mod_jar_sha256"
        )
        or launch_evidence.get("installed_communication_mod_jar_sha256")
        != predecessor["start"]["record"].get(
            "installed_communication_mod_jar_sha256"
        )
        or launch_evidence.get("bridge_stderr_path")
        != predecessor["start"]["record"].get("bridge_stderr_path")
        or launch_evidence.get("bridge_stderr_start_offset")
        != predecessor["start"]["record"].get(
            "bridge_stderr_start_offset"
        )
        or launch_evidence.get("bridge_stderr_start_sha256")
        != predecessor["start"]["record"].get(
            "bridge_stderr_start_sha256"
        )
        or not isinstance(launch_evidence.get("bridge_stderr_path"), str)
        or not launch_evidence["bridge_stderr_path"]
        or any(
            type(launch_evidence.get(field)) is not int
            or launch_evidence[field] < 0
            for field in (
                "bridge_stderr_start_offset",
                "bridge_stderr_current_size",
                "bridge_stderr_delta_size",
            )
        )
        or any(
            not _valid_sha256(launch_evidence.get(field))
            for field in (
                "launch_record_sha256",
                "launch_start_record_sha256",
                "bridge_instance_sha256",
                "bridge_sha256",
                "bridge_runtime_source_digest",
                "root_communication_mod_jar_sha256",
                "installed_communication_mod_jar_sha256",
                "bridge_stderr_start_sha256",
                "bridge_stderr_current_sha256",
                "bridge_stderr_delta_sha256",
            )
        )
        or launch_evidence["bridge_stderr_current_size"]
        < launch_evidence["bridge_stderr_start_offset"]
        or launch_evidence["bridge_stderr_delta_size"]
        != (
            launch_evidence["bridge_stderr_current_size"]
            - launch_evidence["bridge_stderr_start_offset"]
        )
        or not isinstance(bridge_instance, dict)
        or set(bridge_instance) != _BRIDGE_INSTANCE_FIELDS
        or _canonical_sha256(bridge_instance)
        != launch_evidence.get("bridge_instance_sha256")
        or bridge_instance.get("schema_version") != 2
        or bridge_instance.get("protocol_version") != 2
        or bridge_instance.get("instance_token")
        != replacement.get("bridge_instance_token")
        or bridge_instance.get("bridge_pid")
        != replacement.get("bridge_pid")
        or bridge_instance.get("parent_java_pid")
        != replacement.get("java_pid")
        or bridge_instance.get("launch_id")
        != replacement.get("launch_id")
        or bridge_instance.get("bridge_sha256")
        != replacement.get("bridge_sha256")
        or bridge_instance.get("runtime_source_digest")
        != replacement.get("bridge_runtime_source_digest")
        or bridge_instance.get("runtime_source_file_count")
        != replacement.get("bridge_runtime_source_file_count")
        or bridge_instance.get("started_at")
        != replacement.get("bridge_started_at")
        or not isinstance(restart, dict)
        or set(restart) != _RESTART_PREDECESSOR_SUMMARY_FIELDS
        or restart.get("record_path")
        != predecessor["path"].relative_to(root).as_posix()
        or restart.get("record_sha256") != predecessor["sha256"]
        or restart.get("handoff_record_path")
        != predecessor["record"]["restart_handoff_record_path"]
        or restart.get("handoff_record_sha256")
        != predecessor["record"]["restart_handoff_record_sha256"]
        or restart.get("restart_intent_record_path")
        != predecessor["record"]["restart_intent_record_path"]
        or restart.get("restart_intent_sha256")
        != predecessor["record"]["restart_intent_sha256"]
        or restart.get("old_launch_exit_record_path")
        != predecessor["record"]["old_launch_exit_record_path"]
        or restart.get("old_launch_exit_record_sha256")
        != predecessor["record"]["old_launch_exit_record_sha256"]
    ):
        raise LaunchError("completed restart menu transition is invalid")
    transition_created_at = _finite_positive(
        transition.get("created_at"),
        "completed restart transition created_at",
    )
    freeze_generated_at = _finite_positive(
        evidence.get("freeze_generated_at"),
        "completed restart freeze generated_at",
    )
    old_finished_at = _finite_positive(
        old_exit.get("finished_at"),
        "completed restart old finished_at",
    )
    launch_started_at = _finite_positive(
        replacement.get("launch_started_at"),
        "completed restart launch started_at",
    )
    bridge_started_at = _finite_positive(
        replacement.get("bridge_started_at"),
        "completed restart bridge started_at",
    )
    resolution_created_at = _finite_positive(
        resolution.get("created_at"),
        "completed maintenance resolution created_at",
    )
    if not (
        intent["created_at"] < old_finished_at < launch_started_at
        <= bridge_started_at <= freeze_generated_at
        <= resolution_created_at <= transition_created_at
    ):
        raise LaunchError("completed restart transition times are unordered")
    return True


def _build_restart_predecessor(
    root, latest_launch_path, new_launch_record
):
    """Bind a prepared maintenance handoff to the next launch before Popen."""

    root = Path(root).resolve()
    new_launch_id = _safe_component(
        new_launch_record.get("launch_id"), "new launch_id"
    )
    new_started_at = _finite_positive(
        new_launch_record.get("started_at"), "new launch started_at"
    )
    if (
        not isinstance(new_launch_record.get("launch_log_path"), str)
        or not new_launch_record["launch_log_path"]
    ):
        raise LaunchError("new launch log path is invalid")
    previous = _read_json_object(
        latest_launch_path, "previous launch record", missing_ok=True
    )
    previous_launch_id = (
        previous.get("launch_id") if isinstance(previous, dict) else None
    )
    previous_is_safe = True
    if previous_launch_id is not None:
        try:
            _safe_component(previous_launch_id, "previous launch_id")
        except LaunchError:
            previous_is_safe = False

    direct_handoff = (
        maintenance_restart_handoff_path(root, previous_launch_id).resolve()
        if previous_is_safe and previous_launch_id is not None
        else None
    )
    previous_predecessor_path = (
        restart_predecessor_path(root, previous_launch_id).resolve()
        if previous_is_safe and previous_launch_id is not None
        else None
    )
    previous_has_anchor = (
        isinstance(previous, dict)
        and (
            previous.get("restart_predecessor_record_path") is not None
            or previous.get("restart_predecessor_record_sha256") is not None
        )
    )

    parent = None
    if direct_handoff is not None and direct_handoff.exists():
        root_documents = _validate_lineage_root_documents(
            root, direct_handoff
        )
        if previous != root_documents["old_exit"]["record"]:
            raise LaunchError(
                "latest launch differs from immutable old launch exit"
            )
    elif (
        previous_predecessor_path is not None
        and (previous_predecessor_path.exists() or previous_has_anchor)
    ):
        parent_record = _read_json_object(
            previous_predecessor_path,
            "previous replacement restart predecessor",
        )
        if (
            set(parent_record) != _RESTART_PREDECESSOR_FIELDS
            or parent_record.get("schema_version") != 2
            or parent_record.get("new_launch_id") != previous_launch_id
        ):
            raise LaunchError(
                "previous replacement has obsolete restart lineage"
            )
        old_launch_id = _safe_component(
            parent_record.get("old_launch_id"),
            "previous restart old launch_id",
        )
        handoff_path = _canonical_relative(
            root,
            parent_record.get("restart_handoff_record_path"),
            maintenance_restart_handoff_path(root, old_launch_id),
            "previous restart handoff path",
        )
        root_documents = _validate_lineage_root_documents(
            root, handoff_path
        )
        parent_tip = _validate_restart_lineage_record(
            root,
            previous_predecessor_path,
            root_documents["root_fields"],
        )
        parent_tip["root_intent"] = root_documents["intent"]
        if _restart_lineage_was_released(root, parent_tip):
            return None
        parent = _validate_restart_lineage_record(
            root,
            previous_predecessor_path,
            root_documents["root_fields"],
            require_exit=True,
            child_started_at=new_started_at,
        )
        if previous != parent["exit"]["record"]:
            raise LaunchError(
                "latest replacement differs from immutable exit"
            )
    else:
        old_launch_id = _pending_maintenance_restart_launch_id(root)
        if old_launch_id is None:
            return None
        if not previous_is_safe or previous_launch_id is None:
            raise LaunchError(
                "pending maintenance restart has no predecessor launch"
            )
        if previous_launch_id == old_launch_id:
            handoff_path = maintenance_restart_handoff_path(
                root, old_launch_id
            ).resolve()
            root_documents = _validate_lineage_root_documents(
                root, handoff_path
            )
            if previous != root_documents["old_exit"]["record"]:
                raise LaunchError(
                    "latest launch differs from immutable old launch exit"
                )
        else:
            raise LaunchError(
                "pending maintenance restart lost predecessor lineage"
            )

    lineage_root = root_documents["root_fields"]
    if root_documents["old_exit"]["finished_at"] >= new_started_at:
        raise LaunchError("new launch predates maintenance old exit")
    if parent is None:
        depth = 0
        parent_fields = {field: None for field in _RESTART_PARENT_FIELDS}
    else:
        depth = parent["record"]["lineage_depth"] + 1
        parent_fields = {
            "lineage_parent_record_path": (
                parent["path"].relative_to(root).as_posix()
            ),
            "lineage_parent_record_sha256": parent["sha256"],
            "lineage_parent_start_record_path": (
                parent["start"]["path"].relative_to(root).as_posix()
            ),
            "lineage_parent_start_record_sha256": parent["start"]["sha256"],
            "lineage_parent_exit_record_path": (
                parent["exit"]["path"].relative_to(root).as_posix()
            ),
            "lineage_parent_exit_record_sha256": parent["exit"]["sha256"],
            "lineage_parent_launch_id": parent["record"]["new_launch_id"],
            "lineage_parent_started_at": parent["start"]["started_at"],
            "lineage_parent_finished_at": parent["exit"]["finished_at"],
        }
    predecessor = {
        "schema_version": 2,
        "record_type": "restart_predecessor",
        "predecessor_status": "observed_before_new_runtime_launch",
        "lineage_depth": depth,
        "new_launch_id": new_launch_id,
        "new_launcher_pid": os.getpid(),
        "new_launch_started_at": new_started_at,
        "new_launch_log_path": new_launch_record["launch_log_path"],
        **lineage_root,
        **parent_fields,
    }
    if set(predecessor) != _RESTART_PREDECESSOR_FIELDS:
        raise LaunchError("restart predecessor construction is incomplete")
    return predecessor


def _write_json_once(path, payload):
    """Persist immutable launch evidence without replacing an old record."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json_bytes(payload)
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
                raise LaunchError(
                    f"immutable launch evidence is unreadable: {path}"
                ) from read_exc
            if existing != encoded:
                raise LaunchError(
                    f"immutable launch evidence already differs: {path}"
                ) from exc
    except OSError as exc:
        raise LaunchError(
            f"immutable launch evidence could not be written: {path}"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A valid final hard link is already durable; an orphaned uniquely
            # named staging link is harmless and never consumed as evidence.
            pass


def process_is_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as exc:
            # Access denied proves a process occupies the PID even though it
            # prevents obtaining a synchronization handle.  Fail closed.
            return getattr(exc, "winerror", None) == 5
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    return True


def _validate_interrupted_exit_recovery(
    root, launch_id, start_evidence, *, expected_finished_at=None,
):
    """Validate the immutable proof for one unobserved supervisor exit."""

    root = Path(root).resolve()
    recovery_path = interrupted_exit_recovery_path(root, launch_id).resolve()
    recovery = _read_json_object(
        recovery_path, "interrupted launch exit recovery"
    )
    handoff_path = maintenance_restart_handoff_path(
        root, launch_id
    ).resolve()
    handoff, intent = _validate_maintenance_restart_handoff(
        root, handoff_path
    )
    old = intent["old_runtime"]
    recovered_at = _finite_positive(
        recovery.get("recovered_at"), "interrupted exit recovered_at"
    )
    expected = {
        "schema_version": 1,
        "record_type": "interrupted_launch_exit_recovery",
        "recovery_status": "all_bound_processes_observed_dead",
        "launch_id": launch_id,
        "launch_start_record_path": (
            start_evidence["path"].relative_to(root).as_posix()
        ),
        "launch_start_record_sha256": start_evidence["sha256"],
        "maintenance_handoff_record_path": (
            handoff_path.relative_to(root).as_posix()
        ),
        "maintenance_handoff_record_sha256": _canonical_sha256(handoff),
        "maintenance_intent_record_path": handoff[
            "restart_intent_record_path"
        ],
        "maintenance_intent_record_sha256": handoff[
            "restart_intent_sha256"
        ],
        "launcher_pid": old["launcher_pid"],
        "java_pid": old["java_pid"],
        "bridge_pid": old["bridge_pid"],
        "launcher_alive": False,
        "java_alive": False,
        "bridge_alive": False,
        "exit_code_sentinel": INTERRUPTED_EXIT_CODE,
        "recovered_at": recovered_at,
    }
    if (
        set(recovery) != _INTERRUPTED_EXIT_RECOVERY_FIELDS
        or recovery != expected
        or handoff.get("old_launch_id") != launch_id
        or handoff.get("old_launch_start_record_path")
        != expected["launch_start_record_path"]
        or handoff.get("old_launch_start_record_sha256")
        != expected["launch_start_record_sha256"]
        or recovered_at <= _finite_positive(
            intent.get("created_at"), "maintenance restart intent created_at"
        )
        or (
            expected_finished_at is not None
            and recovered_at != expected_finished_at
        )
    ):
        raise LaunchError("interrupted launch exit recovery is invalid")
    return {
        "record": recovery,
        "path": recovery_path,
        "sha256": _canonical_sha256(recovery),
        "recovered_at": recovered_at,
    }


def _ready_runtime_bridge_instance(root, launch_id, start_evidence):
    """Return a bound bridge instance only after proving runtime readiness."""

    root = Path(root).resolve()
    bridge_path = (root / "bridge-instance.json").resolve()
    bridge = _read_json_object(
        bridge_path, "interrupted runtime bridge instance", missing_ok=True
    )
    if bridge is None:
        return None
    if set(bridge) != _BRIDGE_INSTANCE_FIELDS:
        raise LaunchError("interrupted runtime bridge instance is malformed")
    start = start_evidence["record"]
    bridge_pid = bridge.get("bridge_pid")
    parent_java_pid = bridge.get("parent_java_pid")
    if (
        bridge.get("schema_version") != 2
        or bridge.get("protocol_version") != 2
        or not isinstance(bridge.get("instance_token"), str)
        or not bridge["instance_token"]
        or type(bridge_pid) is not int
        or bridge_pid <= 0
        or type(parent_java_pid) is not int
        or parent_java_pid <= 0
        or bridge.get("launch_id") != launch_id
        or parent_java_pid != start.get("java_pid")
        or bridge_pid in {start.get("launcher_pid"), start.get("java_pid")}
        or not _valid_sha256(bridge.get("bridge_sha256"))
        or not _valid_sha256(bridge.get("runtime_source_digest"))
        or type(bridge.get("runtime_source_file_count")) is not int
        or bridge["runtime_source_file_count"] <= 0
    ):
        # A well-formed bridge for a different launch is stale evidence, not
        # proof that this incomplete launch reached readiness.
        if bridge.get("launch_id") != launch_id:
            return None
        raise LaunchError("interrupted runtime bridge instance is invalid")
    log_path = (root / start["launch_log_path"]).resolve()
    try:
        log = log_path.read_bytes()
    except OSError as exc:
        raise LaunchError(
            "interrupted runtime launch log is unreadable"
        ) from exc
    if not (
        b"Mod list:" in log
        and b"Begin patching..." in log
        and b"communicationmod.CommunicationMod> Received message from external process: ready"
        in log
    ):
        return None
    bridge_started_at = _finite_positive(
        bridge.get("started_at"), "interrupted runtime bridge started_at"
    )
    if bridge_started_at < start_evidence["started_at"]:
        raise LaunchError("interrupted runtime bridge started before launch")
    return bridge


def _validate_interrupted_runtime_exit_recovery(
    root, launch_id, start_evidence, *, expected_finished_at=None,
):
    """Validate proof that a ready runtime was interrupted after all PIDs died."""

    root = Path(root).resolve()
    recovery_path = interrupted_runtime_exit_recovery_path(
        root, launch_id
    ).resolve()
    recovery = _read_json_object(
        recovery_path, "interrupted runtime exit recovery"
    )
    start = start_evidence["record"]
    log_path = (root / start["launch_log_path"]).resolve()
    try:
        log = log_path.read_bytes()
    except OSError as exc:
        raise LaunchError(
            "interrupted runtime launch log is unreadable"
        ) from exc
    if not (
        b"Mod list:" in log
        and b"Begin patching..." in log
        and b"communicationmod.CommunicationMod> Received message from external process: ready"
        in log
    ):
        raise LaunchError("interrupted runtime readiness proof is missing")
    recovered_at = _finite_positive(
        recovery.get("recovered_at"), "interrupted runtime recovered_at"
    )
    expected = {
        "schema_version": 1,
        "record_type": "interrupted_runtime_exit_recovery",
        "recovery_status": "ready_runtime_after_bound_processes_dead",
        "launch_id": launch_id,
        "launch_start_record_path": (
            start_evidence["path"].relative_to(root).as_posix()
        ),
        "launch_start_record_sha256": start_evidence["sha256"],
        "launch_log_path": start["launch_log_path"],
        "launch_log_sha256": sha256_file(log_path),
        "bridge_instance_sha256": recovery.get("bridge_instance_sha256"),
        "bridge_sha256": recovery.get("bridge_sha256"),
        "launcher_pid": start["launcher_pid"],
        "java_pid": start["java_pid"],
        "bridge_pid": recovery.get("bridge_pid"),
        "parent_java_pid": recovery.get("parent_java_pid"),
        "launcher_alive": False,
        "java_alive": False,
        "bridge_alive": False,
        "exit_code_sentinel": INTERRUPTED_RUNTIME_EXIT_CODE,
        "recovered_at": recovered_at,
    }
    if (
        set(recovery) != _INTERRUPTED_RUNTIME_EXIT_RECOVERY_FIELDS
        or recovery != expected
        or not _valid_sha256(recovery.get("bridge_instance_sha256"))
        or not _valid_sha256(recovery.get("bridge_sha256"))
        or recovery.get("parent_java_pid") != start.get("java_pid")
        or type(recovery.get("bridge_pid")) is not int
        or recovery["bridge_pid"] <= 0
        or recovery["bridge_pid"] in {
            start.get("launcher_pid"), start.get("java_pid")
        }
        or recovered_at < start_evidence["started_at"]
        or (
            expected_finished_at is not None
            and recovered_at != expected_finished_at
        )
    ):
        raise LaunchError("interrupted runtime exit recovery is invalid")
    return {
        "record": recovery,
        "path": recovery_path,
        "sha256": _canonical_sha256(recovery),
        "recovered_at": recovered_at,
    }


def recover_interrupted_runtime_exit(
    root=ROOT, *, latest_launch_path=None,
    process_alive_fn=process_is_alive, clock=time.time,
):
    """Archive one ready runtime whose bound processes were intentionally stopped."""

    root = Path(root).resolve()
    latest_launch_path = Path(
        latest_launch_path or root / "launch-latest.json"
    ).resolve()
    latest = _read_json_object(
        latest_launch_path, "latest launch", missing_ok=True
    )
    if latest is None or "finished_at" in latest or "exit_code" in latest:
        return None
    launch_id = _safe_component(
        latest.get("launch_id"), "interrupted runtime launch_id"
    )
    start_evidence = _validate_launch_start_record(
        root, launch_id, require_predecessor=False, allow_legacy=True
    )
    if latest != start_evidence["record"]:
        raise LaunchError(
            "interrupted runtime latest launch differs from immutable start"
        )
    if maintenance_restart_handoff_path(root, launch_id).exists():
        raise LaunchError(
            "interrupted runtime launch unexpectedly has a maintenance handoff"
        )
    bridge = _ready_runtime_bridge_instance(root, launch_id, start_evidence)
    if bridge is None:
        raise LaunchError("incomplete launch has no proven runtime readiness")
    try:
        statuses = {
            label: bool(process_alive_fn(pid))
            for label, pid in (
                ("launcher", start_evidence["record"]["launcher_pid"]),
                ("java", start_evidence["record"]["java_pid"]),
                ("bridge", bridge["bridge_pid"]),
            )
        }
    except Exception as exc:
        raise LaunchError(
            "interrupted runtime process exit cannot be verified"
        ) from exc
    alive = sorted(label for label, value in statuses.items() if value)
    if alive:
        raise LaunchError(
            "interrupted runtime processes are still alive: "
            + ",".join(alive)
        )

    recovery_path = interrupted_runtime_exit_recovery_path(
        root, launch_id
    ).resolve()
    if recovery_path.exists():
        recovery_evidence = _validate_interrupted_runtime_exit_recovery(
            root, launch_id, start_evidence
        )
        recovered_at = recovery_evidence["recovered_at"]
    else:
        recovered_at = _finite_positive(
            clock(), "interrupted runtime recovered_at"
        )
        log_path = (root / start_evidence["record"]["launch_log_path"]).resolve()
        recovery = {
            "schema_version": 1,
            "record_type": "interrupted_runtime_exit_recovery",
            "recovery_status": "ready_runtime_after_bound_processes_dead",
            "launch_id": launch_id,
            "launch_start_record_path": (
                start_evidence["path"].relative_to(root).as_posix()
            ),
            "launch_start_record_sha256": start_evidence["sha256"],
            "launch_log_path": start_evidence["record"]["launch_log_path"],
            "launch_log_sha256": sha256_file(log_path),
            "bridge_instance_sha256": _canonical_sha256(bridge),
            "bridge_sha256": bridge["bridge_sha256"],
            "launcher_pid": start_evidence["record"]["launcher_pid"],
            "java_pid": start_evidence["record"]["java_pid"],
            "bridge_pid": bridge["bridge_pid"],
            "parent_java_pid": bridge["parent_java_pid"],
            "launcher_alive": False,
            "java_alive": False,
            "bridge_alive": False,
            "exit_code_sentinel": INTERRUPTED_RUNTIME_EXIT_CODE,
            "recovered_at": recovered_at,
        }
        _write_json_once(recovery_path, recovery)
        _validate_interrupted_runtime_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=recovered_at,
        )

    exit_path = root / LAUNCH_DIRECTORY / f"{launch_id}.exit.json"
    exit_record = {
        **start_evidence["record"],
        "launch_exit_record_path": exit_path.relative_to(root).as_posix(),
        "finished_at": recovered_at,
        "exit_code": INTERRUPTED_RUNTIME_EXIT_CODE,
    }
    _write_json_once(exit_path, exit_record)
    _validate_launch_exit_record(root, launch_id, start_evidence)
    _write_latest_launch(latest_launch_path, exit_record)
    return exit_record


def recover_failed_startup_exit(
    root=ROOT, *, latest_launch_path=None,
    process_alive_fn=process_is_alive, clock=time.time,
):
    """Archive one proven bootstrap failure so a replacement can launch."""

    root = Path(root).resolve()
    latest_launch_path = Path(
        latest_launch_path or root / "launch-latest.json"
    ).resolve()
    latest = _read_json_object(
        latest_launch_path, "latest launch", missing_ok=True
    )
    if latest is None or "finished_at" in latest or "exit_code" in latest:
        return None
    launch_id = _safe_component(
        latest.get("launch_id"), "failed startup launch_id"
    )
    start_evidence = _validate_launch_start_record(
        root, launch_id, require_predecessor=False, allow_legacy=True
    )
    if latest != start_evidence["record"]:
        raise LaunchError(
            "failed startup latest launch differs from immutable start"
        )
    if maintenance_restart_handoff_path(root, launch_id).exists():
        raise LaunchError(
            "failed startup launch unexpectedly has a maintenance handoff"
        )
    log_path = (root / start_evidence["record"]["launch_log_path"]).resolve()
    marker = _startup_failure_marker(log_path)
    if marker is None:
        raise LaunchError(
            "incomplete launch has no proven ModTheSpire bootstrap failure"
        )
    try:
        statuses = {
            label: bool(process_alive_fn(start_evidence["record"][field]))
            for label, field in (
                ("launcher", "launcher_pid"),
                ("java", "java_pid"),
            )
        }
    except Exception as exc:
        raise LaunchError(
            "failed startup process exit cannot be verified"
        ) from exc
    alive = sorted(label for label, value in statuses.items() if value)
    if alive:
        raise LaunchError(
            "failed startup processes are still alive: " + ",".join(alive)
        )

    recovery_path = failed_startup_exit_recovery_path(root, launch_id).resolve()
    if recovery_path.exists():
        recovery_evidence = _validate_failed_startup_exit_recovery(
            root, launch_id, start_evidence
        )
        recovered_at = recovery_evidence["recovered_at"]
    else:
        recovered_at = _finite_positive(
            clock(), "failed startup recovered_at"
        )
        recovery = {
            "schema_version": 1,
            "record_type": "failed_startup_exit_recovery",
            "recovery_status": (
                "bootstrap_failure_after_dead_bound_processes"
            ),
            "launch_id": launch_id,
            "launch_start_record_path": (
                start_evidence["path"].relative_to(root).as_posix()
            ),
            "launch_start_record_sha256": start_evidence["sha256"],
            "launch_log_path": start_evidence["record"]["launch_log_path"],
            "launch_log_sha256": sha256_file(log_path),
            "failure_marker": marker,
            "launcher_pid": start_evidence["record"]["launcher_pid"],
            "java_pid": start_evidence["record"]["java_pid"],
            "launcher_alive": False,
            "java_alive": False,
            "exit_code_sentinel": FAILED_STARTUP_EXIT_CODE,
            "recovered_at": recovered_at,
        }
        _write_json_once(recovery_path, recovery)
        _validate_failed_startup_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=recovered_at,
        )

    exit_path = root / LAUNCH_DIRECTORY / f"{launch_id}.exit.json"
    exit_record = {
        **start_evidence["record"],
        "launch_exit_record_path": exit_path.relative_to(root).as_posix(),
        "finished_at": recovered_at,
        "exit_code": FAILED_STARTUP_EXIT_CODE,
    }
    _write_json_once(exit_path, exit_record)
    _validate_launch_exit_record(root, launch_id, start_evidence)
    _write_latest_launch(latest_launch_path, exit_record)
    return exit_record


def recover_interrupted_maintenance_exit(
    root=ROOT, *, latest_launch_path=None,
    process_alive_fn=process_is_alive, clock=time.time,
):
    """Recover only a prepared maintenance exit whose three PIDs are dead."""

    root = Path(root).resolve()
    latest_launch_path = Path(
        latest_launch_path or root / "launch-latest.json"
    ).resolve()
    latest = _read_json_object(
        latest_launch_path, "latest launch", missing_ok=True
    )
    if latest is None or "finished_at" in latest or "exit_code" in latest:
        return None
    launch_id = _safe_component(
        latest.get("launch_id"), "interrupted maintenance launch_id"
    )
    start_evidence = _validate_launch_start_record(
        root, launch_id, require_predecessor=False, allow_legacy=True
    )
    if latest != start_evidence["record"]:
        raise LaunchError(
            "interrupted latest launch differs from immutable start"
        )
    handoff_path = maintenance_restart_handoff_path(
        root, launch_id
    ).resolve()
    if not handoff_path.is_file():
        # A runtime that reached bridge readiness can be intentionally stopped
        # during a controller/code restart without a maintenance handoff.  Use
        # the strict bridge/log/PID proof first; otherwise retain the narrower
        # bootstrap-failure recovery for launches that never became usable.
        bridge = _ready_runtime_bridge_instance(root, launch_id, start_evidence)
        if bridge is not None:
            return recover_interrupted_runtime_exit(
                root,
                latest_launch_path=latest_launch_path,
                process_alive_fn=process_alive_fn,
                clock=clock,
            )
        return recover_failed_startup_exit(
            root,
            latest_launch_path=latest_launch_path,
            process_alive_fn=process_alive_fn,
            clock=clock,
        )
    handoff, intent = _validate_maintenance_restart_handoff(
        root, handoff_path
    )
    old = intent["old_runtime"]
    if (
        old.get("launch_id") != launch_id
        or old.get("launcher_pid") != latest.get("launcher_pid")
        or old.get("java_pid") != latest.get("java_pid")
    ):
        raise LaunchError("interrupted launch process binding changed")
    try:
        statuses = {
            label: bool(process_alive_fn(old[field]))
            for label, field in (
                ("launcher", "launcher_pid"),
                ("java", "java_pid"),
                ("bridge", "bridge_pid"),
            )
        }
    except Exception as exc:
        raise LaunchError(
            "interrupted launch process exit cannot be verified"
        ) from exc
    alive = sorted(label for label, value in statuses.items() if value)
    if alive:
        raise LaunchError(
            "interrupted maintenance processes are still alive: "
            + ",".join(alive)
        )

    recovery_path = interrupted_exit_recovery_path(
        root, launch_id
    ).resolve()
    if recovery_path.exists():
        recovery_evidence = _validate_interrupted_exit_recovery(
            root, launch_id, start_evidence
        )
        recovered_at = recovery_evidence["recovered_at"]
    else:
        recovered_at = _finite_positive(
            clock(), "interrupted exit recovered_at"
        )
        recovery = {
            "schema_version": 1,
            "record_type": "interrupted_launch_exit_recovery",
            "recovery_status": "all_bound_processes_observed_dead",
            "launch_id": launch_id,
            "launch_start_record_path": (
                start_evidence["path"].relative_to(root).as_posix()
            ),
            "launch_start_record_sha256": start_evidence["sha256"],
            "maintenance_handoff_record_path": (
                handoff_path.relative_to(root).as_posix()
            ),
            "maintenance_handoff_record_sha256": _canonical_sha256(handoff),
            "maintenance_intent_record_path": handoff[
                "restart_intent_record_path"
            ],
            "maintenance_intent_record_sha256": handoff[
                "restart_intent_sha256"
            ],
            "launcher_pid": old["launcher_pid"],
            "java_pid": old["java_pid"],
            "bridge_pid": old["bridge_pid"],
            "launcher_alive": False,
            "java_alive": False,
            "bridge_alive": False,
            "exit_code_sentinel": INTERRUPTED_EXIT_CODE,
            "recovered_at": recovered_at,
        }
        _write_json_once(recovery_path, recovery)
        _validate_interrupted_exit_recovery(
            root, launch_id, start_evidence,
            expected_finished_at=recovered_at,
        )

    exit_path = root / LAUNCH_DIRECTORY / f"{launch_id}.exit.json"
    exit_record = {
        **start_evidence["record"],
        "launch_exit_record_path": exit_path.relative_to(root).as_posix(),
        "finished_at": recovered_at,
        "exit_code": INTERRUPTED_EXIT_CODE,
    }
    _write_json_once(exit_path, exit_record)
    _validate_launch_exit_record(root, launch_id, start_evidence)
    _write_latest_launch(latest_launch_path, exit_record)
    return exit_record


def _read_lease_owner(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError("active launch lease is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "lease_id", "launcher_pid", "started_at"
        }
        or value.get("schema_version") != 1
        or not isinstance(value.get("lease_id"), str)
        or not value["lease_id"]
        or type(value.get("launcher_pid")) is not int
        or value["launcher_pid"] <= 0
        or type(value.get("started_at")) not in {int, float}
    ):
        raise LaunchError("active launch lease is malformed")
    return value


def _read_lease_child(path, owner):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError("active launch child binding is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "lease_id", "launcher_pid", "launch_id",
            "java_pid", "bound_at",
        }
        or value.get("schema_version") != 1
        or value.get("lease_id") != owner.get("lease_id")
        or value.get("launcher_pid") != owner.get("launcher_pid")
        or not isinstance(value.get("launch_id"), str)
        or not value["launch_id"]
        or type(value.get("java_pid")) is not int
        or value["java_pid"] <= 0
        or type(value.get("bound_at")) not in {int, float}
    ):
        raise LaunchError("active launch child binding is malformed")
    return value


def acquire_launch_lease(
    root, *, process_alive_fn=process_is_alive, clock=time.time,
    lease_id=None,
):
    """Atomically exclude another launcher for the whole Java lifetime."""

    root = Path(root).resolve()
    directory = root / LAUNCH_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    active = directory / ACTIVE_LEASE_DIRECTORY
    lease_id = str(lease_id or uuid.uuid4())
    if not lease_id or any(character in lease_id for character in "\\/:"):
        raise LaunchError("launch lease id is unsafe")
    for _ in range(3):
        try:
            active.mkdir()
        except FileExistsError:
            owner = _read_lease_owner(active / "owner.json")
            try:
                launcher_alive = bool(
                    process_alive_fn(owner["launcher_pid"])
                )
            except Exception as exc:
                raise LaunchError(
                    "active launch lease process cannot be verified"
                ) from exc
            if launcher_alive:
                raise LaunchError("another launcher still owns the active lease")
            child = _read_lease_child(active / "child.json", owner)
            if child is None:
                raise LaunchError(
                    "orphaned launch lease has no provable child state"
                )
            try:
                java_alive = bool(process_alive_fn(child["java_pid"]))
            except Exception as exc:
                raise LaunchError(
                    "active launch child process cannot be verified"
                ) from exc
            if java_alive:
                raise LaunchError(
                    "orphaned launcher still has a live Java child"
                )
            stale = directory / f"stale-launch-lease-{owner['lease_id']}"
            if stale.exists():
                raise LaunchError("stale launch lease evidence already exists")
            try:
                os.replace(active, stale)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise LaunchError("stale launch lease cannot be preserved") from exc
            continue
        record = {
            "schema_version": 1,
            "lease_id": lease_id,
            "launcher_pid": os.getpid(),
            "started_at": float(clock()),
        }
        try:
            _write_json_once(active / "owner.json", record)
        except Exception:
            try:
                active.rmdir()
            except OSError:
                pass
            raise
        return {"directory": active, "owner": record}
    raise LaunchError("launch lease acquisition raced repeatedly")


def bind_launch_child(lease, *, launch_id, java_pid, bound_at):
    """Bind the held launcher lease to the one child returned by Popen."""

    active = Path(lease["directory"])
    owner = _read_lease_owner(active / "owner.json")
    if owner != lease.get("owner"):
        raise LaunchError("active launch lease ownership changed")
    if (
        not isinstance(launch_id, str) or not launch_id
        or type(java_pid) is not int or java_pid <= 0
        or java_pid == owner["launcher_pid"]
        or type(bound_at) not in {int, float}
    ):
        raise LaunchError("launch child binding is invalid")
    child = {
        "schema_version": 1,
        "lease_id": owner["lease_id"],
        "launcher_pid": owner["launcher_pid"],
        "launch_id": launch_id,
        "java_pid": java_pid,
        "bound_at": float(bound_at),
    }
    _write_json_once(active / "child.json", child)
    lease["child"] = child
    return child


def release_launch_lease(lease):
    active = Path(lease["directory"])
    owner_path = active / "owner.json"
    observed = _read_lease_owner(owner_path)
    if observed != lease.get("owner"):
        raise LaunchError("active launch lease ownership changed")
    child_path = active / "child.json"
    observed_child = _read_lease_child(child_path, observed)
    if observed_child != lease.get("child"):
        raise LaunchError("active launch child binding changed")
    try:
        if observed_child is not None:
            child_path.unlink()
        owner_path.unlink()
        active.rmdir()
    except OSError as exc:
        raise LaunchError("active launch lease could not be released") from exc


def allocate_launch_log(root=ROOT, *, now=None, launch_id=None):
    """Create one immutable log identity without truncating older evidence."""

    root = Path(root)
    timestamp = float(time.time() if now is None else now)
    launch_id = str(launch_id or uuid.uuid4())
    if not launch_id or any(character in launch_id for character in "\\/:"):
        raise ValueError("launch_id is not a safe filename component")
    directory = root / LAUNCH_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"launch-{int(timestamp * 1000)}-{launch_id}.log"
    if log_path.exists():
        raise FileExistsError(f"launch log already exists: {log_path}")
    return log_path, {
        "schema_version": SCHEMA_VERSION,
        "launch_id": launch_id,
        "started_at": timestamp,
        "launch_log_path": log_path.relative_to(root).as_posix(),
    }


def launch(
    root=ROOT,
    *,
    game_dir=GAME_DIR,
    java_path=None,
    mod_the_spire=MOD_THE_SPIRE,
    root_communication_mod=None,
    installed_communication_mod=None,
    bridge_stderr_path=None,
    latest_launch_path=None,
    popen_factory=subprocess.Popen,
    clock=time.time,
    launch_id=None,
    process_alive_fn=process_is_alive,
    allow_interrupted_maintenance_recovery=False,
):
    """Launch one provenance-bound game process and wait for its exit."""

    root = Path(root).resolve()
    game_dir = Path(game_dir).resolve()
    java_path = Path(java_path or game_dir / "jre" / "bin" / "java.exe")
    root_communication_mod = Path(
        root_communication_mod or root / "CommunicationMod.jar"
    ).resolve()
    installed_communication_mod = Path(
        installed_communication_mod
        or game_dir / "mods" / "CommunicationMod.jar"
    ).resolve()
    bridge_stderr_path = Path(
        bridge_stderr_path or game_dir / "communication_mod_errors.log"
    ).resolve()
    latest_launch_path = Path(
        latest_launch_path or root / "launch-latest.json"
    ).resolve()
    expected_paths = {
        "Java": (game_dir / "jre" / "bin" / "java.exe").resolve(),
        "repository CommunicationMod.jar": (
            root / "CommunicationMod.jar"
        ).resolve(),
        "installed CommunicationMod.jar": (
            game_dir / "mods" / "CommunicationMod.jar"
        ).resolve(),
        "bridge stderr": (
            game_dir / "communication_mod_errors.log"
        ).resolve(),
        "launch evidence": (root / "launch-latest.json").resolve(),
    }
    observed_paths = {
        "Java": java_path.resolve(),
        "repository CommunicationMod.jar": root_communication_mod,
        "installed CommunicationMod.jar": installed_communication_mod,
        "bridge stderr": bridge_stderr_path,
        "launch evidence": latest_launch_path,
    }
    for label, expected in expected_paths.items():
        if observed_paths[label] != expected:
            raise LaunchError(f"{label} path is not canonical")
    if allow_interrupted_maintenance_recovery:
        recover_interrupted_maintenance_exit(
            root,
            latest_launch_path=latest_launch_path,
            process_alive_fn=process_alive_fn,
            clock=clock,
        )
    root_jar_hash, installed_jar_hash = verify_communication_mod_jars(
        root_communication_mod, installed_communication_mod
    )
    _bridge_python_path, java_localappdata = prepare_bridge_runtime(root)
    lease = acquire_launch_lease(
        root, process_alive_fn=process_alive_fn, clock=clock
    )
    process = None
    process_waited = False
    try:
        stderr_offset, stderr_hash = stderr_start_snapshot(bridge_stderr_path)
        started_at = float(clock())
        log_path, launch_record = allocate_launch_log(
            root, now=started_at, launch_id=launch_id
        )
        start_path = root / LAUNCH_DIRECTORY / (
            f"{launch_record['launch_id']}.start.json"
        )
        exit_path = root / LAUNCH_DIRECTORY / (
            f"{launch_record['launch_id']}.exit.json"
        )
        predecessor_path = restart_predecessor_path(
            root, launch_record["launch_id"]
        )
        if (
            start_path.exists()
            or exit_path.exists()
            or predecessor_path.exists()
        ):
            raise LaunchError("immutable launch sidecar identity already exists")
        launch_record["launch_start_record_path"] = (
            start_path.relative_to(root).as_posix()
        )
        launch_record["restart_predecessor_record_path"] = None
        launch_record["restart_predecessor_record_sha256"] = None
        restart_predecessor = _build_restart_predecessor(
            root, latest_launch_path, launch_record
        )
        if restart_predecessor is not None:
            _write_json_once(predecessor_path, restart_predecessor)
            launch_record["restart_predecessor_record_path"] = (
                predecessor_path.relative_to(root).as_posix()
            )
            launch_record["restart_predecessor_record_sha256"] = (
                _canonical_sha256(restart_predecessor)
            )
        command = [
            str(java_path),
            "-jar",
            str(Path(mod_the_spire).resolve()),
            "--skip-intro",
            "--mods",
            "basemod,CommunicationMod",
        ]
        child_environment = os.environ.copy()
        if java_localappdata is not None:
            child_environment["LOCALAPPDATA"] = str(java_localappdata)
        child_environment["STS_LAUNCH_ID"] = launch_record["launch_id"]
        with log_path.open("x", encoding="utf-8") as log:
            process = popen_factory(
                command,
                cwd=game_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                env=child_environment,
            )
            java_pid = getattr(process, "pid", None)
            if type(java_pid) is not int or java_pid <= 0:
                raise LaunchError("launched Java process has no valid pid")
            bind_launch_child(
                lease,
                launch_id=launch_record["launch_id"],
                java_pid=java_pid,
                bound_at=started_at,
            )
            launch_record.update({
                "launcher_pid": os.getpid(),
                "java_pid": java_pid,
                "command": command,
                "game_dir": str(game_dir),
                "root_communication_mod_jar_sha256": root_jar_hash,
                "installed_communication_mod_jar_sha256": installed_jar_hash,
                "bridge_stderr_path": str(bridge_stderr_path),
                "bridge_stderr_start_offset": stderr_offset,
                "bridge_stderr_start_sha256": stderr_hash,
            })
            _write_json_once(start_path, launch_record)
            _write_latest_launch(latest_launch_path, launch_record)
            print(json.dumps(launch_record, ensure_ascii=False), flush=True)
            exit_code = process.wait()
            process_waited = True
        if type(exit_code) is not int:
            raise LaunchError("Java process returned no integer exit code")
        finished_record = {
            **launch_record,
            "launch_exit_record_path": exit_path.relative_to(root).as_posix(),
            "finished_at": float(clock()),
            "exit_code": exit_code,
        }
        _write_json_once(exit_path, finished_record)
        _write_latest_launch(latest_launch_path, finished_record)
        return exit_code
    except BaseException:
        # A provenance failure after Popen must not leave an untracked Java
        # process behind while releasing the single-launch lease.
        if process is not None and not process_waited:
            try:
                process.terminate()
                process.wait(timeout=10)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=10)
                except Exception as cleanup_error:
                    raise LaunchError(
                        "untracked Java process could not be stopped"
                    ) from cleanup_error
        raise
    finally:
        release_launch_lease(lease)


def main():
    return launch(
        ROOT,
        game_dir=GAME_DIR,
        java_path=JAVA,
        mod_the_spire=MOD_THE_SPIRE,
        root_communication_mod=ROOT_COMMUNICATION_MOD,
        installed_communication_mod=COMMUNICATION_MOD,
        bridge_stderr_path=BRIDGE_STDERR,
        latest_launch_path=LATEST_LAUNCH_PATH,
        allow_interrupted_maintenance_recovery=(
            os.environ.get(INTERRUPTED_EXIT_RECOVERY_ENV) == "1"
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
