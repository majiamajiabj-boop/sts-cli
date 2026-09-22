"""Prepare, launch and begin one release-gated campaign attempt quickly.

Expensive static verification runs before the game is launched and is cached
against the exact complete source snapshot.  Once the bridge is ready, only
runtime binding and the authoritative ``campaign_attempt.py run`` START path
remain on the launch-to-play critical path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import freeze_manifest
import quick_start


ROOT = Path(__file__).resolve().parent
PREFLIGHT_PATH = ROOT / "release-preflight.json"
REPLAY_PATH = ROOT / "decision-case-replay.json"
FREEZE_PATH = ROOT / "freeze-manifest.json"
DEFAULT_RUNTIME_TIMEOUT_SECONDS = 45.0
DEFAULT_START_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 0.05


class QuickCampaignError(RuntimeError):
    pass


def _read_json_object(
    path, label, *, missing_ok=False, transient_unreadable_ok=False,
):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise QuickCampaignError(f"{label} is missing")
    except (OSError, json.JSONDecodeError) as exc:
        if transient_unreadable_ok:
            return None
        raise QuickCampaignError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise QuickCampaignError(f"{label} is not an object")
    return value


def current_policy_hashes(root=ROOT):
    return freeze_manifest._fresh_policy_hashes(Path(root).resolve())


def _replay_log_path(root, *, now=None, token=None):
    root = Path(root).resolve()
    timestamp = float(time.time() if now is None else now)
    token = str(token or uuid.uuid4())
    directory = root / "logs" / "preflight"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"decision-replay-{int(timestamp * 1000)}-{token}.log"


def regenerate_replay(
    root,
    decision_hash,
    *,
    executable=None,
    run=subprocess.run,
    now=None,
    token=None,
):
    root = Path(root).resolve()
    executable = str(executable or sys.executable)
    log_path = _replay_log_path(root, now=now, token=token)
    command = [
        executable,
        str(root / "decision_case_replay.py"),
        "--decision-hash",
        decision_hash,
        "--write",
        str(root / "decision-case-replay.json"),
    ]
    with log_path.open("x", encoding="utf-8") as log:
        result = run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise QuickCampaignError(
            f"DecisionCase replay failed; see {log_path}"
        )
    return log_path


def prepare_static_preflight(
    root,
    decision_hash,
    controller_hash,
    *,
    preflight_path=None,
    replay_path=None,
    replay_runner=regenerate_replay,
    clock=time.monotonic,
):
    root = Path(root).resolve()
    preflight_path = Path(
        preflight_path or root / "release-preflight.json"
    )
    replay_path = Path(replay_path or root / "decision-case-replay.json")
    started = float(clock())
    cached = None
    cache_miss_reason = "preflight_missing"
    try:
        cached = _read_json_object(
            preflight_path, "static preflight", missing_ok=True
        )
        if cached is not None:
            freeze_manifest.validate_static_preflight(
                cached, root, decision_hash, controller_hash
            )
            return {
                "preflight": cached,
                "reused": True,
                "cache_miss_reason": None,
                "replay_regenerated": False,
                "elapsed_seconds": round(float(clock()) - started, 3),
            }
    except (QuickCampaignError, freeze_manifest.FreezeManifestError) as exc:
        cache_miss_reason = str(exc)

    replay = _read_json_object(
        replay_path, "DecisionCase replay", missing_ok=True
    )
    replay_regenerated = False
    try:
        freeze_manifest._validate_replay_evidence(replay, decision_hash)
    except freeze_manifest.FreezeManifestError:
        replay_runner(root, decision_hash)
        replay_regenerated = True
        replay = _read_json_object(replay_path, "DecisionCase replay")
        freeze_manifest._validate_replay_evidence(replay, decision_hash)

    preflight = freeze_manifest.build_static_preflight(
        root,
        decision_hash,
        controller_hash,
        decision_case_replay=replay,
    )
    freeze_manifest.write_manifest(preflight_path, preflight)
    return {
        "preflight": preflight,
        "reused": False,
        "cache_miss_reason": cache_miss_reason,
        "replay_regenerated": replay_regenerated,
        "elapsed_seconds": round(float(clock()) - started, 3),
    }


def finalize_runtime_freeze(
    root,
    decision_hash,
    controller_hash,
    preflight,
    *,
    freeze_path=None,
):
    root = Path(root).resolve()
    freeze_path = Path(freeze_path or root / "freeze-manifest.json")
    manifest = freeze_manifest.build_manifest(
        root,
        decision_hash,
        controller_hash,
        static_preflight=preflight,
    )
    freeze_manifest.write_manifest(freeze_path, manifest)
    freeze_manifest.validate_manifest(
        manifest, root, decision_hash, controller_hash
    )
    return manifest


def _campaign_log_path(root, *, now=None, token=None):
    root = Path(root).resolve()
    timestamp = float(time.time() if now is None else now)
    token = str(token or uuid.uuid4())
    directory = root / "logs" / "campaign-starts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"campaign-{int(timestamp * 1000)}-{token}.log"


def spawn_campaign_attempt(
    root,
    decision_hash,
    controller_hash,
    *,
    max_actions=5000,
    validation_batch=False,
    p0_only_batch=False,
    executable=None,
    popen_factory=subprocess.Popen,
    now=None,
    token=None,
):
    root = Path(root).resolve()
    executable = str(executable or sys.executable)
    log_path = _campaign_log_path(root, now=now, token=token)
    command = [
        executable,
        str(root / "campaign_attempt.py"),
        "run",
        "--root",
        str(root),
        "--decision-hash",
        decision_hash,
        "--controller-hash",
        controller_hash,
        "--max-actions",
        str(max_actions),
    ]
    if validation_batch:
        command.append("--validation-batch")
    if p0_only_batch:
        command.append("--p0-only-batch")
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
        process = popen_factory(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    except BaseException:
        log.close()
        raise
    log.close()
    return process, log_path


def wait_for_formal_start(
    root,
    process,
    decision_hash,
    controller_hash,
    *,
    previous_attempt_id=None,
    timeout_seconds=DEFAULT_START_TIMEOUT_SECONDS,
    poll_seconds=DEFAULT_POLL_SECONDS,
    clock=time.monotonic,
    sleep=time.sleep,
):
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise QuickCampaignError("timeout and poll interval must be positive")
    root = Path(root).resolve()
    started = float(clock())
    while True:
        context = _read_json_object(
            root / "run-context.json",
            "run context",
            missing_ok=True,
            transient_unreadable_ok=True,
        )
        state = _read_json_object(
            root / "state.json",
            "authoritative state",
            missing_ok=True,
            transient_unreadable_ok=True,
        )
        attempt_id = context.get("attempt_id") if context else None
        if (
            context is not None
            and state is not None
            and isinstance(attempt_id, str)
            and attempt_id
            and attempt_id != previous_attempt_id
            and context.get("schema_version") == 2
            and context.get("policy_version") == freeze_manifest.POLICY_VERSION
            and context.get("decision_hash") == decision_hash
            and context.get("controller_hash") == controller_hash
            and state.get("protocol_version") == 2
            and state.get("attempt_id") == attempt_id
            and state.get("selection_id") == context.get("selection_id")
            and state.get("in_game") is True
            and state.get("fast_mode") is True
            and str(state.get("phase") or "").upper() != "MAIN_MENU"
            and state.get("ready_for_command") is True
        ):
            game = state.get("game_state") or {}
            if game.get("class") != context.get("character"):
                raise QuickCampaignError(
                    "started state character differs from run context"
                )
            return {
                "attempt_id": attempt_id,
                "run_id": context.get("run_id"),
                "character": context.get("character"),
                "seed": context.get("seed"),
                "selection_id": context.get("selection_id"),
                "state_seq": state.get("state_seq"),
                "phase": state.get("phase"),
                "formal_start_wait_seconds": round(
                    float(clock()) - started, 3
                ),
            }
        if process.poll() is not None:
            raise QuickCampaignError(
                f"campaign_attempt.py exited before formal START "
                f"(code {process.returncode})"
            )
        if float(clock()) - started >= timeout_seconds:
            raise QuickCampaignError(
                f"formal START was not observed within {timeout_seconds:g}s"
            )
        sleep(poll_seconds)


def start_campaign(
    root=ROOT,
    *,
    max_actions=5000,
    validation_batch=False,
    p0_only_batch=False,
    runtime_timeout_seconds=DEFAULT_RUNTIME_TIMEOUT_SECONDS,
    start_timeout_seconds=DEFAULT_START_TIMEOUT_SECONDS,
    poll_seconds=DEFAULT_POLL_SECONDS,
    clock=time.monotonic,
):
    root = Path(root).resolve()
    overall_started = float(clock())
    hashes = current_policy_hashes(root)
    preflight = prepare_static_preflight(
        root,
        hashes["decision_hash"],
        hashes["controller_hash"],
        clock=clock,
    )
    launch_started = float(clock())
    runtime = quick_start.ensure_runtime(
        root,
        timeout_seconds=runtime_timeout_seconds,
        poll_seconds=poll_seconds,
        clock=clock,
    )
    state = _read_json_object(root / "state.json", "authoritative state")
    if (
        state.get("protocol_version") != 2
        or state.get("in_game") is not False
        or str(state.get("phase") or "").upper() != "MAIN_MENU"
        or state.get("ready_for_command") is not True
        or "start" not in {
            str(item).casefold() for item in state.get("legal_actions") or []
        }
    ):
        raise QuickCampaignError(
            "runtime is ready but not at a legal MAIN_MENU START state"
        )
    freeze_started = float(clock())
    manifest = finalize_runtime_freeze(
        root,
        hashes["decision_hash"],
        hashes["controller_hash"],
        preflight["preflight"],
    )
    freeze_elapsed = round(float(clock()) - freeze_started, 3)
    previous_context = _read_json_object(
        root / "run-context.json", "previous run context", missing_ok=True
    )
    previous_attempt_id = (
        previous_context.get("attempt_id") if previous_context else None
    )
    process, log_path = spawn_campaign_attempt(
        root,
        hashes["decision_hash"],
        hashes["controller_hash"],
        max_actions=max_actions,
        validation_batch=validation_batch,
        p0_only_batch=p0_only_batch,
    )
    started_attempt = wait_for_formal_start(
        root,
        process,
        hashes["decision_hash"],
        hashes["controller_hash"],
        previous_attempt_id=previous_attempt_id,
        timeout_seconds=start_timeout_seconds,
        poll_seconds=poll_seconds,
        clock=clock,
    )
    return {
        "status": "started",
        **hashes,
        "source_digest": manifest["source_digest"],
        "preflight_reused": preflight["reused"],
        "preflight_elapsed_seconds": preflight["elapsed_seconds"],
        "replay_regenerated": preflight["replay_regenerated"],
        "runtime_reused": runtime["reused"],
        "runtime_elapsed_seconds": runtime["elapsed_seconds"],
        "freeze_elapsed_seconds": freeze_elapsed,
        "launch_to_formal_start_seconds": round(
            float(clock()) - launch_started, 3
        ),
        "total_elapsed_seconds": round(float(clock()) - overall_started, 3),
        "campaign_pid": process.pid,
        "campaign_log": str(log_path.resolve()),
        **started_attempt,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run/reuse the static release preflight, quickly launch the "
            "game, then return after campaign_attempt.py formally STARTs."
        )
    )
    parser.add_argument("--max-actions", type=int, default=5000)
    parser.add_argument(
        "--validation-batch",
        action="store_true",
        help="run one explicit post-cohort six-run validation batch",
    )
    parser.add_argument(
        "--p0-only-batch",
        action="store_true",
        help="continue a test matrix after complete audits with no P0",
    )
    parser.add_argument(
        "--runtime-timeout",
        type=float,
        default=DEFAULT_RUNTIME_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--start-timeout", type=float, default=DEFAULT_START_TIMEOUT_SECONDS
    )
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        result = start_campaign(
            ROOT,
            max_actions=args.max_actions,
            validation_batch=args.validation_batch,
            p0_only_batch=args.p0_only_batch,
            runtime_timeout_seconds=args.runtime_timeout,
            start_timeout_seconds=args.start_timeout,
            poll_seconds=args.poll,
        )
    except (
        QuickCampaignError,
        quick_start.QuickStartError,
        freeze_manifest.FreezeManifestError,
    ) as exc:
        print(json.dumps({
            "status": "error",
            "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
