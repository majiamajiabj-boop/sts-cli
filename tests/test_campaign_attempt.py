import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import autoplay_runner
import campaign_attempt
import campaign_selector
import freeze_manifest
import launch_game
import pre_run_binding
from tests.test_freeze_manifest import (
    bind_bridge_runtime_sources,
    install_replay_bundle,
)


EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def replay_evidence(decision_hash):
    return {
        "schema_version": 1,
        "history_coverage_version": 2,
        "target_decision_hash": decision_hash,
        "status": "clear",
        "release_gate_passed": True,
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "source_case_count": 1,
        "current_case_count": 1,
        "historical_case_count": 0,
        "audited_case_count": 1,
        "classified_not_applicable_count": 0,
        "resolved_case_count": 0,
        "unresolved_case_count": 0,
        "issue_case_count": 0,
        "historical_audited_count": 0,
        "historical_classified_not_applicable_count": 0,
        "historical_resolved_count": 0,
        "historical_unresolved_count": 0,
        "historical_issue_case_count": 0,
        "not_applicable_classification_counts": {},
        "historical_not_applicable_classification_counts": {},
        "not_applicable_check_counts": {},
        "historical_not_applicable_check_counts": {},
        "fixture_case_count": 1,
        "fixture_audited_count": 1,
        "missing_fixture_phases": [],
        "case_results": [{
            "historical": False,
            "classification": "audited",
            "authority": "canonical test authority",
            "reason": "independent semantic replay",
            "not_applicable_checks": [],
            "issues": [],
            "unknowns": [],
        }],
        "fixture_results": [{
            "classification": "audited",
            "authority": "canonical fixture authority",
            "reason": "independent fixture replay",
            "issues": [],
            "unknowns": [],
        }],
        "resolution_evidence": {
            "authority_version": None,
            "target_decision_hash": decision_hash,
            "status": "not_applicable",
            "resolved_count": 0,
            "resolution_failure_count": 0,
            "failures": [],
        },
    }


def verification():
    process = {
        "returncode": 0,
        "stdout_size": 0,
        "stderr_size": 0,
        "stdout_sha256": EMPTY_HASH,
        "stderr_sha256": EMPTY_HASH,
    }
    return {
        "full_tests": {**process, "test_count": 1},
        "git_diff_check": {**process, "test_count": None},
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": "e" * 64,
        "runtime_artifacts_after_sha256": "e" * 64,
    }


def install_freeze(
    root,
    decision_hash="decision-a",
    controller_hash="controller-a",
    *,
    launch_id="launch-test",
    launcher_pid=110,
    java_pid=111,
    bridge_pid=222,
    bridge_instance_token="bridge-test",
    started_at=10.0,
    bridge_started_at=11.0,
    generated_at=10.0,
):
    (root / "bridge.py").write_text("BRIDGE = 2\n", encoding="utf-8")
    (root / "launch_game.py").write_text(
        "LAUNCHER = 2\n", encoding="utf-8"
    )
    jar = b"communication-mod"
    (root / "CommunicationMod.jar").write_bytes(jar)
    game = root / "runtime-game"
    (game / "mods").mkdir(parents=True, exist_ok=True)
    (game / "jre" / "bin").mkdir(parents=True, exist_ok=True)
    (game / "mods" / "CommunicationMod.jar").write_bytes(jar)
    stderr = game / "communication_mod_errors.log"
    stderr.write_bytes(b"old-prefix")
    launch_log = root / "logs" / "launches" / f"launch-{launch_id}.log"
    launch_log.parent.mkdir(parents=True, exist_ok=True)
    launch_log.write_bytes(b"")
    jar_hash = hashlib.sha256(jar).hexdigest()
    launch = {
        "schema_version": 2,
        "launch_id": launch_id,
        "started_at": started_at,
        "launch_log_path": f"logs/launches/launch-{launch_id}.log",
        "launch_start_record_path": (
            f"logs/launches/{launch_id}.start.json"
        ),
        "launcher_pid": launcher_pid,
        "java_pid": java_pid,
        "command": [
            str((game / "jre" / "bin" / "java.exe").resolve()),
            "-jar", str((game / "ModTheSpire.jar").resolve()),
            "--skip-intro", "--mods", "basemod,CommunicationMod",
        ],
        "game_dir": str(game.resolve()),
        "root_communication_mod_jar_sha256": jar_hash,
        "installed_communication_mod_jar_sha256": jar_hash,
        "bridge_stderr_path": str(stderr.resolve()),
        "bridge_stderr_start_offset": len(b"old-prefix"),
        "bridge_stderr_start_sha256": hashlib.sha256(
            b"old-prefix"
        ).hexdigest(),
        "restart_predecessor_record_path": None,
        "restart_predecessor_record_sha256": None,
    }
    (root / launch["launch_start_record_path"]).write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "launch-latest.json").write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "bridge-instance.json").write_text(json.dumps({
        "schema_version": 2,
        "protocol_version": 2,
        "instance_token": bridge_instance_token,
        "bridge_pid": bridge_pid,
        "parent_java_pid": java_pid,
        "launch_id": launch_id,
        "bridge_sha256": hashlib.sha256(
            (root / "bridge.py").read_bytes()
        ).hexdigest(),
        "started_at": bridge_started_at,
    }), encoding="utf-8")
    # The orchestrator itself is part of the frozen top-level Python surface.
    (root / "campaign_attempt.py").write_text(
        "ORCHESTRATOR = 1\n", encoding="utf-8"
    )
    (root / "policy.py").write_text("POLICY = 1\n", encoding="utf-8")
    replay_report = install_replay_bundle(root, decision_hash)
    bind_bridge_runtime_sources(root)
    with patch.object(
        freeze_manifest,
        "_fresh_policy_hashes",
        return_value={
            "decision_hash": decision_hash,
            "controller_hash": controller_hash,
        },
    ):
        manifest = freeze_manifest.build_manifest(
            root,
            decision_hash,
            controller_hash,
            verification=verification(),
            decision_case_replay=replay_report,
            generated_at=generated_at,
        )
    freeze_manifest.write_manifest(root / "freeze-manifest.json", manifest)
    return manifest


def menu_state(seq=10, decision="menu-decision"):
    return {
        "protocol_version": 2,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": seq,
        "phase": "MAIN_MENU",
        "decision_id": decision,
        "available_commands": ["start", "state"],
        "legal_actions": ["start", "state"],
    }


def started_state(seq=11, character="IRONCLAD"):
    return {
        "protocol_version": 2,
        "in_game": True,
        "ready_for_command": True,
        "state_seq": seq,
        "phase": "NEOW",
        "decision_id": "neow-decision",
        "available_commands": ["choose", "state"],
        "legal_actions": ["choose", "state"],
        "key_system_unlocked": True,
        "ironclad_third_act_win": True,
        "silent_third_act_win": True,
        "defect_third_act_win": True,
        "game_state": {
            "class": character,
            "ascension_level": 0,
            "is_standard_run": True,
            "seed": 12345,
            "screen_type": "EVENT",
        },
    }


def install_start_acceptance(root, payload):
    selection = json.loads(
        (Path(root) / "next-run-selection.json").read_text(encoding="utf-8")
    )
    accepted_menu = {
        "protocol_version": 2,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": payload["expected_seq"],
        "phase": payload["phase"],
        "decision_id": payload["decision_id"],
    }
    record = pre_run_binding.build_start_acceptance_record(
        payload, accepted_menu, selection, created_at=10.0
    )
    path = pre_run_binding.start_acceptance_path(
        root, selection["selection_id"]
    )
    pre_run_binding._write_json_once_idempotent(path, record)
    return record


def start_receipt(payload, root, *, result_seq=None):
    if payload.get("action") == "state":
        accepted = payload["expected_seq"]
        return {
            "request_id": payload["id"],
            "success": True,
            "status": "succeeded",
            "error": None,
            "accepted_state_seq": accepted,
            "result_state_seq": accepted + 1,
            "requested_target_id": "action:state",
            "resolved_target_id": "action:state",
        }
    acceptance = install_start_acceptance(root, payload)
    if result_seq is None:
        result_seq = payload["expected_seq"] + 1
    return {
        "request_id": payload["id"],
        "success": True,
        "status": "succeeded",
        "error": None,
        "accepted_state_seq": payload["expected_seq"],
        "result_state_seq": result_seq,
        "requested_target_id": payload["target_id"],
        "resolved_target_id": payload["target_id"],
        "selection_digest": payload["selection_digest"],
        "start_acceptance_sha256": (
            pre_run_binding.start_acceptance_sha256(acceptance)
        ),
    }


class StateSequence:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    def __call__(self):
        if self.calls >= len(self.states):
            raise AssertionError("unexpected authoritative state read")
        value = self.states[self.calls]
        self.calls += 1
        return value


def result_record(*, heart=False):
    character = "IRONCLAD"
    return {
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "attempt_id": "attempt-a",
        "run_id": f"{character}:0:12345",
        "seed": 12345,
        "goal_mode": "HEART",
        "character": character,
        "class": character,
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": "decision-a",
        "controller_hash": "controller-a",
        "selection_id": "selection-a",
        "selection_digest": "d" * 64,
        "state_seq": 100,
        "terminal_state_seq": 100,
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "victory": heart,
        "heart_defeated": heart,
    }


def attempt_binding(result):
    return {
        field: result[field]
        for field in campaign_attempt.ATTEMPT_BINDING_FIELDS
    }


def terminal_state(result):
    binding = attempt_binding(result)
    return {
        "protocol_version": 2,
        "in_game": True,
        "ready_for_command": True,
        "state_seq": result["terminal_state_seq"],
        "terminal_state_seq": result["terminal_state_seq"],
        "phase": "GAME_OVER",
        "decision_id": "terminal-decision",
        "available_commands": ["proceed", "state"],
        "legal_actions": ["proceed", "state"],
        **binding,
        "game_state": {
            "screen_type": "GAME_OVER",
            "screen_state": {"victory": result["victory"]},
            "class": result["character"],
            "ascension_level": 0,
            "seed": result["seed"],
            "is_standard_run": True,
            "run_victory": result["victory"],
            "heart_defeated": result["heart_defeated"],
        },
    }


def clean_exit(result):
    return {
        "record_type": "controller_exit",
        "controller_exit_status": "clear",
        "schema_version": 2,
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
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "exit_code": 0,
        "stdout_size": 1,
        "stdout_sha256": "a" * 64,
        "stderr_size": 0,
        "stderr_sha256": EMPTY_HASH,
        "stdout_line_count": 1,
        "stdout_semantic_sha256": "b" * 64,
        "freeze_manifest_sha256": "c" * 64,
        "freeze_source_digest": "d" * 64,
        "freeze_generated_at": 10.0,
    }


def proceed_receipt(payload, *, result_seq=101):
    return {
        "request_id": payload["id"],
        "success": True,
        "status": "succeeded",
        "error": None,
        "accepted_state_seq": payload["expected_seq"],
        "result_state_seq": result_seq,
        "requested_target_id": "action:proceed",
        "resolved_target_id": "action:proceed",
        **{
            field: payload[field]
            for field in campaign_attempt.ATTEMPT_BINDING_FIELDS
        },
    }


def install_terminal(root, result):
    (root / "run-result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    attempt_dir = autoplay_runner.attempt_directory(
        root, result["attempt_id"]
    )
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "terminal-state.json").write_text(
        json.dumps(terminal_state(result)), encoding="utf-8"
    )
    return attempt_dir


def issue_audit(result):
    return {
        "record_type": "run_audit",
        "schema_version": 2,
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
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "termination_kind": "game_over",
        "audit_status": "inconclusive",
        "release_gate_passed": False,
        "issue_count": 1,
    }


def issue_exit(result, stdout=b"", stderr=b"controller failure\n"):
    output = autoplay_runner._output_summary(stdout, stderr, 1)
    return {
        "record_type": "controller_exit",
        "schema_version": 2,
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
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        **output,
        "controller_exit_status": "issues",
        "controller_exit_audit": {
            "audit_status": "issues",
            "release_gate_passed": False,
            "issue_count": 1,
        },
    }


