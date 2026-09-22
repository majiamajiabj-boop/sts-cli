import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import campaign_selector
import freeze_manifest
import pre_run_binding
from tests.test_freeze_manifest import (
    bind_bridge_runtime_sources,
    install_replay_bundle,
)


def replay_evidence(decision_hash="decision-a"):
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


def install_runtime_provenance(
    root, *, launcher_pid=110, java_pid=111, bridge_pid=222
):
    root = Path(root)
    bridge_bytes = b"BRIDGE = 2\n"
    jar = b"communication-mod"
    (root / "bridge.py").write_bytes(bridge_bytes)
    (root / "launch_game.py").write_text(
        "LAUNCHER = 2\n", encoding="utf-8"
    )
    (root / "CommunicationMod.jar").write_bytes(jar)
    game = root / "runtime-game"
    (game / "mods").mkdir(parents=True)
    (game / "jre" / "bin").mkdir(parents=True)
    (game / "mods" / "CommunicationMod.jar").write_bytes(jar)
    stderr = game / "communication_mod_errors.log"
    stderr.write_bytes(b"old-prefix")
    launch_log = root / "logs" / "launches" / "launch-test.log"
    launch_log.parent.mkdir(parents=True)
    launch_log.write_bytes(b"")
    launch = {
        "schema_version": 2,
        "launch_id": "launch-test",
        "started_at": 10.0,
        "launch_log_path": "logs/launches/launch-test.log",
        "launch_start_record_path": (
            "logs/launches/launch-test.start.json"
        ),
        "launcher_pid": launcher_pid,
        "java_pid": java_pid,
        "command": [
            str((game / "jre" / "bin" / "java.exe").resolve()),
            "-jar",
            str((game / "ModTheSpire.jar").resolve()),
            "--skip-intro",
            "--mods",
            "basemod,CommunicationMod",
        ],
        "game_dir": str(game.resolve()),
        "root_communication_mod_jar_sha256": hashlib.sha256(jar).hexdigest(),
        "installed_communication_mod_jar_sha256": hashlib.sha256(jar).hexdigest(),
        "bridge_stderr_path": str(stderr.resolve()),
        "bridge_stderr_start_offset": len(b"old-prefix"),
        "bridge_stderr_start_sha256": hashlib.sha256(b"old-prefix").hexdigest(),
        "restart_predecessor_record_path": None,
        "restart_predecessor_record_sha256": None,
    }
    (root / launch["launch_start_record_path"]).write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "launch-latest.json").write_text(
        json.dumps(launch), encoding="utf-8"
    )
    bridge_instance = {
        "schema_version": 2,
        "protocol_version": 2,
        "instance_token": "bridge-test",
        "bridge_pid": bridge_pid,
        "parent_java_pid": java_pid,
        "launch_id": "launch-test",
        "bridge_sha256": hashlib.sha256(bridge_bytes).hexdigest(),
        "started_at": 11.0,
    }
    (root / "bridge-instance.json").write_text(
        json.dumps(bridge_instance), encoding="utf-8"
    )
    return launch, bridge_instance


def menu_state():
    return {
        "protocol_version": 2,
        "in_game": False,
        "ready_for_command": True,
        "state_seq": 50,
        "decision_id": "decision-menu",
        "phase": "MAIN_MENU",
    }


def selection():
    value = campaign_selector.select_character(
        [], "decision-a", controller_hash="controller-a"
    )
    value["selection_id"] = "selection-a"
    value["created_at"] = 100.0
    return value


