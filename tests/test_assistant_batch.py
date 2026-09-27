import json
import tempfile
import unittest
from pathlib import Path

from assistant_batch import run_batch, run_count


class AssistantBatchTests(unittest.TestCase):
    def test_death_then_next_game_and_exact_stop_count(self):
        calls = []
        with tempfile.TemporaryDirectory(prefix="中文 空格 ") as directory:
            root = Path(directory)

            def start(path, **kwargs):
                self.assertTrue(kwargs["p0_only_batch"])
                self.assertTrue(kwargs["wait_for_completion"])
                attempt = {"attempt_id": str(len(calls)), "termination_kind": "game_over",
                           "victory": bool(calls), "heart_defeated": False}
                kwargs["on_started"](attempt)
                calls.append(attempt)
                (path / "run-result.json").write_text(json.dumps(attempt), encoding="utf-8")
                return attempt

            result = run_batch(root, 2, start=start)
            self.assertEqual(2, len(calls))
            self.assertEqual("completed", result["status"])
            self.assertEqual(2, result["completed"])
            self.assertFalse(result["attempts"][0]["victory"])
            self.assertTrue(result["attempts"][1]["victory"])

    def test_operational_error_does_not_retry_or_count_as_game(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def fail(*args, **kwargs):
                calls.append(1)
                raise RuntimeError("bridge disconnected")
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                run_batch(directory, 2, start=fail)
            self.assertEqual(1, len(calls))
            state = json.loads((Path(directory) / "data/auto-batch.json").read_text(encoding="utf-8"))
            self.assertEqual(0, state["completed"])
            self.assertEqual("error", state["status"])

    def test_stale_terminal_does_not_start_next_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run-result.json").write_text(json.dumps({"attempt_id": "old", "termination_kind": "game_over"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                run_batch(root, 2, start=lambda *a, **kw: {"attempt_id": "new"})

    def test_count_rejected_before_launch(self):
        for value in (0, -1, 25, True, 1.5, "", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_count(value)


if __name__ == "__main__":
    unittest.main()