def bind_exit_to_current_runtime(root, exit_record, *, observed_at=20.0):
    root = Path(root)
    manifest_path = root / "freeze-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    launch = manifest["launch_evidence"]
    exit_record.update({
        "launch_id": launch["launch_id"],
        "observed_at": observed_at,
        "freeze_manifest_sha256": hashlib.sha256(
            manifest_bytes
        ).hexdigest(),
        "freeze_source_digest": manifest["source_digest"],
        "freeze_generated_at": manifest["generated_at"],
        "freeze_launch_record_sha256": launch[
            "launch_record_sha256"
        ],
        "freeze_launch_start_record_path": launch[
            "launch_start_record_path"
        ],
        "freeze_launch_start_record_sha256": launch[
            "launch_start_record_sha256"
        ],
        "freeze_launch_id": launch["launch_id"],
        "freeze_launcher_pid": launch["launcher_pid"],
        "freeze_java_pid": launch["java_pid"],
        "freeze_bridge_instance_sha256": launch[
            "bridge_instance_sha256"
        ],
        "freeze_bridge_instance_token": launch[
            "bridge_instance_token"
        ],
        "freeze_bridge_pid": launch["bridge_pid"],
        "freeze_bridge_launch_id": launch["bridge_launch_id"],
        "freeze_bridge_sha256": launch["bridge_sha256"],
        "freeze_bridge_started_at": launch["bridge_started_at"],
        "freeze_root_communication_mod_jar_sha256": launch[
            "root_communication_mod_jar_sha256"
        ],
        "freeze_installed_communication_mod_jar_sha256": launch[
            "installed_communication_mod_jar_sha256"
        ],
        "bridge_stderr_evidence_status": "clear",
        "bridge_stderr_path": launch["bridge_stderr_path"],
        "bridge_stderr_start_size": launch[
            "bridge_stderr_start_offset"
        ],
        "bridge_stderr_start_sha256": launch[
            "bridge_stderr_start_sha256"
        ],
        "bridge_stderr_end_size": launch["bridge_stderr_current_size"],
        "bridge_stderr_end_sha256": launch[
            "bridge_stderr_current_sha256"
        ],
        "bridge_stderr_delta_size": launch["bridge_stderr_delta_size"],
        "bridge_stderr_delta_sha256": launch[
            "bridge_stderr_delta_sha256"
        ],
        "bridge_stderr_error": None,
    })
    return exit_record


def install_launch_exit(root, launch_id, *, finished_at=30.0, exit_code=0):
    root = Path(root)
    start_path = root / "logs" / "launches" / f"{launch_id}.start.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    exit_path = root / "logs" / "launches" / f"{launch_id}.exit.json"
    record = {
        **start,
        "launch_exit_record_path": exit_path.relative_to(root).as_posix(),
        "finished_at": finished_at,
        "exit_code": exit_code,
    }
    exit_path.write_text(json.dumps(record), encoding="utf-8")
    return record


