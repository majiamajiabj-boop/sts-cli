import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import freeze_manifest
import quick_start


class QuickStartTests(unittest.TestCase):
    def runtime_fixture(self, directory):
        root = Path(directory)
        (root / "logs" / "launches" / ".active-launch").mkdir(
            parents=True
        )
        (root / "bridge.py").write_text("# bridge\n", encoding="utf-8")
        (root / "launch_game.py").write_text(
            "# supervisor\n", encoding="utf-8"
        )
        owner = {
            "schema_version": 1,
            "lease_id": "lease",
            "launcher_pid": 101,
            "started_at": 10.0,
        }
        child = {
            "schema_version": 1,
            "lease_id": "lease",
            "launcher_pid": 101,
            "launch_id": "launch",
            "java_pid": 102,
            "bound_at": 11.0,
        }
        active = root / "logs" / "launches" / ".active-launch"
        (active / "owner.json").write_text(
            json.dumps(owner), encoding="utf-8"
        )
        (active / "child.json").write_text(
            json.dumps(child), encoding="utf-8"
        )
        launch = {
            "schema_version": 2,
            "launch_id": "launch",
            "started_at": 10.0,
            "launch_log_path": "logs/launches/launch.log",
            "launch_start_record_path": (
                "logs/launches/launch.start.json"
            ),
            "launcher_pid": 101,
            "java_pid": 102,
        }
        (root / "launch-latest.json").write_text(
            json.dumps(launch), encoding="utf-8"
        )
        (root / "logs" / "launches" / "launch.start.json").write_text(
            json.dumps(launch), encoding="utf-8"
        )
        snapshot = freeze_manifest.source_snapshot(root)
        bridge_digest = quick_start._sha256_file(root / "bridge.py")
        bridge = {
            "schema_version": 2,
            "protocol_version": 2,
            "instance_token": "instance",
            "bridge_pid": 103,
            "parent_java_pid": 102,
            "launch_id": "launch",
            "bridge_sha256": bridge_digest,
            "runtime_source_digest": freeze_manifest.snapshot_digest(
                snapshot
            ),
            "runtime_source_file_count": len(snapshot),
            "started_at": 1.0,
        }
        (root / "bridge-instance.json").write_text(
            json.dumps(bridge), encoding="utf-8"
        )
        state = {
            "protocol_version": 2,
            "state_seq": 55,
            "decision_id": "decision:ready",
            "ready_for_command": True,
            "phase": "MAIN_MENU",
            "in_game": False,
        }
        (root / "state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        return root

    def test_inspect_runtime_proves_ready_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)

            result = quick_start.inspect_runtime(
                root, process_alive_fn=lambda pid: pid in {101, 102, 103}
            )

            self.assertEqual("ready", result["status"])
            self.assertEqual("launch", result["launch_id"])
            self.assertEqual("MAIN_MENU", result["phase"])
            self.assertEqual(55, result["state_seq"])

    def test_inspect_runtime_marks_changed_source_tree_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)
            (root / "policy.py").write_text("changed = True\n", encoding="utf-8")

            result = quick_start.inspect_runtime(
                root,
                process_alive_fn=lambda pid: pid in {101, 102, 103},
            )

            self.assertEqual("stale", result["status"])
            self.assertEqual(102, result["java_pid"])
            self.assertEqual("MAIN_MENU", result["phase"])

    def test_ensure_runtime_restarts_stale_menu_runtime_once(self):
        process = Mock()
        process.poll.return_value = None
        observations = iter((
            {
                "status": "stale",
                "reason": "active_bridge_loaded_different_source_tree",
                "java_pid": 102,
                "phase": "MAIN_MENU",
                "in_game": False,
                "ready_for_command": True,
            },
            {"status": "starting", "reason": "launcher_is_reaping_java"},
            {"status": "absent", "reason": "no_active_launch_lease"},
            {
                "status": "ready", "reason": "verified",
                "launch_id": "new-launch", "phase": "MAIN_MENU",
            },
        ))
        ticks = iter((10.0, 10.1, 10.2))
        terminate = Mock()
        with (
            patch.object(
                quick_start, "_current_source_identity",
                return_value=("digest", 1),
            ),
            patch.object(
                quick_start, "inspect_runtime",
                side_effect=lambda *args, **kwargs: next(observations),
            ),
            patch.object(
                quick_start, "spawn_supervisor",
                return_value=(process, Path("quick.log")),
            ) as spawn,
        ):
            result = quick_start.ensure_runtime(
                Path.cwd(), process_alive_fn=lambda _pid: True,
                clock=lambda: next(ticks), sleep=lambda _seconds: None,
                terminate_fn=terminate,
            )

        terminate.assert_called_once_with(102)
        spawn.assert_called_once()
        self.assertFalse(result["reused"])
        self.assertTrue(result["restarted_stale_runtime"])

    def test_ensure_runtime_never_restarts_stale_active_game(self):
        stale = {
            "status": "stale",
            "reason": "active_bridge_loaded_different_source_tree",
            "java_pid": 102,
            "phase": "COMBAT",
            "in_game": True,
            "ready_for_command": True,
        }
        terminate = Mock()
        with patch.object(
            quick_start, "_current_source_identity",
            return_value=("digest", 1),
        ), patch.object(quick_start, "inspect_runtime", return_value=stale):
            with self.assertRaisesRegex(
                quick_start.QuickStartError, "safe MAIN_MENU"
            ):
                quick_start.ensure_runtime(
                    Path.cwd(), terminate_fn=terminate,
                )
        terminate.assert_not_called()

    def test_inspect_runtime_does_not_accept_stale_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)
            bridge_path = root / "bridge-instance.json"
            bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
            bridge["started_at"] = 4_000_000_000.0
            bridge_path.write_text(json.dumps(bridge), encoding="utf-8")

            result = quick_start.inspect_runtime(
                root, process_alive_fn=lambda pid: pid in {101, 102, 103}
            )

            self.assertEqual("starting", result["status"])
            self.assertEqual("fresh_ready_state_pending", result["reason"])

    def test_ensure_runtime_reuses_ready_process_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)
            popen = Mock()
            ticks = iter((20.0, 20.025))

            result = quick_start.ensure_runtime(
                root,
                process_alive_fn=lambda pid: pid in {101, 102, 103},
                popen_factory=popen,
                clock=lambda: next(ticks),
            )

            self.assertTrue(result["reused"])
            self.assertEqual(0.025, result["elapsed_seconds"])
            popen.assert_not_called()

    def test_ensure_runtime_restarts_after_shutting_down_lease_disappears(self):
        process = Mock()
        process.poll.return_value = None
        observations = iter((
            {"status": "starting", "reason": "launcher_is_reaping_java"},
            {"status": "absent", "reason": "no_active_launch_lease"},
            {
                "status": "ready", "reason": "verified",
                "launch_id": "new-launch", "phase": "MAIN_MENU",
            },
        ))
        ticks = iter((10.0, 10.2))
        with (
            patch.object(
                quick_start, "_current_source_identity",
                return_value=("digest", 1),
            ),
            patch.object(
                quick_start, "inspect_runtime",
                side_effect=lambda *args, **kwargs: next(observations),
            ),
            patch.object(
                quick_start, "spawn_supervisor",
                return_value=(process, Path("quick.log")),
            ) as spawn,
        ):
            result = quick_start.ensure_runtime(
                Path.cwd(), process_alive_fn=lambda _pid: True,
                clock=lambda: next(ticks), sleep=lambda _seconds: None,
            )

        self.assertFalse(result["reused"])
        self.assertEqual("new-launch", result["launch_id"])
        spawn.assert_called_once()

    def test_spawn_supervisor_is_non_shell_and_captures_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs" / "launches").mkdir(parents=True)
            (root / "launch_game.py").write_text(
                "# supervisor\n", encoding="utf-8"
            )
            process = Mock(pid=321)
            popen = Mock(return_value=process)

            observed, log_path = quick_start.spawn_supervisor(
                root, popen_factory=popen, executable="python-test",
                now=123.0, token="token",
            )

            self.assertIs(process, observed)
            self.assertTrue(log_path.is_file())
            args, kwargs = popen.call_args
            self.assertEqual(
                ["python-test", str(root / "launch_game.py")], args[0]
            )
            self.assertEqual(root.resolve(), kwargs["cwd"])
            self.assertFalse(kwargs["start_new_session"])
            self.assertNotEqual(0, kwargs["creationflags"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(
                "1",
                kwargs["env"][
                    quick_start.launch_game.INTERRUPTED_EXIT_RECOVERY_ENV
                ],
            )


    def test_inspect_runtime_reports_external_process_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)
            (root / "bridge-instance.json").unlink()
            (root / "logs" / "launches" / "launch.log").write_text(
                "ERROR communicationmod.CommunicationMod> Could not start external process.\n"
                "java.io.IOException: Cannot run program: \u62d2\u7edd\u8bbf\u95ee\n",
                encoding="utf-8",
            )

            result = quick_start.inspect_runtime(
                root, process_alive_fn=lambda pid: pid in {101, 102, 103}
            )

            self.assertEqual("failed", result["status"])
            self.assertEqual("bridge_external_process_failed", result["reason"])
            self.assertEqual(102, result["java_pid"])
            self.assertIn("拒绝访问", result["startup_failure_detail"])

    def test_ensure_runtime_stops_safe_failed_main_menu_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.runtime_fixture(directory)
            (root / "bridge-instance.json").unlink()
            (root / "logs" / "launches" / "launch.log").write_text(
                "Could not start external process.\n", encoding="utf-8"
            )
            terminate = Mock()

            with self.assertRaisesRegex(
                quick_start.QuickStartError, "startup failed before bridge readiness"
            ):
                quick_start.ensure_runtime(
                    root,
                    process_alive_fn=lambda pid: pid in {101, 102, 103},
                    terminate_fn=terminate,
                )

            terminate.assert_called_once_with(102)


if __name__ == "__main__":
    unittest.main()
