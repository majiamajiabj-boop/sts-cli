import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import launch_game


class LaunchGameRestartPredecessorTests(unittest.TestCase):
    def test_identity_reconciliation_binds_attested_runtime(self):
        intent = {
            "attempt_id": "attempt-a",
            "created_at": 1.0,
            "target_source_digest": "1" * 64,
            "target_source_file_count": 2,
            "maintenance_decision_hash": "decision-a",
            "maintenance_controller_hash": "controller-a",
            "old_runtime": {
                "launch_id": "launch-old",
                "bridge_sha256": "2" * 64,
            },
        }
        replacement = {
            "launch_id": "launch-new",
            "bridge_sha256": "2" * 64,
            "bridge_runtime_source_digest": "3" * 64,
            "bridge_runtime_source_file_count": 2,
        }
        jar_sha256 = "4" * 64
        launch_evidence = {
            "root_communication_mod_jar_sha256": jar_sha256,
            "installed_communication_mod_jar_sha256": jar_sha256,
        }
        old_start = {
            "root_communication_mod_jar_sha256": jar_sha256,
            "installed_communication_mod_jar_sha256": jar_sha256,
        }
        attestation = {
            "release_gate_passed": True,
            "source_digest": "3" * 64,
            "source_file_count": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
        }
        attestation_bytes = json.dumps(attestation).encode("utf-8")
        reconciliation = {
            "schema_version": 2,
            "record_type": "maintenance_restart_source_reconciliation",
            "reconciliation_status": "runtime_identity_preserved",
            "attempt_id": "attempt-a",
            "restart_intent_sha256": launch_game._canonical_sha256(intent),
            "old_launch_id": "launch-old",
            "replacement_launch_id": "launch-new",
            "target_source_digest": "1" * 64,
            "target_source_file_count": 2,
            "bridge_runtime_source_digest": "3" * 64,
            "bridge_runtime_source_file_count": 2,
            "freeze_source_digest": "3" * 64,
            "freeze_source_file_count": 2,
            "maintenance_decision_hash": "decision-a",
            "maintenance_controller_hash": "controller-a",
            "old_bridge_sha256": "2" * 64,
            "replacement_bridge_sha256": "2" * 64,
            "communication_mod_jar_sha256": jar_sha256,
            "freeze_manifest_sha256": hashlib.sha256(
                attestation_bytes
            ).hexdigest(),
            "created_at": 2.0,
        }

        self.assertTrue(
            launch_game._valid_maintenance_source_reconciliation(
                reconciliation,
                attestation,
                attestation_bytes,
                intent,
                replacement,
                launch_evidence,
                old_start,
            )
        )
        reconciliation["replacement_bridge_sha256"] = "5" * 64
        self.assertFalse(
            launch_game._valid_maintenance_source_reconciliation(
                reconciliation,
                attestation,
                attestation_bytes,
                intent,
                replacement,
                launch_evidence,
                old_start,
            )
        )

    HANDOFF_SUFFIX = ".maintenance-restart-handoff.json"
    PREDECESSOR_SUFFIX = ".restart-predecessor.json"

    def runtime_paths(self, directory):
        root = Path(directory) / "root"
        game = Path(directory) / "game"
        root.mkdir()
        (game / "mods").mkdir(parents=True)
        (game / "jre" / "bin").mkdir(parents=True)
        (root / "CommunicationMod.jar").write_bytes(b"jar")
        (game / "mods" / "CommunicationMod.jar").write_bytes(b"jar")
        (game / "communication_mod_errors.log").write_bytes(b"")
        return root, game

    def launch_once(
        self,
        root,
        game,
        *,
        launch_id,
        launcher_pid,
        java_pid,
        ticks,
        popen_factory=None,
        exit_code=0,
    ):
        process = Mock()
        process.pid = java_pid
        process.wait.return_value = exit_code
        popen_factory = popen_factory or Mock(return_value=process)
        tick_values = iter(ticks)
        with patch.object(
            launch_game.os, "getpid", return_value=launcher_pid
        ), patch("builtins.print"):
            code = launch_game.launch(
                root,
                game_dir=game,
                mod_the_spire=game.parent / "ModTheSpire.jar",
                popen_factory=popen_factory,
                clock=lambda: next(tick_values),
                launch_id=launch_id,
            )
        return code, popen_factory

    @staticmethod
    def file_sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    @staticmethod
    def relative(root, path):
        return Path(path).relative_to(root).as_posix()

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path, value):
        Path(path).write_text(
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def launch_evidence_paths(self, root, launch_id):
        launch_directory = Path(root) / "logs" / "launches"
        return {
            "predecessor": launch_directory / (
                launch_id + self.PREDECESSOR_SUFFIX
            ),
            "start": launch_directory / f"{launch_id}.start.json",
            "exit": launch_directory / f"{launch_id}.exit.json",
        }

    def rebind_start_and_exit_to_predecessor(self, root, launch_id):
        paths = self.launch_evidence_paths(root, launch_id)
        start = self.read_json(paths["start"])
        start["restart_predecessor_record_path"] = self.relative(
            root, paths["predecessor"]
        )
        start["restart_predecessor_record_sha256"] = self.file_sha256(
            paths["predecessor"]
        )
        self.write_json(paths["start"], start)
        exit_record = self.read_json(paths["exit"])
        exit_fields = {
            field: exit_record[field]
            for field in (
                "launch_exit_record_path",
                "finished_at",
                "exit_code",
            )
        }
        exit_record = {**start, **exit_fields}
        self.write_json(paths["exit"], exit_record)
        self.write_json(Path(root) / "launch-latest.json", exit_record)
        return paths

    def install_restart_handoff(self, directory):
        root, game = self.runtime_paths(directory)
        launch_directory = root / "logs" / "launches"
        old_start_path = launch_directory / "old-launch.start.json"
        old_exit_path = launch_directory / "old-launch.exit.json"
        intent_path = (
            root
            / "logs"
            / "attempts"
            / "attempt-old"
            / "maintenance-restart-intent.json"
        )
        handoff_path = launch_directory / (
            "old-launch" + self.HANDOFF_SUFFIX
        )
        evidence = {}
        old_process = Mock()
        old_process.pid = 1002

        def write_handoff_before_exit():
            self.assertTrue(old_start_path.is_file())
            self.assertFalse(old_exit_path.exists())
            old_start_sha256 = self.file_sha256(old_start_path)
            binding = {
                "attempt_id": "attempt-old",
                "run_id": "IRONCLAD:0:12345",
                "seed": 12345,
                "character": "IRONCLAD",
                "ascension_level": 0,
                "run_type": "standard",
                "decision_hash": "decision-old",
                "controller_hash": "controller-old",
                "policy_version": "fast-policy-v5",
                "selection_id": "selection-old",
                "selection_digest": "a" * 64,
            }
            terminal_record = {
                "schema_version": 2,
                "record_type": "terminal_result",
                **binding,
                "terminal_state_seq": 100,
            }
            audit_record = {
                "schema_version": 2,
                "record_type": "run_audit",
                **binding,
                "terminal_state_seq": 100,
                "audit_status": "issues",
                "release_gate_passed": False,
            }
            controller_exit_record = {
                "schema_version": 2,
                "record_type": "controller_exit",
                **binding,
                "terminal_state_seq": 100,
                "controller_exit_status": "issues",
                "exit_code": 1,
            }
            history_path = root / "run-history.jsonl"
            history_path.write_bytes(b"".join(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                for record in (
                    terminal_record,
                    audit_record,
                    controller_exit_record,
                )
            ))
            intent = {
                "schema_version": 2,
                "record_type": "maintenance_restart_intent",
                "intent_status": "prepared_before_runtime_restart",
                "eligible_for_cohort": False,
                **binding,
                "terminal_state_seq": 100,
                "terminal_decision_id": "terminal-old",
                "terminal_phase": "GAME_OVER",
                "run_result_sha256": "1" * 64,
                "terminal_state_sha256": "2" * 64,
                "terminal_record_sha256": launch_game._canonical_sha256(
                    terminal_record
                ),
                "audit_record_sha256": launch_game._canonical_sha256(
                    audit_record
                ),
                "controller_exit_record_sha256": (
                    launch_game._canonical_sha256(controller_exit_record)
                ),
                "maintenance_decision_hash": "decision-fixed",
                "maintenance_controller_hash": "controller-fixed",
                "target_source_digest": "d" * 64,
                "target_source_file_count": 7,
                "old_runtime": {
                    "launch_id": "old-launch",
                    "launch_start_record_path": self.relative(
                        root, old_start_path
                    ),
                    "launch_start_record_sha256": old_start_sha256,
                    "launch_started_at": 2.0,
                    "launcher_pid": 1001,
                    "java_pid": 1002,
                    "bridge_pid": 1003,
                    "bridge_instance_token": "bridge-old",
                    "bridge_instance_sha256": "6" * 64,
                    "bridge_sha256": "7" * 64,
                    "bridge_started_at": 2.1,
                    "freeze_source_digest": "8" * 64,
                    "freeze_generated_at": 2.2,
                    "controller_observed_at": 2.4,
                    "process_alive": {
                        "launcher": True,
                        "java": True,
                        "bridge": False,
                    },
                },
                "created_at": 2.5,
            }
            launch_game._write_json_once(intent_path, intent)
            launch_game._write_json_once(
                root / "run-result.json",
                {"schema_version": 2, "attempt_id": "attempt-old"},
            )
            self.assertFalse((
                intent_path.parent / "menu-transition.json"
            ).exists())
            handoff = {
                "schema_version": 1,
                "record_type": "maintenance_restart_handoff",
                "handoff_status": "prepared_before_old_runtime_exit",
                "old_launch_id": "old-launch",
                "old_launch_start_record_path": self.relative(
                    root, old_start_path
                ),
                "old_launch_start_record_sha256": old_start_sha256,
                "restart_intent_record_path": self.relative(
                    root, intent_path
                ),
                "restart_intent_sha256": self.file_sha256(intent_path),
                "target_source_digest": "d" * 64,
                "target_source_file_count": 7,
                "created_at": 2.5,
            }
            launch_game._write_json_once(handoff_path, handoff)
            evidence.update({
                "intent": intent,
                "handoff": handoff,
                "history_path": history_path,
                "history_records": [
                    terminal_record,
                    audit_record,
                    controller_exit_record,
                ],
            })
            return 0

        old_process.wait.side_effect = write_handoff_before_exit
        self.launch_once(
            root,
            game,
            launch_id="old-launch",
            launcher_pid=1001,
            java_pid=1002,
            ticks=(1.0, 2.0, 3.0),
            popen_factory=Mock(return_value=old_process),
        )
        return {
            "root": root,
            "game": game,
            "old_start_path": old_start_path,
            "old_exit_path": old_exit_path,
            "intent_path": intent_path,
            "intent": evidence["intent"],
            "handoff_path": handoff_path,
            "handoff": evidence["handoff"],
            "history_path": evidence["history_path"],
            "history_records": evidence["history_records"],
        }

    def test_restart_evidence_suffixes_are_stable(self):
        self.assertEqual(
            self.HANDOFF_SUFFIX,
            launch_game.MAINTENANCE_RESTART_HANDOFF_SUFFIX,
        )
        self.assertEqual(
            self.PREDECESSOR_SUFFIX,
            launch_game.RESTART_PREDECESSOR_SUFFIX,
        )

    def test_interrupted_maintenance_exit_requires_bound_dead_processes(self):
        for live_pid in (None, 1001, 1002, 1003):
            with self.subTest(live_pid=live_pid), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                evidence["old_exit_path"].unlink()
                start = self.read_json(evidence["old_start_path"])
                self.write_json(root / "launch-latest.json", start)
                alive = lambda pid: live_pid is not None and pid == live_pid

                if live_pid is not None:
                    with self.assertRaisesRegex(
                        launch_game.LaunchError, "still alive"
                    ):
                        launch_game.recover_interrupted_maintenance_exit(
                            root, process_alive_fn=alive, clock=lambda: 4.0
                        )
                    self.assertFalse(evidence["old_exit_path"].exists())
                    self.assertFalse(
                        launch_game.interrupted_exit_recovery_path(
                            root, "old-launch"
                        ).exists()
                    )
                    continue

                recovered = launch_game.recover_interrupted_maintenance_exit(
                    root, process_alive_fn=alive, clock=lambda: 4.0
                )
                self.assertEqual(
                    launch_game.INTERRUPTED_EXIT_CODE,
                    recovered["exit_code"],
                )
                self.assertEqual(
                    recovered,
                    self.read_json(root / "launch-latest.json"),
                )
                recovery = self.read_json(
                    launch_game.interrupted_exit_recovery_path(
                        root, "old-launch"
                    )
                )
                self.assertEqual(
                    "all_bound_processes_observed_dead",
                    recovery["recovery_status"],
                )
                self.assertFalse(recovery["launcher_alive"])
                self.assertFalse(recovery["java_alive"])
                self.assertFalse(recovery["bridge_alive"])

    def test_failed_bootstrap_is_recovered_only_with_dead_processes_and_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            self.launch_once(
                root,
                game,
                launch_id="bootstrap-failure",
                launcher_pid=4001,
                java_pid=4002,
                ticks=(1.0, 2.0, 3.0),
            )
            paths = self.launch_evidence_paths(root, "bootstrap-failure")
            start = self.read_json(paths["start"])
            paths["exit"].unlink()
            self.write_json(root / "launch-latest.json", start)
            log_path = root / start["launch_log_path"]
            log_path.write_bytes(
                b"Begin patching...\n"
                b"java.util.zip.ZipException: bootstrap failure\n"
            )

            recovered = launch_game.recover_failed_startup_exit(
                root,
                process_alive_fn=lambda pid: False,
                clock=lambda: 4.0,
            )

            self.assertEqual(
                launch_game.FAILED_STARTUP_EXIT_CODE,
                recovered["exit_code"],
            )
            self.assertEqual(
                recovered,
                self.read_json(root / "launch-latest.json"),
            )
            recovery = self.read_json(
                launch_game.failed_startup_exit_recovery_path(
                    root, "bootstrap-failure"
                )
            )
            self.assertEqual(
                "bootstrap_failure_after_dead_bound_processes",
                recovery["recovery_status"],
            )
            self.assertEqual("mod_the_spire_zip_error", recovery["failure_marker"])

    def test_failed_bootstrap_recovery_rejects_live_process_or_missing_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            self.launch_once(
                root,
                game,
                launch_id="bootstrap-live",
                launcher_pid=5001,
                java_pid=5002,
                ticks=(1.0, 2.0, 3.0),
            )
            paths = self.launch_evidence_paths(root, "bootstrap-live")
            start = self.read_json(paths["start"])
            paths["exit"].unlink()
            self.write_json(root / "launch-latest.json", start)
            log_path = root / start["launch_log_path"]
            log_path.write_bytes(b"Begin patching...\n")
            with self.assertRaisesRegex(
                launch_game.LaunchError, "no proven ModTheSpire"
            ):
                launch_game.recover_failed_startup_exit(
                    root,
                    process_alive_fn=lambda pid: False,
                    clock=lambda: 4.0,
                )
            self.assertFalse(paths["exit"].exists())

            log_path.write_bytes(
                b"Begin patching...\n"
                b"java.util.zip.ZipException: bootstrap failure\n"
            )
            with self.assertRaisesRegex(
                launch_game.LaunchError, "still alive"
            ):
                launch_game.recover_failed_startup_exit(
                    root,
                    process_alive_fn=lambda pid: pid == 5002,
                    clock=lambda: 4.0,
                )
            self.assertFalse(paths["exit"].exists())

    def test_interrupted_ready_runtime_is_recovered_only_with_bound_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            self.launch_once(
                root,
                game,
                launch_id="ready-runtime",
                launcher_pid=6001,
                java_pid=6002,
                ticks=(1.0, 2.0, 3.0),
            )
            paths = self.launch_evidence_paths(root, "ready-runtime")
            start = self.read_json(paths["start"])
            paths["exit"].unlink()
            self.write_json(root / "launch-latest.json", start)
            (root / "bridge.py").write_text("# bridge\n", encoding="utf-8")
            log_path = root / start["launch_log_path"]
            log_path.write_bytes(
                b"Mod list:\nBegin patching...\n"
                b"communicationmod.CommunicationMod> Received message from external process: ready\n"
            )
            bridge = {
                "schema_version": 2,
                "protocol_version": 2,
                "instance_token": "6003:bridge-token",
                "bridge_pid": 6003,
                "parent_java_pid": 6002,
                "launch_id": "ready-runtime",
                "bridge_sha256": "a" * 64,
                "runtime_source_digest": "b" * 64,
                "runtime_source_file_count": 7,
                "started_at": 2.5,
            }
            self.write_json(root / "bridge-instance.json", bridge)

            recovered = launch_game.recover_interrupted_maintenance_exit(
                root,
                process_alive_fn=lambda pid: False,
                clock=lambda: 4.0,
            )

            self.assertEqual(
                launch_game.INTERRUPTED_RUNTIME_EXIT_CODE,
                recovered["exit_code"],
            )
            recovery = self.read_json(
                launch_game.interrupted_runtime_exit_recovery_path(
                    root, "ready-runtime"
                )
            )
            self.assertEqual(
                "ready_runtime_after_bound_processes_dead",
                recovery["recovery_status"],
            )
            self.assertEqual(6003, recovery["bridge_pid"])
            self.assertFalse(recovery["launcher_alive"])
            self.assertFalse(recovery["java_alive"])
            self.assertFalse(recovery["bridge_alive"])

    def test_interrupted_ready_runtime_rejects_live_bound_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            self.launch_once(
                root,
                game,
                launch_id="ready-live",
                launcher_pid=6101,
                java_pid=6102,
                ticks=(1.0, 2.0, 3.0),
            )
            paths = self.launch_evidence_paths(root, "ready-live")
            start = self.read_json(paths["start"])
            paths["exit"].unlink()
            self.write_json(root / "launch-latest.json", start)
            (root / "bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / start["launch_log_path"]).write_bytes(
                b"Mod list:\nBegin patching...\n"
                b"communicationmod.CommunicationMod> Received message from external process: ready\n"
            )
            self.write_json(root / "bridge-instance.json", {
                "schema_version": 2,
                "protocol_version": 2,
                "instance_token": "6103:bridge-token",
                "bridge_pid": 6103,
                "parent_java_pid": 6102,
                "launch_id": "ready-live",
                "bridge_sha256": "a" * 64,
                "runtime_source_digest": "b" * 64,
                "runtime_source_file_count": 7,
                "started_at": 2.5,
            })
            with self.assertRaisesRegex(
                launch_game.LaunchError, "still alive"
            ):
                launch_game.recover_interrupted_maintenance_exit(
                    root,
                    process_alive_fn=lambda pid: pid == 6102,
                    clock=lambda: 4.0,
                )
            self.assertFalse(paths["exit"].exists())

    def test_restart_handoff_writes_bound_predecessor_before_popen(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.install_restart_handoff(directory)
            root = evidence["root"]
            game = evidence["game"]
            predecessor_path = (
                root
                / "logs"
                / "launches"
                / ("new-launch" + self.PREDECESSOR_SUFFIX)
            )
            process = Mock()
            process.pid = 2002
            process.wait.return_value = 0
            observed = {}

            def popen(*args, **kwargs):
                self.assertTrue(predecessor_path.is_file())
                observed.update(json.loads(
                    predecessor_path.read_text(encoding="utf-8")
                ))
                return process

            code, _ = self.launch_once(
                root,
                game,
                launch_id="new-launch",
                launcher_pid=2001,
                java_pid=2002,
                ticks=(5.0, 6.0, 7.0),
                popen_factory=popen,
            )

            expected = {
                "schema_version": 2,
                "record_type": "restart_predecessor",
                "predecessor_status": (
                    "observed_before_new_runtime_launch"
                ),
                "lineage_depth": 0,
                "new_launch_id": "new-launch",
                "new_launcher_pid": 2001,
                "new_launch_started_at": 6.0,
                "new_launch_log_path": (
                    "logs/launches/launch-6000-new-launch.log"
                ),
                "old_launch_id": "old-launch",
                "old_launch_start_record_path": self.relative(
                    root, evidence["old_start_path"]
                ),
                "old_launch_start_record_sha256": self.file_sha256(
                    evidence["old_start_path"]
                ),
                "old_launch_exit_record_path": self.relative(
                    root, evidence["old_exit_path"]
                ),
                "old_launch_exit_record_sha256": self.file_sha256(
                    evidence["old_exit_path"]
                ),
                "restart_handoff_record_path": self.relative(
                    root, evidence["handoff_path"]
                ),
                "restart_handoff_record_sha256": self.file_sha256(
                    evidence["handoff_path"]
                ),
                "restart_intent_record_path": self.relative(
                    root, evidence["intent_path"]
                ),
                "restart_intent_sha256": self.file_sha256(
                    evidence["intent_path"]
                ),
                "lineage_parent_record_path": None,
                "lineage_parent_record_sha256": None,
                "lineage_parent_start_record_path": None,
                "lineage_parent_start_record_sha256": None,
                "lineage_parent_exit_record_path": None,
                "lineage_parent_exit_record_sha256": None,
                "lineage_parent_launch_id": None,
                "lineage_parent_started_at": None,
                "lineage_parent_finished_at": None,
            }
            self.assertEqual(0, code)
            self.assertEqual(expected, observed)
            self.assertEqual(
                expected,
                json.loads(predecessor_path.read_text(encoding="utf-8")),
            )
            new_start = json.loads((
                root / "logs" / "launches" / "new-launch.start.json"
            ).read_text(encoding="utf-8"))
            for predecessor_field, start_field in (
                ("new_launch_id", "launch_id"),
                ("new_launcher_pid", "launcher_pid"),
                ("new_launch_started_at", "started_at"),
                ("new_launch_log_path", "launch_log_path"),
            ):
                self.assertEqual(
                    expected[predecessor_field], new_start[start_field]
                )

    def test_retry_launch_inherits_original_restart_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.install_restart_handoff(directory)
            root = evidence["root"]
            game = evidence["game"]
            first_code, _ = self.launch_once(
                root,
                game,
                launch_id="replacement-one",
                launcher_pid=2001,
                java_pid=2002,
                ticks=(5.0, 6.0, 7.0),
                exit_code=17,
            )
            first_predecessor_path = (
                root
                / "logs"
                / "launches"
                / ("replacement-one" + self.PREDECESSOR_SUFFIX)
            )
            first_predecessor = json.loads(
                first_predecessor_path.read_text(encoding="utf-8")
            )
            self.assertEqual(17, first_code)
            self.assertTrue((
                root
                / "logs"
                / "launches"
                / "replacement-one.start.json"
            ).is_file())
            first_exit_path = (
                root
                / "logs"
                / "launches"
                / "replacement-one.exit.json"
            )
            self.assertEqual(
                17,
                json.loads(first_exit_path.read_text(encoding="utf-8"))[
                    "exit_code"
                ],
            )
            self.assertIsNone(
                first_predecessor["lineage_parent_record_path"]
            )
            self.assertIsNone(
                first_predecessor["lineage_parent_record_sha256"]
            )
            self.assertEqual(0, first_predecessor["lineage_depth"])

            second_predecessor_path = (
                root
                / "logs"
                / "launches"
                / ("replacement-two" + self.PREDECESSOR_SUFFIX)
            )
            second_process = Mock()
            second_process.pid = 3002
            second_process.wait.return_value = 0
            observed = {}

            def popen(*args, **kwargs):
                self.assertTrue(second_predecessor_path.is_file())
                observed.update(json.loads(
                    second_predecessor_path.read_text(encoding="utf-8")
                ))
                return second_process

            second_code, _ = self.launch_once(
                root,
                game,
                launch_id="replacement-two",
                launcher_pid=3001,
                java_pid=3002,
                ticks=(8.0, 9.0, 10.0),
                popen_factory=popen,
            )

            self.assertEqual(0, second_code)
            self.assertEqual(
                set(launch_game._RESTART_PREDECESSOR_FIELDS), set(observed)
            )
            self.assertEqual(
                self.relative(root, first_predecessor_path),
                observed["lineage_parent_record_path"],
            )
            self.assertEqual(
                self.file_sha256(first_predecessor_path),
                observed["lineage_parent_record_sha256"],
            )
            first_start_path = (
                root
                / "logs"
                / "launches"
                / "replacement-one.start.json"
            )
            self.assertEqual(
                self.relative(root, first_start_path),
                observed["lineage_parent_start_record_path"],
            )
            self.assertEqual(
                self.file_sha256(first_start_path),
                observed["lineage_parent_start_record_sha256"],
            )
            self.assertEqual(
                self.relative(root, first_exit_path),
                observed["lineage_parent_exit_record_path"],
            )
            self.assertEqual(
                self.file_sha256(first_exit_path),
                observed["lineage_parent_exit_record_sha256"],
            )
            self.assertEqual(
                "replacement-one", observed["lineage_parent_launch_id"]
            )
            self.assertEqual(6.0, observed["lineage_parent_started_at"])
            self.assertEqual(7.0, observed["lineage_parent_finished_at"])
            self.assertEqual(1, observed["lineage_depth"])
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
            ):
                self.assertEqual(
                    first_predecessor[field], observed[field]
                )
            self.assertEqual("replacement-two", observed["new_launch_id"])
            self.assertEqual(3001, observed["new_launcher_pid"])
            self.assertEqual(9.0, observed["new_launch_started_at"])
            self.assertEqual(
                "logs/launches/launch-9000-replacement-two.log",
                observed["new_launch_log_path"],
            )

    def test_retry_inherits_lineage_without_run_result_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.install_restart_handoff(directory)
            root = evidence["root"]
            game = evidence["game"]
            self.launch_once(
                root,
                game,
                launch_id="replacement-one",
                launcher_pid=2001,
                java_pid=2002,
                ticks=(5.0, 6.0, 7.0),
                exit_code=17,
            )
            first_paths = self.launch_evidence_paths(
                root, "replacement-one"
            )
            (root / "run-result.json").unlink()

            second_paths = self.launch_evidence_paths(
                root, "replacement-two"
            )
            process = Mock()
            process.pid = 3002
            process.wait.return_value = 0
            popen = Mock(return_value=process)

            def require_predecessor_before_popen(*args, **kwargs):
                self.assertTrue(second_paths["predecessor"].is_file())
                return process

            popen.side_effect = require_predecessor_before_popen
            code, _ = self.launch_once(
                root,
                game,
                launch_id="replacement-two",
                launcher_pid=3001,
                java_pid=3002,
                ticks=(8.0, 9.0, 10.0),
                popen_factory=popen,
            )

            predecessor = self.read_json(second_paths["predecessor"])
            self.assertEqual(0, code)
            self.assertEqual(1, popen.call_count)
            self.assertEqual(
                self.relative(root, first_paths["predecessor"]),
                predecessor["lineage_parent_record_path"],
            )
            self.assertEqual(
                self.file_sha256(first_paths["predecessor"]),
                predecessor["lineage_parent_record_sha256"],
            )

    def test_completed_restart_allows_ordinary_launch_without_tip_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.install_restart_handoff(directory)
            root = evidence["root"]
            game = evidence["game"]
            self.launch_once(
                root,
                game,
                launch_id="replacement-one",
                launcher_pid=2001,
                java_pid=2002,
                ticks=(5.0, 6.0, 7.0),
            )
            first = self.launch_evidence_paths(root, "replacement-one")
            predecessor = self.read_json(first["predecessor"])
            first["exit"].unlink()
            self.write_json(
                root / "launch-latest.json",
                self.read_json(first["start"]),
            )
            intent = evidence["intent"]
            first_start = self.read_json(first["start"])
            old_exit = self.read_json(evidence["old_exit_path"])
            restart_summary = {
                "record_path": self.relative(root, first["predecessor"]),
                "record_sha256": self.file_sha256(first["predecessor"]),
                "handoff_record_path": predecessor[
                    "restart_handoff_record_path"
                ],
                "handoff_record_sha256": predecessor[
                    "restart_handoff_record_sha256"
                ],
                "restart_intent_record_path": predecessor[
                    "restart_intent_record_path"
                ],
                "restart_intent_sha256": predecessor[
                    "restart_intent_sha256"
                ],
                "old_launch_exit_record_path": predecessor[
                    "old_launch_exit_record_path"
                ],
                "old_launch_exit_record_sha256": predecessor[
                    "old_launch_exit_record_sha256"
                ],
            }
            bridge_instance = {
                "schema_version": 2,
                "protocol_version": 2,
                "instance_token": "bridge-replacement",
                "bridge_pid": 2003,
                "parent_java_pid": first_start["java_pid"],
                "launch_id": "replacement-one",
                "bridge_sha256": "7" * 64,
                "runtime_source_digest": intent["target_source_digest"],
                "runtime_source_file_count": intent[
                    "target_source_file_count"
                ],
                "started_at": 6.5,
            }
            self.write_json(root / "bridge-instance.json", bridge_instance)
            replacement = {
                "launch_evidence": {
                    "schema_version": 1,
                    "launch_record_sha256": self.file_sha256(
                        first["start"]
                    ),
                    "launch_start_record_path": self.relative(
                        root, first["start"]
                    ),
                    "launch_start_record_sha256": self.file_sha256(
                        first["start"]
                    ),
                    "launch_id": "replacement-one",
                    "launcher_pid": first_start["launcher_pid"],
                    "java_pid": first_start["java_pid"],
                    "bridge_instance_sha256": (
                        launch_game._canonical_sha256(bridge_instance)
                    ),
                    "bridge_instance_token": "bridge-replacement",
                    "bridge_pid": 2003,
                    "bridge_launch_id": "replacement-one",
                    "bridge_sha256": "7" * 64,
                    "bridge_runtime_source_digest": intent[
                        "target_source_digest"
                    ],
                    "bridge_runtime_source_file_count": intent[
                        "target_source_file_count"
                    ],
                    "runtime_source_migration": None,
                    "bridge_started_at": 6.5,
                    "root_communication_mod_jar_sha256": first_start[
                        "root_communication_mod_jar_sha256"
                    ],
                    "installed_communication_mod_jar_sha256": first_start[
                        "installed_communication_mod_jar_sha256"
                    ],
                    "bridge_stderr_path": first_start[
                        "bridge_stderr_path"
                    ],
                    "bridge_stderr_start_offset": first_start[
                        "bridge_stderr_start_offset"
                    ],
                    "bridge_stderr_start_sha256": first_start[
                        "bridge_stderr_start_sha256"
                    ],
                    "bridge_stderr_current_size": first_start[
                        "bridge_stderr_start_offset"
                    ],
                    "bridge_stderr_current_sha256": first_start[
                        "bridge_stderr_start_sha256"
                    ],
                    "bridge_stderr_delta_size": 0,
                    "bridge_stderr_delta_sha256": hashlib.sha256(
                        b""
                    ).hexdigest(),
                },
                "launch_id": "replacement-one",
                "launcher_pid": first_start["launcher_pid"],
                "java_pid": first_start["java_pid"],
                "bridge_pid": 2003,
                "bridge_instance_token": "bridge-replacement",
                "bridge_sha256": "7" * 64,
                "bridge_runtime_source_digest": intent[
                    "target_source_digest"
                ],
                "bridge_runtime_source_file_count": intent[
                    "target_source_file_count"
                ],
                "launch_started_at": first_start["started_at"],
                "bridge_started_at": 6.5,
                "restart_predecessor": restart_summary,
            }
            process_evidence = {
                "returncode": 0,
                "stdout_size": 0,
                "stderr_size": 0,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            }
            manifest = {
                "schema_version": 1,
                "policy_version": "fast-policy-v5",
                "decision_hash": intent["maintenance_decision_hash"],
                "controller_hash": intent["maintenance_controller_hash"],
                "generated_at": 6.8,
                "source_digest": intent["target_source_digest"],
                "source_file_count": intent["target_source_file_count"],
                "sources": {
                    f"source-{index}": {"sha256": f"{index}" * 64}
                    for index in range(intent["target_source_file_count"])
                },
                "decision_case_replay": {
                    "target_decision_hash": intent[
                        "maintenance_decision_hash"
                    ],
                    "status": "clear",
                    "release_gate_passed": True,
                    "issue_count": 0,
                    "eligible_unknown_count": 0,
                },
                "decision_case_replay_revalidation": {
                    "target_decision_hash": intent[
                        "maintenance_decision_hash"
                    ],
                    "trace_prefix_verified": True,
                },
                "launch_evidence": replacement["launch_evidence"],
                "full_tests": {**process_evidence, "test_count": 1},
                "git_diff_check": {
                    **process_evidence,
                    "test_count": None,
                },
                "runtime_artifacts_unchanged": True,
                "runtime_artifacts_before_sha256": "b" * 64,
                "runtime_artifacts_after_sha256": "b" * 64,
                "release_gate_passed": True,
            }
            manifest_path = root / "freeze-manifest.json"
            self.write_json(manifest_path, manifest)
            manifest_snapshot_path = (
                evidence["intent_path"].parent
                / "maintenance-freeze-manifest.json"
            )
            manifest_snapshot_path.write_bytes(manifest_path.read_bytes())
            resolution = {
                "schema_version": 2,
                "record_type": "maintenance_resolution",
                "resolution_kind": (
                    "terminal_screen_release_after_repair"
                ),
                "resolution_status": "maintenance_gate_passed",
                "eligible_for_cohort": False,
                "original_findings_preserved": True,
                **{
                    field: intent[field]
                    for field in launch_game._ATTEMPT_BINDING_FIELDS
                },
                "terminal_state_seq": intent["terminal_state_seq"],
                "original_audit_status": "issues",
                "original_release_gate_passed": False,
                "original_controller_exit_status": "issues",
                "original_exit_code": 1,
                "maintenance_decision_hash": intent[
                    "maintenance_decision_hash"
                ],
                "maintenance_controller_hash": intent[
                    "maintenance_controller_hash"
                ],
                "freeze_manifest_record_path": self.relative(
                    root, manifest_snapshot_path
                ),
                "freeze_manifest_sha256": self.file_sha256(manifest_path),
                "freeze_source_digest": intent["target_source_digest"],
                "freeze_generated_at": 6.8,
                "history_sha256": self.file_sha256(
                    evidence["history_path"]
                ),
                "history_byte_length": evidence[
                    "history_path"
                ].stat().st_size,
                "terminal_record_sha256": intent[
                    "terminal_record_sha256"
                ],
                "audit_record_sha256": intent[
                    "audit_record_sha256"
                ],
                "controller_exit_record_sha256": intent[
                    "controller_exit_record_sha256"
                ],
                "history_indices": [0, 1, 2],
                "created_at": 7.0,
            }
            self.write_json(
                evidence["intent_path"].parent
                / "maintenance-resolution.json",
                resolution,
            )
            transition = {
                "schema_version": 2,
                "record_type": "menu_transition",
                "transition_kind": (
                    "game_over_to_main_menu_via_runtime_restart"
                ),
                "transition_status": "clear",
                "eligible_for_cohort": False,
                **{
                    field: intent[field]
                    for field in launch_game._ATTEMPT_BINDING_FIELDS
                },
                "terminal_state_seq": intent["terminal_state_seq"],
                "terminal_decision_id": intent["terminal_decision_id"],
                "terminal_phase": "GAME_OVER",
                "menu_state_seq": 102,
                "menu_decision_id": "restart-menu",
                "authorization_kind": "maintenance_resolution",
                "maintenance_resolution_sha256": (
                    launch_game._canonical_sha256(resolution)
                ),
                "restart_evidence": {
                    "schema_version": 1,
                    "restart_intent_sha256": predecessor[
                        "restart_intent_sha256"
                    ],
                    "old_launch_exit": {
                        "record_path": predecessor[
                            "old_launch_exit_record_path"
                        ],
                        "record_sha256": predecessor[
                            "old_launch_exit_record_sha256"
                        ],
                        "finished_at": old_exit["finished_at"],
                        "exit_code": old_exit["exit_code"],
                    },
                    "replacement_runtime": replacement,
                    "freeze_manifest_sha256": resolution[
                        "freeze_manifest_sha256"
                    ],
                    "freeze_source_digest": intent[
                        "target_source_digest"
                    ],
                    "freeze_generated_at": 6.8,
                    "initial_menu_state_seq": 101,
                    "initial_menu_decision_id": "restart-menu",
                    "state_probe": {
                        "request_id": "state-probe",
                        "accepted_state_seq": 101,
                        "result_state_seq": 102,
                        "result_decision_id": "restart-menu",
                        "result_phase": "MAIN_MENU",
                        "requested_target_id": "action:state",
                        "resolved_target_id": "action:state",
                    },
                },
                "created_at": 7.5,
            }
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                transition,
            )
            bad_transition = json.loads(json.dumps(transition))
            bad_transition["restart_evidence"]["state_probe"] = {}
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                bad_transition,
            )
            rejected_popen = Mock()
            with self.assertRaises(launch_game.LaunchError):
                self.launch_once(
                    root,
                    game,
                    launch_id="rejected-partial-clear",
                    launcher_pid=2901,
                    java_pid=2902,
                    ticks=(7.6, 7.7, 7.8),
                    popen_factory=rejected_popen,
                )
            rejected_popen.assert_not_called()
            for case in ("old_exit", "java_pid"):
                with self.subTest(case=case):
                    bad_binding = json.loads(json.dumps(transition))
                    if case == "old_exit":
                        bad_binding["restart_evidence"]["old_launch_exit"][
                            "finished_at"
                        ] += 0.25
                    else:
                        runtime = bad_binding["restart_evidence"][
                            "replacement_runtime"
                        ]
                        runtime["java_pid"] = 2999
                        runtime["launch_evidence"]["java_pid"] = 2999
                    self.write_json(
                        evidence["intent_path"].parent
                        / "menu-transition.json",
                        bad_binding,
                    )
                    tampered_popen = Mock()
                    with self.assertRaises(launch_game.LaunchError):
                        self.launch_once(
                            root,
                            game,
                            launch_id=f"rejected-{case}",
                            launcher_pid=2951,
                            java_pid=2952,
                            ticks=(7.81, 7.82, 7.83),
                            popen_factory=tampered_popen,
                        )
                    tampered_popen.assert_not_called()
            tampered_history = json.loads(json.dumps(
                evidence["history_records"]
            ))
            tampered_history[1]["audit_status"] = "clear"
            evidence["history_path"].write_bytes(b"".join(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                for record in tampered_history
            ))
            bad_history_resolution = json.loads(json.dumps(resolution))
            bad_history_resolution["history_sha256"] = self.file_sha256(
                evidence["history_path"]
            )
            bad_history_resolution["audit_record_sha256"] = (
                launch_game._canonical_sha256(tampered_history[1])
            )
            bad_history_resolution["original_audit_status"] = "clear"
            resolution_path = (
                evidence["intent_path"].parent
                / "maintenance-resolution.json"
            )
            self.write_json(resolution_path, bad_history_resolution)
            bad_history_transition = json.loads(json.dumps(transition))
            bad_history_transition[
                "maintenance_resolution_sha256"
            ] = launch_game._canonical_sha256(bad_history_resolution)
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                bad_history_transition,
            )
            bad_history_popen = Mock()
            with self.assertRaises(launch_game.LaunchError):
                self.launch_once(
                    root,
                    game,
                    launch_id="rejected-rebound-history",
                    launcher_pid=2961,
                    java_pid=2962,
                    ticks=(7.835, 7.836, 7.837),
                    popen_factory=bad_history_popen,
                )
            bad_history_popen.assert_not_called()
            evidence["history_path"].write_bytes(b"".join(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                for record in evidence["history_records"]
            ))
            self.write_json(resolution_path, resolution)
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                transition,
            )
            bad_manifest = json.loads(json.dumps(manifest))
            bad_manifest["release_gate_passed"] = False
            self.write_json(manifest_snapshot_path, bad_manifest)
            bad_resolution = json.loads(json.dumps(resolution))
            bad_resolution["freeze_manifest_sha256"] = self.file_sha256(
                manifest_snapshot_path
            )
            self.write_json(resolution_path, bad_resolution)
            bad_freeze_transition = json.loads(json.dumps(transition))
            bad_freeze_transition[
                "maintenance_resolution_sha256"
            ] = launch_game._canonical_sha256(bad_resolution)
            bad_freeze_transition["restart_evidence"][
                "freeze_manifest_sha256"
            ] = bad_resolution["freeze_manifest_sha256"]
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                bad_freeze_transition,
            )
            bad_freeze_popen = Mock()
            with self.assertRaises(launch_game.LaunchError):
                self.launch_once(
                    root,
                    game,
                    launch_id="rejected-failed-freeze",
                    launcher_pid=2971,
                    java_pid=2972,
                    ticks=(7.84, 7.85, 7.86),
                    popen_factory=bad_freeze_popen,
                )
            bad_freeze_popen.assert_not_called()
            self.write_json(manifest_snapshot_path, manifest)
            self.write_json(resolution_path, resolution)
            self.write_json(
                evidence["intent_path"].parent / "menu-transition.json",
                transition,
            )

            with evidence["history_path"].open("ab") as history_handle:
                history_handle.write(json.dumps(
                    {
                        "schema_version": 2,
                        "record_type": "terminal_result",
                        "attempt_id": "later-attempt",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n")
            self.write_json(
                manifest_path,
                {"schema_version": 1, "superseded": True},
            )
            (root / "bridge-instance.json").unlink()

            ordinary = self.launch_evidence_paths(
                root, "ordinary-after-clear"
            )
            code, popen = self.launch_once(
                root,
                game,
                launch_id="ordinary-after-clear",
                launcher_pid=3001,
                java_pid=3002,
                ticks=(8.0, 9.0, 10.0),
            )

            self.assertEqual(0, code)
            self.assertEqual(1, popen.call_count)
            self.assertFalse(ordinary["predecessor"].exists())
            start = self.read_json(ordinary["start"])
            self.assertIsNone(start["restart_predecessor_record_path"])
            self.assertIsNone(start["restart_predecessor_record_sha256"])

    def test_restart_start_and_exit_anchor_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.install_restart_handoff(directory)
            root = evidence["root"]
            self.launch_once(
                root,
                evidence["game"],
                launch_id="replacement-one",
                launcher_pid=2001,
                java_pid=2002,
                ticks=(5.0, 6.0, 7.0),
                exit_code=17,
            )
            paths = self.launch_evidence_paths(root, "replacement-one")
            expected_path = self.relative(root, paths["predecessor"])
            expected_sha256 = self.file_sha256(paths["predecessor"])

            for label in ("start", "exit"):
                with self.subTest(record=label):
                    record = self.read_json(paths[label])
                    self.assertEqual(
                        expected_path,
                        record["restart_predecessor_record_path"],
                    )
                    self.assertEqual(
                        expected_sha256,
                        record["restart_predecessor_record_sha256"],
                    )

    def test_retry_rejects_noncanonical_child_parent_path_before_popen(self):
        for path_kind in ("absolute", "dot-segment"):
            with self.subTest(path_kind=path_kind), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                game = evidence["game"]
                self.launch_once(
                    root,
                    game,
                    launch_id="replacement-one",
                    launcher_pid=2001,
                    java_pid=2002,
                    ticks=(5.0, 6.0, 7.0),
                    exit_code=17,
                )
                self.launch_once(
                    root,
                    game,
                    launch_id="replacement-two",
                    launcher_pid=3001,
                    java_pid=3002,
                    ticks=(8.0, 9.0, 10.0),
                    exit_code=17,
                )
                first_paths = self.launch_evidence_paths(
                    root, "replacement-one"
                )
                second_paths = self.launch_evidence_paths(
                    root, "replacement-two"
                )
                predecessor = self.read_json(second_paths["predecessor"])
                predecessor["lineage_parent_record_path"] = (
                    str(first_paths["predecessor"].resolve())
                    if path_kind == "absolute"
                    else (
                        "logs/launches/../launches/"
                        "replacement-one.restart-predecessor.json"
                    )
                )
                self.write_json(second_paths["predecessor"], predecessor)
                self.rebind_start_and_exit_to_predecessor(
                    root, "replacement-two"
                )

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        game,
                        launch_id="replacement-three",
                        launcher_pid=4001,
                        java_pid=4002,
                        ticks=(11.0, 12.0, 13.0),
                        popen_factory=popen,
                    )
                popen.assert_not_called()
                self.assertFalse(self.launch_evidence_paths(
                    root, "replacement-three"
                )["predecessor"].exists())

    def test_retry_rejects_missing_tampered_or_reversed_exit_before_popen(self):
        for case in ("missing", "tampered", "reversed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                game = evidence["game"]
                self.launch_once(
                    root,
                    game,
                    launch_id="replacement-one",
                    launcher_pid=2001,
                    java_pid=2002,
                    ticks=(5.0, 6.0, 7.0),
                    exit_code=17,
                )
                paths = self.launch_evidence_paths(root, "replacement-one")
                if case == "missing":
                    paths["exit"].unlink()
                else:
                    exit_record = self.read_json(paths["exit"])
                    if case == "tampered":
                        exit_record["exit_code"] = 99
                    else:
                        exit_record["finished_at"] = 5.0
                    self.write_json(paths["exit"], exit_record)
                    if case == "reversed":
                        self.write_json(root / "launch-latest.json", exit_record)

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        game,
                        launch_id="replacement-two",
                        launcher_pid=3001,
                        java_pid=3002,
                        ticks=(8.0, 9.0, 10.0),
                        popen_factory=popen,
                    )
                popen.assert_not_called()
                self.assertFalse(self.launch_evidence_paths(
                    root, "replacement-two"
                )["predecessor"].exists())

    def test_schema_v1_predecessor_fails_closed_as_tip_or_ancestor(self):
        for position in ("tip", "ancestor"):
            with self.subTest(position=position), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                game = evidence["game"]
                self.launch_once(
                    root,
                    game,
                    launch_id="replacement-one",
                    launcher_pid=2001,
                    java_pid=2002,
                    ticks=(5.0, 6.0, 7.0),
                    exit_code=17,
                )
                first_paths = self.launch_evidence_paths(
                    root, "replacement-one"
                )
                if position == "ancestor":
                    self.launch_once(
                        root,
                        game,
                        launch_id="replacement-two",
                        launcher_pid=3001,
                        java_pid=3002,
                        ticks=(8.0, 9.0, 10.0),
                        exit_code=17,
                    )
                predecessor = self.read_json(first_paths["predecessor"])
                predecessor["schema_version"] = 1
                self.write_json(first_paths["predecessor"], predecessor)

                if position == "tip":
                    self.rebind_start_and_exit_to_predecessor(
                        root, "replacement-one"
                    )
                    next_launch_id = "replacement-two"
                    next_launcher_pid = 3001
                    next_java_pid = 3002
                    ticks = (8.0, 9.0, 10.0)
                else:
                    second_paths = self.launch_evidence_paths(
                        root, "replacement-two"
                    )
                    second = self.read_json(second_paths["predecessor"])
                    second["lineage_parent_record_sha256"] = (
                        self.file_sha256(first_paths["predecessor"])
                    )
                    self.write_json(second_paths["predecessor"], second)
                    self.rebind_start_and_exit_to_predecessor(
                        root, "replacement-two"
                    )
                    next_launch_id = "replacement-three"
                    next_launcher_pid = 4001
                    next_java_pid = 4002
                    ticks = (11.0, 12.0, 13.0)

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        game,
                        launch_id=next_launch_id,
                        launcher_pid=next_launcher_pid,
                        java_pid=next_java_pid,
                        ticks=ticks,
                        popen_factory=popen,
                    )
                popen.assert_not_called()

    def test_intent_and_handoff_semantic_mismatch_fails_before_popen(self):
        for case in (
            "created_at",
            "target_source",
            "old_runtime",
            "same_hashes",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                intent = self.read_json(evidence["intent_path"])
                handoff = self.read_json(evidence["handoff_path"])
                if case == "created_at":
                    handoff["created_at"] = intent["created_at"] + 0.25
                elif case == "target_source":
                    intent["target_source_digest"] = "e" * 64
                    self.write_json(evidence["intent_path"], intent)
                    handoff["restart_intent_sha256"] = self.file_sha256(
                        evidence["intent_path"]
                    )
                elif case == "old_runtime":
                    intent["old_runtime"]["launch_id"] = "forged-old"
                    self.write_json(evidence["intent_path"], intent)
                    handoff["restart_intent_sha256"] = self.file_sha256(
                        evidence["intent_path"]
                    )
                else:
                    intent["maintenance_decision_hash"] = intent[
                        "decision_hash"
                    ]
                    intent["maintenance_controller_hash"] = intent[
                        "controller_hash"
                    ]
                    self.write_json(evidence["intent_path"], intent)
                    handoff["restart_intent_sha256"] = self.file_sha256(
                        evidence["intent_path"]
                    )
                self.write_json(evidence["handoff_path"], handoff)

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        evidence["game"],
                        launch_id="new-launch",
                        launcher_pid=2001,
                        java_pid=2002,
                        ticks=(5.0, 6.0, 7.0),
                        popen_factory=popen,
                    )
                popen.assert_not_called()
                self.assertFalse(self.launch_evidence_paths(
                    root, "new-launch"
                )["predecessor"].exists())

    def test_retry_rejects_tampered_or_missing_parent_before_popen(self):
        for case in ("tampered", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                game = evidence["game"]
                self.launch_once(
                    root,
                    game,
                    launch_id="replacement-one",
                    launcher_pid=2001,
                    java_pid=2002,
                    ticks=(5.0, 6.0, 7.0),
                    exit_code=17,
                )
                parent_path = (
                    root
                    / "logs"
                    / "launches"
                    / ("replacement-one" + self.PREDECESSOR_SUFFIX)
                )
                if case == "tampered":
                    parent = json.loads(
                        parent_path.read_text(encoding="utf-8")
                    )
                    parent["new_launch_id"] = "forged-parent"
                    parent_path.write_text(
                        json.dumps(parent), encoding="utf-8"
                    )
                else:
                    parent_path.unlink()

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        game,
                        launch_id="replacement-two",
                        launcher_pid=3001,
                        java_pid=3002,
                        ticks=(8.0, 9.0, 10.0),
                        popen_factory=popen,
                    )

                popen.assert_not_called()
                self.assertFalse((
                    root
                    / "logs"
                    / "launches"
                    / ("replacement-two" + self.PREDECESSOR_SUFFIX)
                ).exists())

    def test_tampered_restart_chain_fails_before_popen(self):
        for case in ("handoff", "intent", "old_exit"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                evidence = self.install_restart_handoff(directory)
                root = evidence["root"]
                game = evidence["game"]
                if case == "handoff":
                    handoff = dict(evidence["handoff"])
                    handoff["old_launch_start_record_sha256"] = "0" * 64
                    evidence["handoff_path"].write_text(
                        json.dumps(handoff), encoding="utf-8"
                    )
                elif case == "intent":
                    intent = dict(evidence["intent"])
                    intent["intent_status"] = "tampered"
                    evidence["intent_path"].write_text(
                        json.dumps(intent), encoding="utf-8"
                    )
                else:
                    old_exit = json.loads(
                        evidence["old_exit_path"].read_text(encoding="utf-8")
                    )
                    old_exit["exit_code"] = 99
                    evidence["old_exit_path"].write_text(
                        json.dumps(old_exit), encoding="utf-8"
                    )

                popen = Mock()
                with self.assertRaises(launch_game.LaunchError):
                    self.launch_once(
                        root,
                        game,
                        launch_id="new-launch",
                        launcher_pid=2001,
                        java_pid=2002,
                        ticks=(5.0, 6.0, 7.0),
                        popen_factory=popen,
                    )

                popen.assert_not_called()
                self.assertFalse((
                    root
                    / "logs"
                    / "launches"
                    / ("new-launch" + self.PREDECESSOR_SUFFIX)
                ).exists())

    def test_normal_launch_without_handoff_writes_no_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            self.launch_once(
                root,
                game,
                launch_id="ordinary-launch",
                launcher_pid=3001,
                java_pid=3002,
                ticks=(1.0, 2.0, 3.0),
            )

            self.assertFalse((
                root
                / "logs"
                / "launches"
                / ("ordinary-launch" + self.PREDECESSOR_SUFFIX)
            ).exists())


class LaunchGameTests(unittest.TestCase):
    def test_allocates_unique_log_without_touching_legacy_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "launch.log"
            legacy.write_text("preserve me", encoding="utf-8")

            path, record = launch_game.allocate_launch_log(
                root, now=123.5, launch_id="launch-test"
            )

            self.assertEqual(
                root / "logs" / "launches" / "launch-123500-launch-test.log",
                path,
            )
            self.assertFalse(path.exists())
            self.assertEqual("preserve me", legacy.read_text(encoding="utf-8"))
            self.assertEqual(2, record["schema_version"])
            self.assertEqual("logs/launches/launch-123500-launch-test.log", record["launch_log_path"])

    def test_latest_pointer_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launch-latest.json"
            payload = {
                "schema_version": 2,
                "launch_id": "launch-test",
                "started_at": 123.5,
                "launch_log_path": "logs/launches/launch-test.log",
            }

            launch_game._write_latest_launch(path, payload)

            self.assertEqual(payload, json.loads(path.read_text(encoding="utf-8")))
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def runtime_paths(self, directory, *, installed=b"jar", root_jar=b"jar"):
        root = Path(directory) / "root"
        game = Path(directory) / "game"
        root.mkdir()
        (game / "mods").mkdir(parents=True)
        (game / "jre" / "bin").mkdir(parents=True)
        (root / "CommunicationMod.jar").write_bytes(root_jar)
        (game / "mods" / "CommunicationMod.jar").write_bytes(installed)
        return root, game

    def test_launch_refuses_mismatched_installed_jar_before_popen(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(
                directory, installed=b"old", root_jar=b"new"
            )
            popen = Mock()
            with self.assertRaisesRegex(
                launch_game.LaunchError, "hashes differ"
            ):
                launch_game.launch(
                    root,
                    game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=popen,
                )
            popen.assert_not_called()
            self.assertFalse((root / "launch-latest.json").exists())
            self.assertFalse((root / "logs").exists())

    def test_popen_launch_writes_live_then_finished_schema2_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            bridge_stderr = game / "communication_mod_errors.log"
            bridge_stderr.write_bytes(b"old launch prefix\n")
            latest = root / "launch-latest.json"
            observed_live = []

            class Process:
                pid = 4321

                def wait(self):
                    observed_live.append(json.loads(
                        latest.read_text(encoding="utf-8")
                    ))
                    return 7

            popen = Mock(return_value=Process())
            times = iter((90.0, 100.0, 120.0))
            with patch("builtins.print"):
                code = launch_game.launch(
                    root,
                    game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=popen,
                    clock=lambda: next(times),
                    launch_id="launch-one",
                )

            self.assertEqual(7, code)
            self.assertEqual(1, len(observed_live))
            live = observed_live[0]
            self.assertEqual(2, live["schema_version"])
            self.assertGreater(live["launcher_pid"], 0)
            self.assertNotEqual(live["launcher_pid"], live["java_pid"])
            self.assertEqual(4321, live["java_pid"])
            self.assertEqual(100.0, live["started_at"])
            self.assertNotIn("finished_at", live)
            self.assertNotIn("exit_code", live)
            self.assertEqual(len(b"old launch prefix\n"), live[
                "bridge_stderr_start_offset"
            ])
            self.assertEqual(
                hashlib.sha256(b"old launch prefix\n").hexdigest(),
                live["bridge_stderr_start_sha256"],
            )
            self.assertEqual(
                live["root_communication_mod_jar_sha256"],
                live["installed_communication_mod_jar_sha256"],
            )
            finished = json.loads(latest.read_text(encoding="utf-8"))
            self.assertEqual(120.0, finished["finished_at"])
            self.assertEqual(7, finished["exit_code"])
            start_path = root / live["launch_start_record_path"]
            exit_path = root / finished["launch_exit_record_path"]
            self.assertEqual(
                live, json.loads(start_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(
                finished, json.loads(exit_path.read_text(encoding="utf-8"))
            )
            self.assertFalse((
                root / "logs" / "launches" / ".active-launch"
            ).exists())
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(str(game / "jre" / "bin" / "java.exe"), command[0])
            self.assertEqual(game.resolve(), popen.call_args.kwargs["cwd"])
            self.assertEqual(
                "launch-one",
                popen.call_args.kwargs["env"]["STS_LAUNCH_ID"],
            )

    def test_each_launch_uses_a_distinct_stdout_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _ = launch_game.allocate_launch_log(
                root, now=1.0, launch_id="one"
            )
            second, _ = launch_game.allocate_launch_log(
                root, now=1.0, launch_id="two"
            )
            self.assertNotEqual(first, second)
            first.write_text("first", encoding="utf-8")
            self.assertFalse(second.exists())
            self.assertEqual("first", first.read_text(encoding="utf-8"))

    def test_second_launch_preserves_first_immutable_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            (game / "communication_mod_errors.log").write_bytes(b"")
            ticks = iter((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))

            class Process:
                def __init__(self, pid):
                    self.pid = pid

                def wait(self):
                    return 0

            processes = iter((Process(101), Process(102)))
            with patch("builtins.print"):
                launch_game.launch(
                    root, game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=lambda *args, **kwargs: next(processes),
                    clock=lambda: next(ticks), launch_id="first",
                )
            first_start = root / "logs" / "launches" / "first.start.json"
            first_exit = root / "logs" / "launches" / "first.exit.json"
            preserved_start = first_start.read_bytes()
            preserved_exit = first_exit.read_bytes()

            with patch("builtins.print"):
                launch_game.launch(
                    root, game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=lambda *args, **kwargs: next(processes),
                    clock=lambda: next(ticks), launch_id="second",
                )

            self.assertEqual(preserved_start, first_start.read_bytes())
            self.assertEqual(preserved_exit, first_exit.read_bytes())
            latest = json.loads((root / "launch-latest.json").read_text(
                encoding="utf-8"
            ))
            self.assertEqual("second", latest["launch_id"])
            self.assertTrue((
                root / "logs" / "launches" / "second.start.json"
            ).is_file())
            self.assertTrue((
                root / "logs" / "launches" / "second.exit.json"
            ).is_file())

    def test_nested_launcher_is_rejected_while_java_wait_holds_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            (game / "communication_mod_errors.log").write_bytes(b"")
            inner_popen = Mock()

            class Process:
                pid = 4321

                def wait(self):
                    with self_test.assertRaisesRegex(
                        launch_game.LaunchError, "still owns"
                    ):
                        launch_game.launch(
                            root, game_dir=game,
                            mod_the_spire=(
                                Path(directory) / "ModTheSpire.jar"
                            ),
                            popen_factory=inner_popen,
                            launch_id="nested",
                        )
                    return 0

            self_test = self
            with patch("builtins.print"):
                launch_game.launch(
                    root, game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=Mock(return_value=Process()),
                    launch_id="outer",
                )

            inner_popen.assert_not_called()

    def test_stale_launch_lease_is_preserved_before_reacquiring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = launch_game.acquire_launch_lease(
                root, lease_id="old", clock=lambda: 1.0
            )
            launch_game.bind_launch_child(
                stale, launch_id="old-launch", java_pid=987654,
                bound_at=1.5,
            )
            current = launch_game.acquire_launch_lease(
                root,
                lease_id="new",
                clock=lambda: 2.0,
                process_alive_fn=lambda pid: False,
            )

            stale_directory = (
                root / "logs" / "launches" / "stale-launch-lease-old"
            )
            self.assertTrue((stale_directory / "owner.json").is_file())
            self.assertEqual("old", json.loads((
                stale_directory / "owner.json"
            ).read_text(encoding="utf-8"))["lease_id"])
            self.assertNotEqual(
                stale["owner"]["lease_id"],
                current["owner"]["lease_id"],
            )
            launch_game.release_launch_lease(current)

    def test_dead_launcher_with_live_java_child_blocks_new_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = launch_game.acquire_launch_lease(
                root, lease_id="orphan", clock=lambda: 1.0
            )
            launch_game.bind_launch_child(
                lease, launch_id="orphan-launch", java_pid=987654,
                bound_at=1.5,
            )

            with self.assertRaisesRegex(
                launch_game.LaunchError, "live Java child"
            ):
                launch_game.acquire_launch_lease(
                    root,
                    lease_id="new",
                    process_alive_fn=lambda pid: pid == 987654,
                )

            launch_game.release_launch_lease(lease)


    def test_select_bridge_runtime_falls_back_after_probe_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / ".java-python-runtime" / "python.exe"
            cached = root / "cached-python.exe"
            workspace.parent.mkdir(parents=True)
            workspace.write_bytes(b"workspace")
            cached.write_bytes(b"cached")
            calls = []

            def probe(path):
                calls.append(path)
                if path == workspace.resolve():
                    raise launch_game.LaunchError("denied")
                return path

            with patch.object(launch_game, "CACHED_PYTHON", cached):
                selected = launch_game._select_bridge_runtime(
                    root, probe_fn=probe
                )

            self.assertEqual(cached.resolve(), selected)
            self.assertEqual(
                [workspace.resolve(), cached.resolve()], calls
            )

    def test_prepare_bridge_runtime_writes_isolated_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "bridge.py"
            python_path = root / ".java-python-runtime" / "python.exe"
            bridge.write_text("# bridge\n", encoding="utf-8")
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"python")
            with patch.object(
                launch_game, "_select_bridge_runtime",
                return_value=python_path.resolve(),
            ):
                selected, localappdata = launch_game.prepare_bridge_runtime(root)

            self.assertEqual(python_path.resolve(), selected)
            self.assertEqual(
                (root / ".java-localappdata").resolve(), localappdata
            )
            for config in launch_game._bridge_config_paths(localappdata):
                content = config.read_text(encoding="utf-8")
                self.assertIn("command=", content)
                self.assertIn("bridge.py", content)
                self.assertIn("runAtGameStart=true", content)
                self.assertIn("verbose=false", content)

    def test_launch_rejects_unexecutable_bridge_before_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root, game = self.runtime_paths(directory)
            (root / "bridge.py").write_text("# bridge\n", encoding="utf-8")
            (game / "communication_mod_errors.log").write_bytes(b"")
            popen = Mock()
            with patch.object(
                launch_game, "_select_bridge_runtime",
                side_effect=launch_game.LaunchError("access denied"),
            ), self.assertRaisesRegex(
                launch_game.LaunchError, "access denied"
            ):
                launch_game.launch(
                    root,
                    game_dir=game,
                    mod_the_spire=Path(directory) / "ModTheSpire.jar",
                    popen_factory=popen,
                )

            popen.assert_not_called()
            self.assertFalse((root / "logs").exists())

    def test_immutable_writer_recovers_after_prepublication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            payload = {"schema_version": 1, "value": "bound"}
            with patch.object(
                launch_game.os,
                "fsync",
                side_effect=OSError("simulated crash before publish"),
            ), self.assertRaises(launch_game.LaunchError):
                launch_game._write_json_once(path, payload)

            self.assertFalse(path.exists())
            self.assertEqual([], list(path.parent.glob(".*.tmp")))
            launch_game._write_json_once(path, payload)
            launch_game._write_json_once(path, payload)
            self.assertEqual(
                payload,
                json.loads(path.read_text(encoding="utf-8")),
            )
            with self.assertRaises(launch_game.LaunchError):
                launch_game._write_json_once(
                    path, {"schema_version": 1, "value": "changed"}
                )


if __name__ == "__main__":
    unittest.main()