class PreRunBindingTests(unittest.TestCase):
    @staticmethod
    def freeze_evidence():
        return {
            "full_tests": {
                "returncode": 0, "test_count": 1,
                "stdout_size": 0, "stderr_size": 0,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
            },
            "git_diff_check": {
                "returncode": 0, "test_count": None,
                "stdout_size": 0, "stderr_size": 0,
                "stdout_sha256": "c" * 64,
                "stderr_sha256": "d" * 64,
            },
            "runtime_artifacts_unchanged": True,
            "runtime_artifacts_before_sha256": "e" * 64,
            "runtime_artifacts_after_sha256": "e" * 64,
        }

    def build_test_manifest(self, root):
        replay_report = install_replay_bundle(root)
        bind_bridge_runtime_sources(root)
        with patch.object(
            freeze_manifest,
            "_fresh_policy_hashes",
            return_value={
                "decision_hash": "decision-a",
                "controller_hash": "controller-a",
            },
        ):
            return freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=self.freeze_evidence(),
                decision_case_replay=replay_report,
            )

    def install_start_checkpoint(self, root):
        install_runtime_provenance(root)
        (root / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
        selected = selection()
        manifest = self.build_test_manifest(root)
        freeze_manifest.write_manifest(
            root / "freeze-manifest.json", manifest
        )
        (root / "next-run-selection.json").write_text(
            json.dumps(selected), encoding="utf-8"
        )
        (root / "run-history.jsonl").write_text("", encoding="utf-8")
        payload = pre_run_binding.build_start_payload(
            menu_state(), selected, [], "decision-a", "controller-a",
            request_id="request-a",
        )
        return selected, manifest, payload

    def test_start_payload_is_bound_to_exact_selector_artifact(self):
        selected = selection()
        payload = pre_run_binding.build_start_payload(
            menu_state(), selected, [], "decision-a", "controller-a",
            request_id="request-a",
        )
        self.assertEqual("run:IRONCLAD:a0:standard", payload["target_id"])
        self.assertEqual(
            pre_run_binding.selection_digest(selected),
            payload["selection_digest"],
        )
        self.assertIs(
            payload,
            pre_run_binding.validate_start_payload(
                payload, menu_state(), selected, [],
                "decision-a", "controller-a",
            ),
        )

    def test_every_start_binding_field_fails_closed(self):
        selected = selection()
        payload = pre_run_binding.build_start_payload(
            menu_state(), selected, [], "decision-a", "controller-a",
            request_id="request-a",
        )
        for field in (
            "selection_id", "selection_digest", "decision_hash",
            "controller_hash", "character", "player_class",
            "ascension_level", "run_type", "target_id",
            "expected_seq", "decision_id", "phase", "policy_version",
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(payload)
                changed[field] = "wrong" if field != "ascension_level" else 1
                with self.assertRaises(pre_run_binding.PreRunBindingError):
                    pre_run_binding.validate_start_payload(
                        changed, menu_state(), selected, [],
                        "decision-a", "controller-a",
                    )

    def test_handwritten_or_stale_selection_cannot_launch(self):
        selected = selection()
        selected["eligible_attempt_ids"] = ["invented"]
        with self.assertRaises(pre_run_binding.PreRunBindingError):
            pre_run_binding.build_start_payload(
                menu_state(), selected, [], "decision-a", "controller-a",
                request_id="request-a",
            )

    def test_selection_id_cannot_be_reused(self):
        selected = selection()
        history = [{
            "record_type": "terminal_result",
            "attempt_id": "attempt-old",
            "selection_id": selected["selection_id"],
        }]
        with self.assertRaises(pre_run_binding.PreRunBindingError):
            pre_run_binding.validate_pending_selection(
                selected, history, "decision-a", "controller-a"
            )

    def test_pending_selection_uses_same_exact_pair_unresolved_gate(self):
        selected = selection()
        unresolved = {
            "schema_version": 2,
            "record_type": "terminal_result",
            "termination_kind": "operational_error",
            "attempt_id": "attempt-broken",
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
        }
        with self.assertRaises(pre_run_binding.PreRunBindingError):
            pre_run_binding.validate_pending_selection(
                selected,
                [unresolved],
                "decision-a",
                "controller-a",
            )

        old_pair = {
            **unresolved,
            "decision_hash": "decision-old",
        }
        self.assertIs(
            selected,
            pre_run_binding.validate_pending_selection(
                selected,
                [old_pair],
                "decision-a",
                "controller-a",
            ),
        )

    def test_main_menu_state_must_be_ready_protocol_v2(self):
        selected = selection()
        for field, value in (
            ("protocol_version", 1), ("in_game", True),
            ("ready_for_command", False), ("phase", "GAME_OVER"),
        ):
            state = menu_state()
            state[field] = value
            with self.subTest(field=field):
                with self.assertRaises(pre_run_binding.PreRunBindingError):
                    pre_run_binding.build_start_payload(
                        state, selected, [], "decision-a", "controller-a",
                        request_id="request-a",
                    )

    def test_resume_binds_existing_attempt_and_seed(self):
        context = {
            "schema_version": 2,
            "policy_version": "fast-policy-v5",
            "goal_mode": "HEART",
            "attempt_id": "attempt-a",
            "run_id": "DEFECT:0:123",
            "seed": 123,
            "character": "DEFECT",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "selection_id": "selection-a",
            "terminal_state_seq": None,
        }
        payload = pre_run_binding.build_resume_payload(
            menu_state(), context, request_id="request-a"
        )
        self.assertEqual("resume:DEFECT:autosave", payload["target_id"])
        self.assertEqual(123, payload["seed"])
        self.assertIs(
            payload,
            pre_run_binding.validate_resume_payload(
                payload, menu_state(), context
            ),
        )

    def test_terminal_context_cannot_resume(self):
        context = {
            "schema_version": 2,
            "policy_version": "fast-policy-v5",
            "goal_mode": "HEART",
            "attempt_id": "attempt-a",
            "run_id": "DEFECT:0:123",
            "seed": 123,
            "character": "DEFECT",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "selection_id": "selection-a",
            "terminal_state_seq": 99,
        }
        with self.assertRaises(pre_run_binding.PreRunBindingError):
            pre_run_binding.build_resume_payload(
                menu_state(), context, request_id="request-a"
            )

    def test_release_checkpoint_detects_source_change(self):
        selected = selection()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_runtime_provenance(root)
            source = root / "policy.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            manifest = self.build_test_manifest(root)
            manifest_path = root / "freeze-manifest.json"
            freeze_manifest.write_manifest(manifest_path, manifest)
            pre_run_binding.validate_release_checkpoint(
                selected, [], root,
                manifest_path=manifest_path,
                cohort_review_path=root / "cohort-review.json",
            )
            source.write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaises(pre_run_binding.PreRunBindingError):
                pre_run_binding.validate_release_checkpoint(
                    selected, [], root,
                    manifest_path=manifest_path,
                    cohort_review_path=root / "cohort-review.json",
                )

    def test_mandatory_review_cannot_be_missing(self):
        selected = selection()
        attempts = [
            {"attempt_id": f"attempt-{index}", "hidden_entry": False,
             "heart_defeated": False}
            for index in range(6)
        ]
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": attempts,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_runtime_provenance(root)
            (root / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
            manifest = self.build_test_manifest(root)
            freeze_manifest.write_manifest(
                root / "freeze-manifest.json", manifest
            )
            with patch(
                "pre_run_binding.cohort_report.build_cohort_report",
                return_value=cohort,
            ):
                with self.assertRaises(pre_run_binding.PreRunBindingError):
                    pre_run_binding.validate_release_checkpoint(
                        selected, [], root,
                        manifest_path=root / "freeze-manifest.json",
                        cohort_review_path=root / "cohort-review.json",
                    )

    def test_acceptance_time_review_rechecks_attempt_artifacts(self):
        selected = selection()
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": [
                {
                    "attempt_id": f"attempt-{index}",
                    "hidden_entry": False,
                    "heart_defeated": False,
                }
                for index in range(6)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_runtime_provenance(root)
            (root / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
            manifest = self.build_test_manifest(root)
            freeze_manifest.write_manifest(
                root / "freeze-manifest.json", manifest
            )
            (root / "next-run-selection.json").write_text(
                json.dumps(selected), encoding="utf-8"
            )
            (root / "run-history.jsonl").write_text("", encoding="utf-8")
            (root / "cohort-review.json").write_text(
                json.dumps({"review_status": "clear"}), encoding="utf-8"
            )
            payload = pre_run_binding.build_start_payload(
                menu_state(), selected, [], "decision-a", "controller-a",
                request_id="request-a",
            )
            attempt_root = root / "logs" / "attempts"
            attempt_root.mkdir(parents=True)
            (attempt_root / "tampered-after-selection").write_text(
                "changed", encoding="utf-8"
            )

            def reject_tampered(review, observed_cohort, observed_root):
                self.assertEqual(attempt_root, observed_root)
                self.assertEqual(cohort, observed_cohort)
                return False

            with patch(
                "pre_run_binding.cohort_report.build_cohort_report",
                return_value=cohort,
            ), patch(
                "pre_run_binding.cohort_review.validate_review_gate",
                side_effect=reject_tampered,
            ):
                with self.assertRaisesRegex(
                    pre_run_binding.PreRunBindingError,
                    "review gate is not clear",
                ):
                    pre_run_binding.load_and_validate_start_payload(
                        payload,
                        menu_state(),
                        selection_path=root / "next-run-selection.json",
                        history_path=root / "run-history.jsonl",
                        manifest_path=root / "freeze-manifest.json",
                        cohort_review_path=root / "cohort-review.json",
                    )

    def test_start_acceptance_binds_live_java_bridge_and_frozen_jars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, payload = self.install_start_checkpoint(root)
            observed = pre_run_binding.load_and_validate_start_payload(
                payload,
                menu_state(),
                selection_path=root / "next-run-selection.json",
                history_path=root / "run-history.jsonl",
                process_alive_fn=lambda pid: pid in {110, 111, 222},
                current_pid_fn=lambda: 222,
                current_parent_pid_fn=lambda: 111,
            )
            self.assertIs(payload, observed)

    def test_pre_dispatch_runtime_requires_same_frozen_live_pid_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest, _ = self.install_start_checkpoint(root)
            observed = pre_run_binding.validate_pre_dispatch_runtime(
                menu_state(),
                root,
                manifest,
                process_alive_fn=lambda pid: pid in {110, 111, 222},
            )
            self.assertEqual(110, observed["launcher_pid"])
            self.assertEqual(111, observed["java_pid"])
            self.assertEqual(222, observed["bridge_pid"])
            self.assertEqual("launch-test", observed["launch_id"])
            self.assertEqual(
                "bridge-test", observed["bridge_instance_token"]
            )
            self.assertEqual(
                manifest["launch_evidence"], observed["launch_evidence"]
            )

            for dead_pid, label in (
                (110, "launcher"), (111, "java"), (222, "bridge")
            ):
                with self.subTest(dead=label), self.assertRaisesRegex(
                    pre_run_binding.PreRunBindingError, label
                ):
                    pre_run_binding.validate_pre_dispatch_runtime(
                        menu_state(),
                        root,
                        manifest,
                        process_alive_fn=lambda pid, dead=dead_pid: (
                            pid != dead
                        ),
                    )

    def test_start_acceptance_sidecar_is_unique_idempotent_and_full_digest_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected, _, payload = self.install_start_checkpoint(root)
            kwargs = {
                "selection_path": root / "next-run-selection.json",
                "history_path": root / "run-history.jsonl",
                "process_alive_fn": lambda pid: pid in {110, 111, 222},
                "current_pid_fn": lambda: 222,
                "current_parent_pid_fn": lambda: 111,
                "created_at": 123.0,
            }

            first = pre_run_binding.accept_start_payload(
                payload, menu_state(), **kwargs
            )
            second = pre_run_binding.accept_start_payload(
                payload, menu_state(), **kwargs
            )

            self.assertEqual(first["path"], second["path"])
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(selected, first["record"]["selection"])
            self.assertEqual(
                pre_run_binding.selection_digest(selected),
                first["record"]["accepted_receipt"]["selection_digest"],
            )
            self.assertEqual(
                [first["path"]],
                list((root / "logs" / "start-acceptances").rglob("*.json")),
            )

            tampered = copy.deepcopy(selected)
            tampered["reason"] = "tampered-non-core-selection-field"
            self.assertNotEqual(
                pre_run_binding.selection_digest(selected),
                pre_run_binding.selection_digest(tampered),
            )
            with self.assertRaises(pre_run_binding.PreRunBindingError):
                pre_run_binding.load_start_acceptance(root, tampered)

    def test_start_acceptance_tamper_is_never_treated_as_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected, _, payload = self.install_start_checkpoint(root)
            accepted = pre_run_binding.accept_start_payload(
                payload,
                menu_state(),
                selection_path=root / "next-run-selection.json",
                history_path=root / "run-history.jsonl",
                process_alive_fn=lambda pid: pid in {110, 111, 222},
                current_pid_fn=lambda: 222,
                current_parent_pid_fn=lambda: 111,
                created_at=123.0,
            )
            record = copy.deepcopy(accepted["record"])
            record["selection"]["stats"]["IRONCLAD"]["attempts"] = 99
            accepted["path"].write_text(
                json.dumps(record), encoding="utf-8"
            )

            with self.assertRaises(pre_run_binding.PreRunBindingError):
                pre_run_binding.load_start_acceptance(
                    root, selected, payload=payload
                )

    def test_start_acceptance_rejects_old_bridge_pid_chain_and_stale_launch(self):
        for case in (
            "bridge_hash", "runtime_source", "bridge_not_current", "parent_pid",
            "launch_token",
            "bridge_dead", "java_dead", "launcher_dead",
            "launch_finished"
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, payload = self.install_start_checkpoint(root)
                instance_path = root / "bridge-instance.json"
                launch_path = root / "launch-latest.json"
                alive = lambda pid: pid in {110, 111, 222}
                if case in {
                    "bridge_hash", "runtime_source", "bridge_not_current", "parent_pid",
                    "launch_token",
                }:
                    instance = json.loads(
                        instance_path.read_text(encoding="utf-8")
                    )
                    if case == "bridge_hash":
                        instance["bridge_sha256"] = "0" * 64
                    elif case == "runtime_source":
                        instance["runtime_source_digest"] = "0" * 64
                    elif case == "bridge_not_current":
                        instance["bridge_pid"] = 333
                        alive = lambda pid: pid in {110, 111, 222, 333}
                    elif case == "launch_token":
                        instance["launch_id"] = "old-launch"
                    else:
                        instance["parent_java_pid"] = 999
                    instance_path.write_text(
                        json.dumps(instance), encoding="utf-8"
                    )
                elif case == "bridge_dead":
                    alive = lambda pid: pid in {110, 111}
                elif case == "java_dead":
                    alive = lambda pid: pid in {110, 222}
                elif case == "launcher_dead":
                    alive = lambda pid: pid in {111, 222}
                else:
                    launch = json.loads(
                        launch_path.read_text(encoding="utf-8")
                    )
                    launch.update({"finished_at": 12.0, "exit_code": 0})
                    launch_path.write_text(
                        json.dumps(launch), encoding="utf-8"
                    )
                with self.assertRaises(pre_run_binding.PreRunBindingError):
                    pre_run_binding.load_and_validate_start_payload(
                        payload,
                        menu_state(),
                        selection_path=root / "next-run-selection.json",
                        history_path=root / "run-history.jsonl",
                        process_alive_fn=alive,
                        current_pid_fn=lambda: 222,
                        current_parent_pid_fn=lambda: 111,
                    )

    def test_start_acceptance_rejects_startup_log_append_and_jar_tamper(self):
        for case in (
            "startup_log", "stderr_missing", "installed_jar",
            "start_sidecar",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, payload = self.install_start_checkpoint(root)
                launch = json.loads(
                    (root / "launch-latest.json").read_text(
                        encoding="utf-8"
                    )
                )
                if case == "startup_log":
                    with Path(launch["bridge_stderr_path"]).open("ab") as handle:
                        handle.write(b"bridge startup failed\n")
                elif case == "stderr_missing":
                    Path(launch["bridge_stderr_path"]).unlink()
                elif case == "start_sidecar":
                    sidecar = root / launch["launch_start_record_path"]
                    changed = dict(launch)
                    changed["java_pid"] = 999
                    sidecar.write_text(
                        json.dumps(changed), encoding="utf-8"
                    )
                else:
                    game = Path(launch["game_dir"])
                    (game / "mods" / "CommunicationMod.jar").write_bytes(
                        b"stale-installed-jar"
                    )
                with self.assertRaises(pre_run_binding.PreRunBindingError):
                    pre_run_binding.load_and_validate_start_payload(
                        payload,
                        menu_state(),
                        selection_path=root / "next-run-selection.json",
                        history_path=root / "run-history.jsonl",
                        process_alive_fn=lambda pid: pid in {110, 111, 222},
                        current_pid_fn=lambda: 222,
                        current_parent_pid_fn=lambda: 111,
                    )


if __name__ == "__main__":
    unittest.main()
