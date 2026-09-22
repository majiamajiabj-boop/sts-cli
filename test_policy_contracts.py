import unittest
from pathlib import Path

import policy_contracts


ROOT = Path(__file__).resolve().parent
AGENT_SOURCE = (
    ROOT / "src" / "spirecomm-master" / "spirecomm" / "ai" / "agent.py"
)


class PolicyContractTests(unittest.TestCase):
    def test_every_event_choice_has_comparable_candidate_scores(self):
        self.assertEqual(
            [],
            policy_contracts.unscored_event_choice_calls(AGENT_SOURCE),
        )

    def test_scanner_reports_reason_and_line(self):
        source = """
def choose_event_action(self, valid_options):
    def choose_index(index, reason, candidate_scores=None):
        return index
    return choose_index(0, "fixed_threshold")
"""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.py"
            path.write_text(source, encoding="utf-8")
            rows = policy_contracts.unscored_event_choice_calls(path)
        self.assertEqual("fixed_threshold", rows[0]["reason"])
        self.assertIsInstance(rows[0]["line"], int)


if __name__ == "__main__":
    unittest.main()
