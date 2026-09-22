"""Create and verify the immutable source/test checkpoint for a live cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCHEMA_VERSION = 1
STATIC_PREFLIGHT_SCHEMA_VERSION = 1
POLICY_VERSION = "fast-policy-v5"
_SOURCE_SUFFIXES = {".py", ".java", ".json", ".jsonl", ".txt", ".md"}
_SOURCE_ROOTS = (
    "tests",
    "docs",
    "prompts",
    "knowledge",
    "test_fixtures",
    "src/CommunicationMod-1.2.1/src",
    "src/spirecomm-master/spirecomm",
)
_EXCLUDED_PARTS = {
    ".git", "__pycache__", ".deepseek-advisor-cache", "logs",
}
_TOP_LEVEL_EXCLUDED = {
    "state.json", "state-meta.json", "action-receipt.json",
    "bridge-instance.json", "run-context.json", "run-result.json",
    "run-audit.json", "cohort-report.json", "cohort-review.json",
    "death-replay.json", "next-run-selection.json",
    "launch-latest.json",
    "freeze-manifest.json",
    "release-preflight.json",
    "fixed-seed-replay.json",
    "goal-progress.md",
}
_TOP_LEVEL_INCLUDED_BINARY = {"CommunicationMod.jar"}
_PROTECTED_RUNTIME_FILES = (
    "autoplay.log",
    "run-history.jsonl",
    "run-context.json",
    "run-result.json",
    "run-audit.json",
    "state.json",
    "state-meta.json",
    "bridge-instance.json",
    "next-run-selection.json",
    "cohort-report.json",
    "cohort-review.json",
    "death-replay.json",
    "launch-latest.json",
)
_PROTECTED_RUNTIME_DIRECTORIES = (".deepseek-advisor-cache",)
_RUNNING_LAUNCH_FIELDS = {
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
    "restart_predecessor_record_path",
    "restart_predecessor_record_sha256",
}
_BRIDGE_INSTANCE_FIELDS = {
    "schema_version", "protocol_version", "instance_token",
    "bridge_pid", "parent_java_pid", "launch_id", "bridge_sha256",
    "runtime_source_digest", "runtime_source_file_count", "started_at",
}
_RESOLUTION_INVARIANT_SOURCE_PATHS = {
    "src/spirecomm-master/spirecomm/ai/agent.py",
    "autoplay.py",
    "bridge.py",
    "decision_cases.py",
    "decision_case_replay.py",
    "tests/test_live_macro_decisions.py",
}
_VALIDATED_MANIFEST_CACHE = {}

# A running bridge does not execute the policy/audit modules.  When an audit
# repair lands while the game is parked on GAME_OVER, the bridge therefore
# remains safely usable for the one PROCEED transition even though its
# repository snapshot predates the repair.  Keep this migration deliberately
# narrow: changing the controller, bridge, launcher, Mod, or any other source
# still requires a fresh runtime before a freeze can be built.
_RUNTIME_SOURCE_MIGRATION_VERSION = "audit_repair_source_migration_v1"
_RUNTIME_SOURCE_MIGRATION_ALLOWED_PATHS = frozenset({
    "independent_oracle.py",
    "test_independent_oracle.py",
    "freeze_manifest.py",
    "campaign_attempt.py",
    "test_freeze_manifest.py",
    "test_campaign_attempt.py",
    # Audit-only repairs do not execute inside the already-running bridge.
    # Allow the exact auditor/predictor modules and their regression tests to
    # migrate a parked GAME_OVER runtime to a new maintenance freeze; the
    # next controller still binds to the new source digest before START.
    "strategy_audit.py",
    "test_strategy_audit.py",
    "src/spirecomm-master/spirecomm/ai/combat_predictor.py",
    "test_relic_strategy_regressions.py",
    "decision_case_resolution.py",
    "decision-case-replay.json",
    "decision-case-resolutions.json",
    "decision-case-trace-evidence.json",
    # The official quick-start may repair an interrupted maintenance-launch
    # supervisor only after proving the old launcher, Java, and bridge dead.
    # These modules execute before the replacement runtime is created.
    "launch_game.py",
    "quick_start.py",
    "test_launch_game.py",
    "test_quick_start.py",
})
# Preserve explicitly allowed legacy checkpoint paths and their relocated tests.
_RUNTIME_SOURCE_MIGRATION_ALLOWED_PATHS |= frozenset(
    "tests/" + path for path in _RUNTIME_SOURCE_MIGRATION_ALLOWED_PATHS
    if path.startswith("test_") and path.endswith(".py")
)
_PARKED_RUNTIME_SOURCE_SNAPSHOT_NAME = "maintenance-runtime-source-snapshot.json"
_MAINTENANCE_TARGET_SOURCE_SNAPSHOT_FIELDS = frozenset({
    "schema_version",
    "record_type",
    "snapshot_status",
    "attempt_id",
    "restart_intent_record_path",
    "restart_intent_sha256",
    "target_source_digest",
    "target_source_file_count",
    "sources",
    "reconstruction_authority",
    "reconstruction_commit",
    "reconstructed_paths",
    "decision_case_replay_generated_at",
    "decision_case_replay_sha256",
    "created_at",
})
_MAINTENANCE_TARGET_RECONSTRUCTED_PATHS = (
    "decision-case-replay.json",
    "freeze_manifest.py",
    "launch_game.py",
    "quick_start.py",
    "test_launch_game.py",
    "test_quick_start.py",
)
_RUNTIME_SOURCE_MIGRATION_FIELDS = frozenset({
    "version",
    "previous_source_digest",
    "previous_source_file_count",
    "previous_sources",
    "current_source_digest",
    "current_source_file_count",
    "changed_paths",
})


class FreezeManifestError(ValueError):
    pass


def _fresh_policy_hashes(root):
    """Recompute the policy identities from the checked-out production code."""

    root = Path(root).resolve()
    if root != Path(__file__).resolve().parent:
        raise FreezeManifestError(
            "policy fingerprints can only be computed from the repository root"
        )
    import autoplay

    return {
        "decision_hash": autoplay.decision_fingerprint(),
        "controller_hash": autoplay.controller_fingerprint(),
    }


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise FreezeManifestError(f"source is unreadable: {path}") from exc
    return digest.hexdigest()


def discover_sources(root):
    """Return the deterministic set whose mutation invalidates a cohort."""

    root = Path(root).resolve()
    paths = []
    for path in root.glob("*"):
        if (
            path.is_file()
            and path.name not in _TOP_LEVEL_EXCLUDED
            and (
                path.suffix.casefold() in {".py", ".json", ".txt", ".md"}
                or path.name in _TOP_LEVEL_INCLUDED_BINARY
            )
            and not path.name.startswith("e2e-")
        ):
            paths.append(path)
    for relative_root in _SOURCE_ROOTS:
        directory = root / relative_root
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.casefold() in _SOURCE_SUFFIXES
                and not any(part in _EXCLUDED_PARTS for part in path.parts)
            ):
                paths.append(path)
    unique = {path.resolve() for path in paths}
    return sorted(unique, key=lambda item: item.relative_to(root).as_posix())


def source_snapshot(root, paths=None):
    root = Path(root).resolve()
    paths = discover_sources(root) if paths is None else [Path(p).resolve() for p in paths]
    result = {}
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise FreezeManifestError("source escaped the repository") from exc
        stat = path.stat()
        result[relative] = {
            "sha256": _file_digest(path),
            "size": stat.st_size,
        }
    if not result:
        raise FreezeManifestError("freeze source set is empty")
    return result


def snapshot_digest(snapshot):
    return _digest_bytes(json.dumps(
        snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _runtime_source_changed_paths(previous, current):
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    if set(previous) != set(current):
        return None
    return sorted(
        path for path in current
        if previous.get(path) != current.get(path)
    )


def _validate_runtime_source_migration(
    migration, previous_digest, previous_count, current_sources,
):
    """Validate a one-transition audit-only source migration proof."""

    if (
        not isinstance(migration, dict)
        or set(migration) != _RUNTIME_SOURCE_MIGRATION_FIELDS
        or migration.get("version") != _RUNTIME_SOURCE_MIGRATION_VERSION
    ):
        return False
    previous = migration.get("previous_sources")
    if (
        not isinstance(previous, dict)
        or migration.get("previous_source_digest") != previous_digest
        or migration.get("previous_source_file_count") != previous_count
        or len(previous) != previous_count
        or snapshot_digest(previous) != previous_digest
        or migration.get("current_source_digest")
        != snapshot_digest(current_sources)
        or migration.get("current_source_file_count") != len(current_sources)
    ):
        return False
    changed = _runtime_source_changed_paths(previous, current_sources)
    return (
        isinstance(changed, list)
        and changed
        and changed == migration.get("changed_paths")
        and set(changed) <= _RUNTIME_SOURCE_MIGRATION_ALLOWED_PATHS
    )


def _maintenance_target_source_migration(root, current_sources):
    """Bind a pending restart intent to a reconstructed target snapshot.

    The replacement bridge may already attest the current repository, while
    the immutable restart intent necessarily names the source that existed
    before the replacement launch.  In that case the intent target, rather
    than the bridge snapshot, is the previous side of the one-step migration.
    """

    root = Path(root).resolve()
    try:
        result = _read_json_object(
            root / "run-result.json", "maintenance run result"
        )
        attempt_id = result.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return None
        attempts_root = (root / "logs" / "attempts").resolve()
        attempt_dir = (attempts_root / attempt_id).resolve()
        if attempt_dir.parent != attempts_root:
            return None
        if (attempt_dir / "menu-transition.json").exists():
            return None
        snapshot_path = attempt_dir / _PARKED_RUNTIME_SOURCE_SNAPSHOT_NAME
        intent_path = attempt_dir / "maintenance-restart-intent.json"
        snapshot = _read_json_object(
            snapshot_path, "maintenance target source snapshot"
        )
        intent = _read_json_object(
            intent_path, "maintenance restart intent"
        )
        intent_relative = intent_path.relative_to(root).as_posix()
        if (
            set(snapshot) != _MAINTENANCE_TARGET_SOURCE_SNAPSHOT_FIELDS
            or snapshot.get("schema_version") != 2
            or snapshot.get("record_type")
            != "maintenance_target_source_snapshot"
            or snapshot.get("snapshot_status")
            != "precommitted_target_digest_reconstructed"
            or snapshot.get("attempt_id") != attempt_id
            or snapshot.get("restart_intent_record_path")
            != intent_relative
            or snapshot.get("restart_intent_sha256")
            != _file_digest(intent_path)
            or snapshot.get("reconstruction_authority")
            != "precommitted_source_digest_v1"
            or snapshot.get("reconstructed_paths")
            != list(_MAINTENANCE_TARGET_RECONSTRUCTED_PATHS)
            or intent.get("schema_version") != 2
            or intent.get("record_type") != "maintenance_restart_intent"
            or intent.get("intent_status")
            != "prepared_before_runtime_restart"
            or intent.get("attempt_id") != attempt_id
        ):
            return None
        commit = snapshot.get("reconstruction_commit")
        if not (
            isinstance(commit, str)
            and len(commit) == 40
            and all(character in "0123456789abcdef" for character in commit)
        ):
            return None
        previous_sources = snapshot.get("sources")
        target_digest = snapshot.get("target_source_digest")
        target_count = snapshot.get("target_source_file_count")
        if (
            not isinstance(previous_sources, dict)
            or not _valid_sha256(target_digest)
            or type(target_count) is not int
            or target_count <= 0
            or len(previous_sources) != target_count
            or snapshot_digest(previous_sources) != target_digest
            or intent.get("target_source_digest") != target_digest
            or intent.get("target_source_file_count") != target_count
        ):
            return None
        for entry in previous_sources.values():
            if (
                not isinstance(entry, dict)
                or set(entry) != {"sha256", "size"}
                or not _valid_sha256(entry.get("sha256"))
                or type(entry.get("size")) is not int
                or entry["size"] < 0
            ):
                return None
        replay_entry = previous_sources.get("decision-case-replay.json")
        replay_generated_at = snapshot.get(
            "decision_case_replay_generated_at"
        )
        snapshot_created_at = snapshot.get("created_at")
        intent_created_at = intent.get("created_at")
        if (
            not isinstance(replay_entry, dict)
            or snapshot.get("decision_case_replay_sha256")
            != replay_entry.get("sha256")
            or type(replay_generated_at) not in {int, float}
            or not math.isfinite(float(replay_generated_at))
            or float(replay_generated_at) <= 0
            or type(snapshot_created_at) not in {int, float}
            or not math.isfinite(float(snapshot_created_at))
            or type(intent_created_at) not in {int, float}
            or not math.isfinite(float(intent_created_at))
            or float(snapshot_created_at) <= float(intent_created_at)
        ):
            return None
        migration = {
            "version": _RUNTIME_SOURCE_MIGRATION_VERSION,
            "previous_source_digest": target_digest,
            "previous_source_file_count": target_count,
            "previous_sources": previous_sources,
            "current_source_digest": snapshot_digest(current_sources),
            "current_source_file_count": len(current_sources),
            "changed_paths": _runtime_source_changed_paths(
                previous_sources, current_sources
            ),
        }
    except (FreezeManifestError, OSError, TypeError, ValueError):
        return None
    if _validate_runtime_source_migration(
        migration, target_digest, target_count, current_sources
    ):
        return migration
    return None


def _runtime_source_migration_for_launch(
    root, record, bridge_instance, current_sources,
):
    """Return a validated migration proof for an already-running bridge."""

    runtime_digest = bridge_instance.get("runtime_source_digest")
    runtime_count = bridge_instance.get("runtime_source_file_count")
    current_digest = snapshot_digest(current_sources)
    if runtime_digest == current_digest and runtime_count == len(current_sources):
        pending = _maintenance_target_source_migration(
            root, current_sources
        )
        if pending is not None:
            return pending
        try:
            prior = _read_json_object(
                Path(root) / "freeze-manifest.json",
                "prior freeze manifest",
            )
        except FreezeManifestError:
            return None
        prior_launch = prior.get("launch_evidence")
        prior_migration = (
            prior_launch.get("runtime_source_migration")
            if isinstance(prior_launch, dict)
            else None
        )
        previous_digest = (
            prior_migration.get("previous_source_digest")
            if isinstance(prior_migration, dict)
            else None
        )
        previous_count = (
            prior_migration.get("previous_source_file_count")
            if isinstance(prior_migration, dict)
            else None
        )
        if (
            prior.get("release_gate_passed") is True
            and prior.get("source_digest") == current_digest
            and prior.get("source_file_count") == len(current_sources)
            and isinstance(prior_launch, dict)
            and prior_launch.get("launch_id") == record.get("launch_id")
            and _validate_runtime_source_migration(
                prior_migration,
                previous_digest,
                previous_count,
                current_sources,
            )
        ):
            return prior_migration
        return None

    manifest_path = Path(root) / "freeze-manifest.json"
    try:
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        prior = None
    if not isinstance(prior, dict) or prior.get("release_gate_passed") is not True:
        return None
    prior_launch = prior.get("launch_evidence")
    if isinstance(prior_launch, dict):
        prior_migration = prior_launch.get("runtime_source_migration")
        if _validate_runtime_source_migration(
            prior_migration, runtime_digest, runtime_count, current_sources
        ):
            return prior_migration
    previous = prior.get("sources")
    if (
        not isinstance(previous, dict)
        or snapshot_digest(previous) != runtime_digest
        or len(previous) != runtime_count
    ):
        previous = None
    if (
        isinstance(previous, dict)
        and isinstance(prior_launch, dict)
        and prior_launch.get("launch_id") == record.get("launch_id")
    ):
        changed = _runtime_source_changed_paths(previous, current_sources)
        if changed and set(changed) <= _RUNTIME_SOURCE_MIGRATION_ALLOWED_PATHS:
            migration = {
                "version": _RUNTIME_SOURCE_MIGRATION_VERSION,
                "previous_source_digest": runtime_digest,
                "previous_source_file_count": runtime_count,
                "previous_sources": previous,
                "current_source_digest": current_digest,
                "current_source_file_count": len(current_sources),
                "changed_paths": changed,
            }
            if _validate_runtime_source_migration(
                migration, runtime_digest, runtime_count, current_sources
            ):
                return migration

    # A parked maintenance runtime may have loaded an audit-only source
    # snapshot just before the replay report was regenerated.  Accept one
    # explicit, attempt-bound migration proof for that runtime; all changed
    # paths still pass through the same fail-closed allowlist above.
    try:
        result = _read_json_object(
            Path(root) / "run-result.json", "maintenance run result"
        )
        attempt_id = result.get("attempt_id")
        snapshot_path = (
            Path(root) / "logs" / "attempts" / str(attempt_id)
            / _PARKED_RUNTIME_SOURCE_SNAPSHOT_NAME
        )
        snapshot = _read_json_object(
            snapshot_path, "parked runtime source snapshot"
        )
        intent = _read_json_object(
            snapshot_path.parent / "maintenance-restart-intent.json",
            "maintenance restart intent",
        )
        if (
            not isinstance(result, dict)
            or not isinstance(attempt_id, str)
            or not attempt_id
            or snapshot.get("schema_version") != 1
            or snapshot.get("record_type")
            != "maintenance_runtime_source_snapshot"
            or snapshot.get("attempt_id") != attempt_id
            or snapshot.get("launch_id") != record.get("launch_id")
            or snapshot.get("runtime_source_digest") != runtime_digest
            or snapshot.get("runtime_source_file_count") != runtime_count
            or intent.get("record_type")
            != "maintenance_restart_intent"
            or intent.get("attempt_id") != attempt_id
            or intent.get("target_source_digest") != runtime_digest
            or intent.get("target_source_file_count") != runtime_count
        ):
            return None
        previous_sources = snapshot.get("sources")
        migration = {
            "version": _RUNTIME_SOURCE_MIGRATION_VERSION,
            "previous_source_digest": runtime_digest,
            "previous_source_file_count": runtime_count,
            "previous_sources": previous_sources,
            "current_source_digest": current_digest,
            "current_source_file_count": len(current_sources),
            "changed_paths": _runtime_source_changed_paths(
                previous_sources, current_sources
            ),
        }
    except (FreezeManifestError, OSError, TypeError, ValueError):
        return None
    if _validate_runtime_source_migration(
        migration, runtime_digest, runtime_count, current_sources
    ):
        return migration
    return None


def runtime_artifact_snapshot(root):
    """Fingerprint mutable evidence before/after the pre-launch test suite.

    This expensive snapshot belongs to ``run_verification``.  Post-run
    ``validate_manifest`` intentionally validates the stored checkpoint and
    frozen sources without rereading the active historical trace.
    """

    root = Path(root).resolve()
    snapshot = {}
    for relative in _PROTECTED_RUNTIME_FILES:
        path = root / relative
        if not path.exists():
            snapshot[relative] = None
            continue
        stat = path.stat()
        entry = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        # Size/mtime can be restored after an in-place rewrite, so every
        # protected file -- including autoplay.log -- needs an exact digest.
        entry["sha256"] = _file_digest(path)
        snapshot[relative] = entry
    for relative in _PROTECTED_RUNTIME_DIRECTORIES:
        directory = root / relative
        if not directory.exists():
            snapshot[relative] = None
            continue
        files = {}
        for path in sorted(
            (item for item in directory.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(directory).as_posix(),
        ):
            files[path.relative_to(directory).as_posix()] = {
                "size": path.stat().st_size,
                "sha256": _file_digest(path),
            }
        snapshot[relative] = files
    return snapshot


def _completed_process_record(process):
    stdout = process.stdout if isinstance(process.stdout, str) else ""
    stderr = process.stderr if isinstance(process.stderr, str) else ""
    ran = re.search(r"Ran\s+(\d+)\s+tests?", stdout + "\n" + stderr)
    return {
        "returncode": process.returncode,
        "stdout_size": len(stdout.encode("utf-8")),
        "stderr_size": len(stderr.encode("utf-8")),
        "stdout_sha256": _digest_bytes(stdout.encode("utf-8")),
        "stderr_sha256": _digest_bytes(stderr.encode("utf-8")),
        "test_count": int(ran.group(1)) if ran else None,
    }


def _valid_sha256(value):
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_sha256(value):
    return _digest_bytes(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _read_json_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeManifestError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise FreezeManifestError(f"{label} is not an object")
    return value


def _stderr_evidence(path, start_offset, start_sha256):
    path = Path(path)
    if not path.is_file():
        raise FreezeManifestError("bridge stderr evidence file is missing")
    full = hashlib.sha256()
    prefix = hashlib.sha256()
    delta = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                block_start = size
                block_end = size + len(block)
                full.update(block)
                if block_start < start_offset:
                    prefix.update(block[:max(
                        0, min(len(block), start_offset - block_start)
                    )])
                if block_end > start_offset:
                    delta.update(block[max(
                        0, start_offset - block_start
                    ):])
                size = block_end
    except OSError as exc:
        raise FreezeManifestError("bridge stderr is unreadable") from exc
    if size < start_offset:
        raise FreezeManifestError("bridge stderr was truncated after launch")
    if prefix.hexdigest() != start_sha256:
        raise FreezeManifestError(
            "bridge stderr launch prefix changed"
        )
    delta_size = size - start_offset
    if delta_size:
        raise FreezeManifestError(
            "bridge stderr contains startup errors after launch"
        )
    return {
        "bridge_stderr_current_size": size,
        "bridge_stderr_current_sha256": full.hexdigest(),
        "bridge_stderr_delta_size": delta_size,
        "bridge_stderr_delta_sha256": delta.hexdigest(),
    }


def capture_launch_evidence(root):
    """Bind the frozen checkpoint to one exact, still-running Java launch."""

    root = Path(root).resolve()
    record = _read_json_object(root / "launch-latest.json", "launch record")
    if set(record) != _RUNNING_LAUNCH_FIELDS:
        raise FreezeManifestError(
            "launch record is not the exact running schema-v2 envelope"
        )
    if record.get("schema_version") != 2:
        raise FreezeManifestError("launch record schema_version is not 2")
    if not isinstance(record.get("launch_id"), str) or not record["launch_id"].strip():
        raise FreezeManifestError("launch record launch_id is missing")
    if type(record.get("java_pid")) is not int or record["java_pid"] <= 0:
        raise FreezeManifestError("launch record java_pid is invalid")
    if (
        type(record.get("launcher_pid")) is not int
        or record["launcher_pid"] <= 0
        or record["launcher_pid"] == record["java_pid"]
    ):
        raise FreezeManifestError("launch record launcher_pid is invalid")
    started_at = record.get("started_at")
    if (
        type(started_at) not in {int, float}
        or not math.isfinite(float(started_at))
        or float(started_at) <= 0
    ):
        raise FreezeManifestError("launch record started_at is invalid")
    command = record.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise FreezeManifestError("launch record command is invalid")
    game_dir_value = record.get("game_dir")
    if not isinstance(game_dir_value, str) or not game_dir_value.strip():
        raise FreezeManifestError("launch record game_dir is missing")
    game_dir = Path(game_dir_value)
    if not game_dir.is_absolute():
        raise FreezeManifestError("launch record game_dir is not absolute")
    expected_java = (game_dir / "jre" / "bin" / "java.exe").resolve()
    try:
        observed_java = Path(command[0]).resolve()
    except (OSError, RuntimeError) as exc:
        raise FreezeManifestError("launch record Java command is invalid") from exc
    if (
        observed_java != expected_java
        or len(command) != 6
        or command[1] != "-jar"
        or command[3:] != [
            "--skip-intro", "--mods", "basemod,CommunicationMod"
        ]
    ):
        raise FreezeManifestError("launch record command is not canonical")
    launch_log = record.get("launch_log_path")
    if not isinstance(launch_log, str) or not launch_log.strip():
        raise FreezeManifestError("launch log path is missing")
    launch_log_path = (root / launch_log).resolve()
    try:
        launch_log_path.relative_to((root / "logs" / "launches").resolve())
    except ValueError as exc:
        raise FreezeManifestError("launch log escaped immutable log root") from exc
    if not launch_log_path.is_file():
        raise FreezeManifestError("immutable launch log is missing")
    if (
        launch_log_path.suffix.casefold() != ".log"
        or not launch_log_path.name.startswith("launch-")
        or record["launch_id"] not in launch_log_path.stem
    ):
        raise FreezeManifestError("immutable launch log identity mismatch")
    start_record_value = record.get("launch_start_record_path")
    if not isinstance(start_record_value, str) or not start_record_value.strip():
        raise FreezeManifestError("immutable launch start record path is missing")
    start_record_path = (root / start_record_value).resolve()
    expected_start_path = (
        root / "logs" / "launches"
        / f"{record['launch_id']}.start.json"
    ).resolve()
    if start_record_path != expected_start_path:
        raise FreezeManifestError(
            "immutable launch start record path is not canonical"
        )
    start_record = _read_json_object(
        start_record_path, "immutable launch start record"
    )
    if start_record != record:
        raise FreezeManifestError(
            "launch latest differs from immutable launch start record"
        )
    predecessor_value = record.get("restart_predecessor_record_path")
    predecessor_sha256 = record.get("restart_predecessor_record_sha256")
    if predecessor_value is None or predecessor_sha256 is None:
        if predecessor_value is not None or predecessor_sha256 is not None:
            raise FreezeManifestError(
                "restart predecessor launch anchor is incomplete"
            )
    else:
        expected_predecessor = (
            root
            / "logs"
            / "launches"
            / f"{record['launch_id']}.restart-predecessor.json"
        ).resolve()
        predecessor_path = (root / predecessor_value).resolve()
        if (
            predecessor_value
            != expected_predecessor.relative_to(root).as_posix()
            or predecessor_path != expected_predecessor
        ):
            raise FreezeManifestError(
                "restart predecessor launch anchor path is not canonical"
            )
        predecessor = _read_json_object(
            predecessor_path, "restart predecessor launch anchor"
        )
        if (
            not _valid_sha256(predecessor_sha256)
            or _canonical_sha256(predecessor) != predecessor_sha256
        ):
            raise FreezeManifestError(
                "restart predecessor launch anchor changed"
            )
    start_record_sha256 = _canonical_sha256(start_record)
    bridge_instance = _read_json_object(
        root / "bridge-instance.json", "bridge instance"
    )
    if set(bridge_instance) != _BRIDGE_INSTANCE_FIELDS:
        raise FreezeManifestError(
            "bridge instance is not the exact schema-v2 envelope"
        )
    bridge_pid = bridge_instance.get("bridge_pid")
    bridge_started_at = bridge_instance.get("started_at")
    runtime_source_digest = bridge_instance.get("runtime_source_digest")
    runtime_source_file_count = bridge_instance.get(
        "runtime_source_file_count"
    )
    current_sources = source_snapshot(root)
    current_source_digest = snapshot_digest(current_sources)
    if (
        bridge_instance.get("schema_version") != 2
        or bridge_instance.get("protocol_version") != 2
        or bridge_instance.get("launch_id") != record["launch_id"]
        or bridge_instance.get("parent_java_pid") != record["java_pid"]
        or type(bridge_pid) is not int
        or bridge_pid <= 0
        or bridge_pid in {record["launcher_pid"], record["java_pid"]}
        or not isinstance(bridge_instance.get("instance_token"), str)
        or not bridge_instance["instance_token"].strip()
        or type(bridge_started_at) not in {int, float}
        or not math.isfinite(float(bridge_started_at))
        or float(bridge_started_at) < float(record["started_at"])
        or not _valid_sha256(bridge_instance.get("bridge_sha256"))
        or _file_digest(root / "bridge.py")
        != bridge_instance["bridge_sha256"]
    ):
        raise FreezeManifestError(
            "bridge instance does not belong to the active launch"
        )
    if (
        not _valid_sha256(runtime_source_digest)
        or type(runtime_source_file_count) is not int
    ):
        raise FreezeManifestError(
            "bridge runtime source snapshot differs from repository"
        )
    runtime_source_migration = _runtime_source_migration_for_launch(
        root, record, bridge_instance, current_sources
    )
    if (
        runtime_source_digest != current_source_digest
        or runtime_source_file_count != len(current_sources)
    ):
        if runtime_source_migration is None:
            raise FreezeManifestError(
                "bridge runtime source snapshot differs from repository"
            )
    bridge_instance_sha256 = _canonical_sha256(bridge_instance)

    root_jar = root / "CommunicationMod.jar"
    installed_jar = game_dir / "mods" / "CommunicationMod.jar"
    root_hash = _file_digest(root_jar)
    installed_hash = _file_digest(installed_jar)
    recorded_root_hash = record.get(
        "root_communication_mod_jar_sha256"
    )
    recorded_installed_hash = record.get(
        "installed_communication_mod_jar_sha256"
    )
    if (
        not _valid_sha256(recorded_root_hash)
        or not _valid_sha256(recorded_installed_hash)
        or root_hash != installed_hash
        or recorded_root_hash != root_hash
        or recorded_installed_hash != installed_hash
    ):
        raise FreezeManifestError(
            "CommunicationMod.jar runtime provenance mismatch"
        )
    stderr_path_value = record.get("bridge_stderr_path")
    if not isinstance(stderr_path_value, str) or not stderr_path_value.strip():
        raise FreezeManifestError("bridge stderr path is missing")
    stderr_path = Path(stderr_path_value)
    if (
        not stderr_path.is_absolute()
        or stderr_path.resolve()
        != (game_dir / "communication_mod_errors.log").resolve()
    ):
        raise FreezeManifestError("bridge stderr path is not canonical")
    start_offset = record.get("bridge_stderr_start_offset")
    start_hash = record.get("bridge_stderr_start_sha256")
    if type(start_offset) is not int or start_offset < 0:
        raise FreezeManifestError("bridge stderr start offset is invalid")
    if not _valid_sha256(start_hash):
        raise FreezeManifestError("bridge stderr start hash is invalid")
    stderr = _stderr_evidence(stderr_path, start_offset, start_hash)
    return {
        "schema_version": 1,
        "launch_record_sha256": start_record_sha256,
        "launch_start_record_path": start_record_value,
        "launch_start_record_sha256": start_record_sha256,
        "launch_id": record["launch_id"],
        "launcher_pid": record["launcher_pid"],
        "java_pid": record["java_pid"],
        "bridge_instance_sha256": bridge_instance_sha256,
        "bridge_instance_token": bridge_instance["instance_token"],
        "bridge_pid": bridge_pid,
        "bridge_launch_id": bridge_instance["launch_id"],
        "bridge_sha256": bridge_instance["bridge_sha256"],
        "bridge_runtime_source_digest": runtime_source_digest,
        "bridge_runtime_source_file_count": runtime_source_file_count,
        "runtime_source_migration": runtime_source_migration,
        "bridge_started_at": float(bridge_started_at),
        "root_communication_mod_jar_sha256": root_hash,
        "installed_communication_mod_jar_sha256": installed_hash,
        "bridge_stderr_path": str(stderr_path.resolve()),
        "bridge_stderr_start_offset": start_offset,
        "bridge_stderr_start_sha256": start_hash,
        **stderr,
    }


def _validate_process_evidence(value, label, *, require_tests=False):
    if not isinstance(value, dict) or value.get("returncode") != 0:
        raise FreezeManifestError(f"{label} did not pass")
    for field in ("stdout_size", "stderr_size"):
        observed = value.get(field)
        if type(observed) is not int or observed < 0:
            raise FreezeManifestError(f"{label} {field} is invalid")
    for field in ("stdout_sha256", "stderr_sha256"):
        if not _valid_sha256(value.get(field)):
            raise FreezeManifestError(f"{label} {field} is invalid")
    test_count = value.get("test_count")
    if require_tests:
        if type(test_count) is not int or test_count <= 0:
            raise FreezeManifestError("full test suite has no verified tests")
    elif test_count is not None:
        raise FreezeManifestError(f"{label} test_count must be null")
    return value


def _validate_replay_evidence(value, decision_hash):
    import decision_case_replay

    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("history_coverage_version") != 2
        or value.get("target_decision_hash") != decision_hash
        or "placeholder" in str(value.get("target_decision_hash", "")).casefold()
        or value.get("status") != "clear"
        or value.get("release_gate_passed") is not True
        or type(value.get("issue_count")) is not int
        or value["issue_count"] != 0
        or type(value.get("eligible_unknown_count")) is not int
        or value["eligible_unknown_count"] != 0
    ):
        raise FreezeManifestError("DecisionCase replay is not clear and bound")

    count_fields = (
        "source_case_count", "current_case_count", "historical_case_count",
        "audited_case_count", "classified_not_applicable_count",
        "resolved_case_count",
        "unresolved_case_count", "issue_case_count",
        "historical_audited_count",
        "historical_classified_not_applicable_count",
        "historical_resolved_count",
        "historical_unresolved_count", "historical_issue_case_count",
        "fixture_case_count", "fixture_audited_count",
    )
    if any(
        type(value.get(field)) is not int or value[field] < 0
        for field in count_fields
    ):
        raise FreezeManifestError("DecisionCase replay coverage counts are invalid")
    source_count = value["source_case_count"]
    historical_count = value["historical_case_count"]
    if source_count <= 0:
        raise FreezeManifestError(
            "DecisionCase replay cannot self-prove from fixtures or empty history"
        )
    if value["current_case_count"] + historical_count != source_count:
        raise FreezeManifestError("DecisionCase replay source coverage is incomplete")
    if (
        value["audited_case_count"]
        + value["classified_not_applicable_count"]
        + value["resolved_case_count"]
        + value["unresolved_case_count"]
        + value["issue_case_count"]
        != source_count
    ):
        raise FreezeManifestError("DecisionCase replay case accounting is incomplete")
    if (
        value["historical_audited_count"]
        + value["historical_classified_not_applicable_count"]
        + value["historical_resolved_count"]
        + value["historical_unresolved_count"]
        + value["historical_issue_case_count"]
        != historical_count
    ):
        raise FreezeManifestError(
            "DecisionCase replay historical accounting is incomplete"
        )
    if any(value[field] != 0 for field in (
        "unresolved_case_count", "issue_case_count",
        "historical_unresolved_count", "historical_issue_case_count",
    )):
        raise FreezeManifestError("DecisionCase replay has unresolved source cases")

    case_results = value.get("case_results")
    if not isinstance(case_results, list) or len(case_results) != source_count:
        raise FreezeManifestError("DecisionCase replay per-case evidence is incomplete")
    classification_counts = Counter()
    historical_classification_counts = Counter()
    not_applicable_reasons = Counter()
    historical_not_applicable_reasons = Counter()
    not_applicable_checks = Counter()
    historical_not_applicable_checks = Counter()
    observed_historical = 0
    for row in case_results:
        if not isinstance(row, dict) or type(row.get("historical")) is not bool:
            raise FreezeManifestError("DecisionCase replay case binding is invalid")
        classification = row.get("classification")
        if classification not in {"audited", "not_applicable", "resolved"}:
            raise FreezeManifestError("DecisionCase replay case remains unresolved")
        authority = row.get("authority")
        reason = row.get("reason")
        if not isinstance(authority, str) or not authority.strip():
            raise FreezeManifestError("DecisionCase replay case authority is missing")
        if not isinstance(reason, str) or not reason.strip():
            raise FreezeManifestError("DecisionCase replay case reason is missing")
        if row.get("issues") != [] or row.get("unknowns") != []:
            raise FreezeManifestError("DecisionCase replay case is not clear")
        checks = row.get("not_applicable_checks")
        if not isinstance(checks, list):
            raise FreezeManifestError(
                "DecisionCase replay not-applicable checks are malformed"
            )
        if classification == "not_applicable" and not checks:
            raise FreezeManifestError(
                "DecisionCase replay N/A case lacks field classification"
            )
        if classification == "resolved":
            resolution = row.get("resolution")
            original = (
                resolution.get("original")
                if isinstance(resolution, dict) else None
            )
            invariant = (
                resolution.get("invariant")
                if isinstance(resolution, dict) else None
            )
            original_problems = (
                original.get("problem_kinds")
                if isinstance(original, dict) else None
            )
            negative_problems = (
                invariant.get("negative_problem_kinds")
                if isinstance(invariant, dict) else None
            )
            invariant_sources = (
                invariant.get("source_sha256")
                if isinstance(invariant, dict) else None
            )
            selected_ids = (
                invariant.get("selected_choice_ids")
                if isinstance(invariant, dict) else None
            )
            if (
                not isinstance(resolution, dict)
                or resolution.get("authority_version")
                != "decision-case-current-regression-v1"
                or resolution.get("resolution_kind")
                != "resolved_by_current_regression"
                or resolution.get("target_decision_hash") != decision_hash
                or row.get("historical") is not True
                or type(row.get("case")) is not int
                or row.get("attempt_id") != resolution.get("attempt_id")
                or row.get("before_seq") != resolution.get("before_seq")
                or row.get("phase") != resolution.get("phase")
                or row.get("source_decision_hash")
                != resolution.get("source_decision_hash")
                or row.get("reason") != (
                    "resolved_by_current_regression:"
                    + str(resolution.get("fixture_id") or "")
                )
                or not isinstance(original, dict)
                or original.get("classification")
                not in {"unresolved", "audited_issues"}
                or not isinstance(original.get("issues"), list)
                or not isinstance(original.get("unknowns"), list)
                or any(
                    not isinstance(item, str) or not item
                    for item in original.get("issues", [])
                    + original.get("unknowns", [])
                )
                or not isinstance(original_problems, list)
                or not original_problems
                or original_problems != sorted(set(
                    str(item)
                    for item in original.get("issues", [])
                    + original.get("unknowns", [])
                ))
                or resolution.get("original_classification")
                != original.get("classification")
                or resolution.get("original_problem_kinds")
                != original_problems
                or not _valid_sha256(resolution.get("original_case_sha256"))
                or not _valid_sha256(resolution.get("trace_record_sha256"))
                or not _valid_sha256(resolution.get("trace_line_sha256"))
                or not _valid_sha256(resolution.get("fixture_sha256"))
                or not isinstance(resolution.get("fixture_id"), str)
                or not resolution["fixture_id"].strip()
                or type(resolution.get("trace_byte_offset")) is not int
                or resolution["trace_byte_offset"] < 0
                or not isinstance(invariant, dict)
                or invariant.get("status") != "clear"
                or invariant.get("fixture_id") != resolution.get("fixture_id")
                or invariant.get("fixture_sha256")
                != resolution.get("fixture_sha256")
                or not isinstance(invariant.get("producer_probe"), str)
                or not invariant["producer_probe"].strip()
                or not isinstance(invariant.get("resolution_class"), str)
                or not invariant["resolution_class"].strip()
                or not _valid_sha256(invariant.get("positive_case_sha256"))
                or not _valid_sha256(invariant.get("negative_case_sha256"))
                or not decision_case_replay.resolution_permutation_invariant_is_valid(
                    invariant
                )
                or not isinstance(selected_ids, list)
                or not selected_ids
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in selected_ids
                )
                or len(set(selected_ids)) != len(selected_ids)
                or invariant.get("negative_classification")
                not in {"unresolved", "audited_issues"}
                or not isinstance(negative_problems, list)
                or not negative_problems
                or any(
                    not isinstance(item, str) or not item
                    for item in negative_problems
                )
                or negative_problems != sorted(set(negative_problems))
                or not isinstance(invariant_sources, dict)
                or set(invariant_sources) != _RESOLUTION_INVARIANT_SOURCE_PATHS
                or any(
                    not _valid_sha256(digest)
                    for digest in invariant_sources.values()
                )
            ):
                raise FreezeManifestError(
                    "DecisionCase resolved evidence is incomplete"
                )
        for check in checks:
            if not isinstance(check, dict) or any(
                not isinstance(check.get(field), str)
                or not check[field].strip()
                for field in ("check", "authority", "reason")
            ):
                raise FreezeManifestError(
                    "DecisionCase replay N/A field lacks authority or reason"
                )
            not_applicable_checks[check["check"]] += 1
            if row["historical"]:
                historical_not_applicable_checks[check["check"]] += 1
        classification_counts[classification] += 1
        if row["historical"]:
            observed_historical += 1
            historical_classification_counts[classification] += 1
        if classification == "not_applicable":
            not_applicable_reasons[reason] += 1
            if row["historical"]:
                historical_not_applicable_reasons[reason] += 1
    if observed_historical != historical_count:
        raise FreezeManifestError("DecisionCase replay historical rows are incomplete")
    if (
        classification_counts["audited"] != value["audited_case_count"]
        or classification_counts["not_applicable"]
        != value["classified_not_applicable_count"]
        or classification_counts["resolved"]
        != value["resolved_case_count"]
        or historical_classification_counts["audited"]
        != value["historical_audited_count"]
        or historical_classification_counts["not_applicable"]
        != value["historical_classified_not_applicable_count"]
        or historical_classification_counts["resolved"]
        != value["historical_resolved_count"]
    ):
        raise FreezeManifestError("DecisionCase replay row counts do not match")

    resolution_evidence = value.get("resolution_evidence")
    resolved_count = value["resolved_case_count"]
    if not isinstance(resolution_evidence, dict):
        raise FreezeManifestError("DecisionCase resolution evidence is missing")
    if resolved_count:
        resolved_indexes = resolution_evidence.get("resolved_case_indexes")
        if (
            resolution_evidence.get("authority_version")
            != "decision-case-current-regression-v1"
            or resolution_evidence.get("target_decision_hash") != decision_hash
            or resolution_evidence.get("status") != "clear"
            or resolution_evidence.get("resolved_count") != resolved_count
            or resolution_evidence.get("resolution_failure_count") != 0
            or resolution_evidence.get("failures") != []
            or not _valid_sha256(
                resolution_evidence.get("source_case_corpus_sha256")
            )
            or not _valid_sha256(
                resolution_evidence.get("fixture_catalog_sha256")
            )
            or not _valid_sha256(
                resolution_evidence.get("trace_evidence_sha256")
            )
            or not _valid_sha256(
                resolution_evidence.get("resolution_document_sha256")
            )
            or not _valid_sha256(
                resolution_evidence.get("trace_prefix_sha256")
            )
            or type(resolution_evidence.get("trace_scan_byte_length")) is not int
            or resolution_evidence["trace_scan_byte_length"] <= 0
            or not isinstance(resolved_indexes, list)
            or len(resolved_indexes) != resolved_count
            or len(set(resolved_indexes)) != resolved_count
            or any(
                type(index) is not int
                or index < 0 or index >= source_count
                or case_results[index].get("classification") != "resolved"
                for index in resolved_indexes
            )
        ):
            raise FreezeManifestError(
                "DecisionCase resolution summary is not clear and bound"
            )
    elif (
        resolution_evidence.get("resolved_count") != 0
        or resolution_evidence.get("resolution_failure_count") != 0
        or resolution_evidence.get("failures") != []
        or resolution_evidence.get("status") not in {"clear", "not_applicable"}
    ):
        raise FreezeManifestError("DecisionCase resolution summary is invalid")

    expected_summaries = {
        "not_applicable_classification_counts": not_applicable_reasons,
        "historical_not_applicable_classification_counts": (
            historical_not_applicable_reasons
        ),
        "not_applicable_check_counts": not_applicable_checks,
        "historical_not_applicable_check_counts": (
            historical_not_applicable_checks
        ),
    }
    for field, expected in expected_summaries.items():
        observed = value.get(field)
        if not isinstance(observed, dict) or observed != dict(sorted(expected.items())):
            raise FreezeManifestError(
                "DecisionCase replay N/A summary does not match per-case evidence"
            )

    fixtures = value.get("fixture_results")
    if (
        value["fixture_case_count"] <= 0
        or value["fixture_audited_count"] != value["fixture_case_count"]
        or value.get("missing_fixture_phases") != []
        or not isinstance(fixtures, list)
        or len(fixtures) != value["fixture_case_count"]
    ):
        raise FreezeManifestError("DecisionCase replay fixture coverage is incomplete")
    for row in fixtures:
        if (
            not isinstance(row, dict)
            or row.get("classification") != "audited"
            or row.get("issues") != []
            or row.get("unknowns") != []
            or not isinstance(row.get("authority"), str)
            or not row["authority"].strip()
            or not isinstance(row.get("reason"), str)
            or not row["reason"].strip()
        ):
            raise FreezeManifestError("DecisionCase replay fixture is not audited")
    return value


def _live_revalidate_replay(root, value, decision_hash):
    """Recompute replay evidence once, before the source checkpoint freezes."""

    try:
        import decision_case_replay
        import decision_case_resolution

        evidence = decision_case_replay.revalidate_persisted_report(
            root,
            value,
            decision_hash,
            verify_trace_prefix=True,
        )
    except (
        OSError,
        ValueError,
        decision_case_replay.ReplayError,
        decision_case_resolution.ResolutionError,
    ) as exc:
        raise FreezeManifestError(
            f"DecisionCase live recomputation failed: {exc}"
        ) from exc
    return evidence


def _validate_replay_revalidation(value, report, root, decision_hash):
    """Bind the build-time recomputation to the now-frozen report file."""

    try:
        import decision_case_replay

        contract_sha256 = decision_case_replay.replay_contract_sha256(report)
        report_path = Path(root).resolve() / "decision-case-replay.json"
        raw_report = report_path.read_bytes()
        persisted = json.loads(raw_report.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FreezeManifestError(
            "frozen DecisionCase replay report is unreadable"
        ) from exc
    artifacts = value.get("artifact_sha256") if isinstance(value, dict) else None
    fixture_artifact = {"test_fixtures/decision-cases-v2.jsonl"}
    root_case_artifact = {"decision-cases.jsonl"}
    bundled_case_artifact = {"decision-case-corpus-sources.json"}
    resolution_artifacts = {
        "decision-case-resolutions.json",
        "test_fixtures/decision-case-resolution-invariants-v1.json",
        "decision-case-trace-evidence.json",
    }
    allowed_artifact_sets = {
        frozenset(fixture_artifact | root_case_artifact),
        frozenset(fixture_artifact | bundled_case_artifact),
        frozenset(
            fixture_artifact | root_case_artifact | resolution_artifacts
        ),
        frozenset(
            fixture_artifact | bundled_case_artifact | resolution_artifacts
        ),
    }
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("mode") != "live_recomputation"
        or value.get("target_decision_hash") != decision_hash
        or value.get("report_contract_sha256") != contract_sha256
        or value.get("persisted_report_sha256")
        != hashlib.sha256(raw_report).hexdigest()
        or persisted != report
        or not isinstance(artifacts, dict)
        or frozenset(artifacts) not in allowed_artifact_sets
        or any(not _valid_sha256(digest) for digest in artifacts.values())
        or value.get("case_corpus_mode")
        != "ordered_bounded_case_archives_v1"
        or not _valid_sha256(value.get("case_corpus_manifest_sha256"))
        or not _valid_sha256(
            value.get("case_corpus_source_manifest_sha256")
        )
        or type(value.get("case_corpus_source_count")) is not int
        or value["case_corpus_source_count"] <= 0
        or value.get("case_corpus_case_count")
        != report.get("source_case_count")
        or value.get("case_corpus_sha256")
        != report.get("source_case_corpus_sha256")
        or artifacts.get("decision-case-corpus-sources.json")
        != value.get("case_corpus_manifest_sha256")
        or type(value.get("trace_prefix_verified")) is not bool
        or (
            report.get("resolved_case_count", 0) > 0
            and value.get("trace_prefix_verified") is not True
        )
        or (
            report.get("resolved_case_count", 0) > 0
            and value.get("trace_evidence_mode") not in {
                "legacy_prefix_v1", "embedded_raw_lines_v1",
                "multi_source_embedded_gzip_v1",
            }
        )
    ):
        raise FreezeManifestError(
            "DecisionCase live recomputation binding is invalid"
        )
    for relative, expected in artifacts.items():
        path = Path(root).resolve() / Path(relative)
        if _file_digest(path) != expected:
            raise FreezeManifestError(
                f"DecisionCase replay artifact changed: {relative}"
            )
    if report.get("resolved_case_count", 0) > 0:
        evidence_path = (
            Path(root).resolve() / "decision-case-trace-evidence.json"
        )
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FreezeManifestError(
                "DecisionCase trace evidence is unreadable"
            ) from exc
        evidence_mode = (
            evidence.get("evidence_mode", "legacy_prefix_v1")
            if isinstance(evidence, dict) else None
        )
        if evidence_mode != value.get("trace_evidence_mode"):
            raise FreezeManifestError(
                "DecisionCase trace evidence mode binding is invalid"
            )
    return value


def run_verification(root, *, python_executable=None):
    """Run the mandatory full suite and whitespace gate without a shell."""

    root = Path(root).resolve()
    python_executable = str(python_executable or sys.executable)
    runtime_before = runtime_artifact_snapshot(root)
    test_environment = os.environ.copy()
    # Full verification is intentionally sequential and below-normal on
    # Windows so it does not compete aggressively with the game or desktop.
    for name in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        test_environment[name] = "1"
    low_priority = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    tests = subprocess.run(
        [python_executable, "-m", "unittest", "discover", "-p", "test_*.py"],
        cwd=root,
        env=test_environment,
        creationflags=low_priority,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    diff = subprocess.run(
        ["git", "diff", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    runtime_after = runtime_artifact_snapshot(root)
    return {
        "full_tests": _completed_process_record(tests),
        "git_diff_check": _completed_process_record(diff),
        "runtime_artifacts_unchanged": runtime_before == runtime_after,
        "runtime_artifacts_before_sha256": snapshot_digest(runtime_before),
        "runtime_artifacts_after_sha256": snapshot_digest(runtime_after),
    }


def _require_runtime_sources(snapshot):
    for required_source in (
        "launch_game.py", "bridge.py", "CommunicationMod.jar",
        "decision-case-replay.json",
    ):
        if required_source not in snapshot:
            raise FreezeManifestError(
                f"required runtime source is missing: {required_source}"
            )


def _validate_verification_evidence(verification):
    if not isinstance(verification, dict):
        raise FreezeManifestError("verification result is missing")
    tests = verification.get("full_tests")
    diff = verification.get("git_diff_check")
    _validate_process_evidence(tests, "full test suite", require_tests=True)
    _validate_process_evidence(diff, "git diff --check")
    if (
        verification.get("runtime_artifacts_unchanged") is not True
        or not _valid_sha256(
            verification.get("runtime_artifacts_before_sha256")
        )
        or verification.get("runtime_artifacts_before_sha256")
        != verification.get("runtime_artifacts_after_sha256")
    ):
        raise FreezeManifestError(
            "full test suite changed protected runtime evidence"
        )
    return verification


def build_static_preflight(
    root,
    decision_hash,
    controller_hash,
    *,
    verification=None,
    decision_case_replay=None,
    generated_at=None,
):
    """Run expensive release checks without requiring a live game runtime.

    The result is reusable only for the exact complete source snapshot.  A
    later freeze still re-proves the active Java/bridge launch before START.
    """

    if not isinstance(decision_hash, str) or not decision_hash.strip():
        raise FreezeManifestError("decision_hash is missing")
    if not isinstance(controller_hash, str) or not controller_hash.strip():
        raise FreezeManifestError("controller_hash is missing")
    current_hashes = _fresh_policy_hashes(root)
    if current_hashes != {
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
    }:
        raise FreezeManifestError(
            "requested policy hashes do not match fresh source fingerprints"
        )
    before = source_snapshot(root)
    _require_runtime_sources(before)
    _validate_replay_evidence(decision_case_replay, decision_hash)
    if verification is None:
        # The complete suite runs in a child process while the independent
        # DecisionCase oracle recomputes in this process.  Both are read-only
        # against the same source checkpoint, so serializing them only delays
        # cold starts without adding evidence.
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="static-preflight",
        ) as executor:
            verification_future = executor.submit(run_verification, root)
            replay_future = executor.submit(
                _live_revalidate_replay,
                root,
                decision_case_replay,
                decision_hash,
            )
            verification = verification_future.result()
            replay_revalidation = replay_future.result()
    else:
        replay_revalidation = _live_revalidate_replay(
            root, decision_case_replay, decision_hash
        )
    _validate_verification_evidence(verification)
    if before != source_snapshot(root):
        raise FreezeManifestError(
            "source changed while static preflight checks were running"
        )
    return {
        "schema_version": STATIC_PREFLIGHT_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "generated_at": time.time() if generated_at is None else generated_at,
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "source_digest": snapshot_digest(before),
        "source_file_count": len(before),
        "sources": before,
        "full_tests": verification["full_tests"],
        "git_diff_check": verification["git_diff_check"],
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": verification[
            "runtime_artifacts_before_sha256"
        ],
        "runtime_artifacts_after_sha256": verification[
            "runtime_artifacts_after_sha256"
        ],
        "decision_case_replay": decision_case_replay,
        "decision_case_replay_revalidation": replay_revalidation,
        "preflight_passed": True,
    }


def validate_static_preflight(
    preflight, root, decision_hash, controller_hash,
):
    """Fail closed unless a cached preflight matches every current source."""

    if not isinstance(preflight, dict):
        raise FreezeManifestError("static preflight is not an object")
    expected = {
        "schema_version": STATIC_PREFLIGHT_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "preflight_passed": True,
    }
    for field, wanted in expected.items():
        observed = preflight.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise FreezeManifestError(f"static preflight {field} mismatch")
    current_hashes = _fresh_policy_hashes(root)
    if current_hashes != {
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
    }:
        raise FreezeManifestError(
            "cached preflight policy hashes do not match fresh source fingerprints"
        )
    sources = preflight.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise FreezeManifestError("static preflight sources are missing")
    _require_runtime_sources(sources)
    current = source_snapshot(root)
    if (
        current != sources
        or snapshot_digest(current) != preflight.get("source_digest")
        or preflight.get("source_file_count") != len(current)
    ):
        raise FreezeManifestError(
            "source changed after the static preflight checkpoint"
        )
    verification = {
        "full_tests": preflight.get("full_tests"),
        "git_diff_check": preflight.get("git_diff_check"),
        "runtime_artifacts_unchanged": preflight.get(
            "runtime_artifacts_unchanged"
        ),
        "runtime_artifacts_before_sha256": preflight.get(
            "runtime_artifacts_before_sha256"
        ),
        "runtime_artifacts_after_sha256": preflight.get(
            "runtime_artifacts_after_sha256"
        ),
    }
    _validate_verification_evidence(verification)
    _validate_replay_evidence(
        preflight.get("decision_case_replay"), decision_hash
    )
    _validate_replay_revalidation(
        preflight.get("decision_case_replay_revalidation"),
        preflight.get("decision_case_replay"),
        root,
        decision_hash,
    )
    return preflight


def build_manifest(
    root,
    decision_hash,
    controller_hash,
    *,
    verification=None,
    decision_case_replay=None,
    static_preflight=None,
    generated_at=None,
):
    launch_before = capture_launch_evidence(root)
    if static_preflight is None:
        static_preflight = build_static_preflight(
            root,
            decision_hash,
            controller_hash,
            verification=verification,
            decision_case_replay=decision_case_replay,
        )
    else:
        if verification is not None or decision_case_replay is not None:
            raise FreezeManifestError(
                "cached static preflight cannot be mixed with new evidence"
            )
        validate_static_preflight(
            static_preflight, root, decision_hash, controller_hash
        )
    launch_after = capture_launch_evidence(root)
    if launch_before != launch_after:
        raise FreezeManifestError(
            "runtime launch changed while verification was running"
        )
    before = static_preflight["sources"]
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "generated_at": time.time() if generated_at is None else generated_at,
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "source_digest": snapshot_digest(before),
        "source_file_count": len(before),
        "sources": before,
        "full_tests": static_preflight["full_tests"],
        "git_diff_check": static_preflight["git_diff_check"],
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": static_preflight[
            "runtime_artifacts_before_sha256"
        ],
        "runtime_artifacts_after_sha256": static_preflight[
            "runtime_artifacts_after_sha256"
        ],
        "decision_case_replay": static_preflight["decision_case_replay"],
        "decision_case_replay_revalidation": static_preflight[
            "decision_case_replay_revalidation"
        ],
        "launch_evidence": launch_after,
        "release_gate_passed": True,
    }


def _validate_manifest_checkpoint(manifest, root, decision_hash, controller_hash):
    """Re-prove mutable source/runtime bindings for known-valid bytes."""

    if not isinstance(manifest, dict):
        raise FreezeManifestError("freeze manifest is not an object")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "release_gate_passed": True,
    }
    for field, wanted in expected.items():
        observed = manifest.get(field)
        if type(observed) is not type(wanted) or observed != wanted:
            raise FreezeManifestError(f"freeze manifest {field} mismatch")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise FreezeManifestError("freeze manifest sources are missing")
    for required_source in (
        "launch_game.py", "bridge.py", "CommunicationMod.jar",
        "decision-case-replay.json",
    ):
        if required_source not in sources:
            raise FreezeManifestError(
                f"freeze manifest lacks runtime source: {required_source}"
            )
    current = source_snapshot(root)
    if current != sources or snapshot_digest(current) != manifest.get("source_digest"):
        raise FreezeManifestError("source changed after the freeze checkpoint")
    if manifest.get("source_file_count") != len(current):
        raise FreezeManifestError("freeze source count mismatch")
    launch_evidence = manifest.get("launch_evidence")
    current_launch_evidence = capture_launch_evidence(root)
    if (
        not isinstance(launch_evidence, dict)
        or launch_evidence != current_launch_evidence
    ):
        raise FreezeManifestError(
            "runtime launch changed after the freeze checkpoint"
        )
    _validate_process_evidence(
        manifest.get("full_tests"), "full test suite", require_tests=True
    )
    _validate_process_evidence(
        manifest.get("git_diff_check"), "git diff --check"
    )
    if (
        manifest.get("runtime_artifacts_unchanged") is not True
        or not _valid_sha256(
            manifest.get("runtime_artifacts_before_sha256")
        )
        or manifest.get("runtime_artifacts_before_sha256")
        != manifest.get("runtime_artifacts_after_sha256")
    ):
        raise FreezeManifestError(
            "freeze evidence reports protected runtime pollution"
        )
    return manifest


def validate_manifest(manifest, root, decision_hash, controller_hash):
    _validate_manifest_checkpoint(
        manifest, root, decision_hash, controller_hash
    )
    _validate_replay_evidence(
        manifest.get("decision_case_replay"), decision_hash
    )
    _validate_replay_revalidation(
        manifest.get("decision_case_replay_revalidation"),
        manifest.get("decision_case_replay"),
        root,
        decision_hash,
    )
    return manifest


def load_validated_manifest(path, root, decision_hash, controller_hash):
    """Load a freeze with exact-byte semantic caching inside one process.

    Every call still hashes the complete manifest bytes and re-proves the
    current source tree plus live Java/bridge evidence.  Only the expensive
    replay semantics of identical, already-validated bytes are reused.
    """

    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise FreezeManifestError("freeze manifest is missing") from exc
    except OSError as exc:
        raise FreezeManifestError("freeze manifest is unreadable") from exc
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    cache_key = (
        str(path), str(Path(root).resolve()), decision_hash, controller_hash,
    )
    cached = _VALIDATED_MANIFEST_CACHE.get(cache_key)
    if cached is not None and cached["sha256"] == raw_sha256:
        manifest = cached["manifest"]
        return _validate_manifest_checkpoint(
            manifest, root, decision_hash, controller_hash
        )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeManifestError("freeze manifest is unreadable") from exc
    validate_manifest(manifest, root, decision_hash, controller_hash)
    _VALIDATED_MANIFEST_CACHE[cache_key] = {
        "sha256": raw_sha256,
        "manifest": manifest,
    }
    return manifest


def write_manifest(path, manifest):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--controller-hash", required=True)
    evidence = parser.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--decision-case-replay", type=Path)
    evidence.add_argument("--static-preflight", type=Path)
    parser.add_argument("--write", type=Path, default=Path("freeze-manifest.json"))
    args = parser.parse_args()
    try:
        if args.static_preflight is not None:
            preflight = json.loads(
                args.static_preflight.read_text(encoding="utf-8")
            )
            manifest = build_manifest(
                args.root,
                args.decision_hash,
                args.controller_hash,
                static_preflight=preflight,
            )
        else:
            replay = json.loads(
                args.decision_case_replay.read_text(encoding="utf-8")
            )
            manifest = build_manifest(
                args.root,
                args.decision_hash,
                args.controller_hash,
                decision_case_replay=replay,
            )
        write_manifest(args.write, manifest)
    except (OSError, json.JSONDecodeError, FreezeManifestError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
