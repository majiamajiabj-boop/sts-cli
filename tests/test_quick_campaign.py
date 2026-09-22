import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import freeze_manifest
import quick_campaign


class QuickCampaignTests(unittest.TestCase):
    def test_cached_preflight_skips_all_expensive_build_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = {"source_digest": "a" * 64}
            (root / "release-preflight.json").write_text(
                json.dumps(cached), encoding="utf-8"
            )
            replay_runner = Mock()
            ticks = iter((10.0, 10.025))
            with patch.object(
                freeze_manifest, "validate_static_preflight"
            ) as validate, patch.object(
                freeze_manifest, "build_static_preflight"
            ) as build:
                result = quick_campaign.prepare_static_preflight(
                    root,
                    "decision-a",
                    "controller-a",
                    replay_runner=replay_runner,
                    clock=lambda: next(ticks),
                )
            self.assertTrue(result["reused"])
            self.assertEqual(0.025, result["elapsed_seconds"])
            validate.assert_called_once_with(
                cached, root.resolve(), "decision-a", "controller-a"
            )
            build.assert_not_called()
            replay_runner.assert_not_called()

    def test_cache_miss_reuses_a_current_replay_and_writes_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay = {"target_decision_hash": "decision-a"}
            (root / "decision-case-replay.json").write_text(
                json.dumps(replay), encoding="utf-8"
            )
            built = {"source_digest": "b" * 64}
            replay_runner = Mock()
            ticks = iter((1.0, 2.0))
            with patch.object(
                freeze_manifest, "_validate_replay_evidence"
            ) as validate_replay, patch.object(
                freeze_manifest,
                "build_static_preflight",
                return_value=built,
            ) as build, patch.object(
                freeze_manifest, "write_manifest"
            ) as write:
                result = quick_campaign.prepare_static_preflight(
                    root,
                    "decision-a",
                    "controller-a",
                    replay_runner=replay_runner,
                    clock=lambda: next(ticks),
                )
            self.assertFalse(result["reused"])
            self.assertFalse(result["replay_regenerated"])
            replay_runner.assert_not_called()
            validate_replay.assert_called_once_with(replay, "decision-a")
            build.assert_called_once_with(
                root.resolve(),
                "decision-a",
                "controller-a",
                decision_case_replay=replay,
            )
            write.assert_called_once_with(
                root / "release-preflight.json", built
            )

    def test_stale_replay_is_regenerated_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_path = root / "decision-case-replay.json"
            replay_path.write_text('{"old":true}', encoding="utf-8")

            def regenerate(observed_root, observed_hash):
                self.assertEqual(root.resolve(), observed_root)
                self.assertEqual("decision-a", observed_hash)
                replay_path.write_text('{"new":true}', encoding="utf-8")

            with patch.object(
                freeze_manifest,
                "_validate_replay_evidence",
                side_effect=(
                    freeze_manifest.FreezeManifestError("stale"),
                    None,
                ),
            ), patch.object(
                freeze_manifest,
                "build_static_preflight",
                return_value={"source_digest": "c" * 64},
            ), patch.object(freeze_manifest, "write_manifest"):
                result = quick_campaign.prepare_static_preflight(
                    root,
                    "decision-a",
                    "controller-a",
                    replay_runner=regenerate,
                    clock=iter((1.0, 2.0)).__next__,
                )
            self.assertTrue(result["replay_regenerated"])

    def test_spawn_uses_only_authoritative_campaign_run_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = Mock(pid=321)
            popen = Mock(return_value=process)
            observed, log_path = quick_campaign.spawn_campaign_attempt(
                root,
                "decision-a",
                "controller-a",
                max_actions=4321,
                executable="python-test",
                popen_factory=popen,
                now=123.0,
                token="token",
            )
            self.assertIs(process, observed)
            self.assertTrue(log_path.is_file())
            command = popen.call_args.args[0]
            self.assertEqual(
                [
                    "python-test",
                    str(root.resolve() / "campaign_attempt.py"),
                    "run",
                    "--root",
                    str(root.resolve()),
                    "--decision-hash",
                    "decision-a",
                    "--controller-hash",
                    "controller-a",
                    "--max-actions",
                    "4321",
                ],
                command,
            )
            self.assertNotIn("shell", popen.call_args.kwargs)

    def test_wait_for_formal_start_binds_new_context_to_live_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = {
                "schema_version": 2,
                "policy_version": "fast-policy-v5",
                "attempt_id": "attempt-new",
                "run_id": "IRONCLAD:0:1",
                "character": "IRONCLAD",
                "seed": 1,
                "selection_id": "selection-new",
                "decision_hash": "decision-a",
                "controller_hash": "controller-a",
            }
            state = {
                "protocol_version": 2,
                "attempt_id": "attempt-new",
                "selection_id": "selection-new",
                "in_game": True,
                "fast_mode": True,
                "phase": "EVENT",
                "ready_for_command": True,
                "state_seq": 7,
                "game_state": {"class": "IRONCLAD"},
            }
            (root / "run-context.json").write_text(
                json.dumps(context), encoding="utf-8"
            )
            (root / "state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            process = Mock()
            ticks = iter((10.0, 10.15))
            result = quick_campaign.wait_for_formal_start(
                root,
                process,
                "decision-a",
                "controller-a",
                previous_attempt_id="attempt-old",
                clock=lambda: next(ticks),
            )
            self.assertEqual("attempt-new", result["attempt_id"])
            self.assertEqual(0.15, result["formal_start_wait_seconds"])
            process.poll.assert_not_called()

    def test_wait_for_formal_start_reports_early_campaign_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = Mock(returncode=2)
            process.poll.return_value = 2
            with self.assertRaisesRegex(
                quick_campaign.QuickCampaignError,
                "exited before formal START",
            ):
                quick_campaign.wait_for_formal_start(
                    root,
                    process,
                    "decision-a",
                    "controller-a",
                    clock=lambda: 1.0,
                )

    def test_wait_for_formal_start_retries_transient_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context_path = root / "run-context.json"
            state_path = root / "state.json"
            context_path.write_text("{", encoding="utf-8")
            state_path.write_text("{", encoding="utf-8")
            process = Mock()
            process.poll.return_value = None
            context = {
                "schema_version": 2,
                "policy_version": "fast-policy-v5",
                "attempt_id": "attempt-new",
                "run_id": "THE_SILENT:0:1",
                "character": "THE_SILENT",
                "seed": 1,
                "selection_id": "selection-new",
                "decision_hash": "decision-a",
                "controller_hash": "controller-a",
            }
            state = {
                "protocol_version": 2,
                "attempt_id": "attempt-new",
                "selection_id": "selection-new",
                "in_game": True,
                "fast_mode": True,
                "phase": "COMBAT_TURN_1",
                "ready_for_command": True,
                "state_seq": 8,
                "game_state": {"class": "THE_SILENT"},
            }

            def publish_valid_state(_seconds):
                context_path.write_text(
                    json.dumps(context), encoding="utf-8"
                )
                state_path.write_text(json.dumps(state), encoding="utf-8")

            result = quick_campaign.wait_for_formal_start(
                root,
                process,
                "decision-a",
                "controller-a",
                previous_attempt_id="attempt-old",
                clock=iter((10.0, 10.1, 10.2)).__next__,
                sleep=publish_valid_state,
            )
            self.assertEqual("attempt-new", result["attempt_id"])
            self.assertEqual(0.2, result["formal_start_wait_seconds"])
            process.poll.assert_called_once_with()


    def test_start_campaign_keeps_expensive_preflight_before_runtime_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state.json").write_text(json.dumps({
                "protocol_version": 2,
                "in_game": False,
                "phase": "MAIN_MENU",
                "ready_for_command": True,
                "legal_actions": ["start", "state"],
            }), encoding="utf-8")
            order = []
            process = Mock(pid=987)

            def prepare(*args, **kwargs):
                order.append("preflight")
                return {
                    "preflight": {"checkpoint": True},
                    "reused": True,
                    "elapsed_seconds": 0.02,
                    "replay_regenerated": False,
                }

            def ensure(*args, **kwargs):
                order.append("runtime")
                return {"reused": False, "elapsed_seconds": 8.0}

            def finalize(*args, **kwargs):
                order.append("freeze")
                return {"source_digest": "d" * 64}

            def spawn(*args, **kwargs):
                order.append("spawn")
                return process, root / "campaign.log"

            def wait(*args, **kwargs):
                order.append("start")
                return {
                    "attempt_id": "attempt-new",
                    "character": "IRONCLAD",
                    "formal_start_wait_seconds": 0.5,
                }

            ticks = iter((0.0, 1.0, 2.0, 2.2, 3.0, 4.0))
            with patch.object(
                quick_campaign,
                "current_policy_hashes",
                return_value={
                    "decision_hash": "decision-a",
                    "controller_hash": "controller-a",
                },
            ), patch.object(
                quick_campaign,
                "prepare_static_preflight",
                side_effect=prepare,
            ), patch.object(
                quick_campaign.quick_start,
                "ensure_runtime",
                side_effect=ensure,
            ), patch.object(
                quick_campaign,
                "finalize_runtime_freeze",
                side_effect=finalize,
            ), patch.object(
                quick_campaign,
                "spawn_campaign_attempt",
                side_effect=spawn,
            ), patch.object(
                quick_campaign,
                "wait_for_formal_start",
                side_effect=wait,
            ):
                result = quick_campaign.start_campaign(
                    root, clock=lambda: next(ticks)
                )
            self.assertEqual(
                ["preflight", "runtime", "freeze", "spawn", "start"],
                order,
            )
            self.assertEqual("started", result["status"])
            self.assertEqual("attempt-new", result["attempt_id"])
            self.assertEqual(987, result["campaign_pid"])
            self.assertEqual(2.0, result["launch_to_formal_start_seconds"])
            self.assertEqual(4.0, result["total_elapsed_seconds"])


if __name__ == "__main__":
    unittest.main()
