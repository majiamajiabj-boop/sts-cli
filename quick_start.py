"""Start the provenance-bound game runtime and return when it is usable.

``launch_game.py`` intentionally owns the launcher lease until Java exits.  It
is therefore the right long-lived supervisor but a poor interactive entry
point: a caller appears to hang for the whole game lifetime.  This module
starts that supervisor in the background and waits only for a fresh, fully
bound protocol-v2 bridge state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import freeze_manifest
import launch_game


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
BRIDGE_INSTANCE_PATH = ROOT / "bridge-instance.json"
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_POLL_SECONDS = 0.1


class QuickStartError(RuntimeError):
    """Raised when a usable runtime cannot be proved quickly and safely."""


def _read_json_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise QuickStartError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise QuickStartError(f"{label} is not an object")
    return value


def _sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise QuickStartError(f"source is unreadable: {path}") from exc
    return digest.hexdigest()


def _current_source_identity(root):
    snapshot = freeze_manifest.source_snapshot(root)
    return freeze_manifest.snapshot_digest(snapshot), len(snapshot)


def _active_lease(root):
    active = (
        Path(root) / launch_game.LAUNCH_DIRECTORY
        / launch_game.ACTIVE_LEASE_DIRECTORY
    )
    if not active.is_dir():
        return None, None, active
    try:
        owner = launch_game._read_lease_owner(active / "owner.json")
        child = launch_game._read_lease_child(active / "child.json", owner)
    except launch_game.LaunchError as exc:
        raise QuickStartError(str(exc)) from exc
    return owner, child, active


_STARTUP_FAILURE_MARKERS = (
    "Could not start external process.",
    "Cannot run program",
    "ProcessBuilder.start",
    "Access is denied",
    "拒绝访问",
)


def _launch_startup_failure(root, start_record):
    """Return a bounded diagnostic when Java could not spawn CommunicationMod."""

    value = start_record.get("launch_log_path")
    if not isinstance(value, str) or not value:
        raise QuickStartError("launch log path is missing")
    log_path = (Path(root) / value).resolve()
    launch_directory = (Path(root) / launch_game.LAUNCH_DIRECTORY).resolve()
    try:
        log_path.relative_to(launch_directory)
    except ValueError as exc:
        raise QuickStartError("launch log path is not canonical") from exc
    if not log_path.is_file():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise QuickStartError("launch diagnostics are unreadable") from exc
    marker = next(
        (candidate for candidate in _STARTUP_FAILURE_MARKERS if candidate in text),
        None,
    )
    if marker is None:
        return None
    relevant = [
        line.strip()
        for line in text.splitlines()
        if any(candidate in line for candidate in _STARTUP_FAILURE_MARKERS)
    ]
    detail = " | ".join(relevant[-4:])[:1200]
    return {
        "reason": "bridge_external_process_failed",
        "launch_log_path": str(log_path),
        "startup_failure_marker": marker,
        "startup_failure_detail": detail,
    }


def _failed_runtime_observation(root, owner, child, start_record):
    failure = _launch_startup_failure(root, start_record)
    if failure is None:
        return None
    state = _read_json_object(Path(root) / "state.json", "authoritative state")
    return {
        "status": "failed",
        **failure,
        "launch_id": child["launch_id"],
        "launcher_pid": owner["launcher_pid"],
        "java_pid": child["java_pid"],
        "phase": state.get("phase") if state else None,
        "in_game": state.get("in_game") if state else None,
        "ready_for_command": state.get("ready_for_command") if state else None,
    }


def _fail_fast_runtime(observed, terminate_fn):
    if (
        observed.get("in_game") is False
        and str(observed.get("phase") or "").upper() == "MAIN_MENU"
    ):
        terminate_fn(observed["java_pid"])
        action = "the failed MAIN_MENU Java child was stopped"
    else:
        action = "the Java child was left running because its state was not a safe MAIN_MENU"
    detail = observed.get("startup_failure_detail") or observed.get("reason")
    raise QuickStartError(
        f"CommunicationMod startup failed before bridge readiness ({detail}); {action}; "
        f"see {observed.get('launch_log_path')}"
    )


def inspect_runtime(
    root=ROOT, *, process_alive_fn=launch_game.process_is_alive,
    source_identity=None,
):
    """Return a fail-closed snapshot of the active launch, bridge and state."""

    root = Path(root).resolve()
    owner, child, _active = _active_lease(root)
    if owner is None:
        return {"status": "absent", "reason": "no_active_launch_lease"}
    try:
        launcher_alive = bool(process_alive_fn(owner["launcher_pid"]))
    except Exception as exc:
        raise QuickStartError("launcher liveness cannot be verified") from exc
    if not launcher_alive:
        if child is None:
            return {
                "status": "absent",
                "reason": "dead_unbound_launcher_lease",
            }
        try:
            java_alive = bool(process_alive_fn(child["java_pid"]))
        except Exception as exc:
            raise QuickStartError("Java liveness cannot be verified") from exc
        if java_alive:
            raise QuickStartError("dead launcher still has a live Java child")
        return {"status": "absent", "reason": "dead_launch_lease"}
    if child is None:
        return {
            "status": "starting",
            "reason": "launcher_has_not_bound_java",
            "launcher_pid": owner["launcher_pid"],
        }
    try:
        java_alive = bool(process_alive_fn(child["java_pid"]))
    except Exception as exc:
        raise QuickStartError("Java liveness cannot be verified") from exc
    if not java_alive:
        return {
            "status": "starting",
            "reason": "launcher_is_reaping_java",
            "launcher_pid": owner["launcher_pid"],
            "java_pid": child["java_pid"],
        }

    latest = _read_json_object(root / "launch-latest.json", "latest launch")
    if latest is None:
        return {"status": "starting", "reason": "launch_record_pending"}
    if (
        latest.get("launch_id") != child["launch_id"]
        or latest.get("launcher_pid") != owner["launcher_pid"]
        or latest.get("java_pid") != child["java_pid"]
        or "finished_at" in latest
        or "exit_code" in latest
    ):
        return {"status": "starting", "reason": "launch_record_pending"}
    start_value = latest.get("launch_start_record_path")
    if not isinstance(start_value, str) or not start_value:
        raise QuickStartError("launch start sidecar path is missing")
    start_path = (root / start_value).resolve()
    expected_start = (
        root / launch_game.LAUNCH_DIRECTORY
        / f"{child['launch_id']}.start.json"
    ).resolve()
    if start_path != expected_start:
        raise QuickStartError("launch start sidecar path is not canonical")
    start_record = _read_json_object(start_path, "launch start sidecar")
    if start_record != latest:
        raise QuickStartError("latest launch differs from immutable start sidecar")

    bridge_instance = _read_json_object(
        root / "bridge-instance.json", "bridge instance"
    )
    if bridge_instance is None:
        startup_failure = _failed_runtime_observation(
            root, owner, child, start_record
        )
        if startup_failure is not None:
            return startup_failure
        return {"status": "starting", "reason": "bridge_instance_pending"}
    bridge_pid = bridge_instance.get("bridge_pid")
    if (
        bridge_instance.get("schema_version") != 2
        or bridge_instance.get("protocol_version") != 2
        or bridge_instance.get("launch_id") != child["launch_id"]
        or bridge_instance.get("parent_java_pid") != child["java_pid"]
        or type(bridge_pid) is not int
        or bridge_pid <= 0
    ):
        startup_failure = _failed_runtime_observation(
            root, owner, child, start_record
        )
        if startup_failure is not None:
            return startup_failure
        return {"status": "starting", "reason": "bridge_binding_pending"}
    try:
        bridge_alive = bool(process_alive_fn(bridge_pid))
    except Exception as exc:
        raise QuickStartError("bridge liveness cannot be verified") from exc
    if not bridge_alive:
        startup_failure = _failed_runtime_observation(
            root, owner, child, start_record
        )
        if startup_failure is not None:
            return startup_failure
        return {"status": "starting", "reason": "bridge_process_pending"}
    stale_reason = None
    if bridge_instance.get("bridge_sha256") != _sha256_file(root / "bridge.py"):
        stale_reason = "active_bridge_loaded_different_bridge"
    current_digest, current_count = (
        source_identity or _current_source_identity(root)
    )
    if (
        bridge_instance.get("runtime_source_digest") != current_digest
        or bridge_instance.get("runtime_source_file_count") != current_count
    ):
        stale_reason = "active_bridge_loaded_different_source_tree"

    state_path = root / "state.json"
    state = _read_json_object(state_path, "authoritative state")
    if state is None:
        return {"status": "starting", "reason": "state_pending"}
    bridge_started = bridge_instance.get("started_at")
    try:
        state_mtime = state_path.stat().st_mtime
    except OSError as exc:
        raise QuickStartError("authoritative state timestamp is unreadable") from exc
    if (
        type(bridge_started) not in {int, float}
        or state_mtime < float(bridge_started)
        or state.get("protocol_version") != 2
        or type(state.get("state_seq")) is not int
        or state.get("state_seq") < 0
        or not isinstance(state.get("decision_id"), str)
        or not state["decision_id"]
        or state.get("ready_for_command") is not True
    ):
        return {"status": "starting", "reason": "fresh_ready_state_pending"}
    if stale_reason is not None:
        return {
            "status": "stale",
            "reason": stale_reason,
            "launch_id": child["launch_id"],
            "launcher_pid": owner["launcher_pid"],
            "java_pid": child["java_pid"],
            "bridge_pid": bridge_pid,
            "state_seq": state["state_seq"],
            "phase": state.get("phase"),
            "in_game": state.get("in_game"),
            "ready_for_command": state.get("ready_for_command"),
        }
    return {
        "status": "ready",
        "reason": "runtime_provenance_and_state_verified",
        "launch_id": child["launch_id"],
        "launcher_pid": owner["launcher_pid"],
        "java_pid": child["java_pid"],
        "bridge_pid": bridge_pid,
        "state_seq": state["state_seq"],
        "phase": state.get("phase"),
        "in_game": state.get("in_game"),
    }


def _quick_log_path(root, *, now=None, token=None):
    timestamp = float(time.time() if now is None else now)
    token = str(token or uuid.uuid4())
    directory = Path(root) / launch_game.LAUNCH_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"quick-start-{int(timestamp * 1000)}-{token}.log"


def spawn_supervisor(
    root=ROOT, *, popen_factory=subprocess.Popen, executable=None,
    now=None, token=None,
):
    """Start launch_game.py without keeping the interactive caller attached."""

    root = Path(root).resolve()
    executable = str(executable or sys.executable)
    log_path = _quick_log_path(root, now=now, token=token)
    command = [executable, str(root / "launch_game.py")]
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        start_new_session = True
    log = log_path.open("x", encoding="utf-8")
    try:
        environment = os.environ.copy()
        environment[launch_game.INTERRUPTED_EXIT_RECOVERY_ENV] = "1"
        process = popen_factory(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
            env=environment,
        )
    except BaseException:
        log.close()
        raise
    log.close()
    return process, log_path


def terminate_stale_java(java_pid):
    """Stop only the exact Java child proven by the active launch lease."""

    if type(java_pid) is not int or java_pid <= 0:
        raise QuickStartError("stale runtime Java pid is invalid")
    try:
        os.kill(java_pid, signal.SIGTERM)
    except OSError as exc:
        raise QuickStartError("stale game runtime could not be stopped") from exc


def ensure_runtime(
    root=ROOT, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    poll_seconds=DEFAULT_POLL_SECONDS, process_alive_fn=None,
    popen_factory=subprocess.Popen, clock=time.monotonic,
    sleep=time.sleep, terminate_fn=terminate_stale_java,
):
    root = Path(root).resolve()
    process_alive_fn = process_alive_fn or launch_game.process_is_alive
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise QuickStartError("timeout and poll interval must be positive")
    started = float(clock())
    source_identity = _current_source_identity(root)
    observed = inspect_runtime(
        root, process_alive_fn=process_alive_fn,
        source_identity=source_identity,
    )
    if observed["status"] == "failed":
        _fail_fast_runtime(observed, terminate_fn)
    process = None
    log_path = None
    reused = observed["status"] != "absent"
    restarted_stale_runtime = False
    if observed["status"] == "stale":
        if (
            observed.get("in_game") is not False
            or str(observed.get("phase") or "").upper() != "MAIN_MENU"
            or observed.get("ready_for_command") is not True
        ):
            raise QuickStartError(
                "stale runtime is not at a safe MAIN_MENU restart point"
            )
        terminate_fn(observed["java_pid"])
        restarted_stale_runtime = True
        reused = False
    if observed["status"] == "absent":
        process, log_path = spawn_supervisor(
            root, popen_factory=popen_factory
        )
        reused = False
    while True:
        observed = inspect_runtime(
            root, process_alive_fn=process_alive_fn,
            source_identity=source_identity,
        )
        if observed["status"] == "failed":
            _fail_fast_runtime(observed, terminate_fn)
        if observed["status"] == "absent" and process is None:
            # A launcher that was already shutting down can release its lease
            # between the first probe and this loop.  Recover immediately
            # instead of making the caller wait for the full timeout.
            process, log_path = spawn_supervisor(
                root, popen_factory=popen_factory
            )
            reused = False
            continue
        if observed["status"] == "ready":
            return {
                **observed,
                "reused": reused,
                "restarted_stale_runtime": restarted_stale_runtime,
                "elapsed_seconds": round(float(clock()) - started, 3),
                "supervisor_log": (
                    str(log_path.resolve()) if log_path is not None else None
                ),
            }
        elapsed = float(clock()) - started
        if elapsed >= timeout_seconds:
            raise QuickStartError(
                f"runtime was not ready within {timeout_seconds:g}s; "
                f"last state: {observed.get('reason')}"
            )
        if process is not None and process.poll() is not None:
            # Another racing quick-start may own the live runtime.  Only fail
            # when no launch survived this supervisor's exit.
            retry = inspect_runtime(
                root, process_alive_fn=process_alive_fn,
                source_identity=source_identity,
            )
            if retry["status"] == "failed":
                _fail_fast_runtime(retry, terminate_fn)
            if retry["status"] == "absent":
                raise QuickStartError(
                    "launch supervisor exited before runtime readiness; "
                    f"see {log_path}"
                )
        sleep(poll_seconds)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Start Slay the Spire in the background and return after the "
            "protocol-v2 bridge publishes a fresh ready state."
        )
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="maximum seconds to wait for a ready bridge (default: 45)",
    )
    parser.add_argument(
        "--poll", type=float, default=DEFAULT_POLL_SECONDS,
        help="state polling interval in seconds (default: 0.1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_args(argv)
    try:
        result = ensure_runtime(
            ROOT, timeout_seconds=arguments.timeout,
            poll_seconds=arguments.poll,
        )
    except QuickStartError as exc:
        print(json.dumps({
            "status": "error", "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
