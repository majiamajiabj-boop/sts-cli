import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import freeze_manifest
import release_preflight


class ReleasePreflightTests(unittest.TestCase):
    def test_build_and_write_persists_atomic_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_path = root / "decision-case-replay.json"
            output_path = root / "release-preflight.json"
            replay_path.write_text('{"release_gate_passed":true}', encoding="utf-8")
            expected = {
                "source_digest": "a" * 64,
                "source_file_count": 9,
                "full_tests": {"test_count": 10},
            }
            with patch.object(
                freeze_manifest,
                "build_static_preflight",
                return_value=expected,
            ) as build, patch.object(
                freeze_manifest, "write_manifest"
            ) as write:
                observed = release_preflight.build_and_write(
                    root,
                    "decision-a",
                    "controller-a",
                    replay_path,
                    output_path,
                )
            self.assertIs(expected, observed)
            build.assert_called_once_with(
                root.resolve(),
                "decision-a",
                "controller-a",
                decision_case_replay={"release_gate_passed": True},
            )
            write.assert_called_once_with(output_path, expected)

    def test_main_reports_concise_checkpoint_identity(self):
        preflight = {
            "source_digest": "b" * 64,
            "source_file_count": 11,
            "full_tests": {"test_count": 12},
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            release_preflight,
            "build_and_write",
            return_value=preflight,
        ), patch("builtins.print") as emit:
            output = Path(directory) / "release-preflight.json"
            code = release_preflight.main([
                "--decision-hash", "decision-a",
                "--controller-hash", "controller-a",
                "--write", str(output),
            ])
        self.assertEqual(0, code)
        payload = json.loads(emit.call_args.args[0])
        self.assertEqual("clear", payload["status"])
        self.assertEqual(12, payload["test_count"])
        self.assertEqual(str(output.resolve()), payload["preflight_path"])


if __name__ == "__main__":
    unittest.main()