def install_restart_predecessor(
    root, manifest, old_exit, *, lineage_parent=None
):
    root = Path(root)
    result = json.loads((root / "run-result.json").read_text(encoding="utf-8"))
    attempt_dir = autoplay_runner.attempt_directory(
        root, result["attempt_id"]
    )
    intent_path = (
        attempt_dir / campaign_attempt.MAINTENANCE_RESTART_INTENT_NAME
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    old_launch_id = intent["old_runtime"]["launch_id"]
    handoff_path = launch_game.maintenance_restart_handoff_path(
        root, old_launch_id
    )
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    launch_id = manifest["launch_evidence"]["launch_id"]
    start_path = root / "logs" / "launches" / f"{launch_id}.start.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    if lineage_parent is None:
        lineage_depth = 0
        parent_fields = {
            field: None for field in launch_game._RESTART_PARENT_FIELDS
        }
    else:
        parent_launch_id = lineage_parent["new_launch_id"]
        parent_path = launch_game.restart_predecessor_path(
            root, parent_launch_id
        )
        parent_start_path = (
            root / "logs" / "launches" / f"{parent_launch_id}.start.json"
        )
        parent_exit_path = (
            root / "logs" / "launches" / f"{parent_launch_id}.exit.json"
        )
        parent_start = json.loads(
            parent_start_path.read_text(encoding="utf-8")
        )
        parent_exit = json.loads(
            parent_exit_path.read_text(encoding="utf-8")
        )
        lineage_depth = lineage_parent["lineage_depth"] + 1
        parent_fields = {
            "lineage_parent_record_path": parent_path.relative_to(
                root
            ).as_posix(),
            "lineage_parent_record_sha256": (
                campaign_attempt._sha256_object(lineage_parent)
            ),
            "lineage_parent_start_record_path": (
                parent_start_path.relative_to(root).as_posix()
            ),
            "lineage_parent_start_record_sha256": (
                campaign_attempt._sha256_object(parent_start)
            ),
            "lineage_parent_exit_record_path": (
                parent_exit_path.relative_to(root).as_posix()
            ),
            "lineage_parent_exit_record_sha256": (
                campaign_attempt._sha256_object(parent_exit)
            ),
            "lineage_parent_launch_id": parent_launch_id,
            "lineage_parent_started_at": parent_start["started_at"],
            "lineage_parent_finished_at": parent_exit["finished_at"],
        }
    predecessor = {
        "schema_version": 2,
        "record_type": "restart_predecessor",
        "predecessor_status": "observed_before_new_runtime_launch",
        "lineage_depth": lineage_depth,
        "new_launch_id": launch_id,
        "new_launcher_pid": start["launcher_pid"],
        "new_launch_started_at": start["started_at"],
        "new_launch_log_path": start["launch_log_path"],
        "old_launch_id": old_launch_id,
        "old_launch_start_record_path": intent["old_runtime"][
            "launch_start_record_path"
        ],
        "old_launch_start_record_sha256": intent["old_runtime"][
            "launch_start_record_sha256"
        ],
        "old_launch_exit_record_path": old_exit[
            "launch_exit_record_path"
        ],
        "old_launch_exit_record_sha256": (
            campaign_attempt._sha256_object(old_exit)
        ),
        "restart_handoff_record_path": (
            handoff_path.relative_to(root).as_posix()
        ),
        "restart_handoff_record_sha256": (
            campaign_attempt._sha256_object(handoff)
        ),
        "restart_intent_record_path": (
            intent_path.relative_to(root).as_posix()
        ),
        "restart_intent_sha256": campaign_attempt._sha256_object(intent),
        **parent_fields,
    }
    path = launch_game.restart_predecessor_path(root, launch_id)
    path.write_text(json.dumps(predecessor), encoding="utf-8")
    start["restart_predecessor_record_path"] = (
        path.relative_to(root).as_posix()
    )
    start["restart_predecessor_record_sha256"] = (
        campaign_attempt._sha256_object(predecessor)
    )
    start_path.write_text(json.dumps(start), encoding="utf-8")
    (root / "launch-latest.json").write_text(
        json.dumps(start), encoding="utf-8"
    )
    replay = json.loads(
        (root / "decision-case-replay.json").read_text(encoding="utf-8")
    )
    with patch.object(
        freeze_manifest,
        "_fresh_policy_hashes",
        return_value={
            "decision_hash": manifest["decision_hash"],
            "controller_hash": manifest["controller_hash"],
        },
    ):
        refreshed = freeze_manifest.build_manifest(
            root,
            manifest["decision_hash"],
            manifest["controller_hash"],
            verification=verification(),
            decision_case_replay=replay,
            generated_at=manifest["generated_at"],
        )
    freeze_manifest.write_manifest(root / "freeze-manifest.json", refreshed)
    manifest.clear()
    manifest.update(refreshed)
    return predecessor


def runtime_proof(manifest):
    launch = manifest["launch_evidence"]
    return {
        "launch_evidence": launch,
        "launch_id": launch["launch_id"],
        "launcher_pid": launch["launcher_pid"],
        "java_pid": launch["java_pid"],
        "bridge_pid": launch["bridge_pid"],
        "bridge_instance_token": launch["bridge_instance_token"],
        "bridge_sha256": launch["bridge_sha256"],
        "bridge_runtime_source_digest": launch[
            "bridge_runtime_source_digest"
        ],
        "bridge_runtime_source_file_count": launch[
            "bridge_runtime_source_file_count"
        ],
    }


def install_issue_chain(
    root,
    result,
    *,
    include_audit=True,
    include_exit=True,
    duplicate=None,
    runtime_bound=False,
    observed_at=20.0,
):
    attempt_dir = install_terminal(root, result)
    terminal = {**result, "record_type": "terminal_result"}
    audit = issue_audit(result)
    exit_record = issue_exit(result)
    if runtime_bound:
        bind_exit_to_current_runtime(
            root, exit_record, observed_at=observed_at
        )
    records = [terminal]
    if include_audit:
        records.append(audit)
        if duplicate == "audit":
            records.append(dict(audit))
    if include_exit:
        records.append(exit_record)
        if duplicate == "exit":
            records.append(dict(exit_record))
        (attempt_dir / "controller-exit.json").write_text(
            json.dumps(exit_record), encoding="utf-8"
        )
        (attempt_dir / "controller.stdout.log").write_bytes(b"")
        (attempt_dir / "controller.stderr.log").write_bytes(
            b"controller failure\n"
        )
    if duplicate == "terminal":
        records.insert(1, dict(terminal))
    (root / "run-history.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return attempt_dir, records


class CampaignAttemptLaunchTests(unittest.TestCase):
    def _install_completed_unconsumed_recovery(self, root):
        install_freeze(root)
        states = StateSequence(
            menu_state(), menu_state(), menu_state(), menu_state(11)
        )

        def write_then_timeout(payload):
            if payload.get("action") == "state":
                return start_receipt(payload, root)
            (root / "command.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            raise TimeoutError("bridge did not consume command")

        with self.assertRaises(campaign_attempt.CampaignAttemptError):
            campaign_attempt.launch_attempt(
                root,
                "decision-a",
                "controller-a",
                state_loader=states,
                send_payload_fn=write_then_timeout,
                runner_fn=Mock(),
                request_id_factory=lambda: "unconsumed-start",
            )
        selection = json.loads(
            (root / "next-run-selection.json").read_text(encoding="utf-8")
        )
        record = campaign_attempt.recover_proven_unconsumed_start(
            root,
            state_loader=lambda: menu_state(11),
            process_alive_fn=lambda _pid: False,
            clock=lambda: 20.0,
        )
        incident_path = campaign_attempt._launch_incident_path(
            root, selection
        )
        recovery_path = (
            incident_path.parent
            / campaign_attempt.UNCONSUMED_START_RECOVERY_NAME
        )
        return record, incident_path, recovery_path

    def setUp(self):
        patcher = patch.object(
            pre_run_binding, "process_is_alive", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install_ambiguous_start(self, root):
        states = StateSequence(
            menu_state(), menu_state(), menu_state(), menu_state(11)
        )

        def accept_then_lose_receipt(payload):
            if payload.get("action") == "state":
                return start_receipt(payload, root)
            install_start_acceptance(root, payload)
            raise OSError("receipt lost")

        with self.assertRaises(campaign_attempt.CampaignAttemptError):
            campaign_attempt.launch_attempt(
                root,
                "decision-a",
                "controller-a",
                state_loader=states,
                send_payload_fn=accept_then_lose_receipt,
                runner_fn=Mock(),
                request_id_factory=lambda: "ambiguous-start",
            )
        selection = json.loads(
            (root / "next-run-selection.json").read_text(encoding="utf-8")
        )
        return selection, campaign_attempt._launch_incident_path(
            root, selection
        )

    def test_launch_uses_all_three_gates_and_exactly_one_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = install_freeze(root)
            self.assertIn("campaign_attempt.py", manifest["sources"])
            states = StateSequence(
                menu_state(10), menu_state(10), menu_state(10),
                menu_state(11), started_state(12),
            )
            sent = []

            def send(payload):
                sent.append(payload)
                receipt = start_receipt(payload, root)
                if payload.get("action") == "state":
                    (root / "action-receipt.json").write_text(
                        json.dumps(receipt), encoding="utf-8"
                    )
                return receipt

            runner = Mock()

            def run_once(*, root, max_actions):
                (root / "next-run-selection.json").unlink()
                return {"controller_exit_status": "clear", "exit_code": 0}

            runner.side_effect = run_once
            with patch.object(
                campaign_selector,
                "select_character",
                wraps=campaign_selector.select_character,
            ) as select, patch.object(
                campaign_selector,
                "write_selection",
                wraps=campaign_selector.write_selection,
            ) as write, patch.object(
                campaign_selector,
                "validate_launch_prerequisites",
                wraps=campaign_selector.validate_launch_prerequisites,
            ) as selector_gate, patch.object(
                pre_run_binding,
                "validate_release_checkpoint",
                wraps=pre_run_binding.validate_release_checkpoint,
            ) as pre_run_gate, patch.object(
                pre_run_binding,
                "build_start_payload",
                wraps=pre_run_binding.build_start_payload,
            ) as build_start, patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                wraps=pre_run_binding.validate_pre_dispatch_runtime,
            ) as runtime_gate:
                observed = campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    max_actions=77,
                    state_loader=states,
                    send_payload_fn=send,
                    runner_fn=runner,
                    selector_rng=random_for_tests(),
                    request_id_factory=lambda: "start-request",
                )

            self.assertEqual(2, len(sent))
            self.assertEqual("state", sent[0]["action"])
            self.assertEqual("start", sent[1]["action"])
            self.assertEqual("IRONCLAD", sent[1]["character"])
            self.assertEqual("start-request", sent[1]["id"])
            self.assertEqual(11, sent[1]["expected_seq"])
            self.assertEqual("clear", observed["controller_exit"]["controller_exit_status"])
            self.assertFalse((root / "next-run-selection.json").exists())
            self.assertFalse((root / "action-receipt.json").exists())
            select.assert_called_once()
            write.assert_called_once()
            selector_gate.assert_called_once()
            self.assertEqual(2, pre_run_gate.call_count)
            self.assertEqual(2, runtime_gate.call_count)
            build_start.assert_called_once()
            runner.assert_called_once_with(root=root.resolve(), max_actions=77)
            self.assertEqual(
                {
                    "accepted_state_seq": 10,
                    "result_state_seq": 11,
                    "result_decision_id": "menu-decision",
                    "result_phase": "MAIN_MENU",
                    "requested_target_id": "action:state",
                    "resolved_target_id": "action:state",
                },
                {
                    key: observed["pre_start_state_probe"][key]
                    for key in (
                        "accepted_state_seq",
                        "result_state_seq",
                        "result_decision_id",
                        "result_phase",
                        "requested_target_id",
                        "resolved_target_id",
                    )
                },
            )

    def test_pending_transport_or_selection_blocks_before_selector(self):
        for artifact, value in (
            ("command.json", {}),
            ("next-run-selection.json", {}),
            (
                "action-receipt.json",
                {"status": "accepted", "success": None},
            ),
        ):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                (root / artifact).write_text(json.dumps(value), encoding="utf-8")
                selector = Mock(side_effect=AssertionError("selector called"))
                with patch.object(campaign_selector, "select_character", selector):
                    with self.assertRaises(campaign_attempt.CampaignAttemptError):
                        campaign_attempt.launch_attempt(
                            root,
                            "decision-a",
                            "controller-a",
                            state_loader=lambda: menu_state(),
                            send_payload_fn=Mock(),
                            runner_fn=Mock(),
                        )
                selector.assert_not_called()

    def test_dead_runtime_blocks_before_start_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(menu_state(), menu_state(), menu_state())
            sender = Mock(side_effect=AssertionError("START was dispatched"))
            runner = Mock(side_effect=AssertionError("runner was started"))

            with patch.object(
                pre_run_binding,
                "process_is_alive",
                side_effect=lambda pid: pid != 222,
            ):
                with self.assertRaisesRegex(
                    campaign_attempt.CampaignAttemptError,
                    "bridge",
                ):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=sender,
                        runner_fn=runner,
                    )

            sender.assert_not_called()
            runner.assert_not_called()
            self.assertFalse((root / "command.json").exists())

    def test_runtime_death_after_fresh_state_probe_blocks_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(
                menu_state(), menu_state(), menu_state(), menu_state(11)
            )
            sent = []

            def send(payload):
                sent.append(payload)
                return start_receipt(payload, root)

            bridge_probes = 0

            def alive(pid):
                nonlocal bridge_probes
                if pid == 222:
                    bridge_probes += 1
                    return bridge_probes == 1
                return True

            start_id = Mock(side_effect=AssertionError("START id minted"))
            with patch.object(pre_run_binding, "process_is_alive", side_effect=alive):
                with self.assertRaisesRegex(
                    campaign_attempt.CampaignAttemptError, "bridge"
                ):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=send,
                        runner_fn=Mock(),
                        request_id_factory=start_id,
                    )
            self.assertEqual(["state"], [item["action"] for item in sent])
            start_id.assert_not_called()
            self.assertFalse((root / "next-run-selection.json").exists())

    def test_forged_or_stale_state_probe_never_dispatches_start(self):
        for case in ("wrong_id", "no_advance", "stale_state", "seq_mismatch"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                observed_state = menu_state(12 if case == "seq_mismatch" else 11)
                if case == "stale_state":
                    observed_state = menu_state(10)
                states = StateSequence(
                    menu_state(), menu_state(), menu_state(), observed_state
                )
                sent = []

                def send(payload):
                    sent.append(payload)
                    receipt = start_receipt(payload, root)
                    if case == "wrong_id":
                        receipt["request_id"] = "forged-request"
                    elif case == "no_advance":
                        receipt["result_state_seq"] = receipt[
                            "accepted_state_seq"
                        ]
                    return receipt

                start_id = Mock(side_effect=AssertionError("START id minted"))
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=send,
                        runner_fn=Mock(),
                        request_id_factory=start_id,
                    )
                self.assertEqual(["state"], [item["action"] for item in sent])
                start_id.assert_not_called()
                self.assertFalse((root / "next-run-selection.json").exists())
                self.assertFalse((root / "command.json").exists())
                incidents = list(
                    (root / "logs" / "blocked-launches").glob(
                        "*/campaign-launch-incident.json"
                    )
                )
                self.assertEqual([], incidents)

    def test_timed_out_readonly_state_probe_cleans_only_its_exact_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(menu_state(), menu_state(), menu_state())
            sent = []

            def write_then_timeout(payload):
                sent.append(payload)
                (root / "command.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                raise TimeoutError("STATE bridge timeout")

            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    state_loader=states,
                    send_payload_fn=write_then_timeout,
                    runner_fn=Mock(),
                )
            self.assertEqual(["state"], [item["action"] for item in sent])
            self.assertFalse((root / "command.json").exists())
            self.assertFalse((root / "next-run-selection.json").exists())
            self.assertEqual(
                [],
                list((root / "logs" / "blocked-launches").glob("*/*.json")),
            )

    def test_unconsumed_start_is_archived_before_transport_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(
                menu_state(), menu_state(), menu_state(), menu_state(11)
            )

            def write_then_timeout(payload):
                if payload.get("action") == "state":
                    return start_receipt(payload, root)
                (root / "command.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                raise TimeoutError("bridge did not consume command")

            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    state_loader=states,
                    send_payload_fn=write_then_timeout,
                    runner_fn=Mock(),
                    request_id_factory=lambda: "unconsumed-start",
                )
            selection = json.loads(
                (root / "next-run-selection.json").read_text(encoding="utf-8")
            )
            record = campaign_attempt.recover_proven_unconsumed_start(
                root,
                state_loader=lambda: menu_state(11),
                process_alive_fn=lambda _pid: False,
                clock=lambda: 20.0,
            )

            self.assertEqual("proven_unconsumed", record["recovery_status"])
            self.assertFalse(record["start_was_resent"])
            self.assertEqual(11, record["pre_start_state_probe"]["result_state_seq"])
            self.assertEqual(selection, record["selection"])
            self.assertFalse((root / "command.json").exists())
            self.assertFalse((root / "next-run-selection.json").exists())
            recovery_path = (
                campaign_attempt._launch_incident_path(root, selection).parent
                / campaign_attempt.UNCONSUMED_START_RECOVERY_NAME
            )
            self.assertEqual(
                record, json.loads(recovery_path.read_text(encoding="utf-8"))
            )

    def test_completed_unconsumed_recovery_reconciles_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record, _incident_path, _recovery_path = (
                self._install_completed_unconsumed_recovery(root)
            )
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            state_loader = Mock(
                side_effect=AssertionError("live state must not be read")
            )
            process_probe = Mock(
                side_effect=AssertionError("processes must not be probed")
            )

            replay = campaign_attempt.recover_proven_unconsumed_start(
                root,
                state_loader=state_loader,
                process_alive_fn=process_probe,
                clock=Mock(side_effect=AssertionError("clock was read")),
            )

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(record, replay)
            self.assertEqual(before, after)
            state_loader.assert_not_called()
            process_probe.assert_not_called()

    def test_completed_unconsumed_recovery_accepts_exact_legacy_schema_2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record, incident_path, recovery_path = (
                self._install_completed_unconsumed_recovery(root)
            )
            incident = json.loads(incident_path.read_text(encoding="utf-8"))
            incident.pop("pre_start_state_probe")
            incident.pop("pre_dispatch_runtime")
            incident_path.write_text(
                json.dumps(incident, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            for field in (
                "start_was_resent",
                "incident_dispatch_status",
                "pre_start_state_probe",
                "pre_dispatch_runtime",
            ):
                record.pop(field)
            record["incident_sha256"] = hashlib.sha256(
                incident_path.read_bytes()
            ).hexdigest()
            recovery_path.write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            replay = campaign_attempt.recover_proven_unconsumed_start(
                root,
                state_loader=Mock(
                    side_effect=AssertionError("live state was read")
                ),
                process_alive_fn=Mock(
                    side_effect=AssertionError("processes were probed")
                ),
            )

            self.assertEqual(record, replay)

    def test_completed_unconsumed_recovery_rejects_multiple_or_tampered_proof(self):
        for case in (
            "multiple",
            "recovery_extra",
            "command_tamper",
            "selection_tamper",
            "incident_tamper",
            "new_probe_missing",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record, incident_path, recovery_path = (
                    self._install_completed_unconsumed_recovery(root)
                )
                if case == "multiple":
                    duplicate = (
                        root / "logs" / "blocked-launches" / "duplicate"
                        / campaign_attempt.UNCONSUMED_START_RECOVERY_NAME
                    )
                    duplicate.parent.mkdir(parents=True)
                    duplicate.write_bytes(recovery_path.read_bytes())
                elif case == "incident_tamper":
                    incident = json.loads(
                        incident_path.read_text(encoding="utf-8")
                    )
                    incident["error"] = "different timeout"
                    incident_path.write_text(
                        json.dumps(incident), encoding="utf-8"
                    )
                else:
                    if case == "recovery_extra":
                        record["unclassified"] = True
                    elif case == "command_tamper":
                        record["command"]["id"] = "different-command"
                    elif case == "selection_tamper":
                        record["selection"]["character"] = "THE_SILENT"
                    else:
                        record.pop("pre_start_state_probe")
                    recovery_path.write_text(
                        json.dumps(record), encoding="utf-8"
                    )

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_proven_unconsumed_start(root)

    def test_completed_unconsumed_recovery_rejects_residual_transport(self):
        for residue in (
            "selection",
            "command",
            "receipt",
            "selection_tmp",
            "command_tmp",
            "receipt_tmp",
            "context",
            "acceptance",
        ):
            with self.subTest(residue=residue), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record, _incident_path, _recovery_path = (
                    self._install_completed_unconsumed_recovery(root)
                )
                paths = {
                    "selection": root / campaign_attempt.SELECTION_NAME,
                    "command": root / campaign_attempt.COMMAND_NAME,
                    "receipt": root / campaign_attempt.RECEIPT_NAME,
                    "selection_tmp": (
                        root / f"{campaign_attempt.SELECTION_NAME}.tmp"
                    ),
                    "command_tmp": root / f"{campaign_attempt.COMMAND_NAME}.tmp",
                    "receipt_tmp": root / f"{campaign_attempt.RECEIPT_NAME}.tmp",
                    "context": root / "run-context.json",
                }
                if residue == "acceptance":
                    accepted_menu = {
                        "protocol_version": 2,
                        "in_game": False,
                        "ready_for_command": True,
                        "state_seq": record["state_seq"],
                        "phase": record["phase"],
                        "decision_id": record["decision_id"],
                    }
                    acceptance = pre_run_binding.build_start_acceptance_record(
                        record["command"],
                        accepted_menu,
                        record["selection"],
                        created_at=21.0,
                    )
                    path = pre_run_binding.start_acceptance_path(
                        root, record["selection_id"]
                    )
                    pre_run_binding._write_json_once_idempotent(
                        path, acceptance
                    )
                else:
                    paths[residue].write_text("{}", encoding="utf-8")

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_proven_unconsumed_start(root)

    def test_unconsumed_recovery_rejects_acceptance_or_live_bridge(self):
        for mode in ("accepted", "live"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                states = StateSequence(
                    menu_state(), menu_state(), menu_state(), menu_state(11)
                )

                def write_then_timeout(payload):
                    if payload.get("action") == "state":
                        return start_receipt(payload, root)
                    (root / "command.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    if mode == "accepted":
                        install_start_acceptance(root, payload)
                    raise TimeoutError("ambiguous START")

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=write_then_timeout,
                        runner_fn=Mock(),
                    )
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_proven_unconsumed_start(
                        root,
                        state_loader=lambda: menu_state(11),
                        process_alive_fn=(
                            (lambda pid: pid == 222)
                            if mode == "live" else (lambda _pid: False)
                        ),
                    )
                self.assertTrue((root / "command.json").exists())
                self.assertTrue((root / "next-run-selection.json").exists())

    def test_unconsumed_recovery_rejects_contradictory_evidence(self):
        for case in (
            "committed", "wrong_dispatch", "wrong_error", "probe_tamper",
            "runtime_tamper", "bound_context", "command_tamper", "state_changed",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                states = StateSequence(
                    menu_state(), menu_state(), menu_state(), menu_state(11)
                )

                def write_then_timeout(payload):
                    if payload.get("action") == "state":
                        return start_receipt(payload, root)
                    (root / "command.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    raise TimeoutError("no final action receipt")

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=write_then_timeout,
                        runner_fn=Mock(),
                    )
                selection = json.loads(
                    (root / "next-run-selection.json").read_text(encoding="utf-8")
                )
                incident_path = campaign_attempt._launch_incident_path(
                    root, selection
                )
                incident = json.loads(incident_path.read_text(encoding="utf-8"))
                recovery_state = menu_state(11)
                if case == "committed":
                    incident["start_committed"] = True
                elif case == "wrong_dispatch":
                    incident["dispatch_status"] = "ambiguous_final_receipt"
                elif case == "wrong_error":
                    incident["error_class"] = "OSError"
                elif case == "probe_tamper":
                    incident["pre_start_state_probe"]["result_state_seq"] = 999
                elif case == "runtime_tamper":
                    incident["pre_dispatch_runtime"]["bridge_pid"] = 999
                elif case == "bound_context":
                    (root / "run-context.json").write_text(
                        json.dumps({
                            "selection_id": selection["selection_id"],
                            "character": selection["character"],
                            "decision_hash": selection["decision_hash"],
                            "controller_hash": selection["controller_hash"],
                            "policy_version": selection["policy_version"],
                            "ascension_level": 0,
                            "run_type": "standard",
                        }),
                        encoding="utf-8",
                    )
                elif case == "command_tamper":
                    command = json.loads(
                        (root / "command.json").read_text(encoding="utf-8")
                    )
                    command["id"] = "different-command"
                    (root / "command.json").write_text(
                        json.dumps(command), encoding="utf-8"
                    )
                else:
                    recovery_state = menu_state(12)
                if case in {
                    "committed", "wrong_dispatch", "wrong_error",
                    "probe_tamper", "runtime_tamper",
                }:
                    incident_path.write_text(
                        json.dumps(incident), encoding="utf-8"
                    )
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_proven_unconsumed_start(
                        root,
                        state_loader=lambda value=recovery_state: value,
                        process_alive_fn=lambda _pid: False,
                    )
                self.assertTrue((root / "command.json").exists())
                self.assertTrue((root / "next-run-selection.json").exists())

    def test_initial_game_over_is_not_treated_as_attempt_recovery(self):
        state = terminal_state(result_record())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            selector = Mock(side_effect=AssertionError("selector called"))
            with patch.object(campaign_selector, "select_character", selector):
                with self.assertRaisesRegex(
                    campaign_attempt.CampaignAttemptError, "MAIN_MENU"
                ):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=lambda: state,
                        send_payload_fn=Mock(),
                        runner_fn=Mock(),
                    )
            selector.assert_not_called()

    def test_occupied_controller_or_runner_lease_blocks_launch(self):
        for lock_name in (
            campaign_attempt.CONTROLLER_LOCK_NAME,
            campaign_attempt.RUNNER_LOCK_NAME,
        ):
            with self.subTest(lock=lock_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                with campaign_attempt.OSLease(root / lock_name, "occupied"):
                    with self.assertRaisesRegex(
                        campaign_attempt.CampaignAttemptError, "lease"
                    ):
                        campaign_attempt.launch_attempt(
                            root,
                            "decision-a",
                            "controller-a",
                            state_loader=lambda: menu_state(),
                            send_payload_fn=Mock(),
                            runner_fn=Mock(),
                        )
                self.assertFalse((root / "next-run-selection.json").exists())

    def test_authoritatively_rejected_start_cleans_selection_and_never_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(
                menu_state(), menu_state(), menu_state(), menu_state(11)
            )
            runner = Mock()

            def send(payload):
                if payload.get("action") == "state":
                    return start_receipt(payload, root)
                return {
                    "request_id": payload["id"],
                    "success": False,
                    "status": "rejected",
                    "error": "selection rejected",
                    "accepted_state_seq": None,
                    "result_state_seq": payload["expected_seq"],
                    "requested_target_id": payload["target_id"],
                    "resolved_target_id": None,
                }

            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "success",
            ):
                campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    state_loader=states,
                    send_payload_fn=send,
                    runner_fn=runner,
                    request_id_factory=lambda: "start-request",
                )
            runner.assert_not_called()
            self.assertFalse((root / "next-run-selection.json").exists())

    def test_wrong_active_character_retains_selection_and_writes_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(
                menu_state(),
                menu_state(),
                menu_state(),
                menu_state(11),
                started_state(12, character="DEFECT"),
            )
            runner = Mock()

            def send(payload):
                return start_receipt(payload, root)

            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError, "wrong character"
            ):
                campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    state_loader=states,
                    send_payload_fn=send,
                    runner_fn=runner,
                )
            runner.assert_not_called()
            selection = json.loads(
                (root / "next-run-selection.json").read_text(
                    encoding="utf-8"
                )
            )
            incident = json.loads(
                campaign_attempt._launch_incident_path(root, selection).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(incident["start_committed"])
            self.assertFalse(incident["runner_invoked"])
            self.assertTrue(incident["pending_selection_present"])

    def test_start_success_then_runner_exception_retains_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            states = StateSequence(
                menu_state(), menu_state(), menu_state(), menu_state(11),
                started_state(12),
            )
            runner = Mock(side_effect=RuntimeError("runner failed"))
            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError, "runner failed"
            ):
                campaign_attempt.launch_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    state_loader=states,
                    send_payload_fn=lambda payload: start_receipt(payload, root),
                    runner_fn=runner,
                )
            runner.assert_called_once()
            selection = json.loads(
                (root / "next-run-selection.json").read_text(
                    encoding="utf-8"
                )
            )
            incident = json.loads(
                campaign_attempt._launch_incident_path(root, selection).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(incident["start_committed"])
            self.assertTrue(incident["runner_invoked"])
            self.assertEqual(
                "authoritatively_succeeded", incident["dispatch_status"]
            )

    def test_post_acceptance_checkpoint_blocks_mutation_before_runner(self):
        for kind in ("history", "source", "sidecar"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                states = StateSequence(
                    menu_state(), menu_state(), menu_state(), menu_state(11),
                    started_state(12),
                )
                runner = Mock(side_effect=AssertionError("runner spawned"))

                def send(payload):
                    receipt = start_receipt(payload, root)
                    if payload.get("action") == "state":
                        return receipt
                    if kind == "history":
                        orphan = {
                            "record_type": "run_audit",
                            "schema_version": 2,
                            "attempt_id": "attempt-after-acceptance",
                            "decision_hash": "decision-a",
                            "controller_hash": "controller-a",
                            "selection_id": "selection-orphan",
                            "selection_digest": "f" * 64,
                        }
                        (root / "run-history.jsonl").write_text(
                            json.dumps(orphan) + "\n", encoding="utf-8"
                        )
                    elif kind == "source":
                        (root / "policy.py").write_text(
                            "POLICY = 999\n", encoding="utf-8"
                        )
                    else:
                        selection = json.loads((
                            root / "next-run-selection.json"
                        ).read_text(encoding="utf-8"))
                        path = pre_run_binding.start_acceptance_path(
                            root, selection["selection_id"]
                        )
                        sidecar = json.loads(path.read_text(encoding="utf-8"))
                        sidecar["accepted_receipt"][
                            "selection_digest"
                        ] = "0" * 64
                        path.write_text(json.dumps(sidecar), encoding="utf-8")
                    return receipt

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=send,
                        runner_fn=runner,
                        selector_rng=random_for_tests(),
                        request_id_factory=lambda: "start-request",
                    )
                runner.assert_not_called()
                selection = json.loads((
                    root / "next-run-selection.json"
                ).read_text(encoding="utf-8"))
                incident = json.loads(
                    campaign_attempt._launch_incident_path(
                        root, selection
                    ).read_text(encoding="utf-8")
                )
                self.assertTrue(incident["start_committed"])
                self.assertFalse(incident["runner_invoked"])
                self.assertEqual(
                    selection["selection_id"], incident["selection_id"]
                )

    def test_recovery_checkpoint_blocks_mutation_before_runner(self):
        for kind in ("history", "source", "sidecar"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                selection, incident_path = self._install_ambiguous_start(root)
                runner = Mock(side_effect=AssertionError("runner spawned"))

                def active_state():
                    if kind == "history":
                        orphan = {
                            "record_type": "run_audit",
                            "schema_version": 2,
                            "attempt_id": "attempt-after-recovery-gate",
                            "decision_hash": "decision-a",
                            "controller_hash": "controller-a",
                            "selection_id": "selection-orphan",
                            "selection_digest": "f" * 64,
                        }
                        (root / "run-history.jsonl").write_text(
                            json.dumps(orphan) + "\n", encoding="utf-8"
                        )
                    elif kind == "source":
                        (root / "policy.py").write_text(
                            "POLICY = 999\n", encoding="utf-8"
                        )
                    else:
                        path = pre_run_binding.start_acceptance_path(
                            root, selection["selection_id"]
                        )
                        sidecar = json.loads(path.read_text(encoding="utf-8"))
                        sidecar["accepted_receipt"][
                            "selection_digest"
                        ] = "0" * 64
                        path.write_text(json.dumps(sidecar), encoding="utf-8")
                    return started_state(12)

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_started_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=active_state,
                        runner_fn=runner,
                    )
                runner.assert_not_called()
                self.assertTrue((root / "next-run-selection.json").exists())
                self.assertTrue(incident_path.exists())
                recovery_path = (
                    incident_path.parent / campaign_attempt.START_RECOVERY_NAME
                )
                if recovery_path.exists():
                    recovery = json.loads(
                        recovery_path.read_text(encoding="utf-8")
                    )
                    self.assertFalse(recovery["start_was_resent"])
                else:
                    incident = json.loads(
                        incident_path.read_text(encoding="utf-8")
                    )
                    self.assertTrue(incident["start_dispatched"])
                    self.assertEqual(
                        selection["selection_id"], incident["selection_id"]
                    )

    def test_ambiguous_started_run_recovery_attaches_once_without_new_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            selection, incident_path = self._install_ambiguous_start(root)
            runner = Mock()

            def run_once(*, root, max_actions):
                (root / "next-run-selection.json").unlink()
                return {"controller_exit_status": "issues", "exit_code": 1}

            runner.side_effect = run_once
            observed = campaign_attempt.recover_started_attempt(
                root,
                "decision-a",
                "controller-a",
                max_actions=77,
                state_loader=lambda: started_state(12),
                runner_fn=runner,
            )
            runner.assert_called_once_with(root=root.resolve(), max_actions=77)
            self.assertEqual(
                "recover_started_attempt", observed["operation"]
            )
            self.assertFalse(observed["start_was_resent"])
            self.assertEqual(
                selection["selection_id"],
                observed["recovery"]["selection_id"],
            )
            recovery = json.loads(
                (incident_path.parent / "started-run-recovery.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(recovery["start_was_resent"])

    def test_abandoned_accepted_start_requires_dead_old_and_live_repair_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            selection, incident_path = self._install_ambiguous_start(root)
            incident = json.loads(incident_path.read_text(encoding="utf-8"))
            old_runtime = incident["pre_dispatch_runtime"]
            old_runtime.update({
                "launch_id": "launch-abandoned",
                "launcher_pid": 310,
                "java_pid": 311,
                "bridge_pid": 322,
            })
            old_runtime["launch_evidence"].update({
                "launch_id": "launch-abandoned",
                "launcher_pid": 310,
                "java_pid": 311,
                "bridge_pid": 322,
            })
            incident_path.write_text(json.dumps(incident), encoding="utf-8")

            install_freeze(root, "decision-repair", "controller-repair")
            current_pids = {110, 111, 222}
            old_pids = {310, 311, 322}

            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.recover_abandoned_accepted_start(
                    root,
                    "decision-repair",
                    "controller-repair",
                    state_loader=lambda: menu_state(20, "repair-menu"),
                    process_alive_fn=lambda pid: pid in current_pids | old_pids,
                )
            self.assertTrue((root / "next-run-selection.json").exists())

            observed = campaign_attempt.recover_abandoned_accepted_start(
                root,
                "decision-repair",
                "controller-repair",
                state_loader=lambda: menu_state(20, "repair-menu"),
                process_alive_fn=lambda pid: pid in current_pids,
                clock=lambda: 30.0,
            )
            self.assertEqual(
                "abandoned_after_dead_runtime_repair",
                observed["recovery_status"],
            )
            self.assertFalse(observed["eligible_for_cohort"])
            self.assertFalse(observed["start_was_resent"])
            self.assertEqual(selection["selection_id"], observed["selection_id"])
            self.assertEqual(
                "decision-repair", observed["repair_decision_hash"]
            )
            self.assertEqual(
                {"launcher": 310, "java": 311, "bridge": 322},
                observed["dead_original_processes"],
            )
            self.assertFalse((root / "next-run-selection.json").exists())
            recovery_path = (
                incident_path.parent
                / campaign_attempt.ABANDONED_ACCEPTED_START_RECOVERY_NAME
            )
            self.assertTrue(recovery_path.exists())

    def test_started_recovery_rejects_wrong_character_duplicate_or_active_context(self):
        cases = ("character", "duplicate", "context")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                selection, incident_path = self._install_ambiguous_start(root)
                active = started_state(12)
                if case == "character":
                    active = started_state(12, character="DEFECT")
                elif case == "duplicate":
                    duplicate = (
                        root / "logs" / "blocked-launches" / "duplicate"
                        / "campaign-launch-incident.json"
                    )
                    duplicate.parent.mkdir(parents=True)
                    duplicate.write_bytes(incident_path.read_bytes())
                else:
                    (root / "run-context.json").write_text(
                        json.dumps({
                            "run_id": "IRONCLAD:0:12345",
                            "attempt_id": "already-owned",
                        }),
                        encoding="utf-8",
                    )
                runner = Mock()
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.recover_started_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=lambda value=active: value,
                        runner_fn=runner,
                    )
                runner.assert_not_called()
                self.assertTrue((root / "next-run-selection.json").exists())
                self.assertEqual(selection["selection_id"], json.loads(
                    (root / "next-run-selection.json").read_text(
                        encoding="utf-8"
                    )
                )["selection_id"])

    def test_started_recovery_rejects_incident_seed_mismatch_and_busy_lease(self):
        for case in ("seed", "lease"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install_freeze(root)
                states = StateSequence(
                    menu_state(), menu_state(), menu_state(), menu_state(11),
                    started_state(12),
                )

                def runner_failed(*, root, max_actions):
                    raise RuntimeError("runner never attached")

                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.launch_attempt(
                        root,
                        "decision-a",
                        "controller-a",
                        state_loader=states,
                        send_payload_fn=lambda payload: start_receipt(payload, root),
                        runner_fn=runner_failed,
                    )
                active = started_state(12)
                if case == "seed":
                    active["game_state"]["seed"] = 99999
                    manager = contextlib.nullcontext()
                else:
                    manager = campaign_attempt.OSLease(
                        root / campaign_attempt.CONTROLLER_LOCK_NAME,
                        "occupied controller",
                    )
                runner = Mock()
                with manager:
                    with self.assertRaises(campaign_attempt.CampaignAttemptError):
                        campaign_attempt.recover_started_attempt(
                            root,
                            "decision-a",
                            "controller-a",
                            state_loader=lambda value=active: value,
                            runner_fn=runner,
                        )
                runner.assert_not_called()

    def test_started_recovery_cleans_only_exact_authoritative_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root)
            selection, incident_path = self._install_ambiguous_start(root)
            incident = json.loads(incident_path.read_text(encoding="utf-8"))
            payload = incident["start_payload"]
            incident.update({
                "dispatch_status": "authoritatively_rejected",
                "start_committed": False,
                "selection_must_be_retained": False,
                "start_receipt": {
                    "request_id": payload["id"],
                    "success": False,
                    "status": "rejected",
                    "error": "selection rejected",
                    "accepted_state_seq": None,
                    "result_state_seq": menu_state()["state_seq"],
                    "requested_target_id": payload["target_id"],
                    "resolved_target_id": None,
                },
            })
            incident_path.write_text(json.dumps(incident), encoding="utf-8")
            runner = Mock()
            observed = campaign_attempt.recover_started_attempt(
                root,
                "decision-a",
                "controller-a",
                state_loader=lambda: menu_state(),
                runner_fn=runner,
            )
            self.assertEqual(
                "authoritative_rejection_cleaned",
                observed["recovery_status"],
            )
            self.assertFalse((root / "next-run-selection.json").exists())
            runner.assert_not_called()


class CampaignAttemptReturnTests(unittest.TestCase):
    def test_clean_nonheart_terminal_sends_ten_field_proceed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir = install_terminal(root, result)
            terminal = terminal_state(result)
            menu = menu_state(101, "menu-after-terminal")
            states = StateSequence(terminal, menu)
            sent = []

            def send(payload):
                sent.append(payload)
                return proceed_receipt(payload)

            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=states,
                send_payload_fn=send,
                reconcile_fn=lambda observed_root: clean_exit(result),
                request_id_factory=lambda: "proceed-request",
                clock=lambda: 20.0,
            )
            self.assertEqual(1, len(sent))
            self.assertEqual(
                set(campaign_attempt.ATTEMPT_BINDING_FIELDS),
                set(campaign_attempt.ATTEMPT_BINDING_FIELDS) & set(sent[0]),
            )
            self.assertEqual("proceed", sent[0]["action"])
            self.assertEqual("clear", transition["transition_status"])
            sidecar = json.loads(
                (attempt_dir / "menu-transition.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transition, sidecar)
            self.assertFalse(transition["recovered_from_receipt"])

            # A later MAIN_MENU frame reuses the exact sidecar without
            # emitting another command.
            later_menu = menu_state(102, "menu-after-terminal")
            no_send = Mock(side_effect=AssertionError("duplicate PROCEED"))
            repeated = campaign_attempt.return_to_main_menu(
                root,
                state_loader=lambda: later_menu,
                send_payload_fn=no_send,
                reconcile_fn=lambda observed_root: clean_exit(result),
            )
            self.assertEqual(sidecar, repeated)
            no_send.assert_not_called()

    def test_incomplete_or_nonclear_chain_never_sends(self):
        for reconciled in (None, {"controller_exit_status": "issues"}):
            with self.subTest(reconciled=reconciled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = result_record()
                install_terminal(root, result)
                send = Mock()
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: terminal_state(result),
                        send_payload_fn=send,
                        reconcile_fn=lambda observed_root, value=reconciled: value,
                    )
                send.assert_not_called()

    def test_complete_nonclear_chain_requires_explicit_repaired_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            install_issue_chain(root, result)
            send = Mock()

            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "nonclear audit blocks GAME_OVER return",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state(result),
                    send_payload_fn=send,
                    reconcile_fn=lambda observed_root: issue_exit(result),
                )

            send.assert_not_called()

    def test_clear_heart_victory_is_held_after_full_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record(heart=True)
            install_terminal(root, result)
            reconcile = Mock(return_value=clean_exit(result))
            send = Mock(side_effect=AssertionError("left Heart victory"))
            with self.assertRaises(campaign_attempt.HeartVictoryHeld):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state(result),
                    send_payload_fn=send,
                    reconcile_fn=reconcile,
                )
            reconcile.assert_called_once_with(root.resolve())
            send.assert_not_called()

    def test_mismatched_proceed_receipt_never_lands_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir = install_terminal(root, result)

            def send(payload):
                receipt = proceed_receipt(payload)
                receipt["selection_id"] = "wrong-selection"
                return receipt

            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError, "selection_id"
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state(result),
                    send_payload_fn=send,
                    reconcile_fn=lambda observed_root: clean_exit(result),
                )
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_post_receipt_non_menu_state_never_lands_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir = install_terminal(root, result)
            states = StateSequence(
                terminal_state(result), terminal_state(result)
            )
            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=states,
                    send_payload_fn=lambda payload: proceed_receipt(payload),
                    reconcile_fn=lambda observed_root: clean_exit(result),
                )
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_exact_receipt_recovers_crash_between_menu_and_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_freeze(root, generated_at=12.0)
            result = result_record()
            attempt_dir = install_terminal(root, result)
            exit_record = bind_exit_to_current_runtime(
                root, clean_exit(result), observed_at=20.0
            )
            terminal = terminal_state(result)
            payload = campaign_attempt._build_proceed_payload(
                terminal, attempt_binding(result), "proceed-request"
            )
            receipt = proceed_receipt(payload)
            (root / "action-receipt.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            no_send = Mock(side_effect=AssertionError("duplicate PROCEED"))
            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=lambda: menu_state(102, "menu-after-terminal"),
                send_payload_fn=no_send,
                reconcile_fn=lambda observed_root: exit_record,
                process_alive_fn=lambda pid: pid in {110, 111, 222},
                clock=lambda: 30.0,
            )
            no_send.assert_not_called()
            self.assertTrue(transition["recovered_from_receipt"])
            self.assertEqual(101, transition["menu_state_seq"])
            self.assertTrue((attempt_dir / "menu-transition.json").exists())

    def test_tampered_existing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir = install_terminal(root, result)
            terminal = terminal_state(result)
            menu = menu_state(101, "menu-after-terminal")
            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=StateSequence(terminal, menu),
                send_payload_fn=lambda payload: proceed_receipt(payload),
                reconcile_fn=lambda observed_root: clean_exit(result),
                clock=lambda: 20.0,
            )
            transition["decision_hash"] = "tampered"
            (attempt_dir / "menu-transition.json").write_text(
                json.dumps(transition), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError, "decision_hash"
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(
                        102, "menu-after-terminal"
                    ),
                    send_payload_fn=Mock(),
                    reconcile_fn=lambda observed_root: clean_exit(result),
                )


class CampaignAttemptMaintenanceTests(unittest.TestCase):
    OLD_PIDS = {110, 111, 222}
    NEW_PIDS = {210, 211, 212}

    def _install_restart_source(self, root):
        old_manifest = install_freeze(
            root,
            launch_id="launch-old",
            bridge_instance_token="bridge-old",
            generated_at=15.0,
        )
        result = result_record()
        attempt_dir, records = install_issue_chain(
            root,
            result,
            runtime_bound=True,
            observed_at=20.0,
        )
        exit_record = json.loads(
            (attempt_dir / "controller-exit.json").read_text(
                encoding="utf-8"
            )
        )
        # The restart intent is prepared after the repair source and its
        # DecisionCases have landed, but before a replacement runtime exists
        # to authorize the new freeze.  Keep the old bridge/exit attestation
        # untouched while making the on-disk target snapshot final.
        install_replay_bundle(root, "decision-fixed")
        return result, attempt_dir, records, exit_record, old_manifest

    def _prepare_restart(self, root):
        (
            result,
            attempt_dir,
            records,
            exit_record,
            old_manifest,
        ) = self._install_restart_source(root)
        intent = campaign_attempt.prepare_maintenance_restart(
            root,
            maintenance_decision_hash="decision-fixed",
            maintenance_controller_hash="controller-fixed",
            state_loader=lambda: terminal_state(result),
            # The launcher and Java process must still be alive when the
            # intent is committed.  The external bridge is allowed to have
            # exited after publishing the authoritative terminal frame.
            process_alive_fn=lambda pid: pid in {110, 111},
            clock=lambda: 25.0,
        )
        return (
            result,
            attempt_dir,
            records,
            exit_record,
            old_manifest,
            intent,
        )

    def _install_restart_target(self, root):
        old_exit = install_launch_exit(root, "launch-old")
        manifest = install_freeze(
            root,
            "decision-fixed",
            "controller-fixed",
            launch_id="launch-new",
            launcher_pid=210,
            java_pid=211,
            bridge_pid=212,
            bridge_instance_token="bridge-new",
            started_at=40.0,
            bridge_started_at=41.0,
            generated_at=50.0,
        )
        install_restart_predecessor(root, manifest, old_exit)
        return manifest

    def _complete_restart(
        self,
        root,
        result,
        exit_record,
        replacement,
        *,
        states=None,
        runtime_side_effect=None,
        process_alive_fn=None,
        state_probe_accepted_offset=0,
    ):
        states = states or StateSequence(
            menu_state(101, "restart-menu"),
            menu_state(102, "restart-menu"),
        )
        sent = []

        def send(payload):
            sent.append(payload)
            if payload.get("action") != "state":
                raise AssertionError("restart recovery emitted PROCEED")
            receipt = start_receipt(payload, root)
            if state_probe_accepted_offset:
                receipt["accepted_state_seq"] += state_probe_accepted_offset
                receipt["result_state_seq"] += state_probe_accepted_offset
            (Path(root) / "action-receipt.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            return receipt

        runtime_side_effect = runtime_side_effect or [
            replacement,
            replacement,
        ]
        process_alive_fn = process_alive_fn or (
            lambda pid: pid in self.NEW_PIDS
        )
        with patch.object(
            pre_run_binding,
            "validate_pre_dispatch_runtime",
            side_effect=runtime_side_effect,
        ) as runtime_gate:
            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=states,
                send_payload_fn=send,
                reconcile_fn=lambda observed_root: exit_record,
                maintenance_decision_hash="decision-fixed",
                maintenance_controller_hash="controller-fixed",
                process_alive_fn=process_alive_fn,
                clock=lambda: 60.0,
            )
        return transition, sent, runtime_gate

    def test_repaired_freeze_can_release_noneligible_issue_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir, records = install_issue_chain(root, result)
            install_freeze(root, "decision-fixed", "controller-fixed")
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(
                    records,
                    "decision-a",
                    controller_hash="controller-a",
                ),
            )
            states = StateSequence(
                terminal_state(result),
                menu_state(101, "menu-after-terminal"),
            )
            sent = []

            def send(payload):
                sent.append(payload)
                return proceed_receipt(payload)

            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=states,
                send_payload_fn=send,
                reconcile_fn=lambda observed_root: issue_exit(result),
                maintenance_decision_hash="decision-fixed",
                maintenance_controller_hash="controller-fixed",
                request_id_factory=lambda: "maintenance-proceed",
                clock=lambda: 40.0,
            )
            self.assertEqual(1, len(sent))
            self.assertEqual(
                "maintenance_resolution", transition["authorization_kind"]
            )
            resolution = json.loads(
                (attempt_dir / "maintenance-resolution.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(resolution["eligible_for_cohort"])
            self.assertTrue(resolution["original_findings_preserved"])
            self.assertEqual("inconclusive", resolution["original_audit_status"])
            self.assertEqual(
                campaign_attempt._sha256_object(resolution),
                transition["maintenance_resolution_sha256"],
            )
            # Maintenance evidence is sidecar-only and cannot make the old
            # attempt selector eligible.
            unchanged = campaign_selector.load_history(root / "run-history.jsonl")
            self.assertEqual(records, unchanged)
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(
                    unchanged,
                    "decision-a",
                    controller_hash="controller-a",
                ),
            )

    def test_maintenance_rejects_missing_or_duplicate_triplet_members(self):
        cases = (
            {"include_audit": False},
            {"include_exit": False},
            {"duplicate": "terminal"},
            {"duplicate": "audit"},
            {"duplicate": "exit"},
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = result_record()
                attempt_dir, _ = install_issue_chain(root, result, **case)
                install_freeze(root, "decision-fixed", "controller-fixed")
                send = Mock()
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: terminal_state(result),
                        send_payload_fn=send,
                        reconcile_fn=lambda observed_root: issue_exit(result),
                        maintenance_decision_hash="decision-fixed",
                        maintenance_controller_hash="controller-fixed",
                    )
                send.assert_not_called()
                self.assertFalse(
                    (attempt_dir / "maintenance-resolution.json").exists()
                )

    def test_maintenance_requires_a_new_passing_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record()
            attempt_dir, _ = install_issue_chain(root, result)
            # A valid old-hash manifest cannot authorize releasing an issue
            # attempt after an alleged repair.
            install_freeze(root, "decision-a", "controller-a")
            send = Mock()
            with self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError, "newly frozen"
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state(result),
                    send_payload_fn=send,
                    reconcile_fn=lambda observed_root: issue_exit(result),
                    maintenance_decision_hash="decision-a",
                    maintenance_controller_hash="controller-a",
                )
            send.assert_not_called()
            self.assertFalse(
                (attempt_dir / "maintenance-resolution.json").exists()
            )

    def test_maintenance_releases_only_nonclear_heart_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record(heart=True)
            attempt_dir, _ = install_issue_chain(root, result)
            install_freeze(root, "decision-fixed", "controller-fixed")
            states = StateSequence(
                terminal_state(result),
                menu_state(101, "menu-after-failed-heart"),
            )
            sent = []

            def send(payload):
                sent.append(payload)
                return proceed_receipt(payload)

            transition = campaign_attempt.return_to_main_menu(
                root,
                state_loader=states,
                send_payload_fn=send,
                reconcile_fn=lambda observed_root: issue_exit(result),
                maintenance_decision_hash="decision-fixed",
                maintenance_controller_hash="controller-fixed",
                request_id_factory=lambda: "failed-heart-maintenance",
            )
            self.assertEqual(1, len(sent))
            self.assertEqual(
                "maintenance_resolution", transition["authorization_kind"]
            )
            resolution = json.loads(
                (attempt_dir / "maintenance-resolution.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(resolution["eligible_for_cohort"])
            self.assertTrue(resolution["original_findings_preserved"])

    def test_nonclear_heart_without_explicit_maintenance_never_leaves_if_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record(heart=True)
            install_issue_chain(root, result)
            send = Mock()
            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: terminal_state(result),
                    send_payload_fn=send,
                    reconcile_fn=lambda observed_root: issue_exit(result),
                )
            send.assert_not_called()

    def test_prepare_maintenance_restart_binds_old_runtime_and_target_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                _exit_record,
                old_manifest,
                intent,
            ) = self._prepare_restart(root)
            source_digest = freeze_manifest.snapshot_digest(
                freeze_manifest.source_snapshot(root)
            )

            self.assertEqual(
                "maintenance_restart_intent", intent["record_type"]
            )
            self.assertFalse(intent["eligible_for_cohort"])
            self.assertEqual(
                "decision-fixed", intent["maintenance_decision_hash"]
            )
            self.assertEqual(
                "controller-fixed", intent["maintenance_controller_hash"]
            )
            self.assertEqual(
                source_digest, intent["target_source_digest"]
            )
            self.assertEqual(
                old_manifest["source_digest"],
                intent["old_runtime"]["freeze_source_digest"],
            )
            self.assertEqual(
                "launch-old", intent["old_runtime"]["launch_id"]
            )
            self.assertEqual(
                (110, 111, 222),
                (
                    intent["old_runtime"]["launcher_pid"],
                    intent["old_runtime"]["java_pid"],
                    intent["old_runtime"]["bridge_pid"],
                ),
            )
            self.assertEqual(
                {"launcher": True, "java": True, "bridge": False},
                intent["old_runtime"]["process_alive"],
            )
            intent_path = (
                attempt_dir
                / campaign_attempt.MAINTENANCE_RESTART_INTENT_NAME
            )
            self.assertEqual(
                intent,
                json.loads(intent_path.read_text(encoding="utf-8")),
            )

            replay = campaign_attempt.prepare_maintenance_restart(
                root,
                maintenance_decision_hash="decision-fixed",
                maintenance_controller_hash="controller-fixed",
                state_loader=lambda: terminal_state(result),
                process_alive_fn=lambda pid: pid in {110, 111},
                clock=Mock(
                    side_effect=AssertionError("intent was rewritten")
                ),
            )
            self.assertEqual(intent, replay)

    def test_verified_restart_release_is_state_only_and_receipt_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                records,
                exit_record,
                _old_manifest,
                intent,
            ) = self._prepare_restart(root)
            history_before = (root / "run-history.jsonl").read_bytes()
            new_manifest = self._install_restart_target(root)
            replacement = runtime_proof(new_manifest)
            terminal_payload = campaign_attempt._build_proceed_payload(
                terminal_state(result),
                attempt_binding(result),
                "terminal-action",
            )
            terminal_receipt = proceed_receipt(
                terminal_payload,
                result_seq=result["terminal_state_seq"],
            )
            terminal_receipt["accepted_state_seq"] = (
                result["terminal_state_seq"] - 1
            )
            terminal_receipt["requested_target_id"] = "action:end"
            terminal_receipt["resolved_target_id"] = "action:end"
            (root / "action-receipt.json").write_text(
                json.dumps(terminal_receipt), encoding="utf-8"
            )

            transition, sent, runtime_gate = self._complete_restart(
                root, result, exit_record, replacement
            )

            self.assertEqual(["state"], [
                payload["action"] for payload in sent
            ])
            self.assertEqual(2, runtime_gate.call_count)
            self.assertEqual(
                "game_over_to_main_menu_via_runtime_restart",
                transition["transition_kind"],
            )
            self.assertEqual("clear", transition["transition_status"])
            self.assertEqual(
                "maintenance_resolution", transition["authorization_kind"]
            )
            self.assertFalse(transition["eligible_for_cohort"])
            self.assertNotIn("payload", transition)
            self.assertNotIn("receipt", transition)
            self.assertNotIn("recovered_from_receipt", transition)
            self.assertEqual(
                campaign_attempt._sha256_object(intent),
                transition["restart_evidence"][
                    "restart_intent_sha256"
                ],
            )
            observed_runtime = transition["restart_evidence"][
                "replacement_runtime"
            ]
            for field, wanted in replacement.items():
                self.assertEqual(wanted, observed_runtime[field])
            self.assertEqual(40.0, observed_runtime["launch_started_at"])
            self.assertEqual(41.0, observed_runtime["bridge_started_at"])
            self.assertEqual(
                "logs/launches/launch-new.restart-predecessor.json",
                observed_runtime["restart_predecessor"]["record_path"],
            )
            self.assertEqual(
                102,
                transition["restart_evidence"]["state_probe"][
                    "result_state_seq"
                ],
            )
            sidecar = json.loads(
                (attempt_dir / "menu-transition.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transition, sidecar)
            self.assertEqual(
                history_before, (root / "run-history.jsonl").read_bytes()
            )
            self.assertEqual(
                [],
                campaign_selector.eligible_attempts(
                    records,
                    "decision-a",
                    controller_hash="controller-a",
                ),
            )

    def test_stale_precommit_resolution_is_archived_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                _exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            observed_result, binding = (
                campaign_attempt._validated_terminal_result(root)
            )
            old_resolution = (
                campaign_attempt._build_or_validate_maintenance_resolution(
                    root,
                    observed_result,
                    binding,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    clock=lambda: 55.0,
                )
            )
            old_manifest_path = (
                attempt_dir / "maintenance-freeze-manifest.json"
            )
            old_resolution_path = (
                attempt_dir / "maintenance-resolution.json"
            )
            old_manifest_bytes = old_manifest_path.read_bytes()
            old_resolution_bytes = old_resolution_path.read_bytes()

            install_freeze(
                root,
                "decision-new",
                "controller-new",
                launch_id="launch-newer",
                launcher_pid=310,
                java_pid=311,
                bridge_pid=312,
                bridge_instance_token="bridge-newer",
                started_at=70.0,
                bridge_started_at=71.0,
                generated_at=80.0,
            )
            new_resolution = (
                campaign_attempt._build_or_validate_maintenance_resolution(
                    root,
                    result,
                    binding,
                    maintenance_decision_hash="decision-new",
                    maintenance_controller_hash="controller-new",
                    clock=lambda: 90.0,
                    allow_uncommitted_recovery=True,
                )
            )

            recovery = json.loads(
                (
                    attempt_dir
                    / campaign_attempt.MAINTENANCE_PRECOMMIT_RECOVERY_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                old_manifest_bytes,
                (root / recovery[
                    "freeze_manifest_archive_path"
                ]).read_bytes(),
            )
            self.assertEqual(
                old_resolution_bytes,
                (root / recovery[
                    "maintenance_resolution_archive_path"
                ]).read_bytes(),
            )
            self.assertEqual(
                "decision-fixed",
                old_resolution["maintenance_decision_hash"],
            )
            self.assertEqual(
                "decision-new",
                new_resolution["maintenance_decision_hash"],
            )
            self.assertEqual(
                new_resolution,
                json.loads(old_resolution_path.read_text(encoding="utf-8")),
            )
            self.assertFalse(
                (attempt_dir / "menu-transition.json").exists()
            )
            second_resolution_bytes = old_resolution_path.read_bytes()
            second_resolution_sha256 = hashlib.sha256(
                second_resolution_bytes
            ).hexdigest()
            install_freeze(
                root,
                "decision-newest",
                "controller-newest",
                launch_id="launch-newest",
                launcher_pid=410,
                java_pid=411,
                bridge_pid=412,
                bridge_instance_token="bridge-newest",
                started_at=100.0,
                bridge_started_at=101.0,
                generated_at=110.0,
            )
            newest_resolution = (
                campaign_attempt._build_or_validate_maintenance_resolution(
                    root,
                    result,
                    binding,
                    maintenance_decision_hash="decision-newest",
                    maintenance_controller_hash="controller-newest",
                    clock=lambda: 120.0,
                    allow_uncommitted_recovery=True,
                )
            )
            chained_recovery_path = attempt_dir / (
                "maintenance-resolution-precommit-recovery-"
                f"{second_resolution_sha256}.json"
            )
            chained_recovery = json.loads(
                chained_recovery_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                second_resolution_bytes,
                (root / chained_recovery[
                    "maintenance_resolution_archive_path"
                ]).read_bytes(),
            )
            self.assertEqual(
                "decision-newest",
                newest_resolution["maintenance_decision_hash"],
            )
            (attempt_dir / "menu-transition.json").write_text(
                "{}", encoding="utf-8"
            )
            replayed = (
                campaign_attempt._build_or_validate_maintenance_resolution(
                    root,
                    result,
                    binding,
                    maintenance_decision_hash="decision-newest",
                    maintenance_controller_hash="controller-newest",
                    clock=Mock(
                        side_effect=AssertionError(
                            "completed recovery was rewritten"
                        )
                    ),
                    allow_uncommitted_recovery=True,
                )
            )
            self.assertEqual(newest_resolution, replayed)

    def test_replacement_runtime_source_chain_accepts_bound_migration(self):
        previous_sources = {
            "campaign_attempt.py": {"sha256": "a" * 64, "size": 1},
        }
        current_sources = {
            "campaign_attempt.py": {"sha256": "b" * 64, "size": 2},
        }
        previous_digest = freeze_manifest.snapshot_digest(
            previous_sources
        )
        current_digest = freeze_manifest.snapshot_digest(current_sources)
        migration = {
            "version": freeze_manifest._RUNTIME_SOURCE_MIGRATION_VERSION,
            "previous_source_digest": previous_digest,
            "previous_source_file_count": 1,
            "previous_sources": previous_sources,
            "current_source_digest": current_digest,
            "current_source_file_count": 1,
            "changed_paths": ["campaign_attempt.py"],
        }
        intent = {
            "target_source_digest": previous_digest,
            "target_source_file_count": 1,
        }
        manifest = {
            "source_digest": current_digest,
            "source_file_count": 1,
            "sources": current_sources,
        }
        runtime = {
            "bridge_runtime_source_digest": current_digest,
            "bridge_runtime_source_file_count": 1,
            "launch_evidence": {
                "runtime_source_migration": migration,
            },
        }
        self.assertTrue(
            campaign_attempt._replacement_runtime_sources_match(
                runtime, manifest, intent
            )
        )
        runtime["bridge_runtime_source_digest"] = previous_digest
        self.assertFalse(
            campaign_attempt._replacement_runtime_sources_match(
                runtime, manifest, intent
            )
        )

    def test_restart_reconciles_identity_preserving_prelaunch_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
            ) = self._install_restart_source(root)
            marker = root / "audit_marker.py"
            marker.write_text("AUDIT_MARKER = 1\n", encoding="utf-8")
            intent = campaign_attempt.prepare_maintenance_restart(
                root,
                maintenance_decision_hash="decision-fixed",
                maintenance_controller_hash="controller-fixed",
                state_loader=lambda: terminal_state(result),
                process_alive_fn=lambda pid: pid in {110, 111},
                clock=lambda: 25.0,
            )
            marker.write_text("AUDIT_MARKER = 2\n", encoding="utf-8")
            new_manifest = self._install_restart_target(root)
            self.assertNotEqual(
                intent["target_source_digest"],
                new_manifest["source_digest"],
            )
            replacement = runtime_proof(new_manifest)

            transition, sent, _runtime_gate = self._complete_restart(
                root, result, exit_record, replacement
            )

            self.assertEqual(1, len(sent))
            self.assertEqual("state", sent[0]["action"])
            self.assertEqual(
                "game_over_to_main_menu_via_runtime_restart",
                transition["transition_kind"],
            )
            reconciliation_path = attempt_dir / (
                campaign_attempt.MAINTENANCE_SOURCE_RECONCILIATION_NAME
            )
            reconciliation = json.loads(
                reconciliation_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                "runtime_identity_preserved",
                reconciliation["reconciliation_status"],
            )
            self.assertEqual(
                intent["target_source_digest"],
                reconciliation["target_source_digest"],
            )
            self.assertEqual(
                replacement["bridge_runtime_source_digest"],
                reconciliation["freeze_source_digest"],
            )

    def test_restart_transition_replays_after_state_acceptance_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                _attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            new_manifest = self._install_restart_target(root)
            replacement = runtime_proof(new_manifest)
            states = StateSequence(
                menu_state(101, "restart-menu"),
                menu_state(103, "restart-menu"),
            )
            transition, _sent, _runtime_gate = self._complete_restart(
                root,
                result,
                exit_record,
                replacement,
                states=states,
                state_probe_accepted_offset=1,
            )
            probe = transition["restart_evidence"]["state_probe"]
            self.assertEqual(102, probe["accepted_state_seq"])
            self.assertEqual(103, probe["result_state_seq"])

            no_send = Mock(
                side_effect=AssertionError("idempotent recovery sent command")
            )
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                return_value=replacement,
            ):
                replay = campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(
                        104, "restart-menu"
                    ),
                    send_payload_fn=no_send,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )
            self.assertEqual(transition, replay)
            no_send.assert_not_called()

    def test_restart_release_accepts_and_revalidates_two_hop_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            old_exit = install_launch_exit(root, "launch-old")
            first_manifest = install_freeze(
                root,
                "decision-fixed",
                "controller-fixed",
                launch_id="launch-retry-one",
                launcher_pid=310,
                java_pid=311,
                bridge_pid=312,
                bridge_instance_token="bridge-retry-one",
                started_at=32.0,
                bridge_started_at=33.0,
                generated_at=34.0,
            )
            first_predecessor = install_restart_predecessor(
                root, first_manifest, old_exit
            )
            install_launch_exit(
                root, "launch-retry-one", finished_at=35.0, exit_code=17
            )
            final_manifest = install_freeze(
                root,
                "decision-fixed",
                "controller-fixed",
                launch_id="launch-new",
                launcher_pid=210,
                java_pid=211,
                bridge_pid=212,
                bridge_instance_token="bridge-new",
                started_at=40.0,
                bridge_started_at=41.0,
                generated_at=50.0,
            )
            install_restart_predecessor(
                root,
                final_manifest,
                old_exit,
                lineage_parent=first_predecessor,
            )
            replacement = runtime_proof(final_manifest)
            transition, _sent, _runtime_gate = self._complete_restart(
                root, result, exit_record, replacement
            )
            parent_path = (
                transition["restart_evidence"]["replacement_runtime"][
                    "restart_predecessor"
                ]["record_path"]
            )
            self.assertEqual(
                "logs/launches/launch-new.restart-predecessor.json",
                parent_path,
            )
            current_predecessor = json.loads(
                (root / parent_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                (
                    "logs/launches/"
                    "launch-retry-one.restart-predecessor.json"
                ),
                current_predecessor["lineage_parent_record_path"],
            )
            self.assertEqual(
                campaign_attempt._sha256_object(first_predecessor),
                current_predecessor["lineage_parent_record_sha256"],
            )

            first_path = launch_game.restart_predecessor_path(
                root, "launch-retry-one"
            )
            tampered = json.loads(first_path.read_text(encoding="utf-8"))
            tampered["new_launcher_pid"] = 999
            first_path.write_text(json.dumps(tampered), encoding="utf-8")
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                return_value=replacement,
            ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(
                        103, "restart-menu"
                    ),
                    send_payload_fn=Mock(),
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )
            self.assertTrue((attempt_dir / "menu-transition.json").exists())

    def test_malformed_proceed_receipt_never_falls_back_to_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            terminal = terminal_state(result)
            payload = campaign_attempt._build_proceed_payload(
                terminal, attempt_binding(result), "proceed-request"
            )
            receipt = proceed_receipt(payload)
            receipt["selection_id"] = "malformed-selection"
            no_send = Mock(
                side_effect=AssertionError("malformed PROCEED fell back to STATE")
            )

            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                side_effect=AssertionError("restart validation was attempted"),
            ) as runtime_gate, self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "cannot recover an old PROCEED receipt",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(102, "restart-menu"),
                    send_payload_fn=no_send,
                    receipt_loader=lambda: receipt,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )

            runtime_gate.assert_not_called()
            no_send.assert_not_called()
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_resolved_proceed_receipt_never_falls_back_to_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            terminal = terminal_state(result)
            payload = campaign_attempt._build_proceed_payload(
                terminal, attempt_binding(result), "proceed-request"
            )
            receipt = proceed_receipt(payload)
            receipt["requested_target_id"] = "action:end"
            no_send = Mock(
                side_effect=AssertionError("resolved PROCEED fell back to STATE")
            )

            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                side_effect=AssertionError("restart validation was attempted"),
            ) as runtime_gate, self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "cannot recover an old PROCEED receipt",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(102, "restart-menu"),
                    send_payload_fn=no_send,
                    receipt_loader=lambda: receipt,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )

            runtime_gate.assert_not_called()
            no_send.assert_not_called()
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_exact_old_proceed_receipt_cannot_bypass_restart_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            payload = campaign_attempt._build_proceed_payload(
                terminal_state(result),
                attempt_binding(result),
                "forged-old-proceed",
            )
            receipt = proceed_receipt(payload)
            no_send = Mock(
                side_effect=AssertionError("old PROCEED bypassed restart")
            )
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                side_effect=AssertionError("restart validation was attempted"),
            ) as runtime_gate, self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "cannot recover an old PROCEED receipt",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(102, "restart-menu"),
                    send_payload_fn=no_send,
                    receipt_loader=lambda: receipt,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )
            runtime_gate.assert_not_called()
            no_send.assert_not_called()
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_malformed_terminal_leftover_receipt_cannot_trigger_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            payload = campaign_attempt._build_proceed_payload(
                terminal_state(result),
                attempt_binding(result),
                "terminal-action",
            )
            receipt = proceed_receipt(
                payload, result_seq=result["terminal_state_seq"]
            )
            receipt["accepted_state_seq"] = (
                result["terminal_state_seq"] - 1
            )
            receipt["requested_target_id"] = "action:end"
            receipt["resolved_target_id"] = "action:end"
            del receipt["resolved_target_id"]
            no_send = Mock(
                side_effect=AssertionError("malformed receipt triggered restart")
            )
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                side_effect=AssertionError("restart validation was attempted"),
            ) as runtime_gate, self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "leftover receipt fields are invalid",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(102, "restart-menu"),
                    send_payload_fn=no_send,
                    receipt_loader=lambda: receipt,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )
            runtime_gate.assert_not_called()
            no_send.assert_not_called()
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_unreadable_receipt_never_falls_back_to_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            self._install_restart_target(root)
            no_send = Mock(
                side_effect=AssertionError("unreadable receipt fell back to STATE")
            )

            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                side_effect=AssertionError("restart validation was attempted"),
            ) as runtime_gate, self.assertRaisesRegex(
                campaign_attempt.CampaignAttemptError,
                "receipt is unreadable",
            ):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(102, "restart-menu"),
                    send_payload_fn=no_send,
                    receipt_loader=Mock(
                        side_effect=campaign_attempt.CampaignAttemptError(
                            "receipt is unreadable"
                        )
                    ),
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )

            runtime_gate.assert_not_called()
            no_send.assert_not_called()
            self.assertFalse((attempt_dir / "menu-transition.json").exists())

    def test_restart_release_rejects_any_live_old_or_dead_new_pid(self):
        cases = [
            ("old_launcher_live", self.NEW_PIDS | {110}),
            ("old_java_live", self.NEW_PIDS | {111}),
            ("old_bridge_live", self.NEW_PIDS | {222}),
            ("new_launcher_dead", self.NEW_PIDS - {210}),
            ("new_java_dead", self.NEW_PIDS - {211}),
            ("new_bridge_dead", self.NEW_PIDS - {212}),
        ]
        for label, live_pids in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    result,
                    attempt_dir,
                    _records,
                    exit_record,
                    _old_manifest,
                    _intent,
                ) = self._prepare_restart(root)
                new_manifest = self._install_restart_target(root)
                send = Mock()

                def validate_runtime(
                    state,
                    observed_root,
                    manifest,
                    *,
                    process_alive_fn,
                ):
                    replacement = runtime_proof(new_manifest)
                    dead = [
                        pid
                        for pid in (
                            replacement["launcher_pid"],
                            replacement["java_pid"],
                            replacement["bridge_pid"],
                        )
                        if process_alive_fn(pid) is not True
                    ]
                    if dead:
                        raise pre_run_binding.PreRunBindingError(
                            "replacement runtime process is not alive"
                        )
                    return replacement

                with patch.object(
                    pre_run_binding,
                    "validate_pre_dispatch_runtime",
                    side_effect=validate_runtime,
                ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: menu_state(
                            101, "restart-menu"
                        ),
                        send_payload_fn=send,
                        reconcile_fn=lambda observed_root: exit_record,
                        maintenance_decision_hash="decision-fixed",
                        maintenance_controller_hash="controller-fixed",
                        process_alive_fn=lambda pid, live=live_pids: (
                            pid in live
                        ),
                    )
                send.assert_not_called()
                self.assertFalse(
                    (attempt_dir / "menu-transition.json").exists()
                )

    def test_restart_release_rejects_same_launch_or_pid_overlap(self):
        for case in ("same_launch", "pid_overlap"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    result,
                    attempt_dir,
                    _records,
                    exit_record,
                    _old_manifest,
                    _intent,
                ) = self._prepare_restart(root)
                new_manifest = self._install_restart_target(root)
                replacement = runtime_proof(new_manifest)
                replacement = json.loads(json.dumps(replacement))
                if case == "same_launch":
                    replacement["launch_id"] = "launch-old"
                    replacement["launch_evidence"]["launch_id"] = (
                        "launch-old"
                    )
                    live_pids = set(self.NEW_PIDS)
                else:
                    replacement["launcher_pid"] = 110
                    replacement["launch_evidence"]["launcher_pid"] = 110
                    live_pids = set(self.NEW_PIDS)
                send = Mock()
                with patch.object(
                    pre_run_binding,
                    "validate_pre_dispatch_runtime",
                    return_value=replacement,
                ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: menu_state(
                            101, "restart-menu"
                        ),
                        send_payload_fn=send,
                        reconcile_fn=lambda observed_root: exit_record,
                        maintenance_decision_hash="decision-fixed",
                        maintenance_controller_hash="controller-fixed",
                        process_alive_fn=lambda pid, live=live_pids: (
                            pid in live
                        ),
                    )
                send.assert_not_called()
                self.assertFalse(
                    (attempt_dir / "menu-transition.json").exists()
                )

    def test_restart_release_rejects_missing_or_tampered_intent_or_exit(self):
        for case in (
            "missing_intent",
            "tampered_intent",
            "missing_exit",
            "tampered_exit",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if case == "missing_intent":
                    (
                        result,
                        attempt_dir,
                        _records,
                        exit_record,
                        _old_manifest,
                    ) = self._install_restart_source(root)
                else:
                    (
                        result,
                        attempt_dir,
                        _records,
                        exit_record,
                        _old_manifest,
                        _intent,
                    ) = self._prepare_restart(root)
                if case != "missing_exit":
                    old_exit = install_launch_exit(root, "launch-old")
                    if case == "tampered_exit":
                        old_exit["java_pid"] = 999
                        (
                            root / "logs" / "launches"
                            / "launch-old.exit.json"
                        ).write_text(
                            json.dumps(old_exit), encoding="utf-8"
                        )
                if case == "tampered_intent":
                    intent_path = (
                        attempt_dir
                        / campaign_attempt.MAINTENANCE_RESTART_INTENT_NAME
                    )
                    intent = json.loads(
                        intent_path.read_text(encoding="utf-8")
                    )
                    intent["maintenance_decision_hash"] = "tampered"
                    intent_path.write_text(
                        json.dumps(intent), encoding="utf-8"
                    )
                new_manifest = install_freeze(
                    root,
                    "decision-fixed",
                    "controller-fixed",
                    launch_id="launch-new",
                    launcher_pid=210,
                    java_pid=211,
                    bridge_pid=212,
                    bridge_instance_token="bridge-new",
                    started_at=40.0,
                    bridge_started_at=41.0,
                    generated_at=50.0,
                )
                send = Mock()
                with patch.object(
                    pre_run_binding,
                    "validate_pre_dispatch_runtime",
                    return_value=runtime_proof(new_manifest),
                ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: menu_state(
                            101, "restart-menu"
                        ),
                        send_payload_fn=send,
                        reconcile_fn=lambda observed_root: exit_record,
                        maintenance_decision_hash="decision-fixed",
                        maintenance_controller_hash="controller-fixed",
                        process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                    )
                send.assert_not_called()
                self.assertFalse(
                    (attempt_dir / "menu-transition.json").exists()
                )
                self.assertFalse(
                    (attempt_dir / "maintenance-resolution.json").exists()
                )
                self.assertFalse(
                    (
                        attempt_dir
                        / "maintenance-freeze-manifest.json"
                    ).exists()
                )

    def test_restart_release_rejects_runtime_change_or_invalid_final_state(self):
        for case in (
            "runtime_change",
            "state_mismatch",
            "stale_attempt_binding",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    result,
                    attempt_dir,
                    _records,
                    exit_record,
                    _old_manifest,
                    _intent,
                ) = self._prepare_restart(root)
                new_manifest = self._install_restart_target(root)
                replacement = runtime_proof(new_manifest)
                if case == "runtime_change":
                    changed = json.loads(json.dumps(replacement))
                    changed["bridge_instance_token"] = "bridge-raced"
                    runtime_side_effect = [replacement, changed]
                    states = StateSequence(
                        menu_state(101, "restart-menu"),
                        menu_state(102, "restart-menu"),
                    )
                elif case == "state_mismatch":
                    runtime_side_effect = [replacement, replacement]
                    states = StateSequence(
                        menu_state(101, "restart-menu"),
                        menu_state(103, "restart-menu"),
                    )
                else:
                    runtime_side_effect = [replacement, replacement]
                    stale_menu = menu_state(102, "restart-menu")
                    stale_menu.update(attempt_binding(result))
                    stale_menu["terminal_state_seq"] = result[
                        "terminal_state_seq"
                    ]
                    states = StateSequence(
                        menu_state(101, "restart-menu"),
                        stale_menu,
                    )
                with self.assertRaises(campaign_attempt.CampaignAttemptError):
                    self._complete_restart(
                        root,
                        result,
                        exit_record,
                        replacement,
                        states=states,
                        runtime_side_effect=runtime_side_effect,
                    )
                self.assertFalse(
                    (attempt_dir / "menu-transition.json").exists()
                )

    def test_restart_transition_is_idempotent_and_tamper_evident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                result,
                attempt_dir,
                _records,
                exit_record,
                _old_manifest,
                _intent,
            ) = self._prepare_restart(root)
            new_manifest = self._install_restart_target(root)
            replacement = runtime_proof(new_manifest)
            transition, _sent, _runtime_gate = self._complete_restart(
                root, result, exit_record, replacement
            )

            no_send = Mock(
                side_effect=AssertionError("idempotent recovery sent command")
            )
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                return_value=replacement,
            ):
                replay = campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(
                        103, "restart-menu"
                    ),
                    send_payload_fn=no_send,
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                    clock=Mock(
                        side_effect=AssertionError(
                            "restart transition was rewritten"
                        )
                    ),
                )
            self.assertEqual(transition, replay)
            no_send.assert_not_called()

            valid_created_at = transition["created_at"]
            for impossible_created_at in (20.0, 45.0, 55.0):
                transition["created_at"] = impossible_created_at
                (attempt_dir / "menu-transition.json").write_text(
                    json.dumps(transition), encoding="utf-8"
                )
                with patch.object(
                    pre_run_binding,
                    "validate_pre_dispatch_runtime",
                    return_value=replacement,
                ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                    campaign_attempt.return_to_main_menu(
                        root,
                        state_loader=lambda: menu_state(
                            103, "restart-menu"
                        ),
                        send_payload_fn=no_send,
                        reconcile_fn=lambda observed_root: exit_record,
                        maintenance_decision_hash="decision-fixed",
                        maintenance_controller_hash="controller-fixed",
                        process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                    )
            transition["created_at"] = valid_created_at
            transition["restart_evidence"]["replacement_runtime"][
                "bridge_instance_token"
            ] = "tampered"
            (attempt_dir / "menu-transition.json").write_text(
                json.dumps(transition), encoding="utf-8"
            )
            with patch.object(
                pre_run_binding,
                "validate_pre_dispatch_runtime",
                return_value=replacement,
            ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt.return_to_main_menu(
                    root,
                    state_loader=lambda: menu_state(
                        104, "restart-menu"
                    ),
                    send_payload_fn=Mock(),
                    reconcile_fn=lambda observed_root: exit_record,
                    maintenance_decision_hash="decision-fixed",
                    maintenance_controller_hash="controller-fixed",
                    process_alive_fn=lambda pid: pid in self.NEW_PIDS,
                )


class CampaignAttemptRunTests(unittest.TestCase):
    def test_p0_only_batch_releases_heart_victory_for_next_subcohort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = result_record(heart=True)
            launch = {
                "controller_exit": {
                    "controller_exit_status": "issues",
                },
            }
            transition = {
                "transition_status": "p0_only_validation",
            }
            with patch.object(
                campaign_attempt,
                "launch_attempt",
                return_value=launch,
            ) as launch_call, patch.object(
                campaign_attempt,
                "_validated_terminal_result",
                return_value=(result, {}),
            ), patch.object(
                campaign_attempt,
                "return_to_main_menu",
                return_value=transition,
            ) as return_call:
                outcome = campaign_attempt.run_attempt(
                    root,
                    "decision-a",
                    "controller-a",
                    p0_only_batch=True,
                )

            self.assertEqual("cohort_nonclear", outcome["return_status"])
            self.assertEqual(transition, outcome["menu_transition"])
            launch_call.assert_called_once()
            self.assertTrue(return_call.call_args.kwargs["p0_only_batch"])


class CampaignAttemptCliTests(unittest.TestCase):
    def test_run_cli_dispatches_and_prints_json(self):
        expected = {"return_status": "held_heart_victory"}
        output = io.StringIO()
        with patch.object(
            campaign_attempt, "run_attempt", return_value=expected
        ) as run, contextlib.redirect_stdout(output):
            code = campaign_attempt.main(
                [
                    "run",
                    "--decision-hash",
                    "decision-a",
                    "--controller-hash",
                    "controller-a",
                    "--max-actions",
                    "50",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual(expected, json.loads(output.getvalue()))
        self.assertEqual(50, run.call_args.kwargs["max_actions"])

    def test_prepare_maintenance_restart_cli_forwards_hashes(self):
        expected = {"record_type": "maintenance_restart_intent"}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                campaign_attempt,
                "prepare_maintenance_restart",
                return_value=expected,
            ) as prepare, contextlib.redirect_stdout(output):
                code = campaign_attempt.main(
                    [
                        "prepare-maintenance-restart",
                        "--root",
                        str(root),
                        "--decision-hash",
                        "decision-fixed",
                        "--controller-hash",
                        "controller-fixed",
                    ]
                )

        self.assertEqual(0, code)
        self.assertEqual(expected, json.loads(output.getvalue()))
        prepare.assert_called_once_with(
            root,
            maintenance_decision_hash="decision-fixed",
            maintenance_controller_hash="controller-fixed",
        )


class CampaignAttemptAtomicEvidenceTests(unittest.TestCase):
    def test_create_once_failure_never_leaves_partial_final_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance-evidence.json"
            payload = {"schema_version": 2, "value": "bound"}
            with patch.object(
                campaign_attempt.os,
                "fsync",
                side_effect=OSError("simulated crash before publish"),
            ), self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt._write_json_once(path, payload)

            self.assertFalse(path.exists())
            self.assertEqual([], list(path.parent.glob(".*.tmp")))
            campaign_attempt._write_json_once(path, payload)
            campaign_attempt._write_json_once(path, payload)
            with self.assertRaises(campaign_attempt.CampaignAttemptError):
                campaign_attempt._write_json_once(
                    path, {"schema_version": 2, "value": "changed"}
                )


def random_for_tests():
    # The empty-history bootstrap path is deterministic, but supplying a real
    # RNG also exercises the public selector API without hand-written output.
    return __import__("random").Random(7)


if __name__ == "__main__":
    unittest.main()
