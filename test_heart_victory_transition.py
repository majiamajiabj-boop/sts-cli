import copy
import json
import unittest
from pathlib import Path

import death_replay

FRAMES = json.loads((Path(__file__).parent / "test_fixtures/heart_victory_transition.json").read_text(encoding="utf-8"))


class HeartVictoryTransitionTests(unittest.TestCase):
    def test_real_heart_victory_bridge_clears_last_turn_replay(self):
        rows = copy.deepcopy(FRAMES["records"])
        replay = death_replay.build_death_replay(rows, rows[-1], last_turns=1)
        self.assertEqual("act4_terminal", replay["replay_kind"])
        self.assertEqual("clear", replay["status"])
        self.assertEqual(0, replay["issue_count"])
        self.assertEqual(0, replay["eligible_unknown_count"])

    def test_missing_or_duplicate_proceed_cannot_bridge_victory(self):
        for mode in ("missing", "duplicate"):
            rows = copy.deepcopy(FRAMES["records"])
            if mode == "missing":
                rows.pop(-2)
            else:
                rows.insert(-2, copy.deepcopy(rows[-2]))
            with self.subTest(mode=mode):
                replay = death_replay.build_death_replay(rows, rows[-1], last_turns=1)
                self.assertNotEqual("clear", replay["status"])

    def test_invalid_transition_evidence_is_rejected(self):
        mutations = {
            "wrong_action": lambda r: r[-2].update(action="choose"),
            "different_run": lambda r: r[-2].update(run_id="another-run"),
            "different_snapshot_hash": lambda r: r[-2]["authoritative_state_before"].update(decision_hash="other"),
            "wrong_before_sequence": lambda r: r[-2].update(before_seq=620153),
            "wrong_after_sequence": lambda r: r[-2]["authoritative_state_after"].update(state_seq=620157),
            "missing_complete": lambda r: r[-3]["authoritative_state_after"].update(phase="NONE"),
            "mismatched_hp": lambda r: r[-2].update(hp_before=48),
            "mismatched_snapshot_hp": lambda r: r[-2]["authoritative_state_before"]["game_state"].update(current_hp=48),
            "mismatched_max_hp": lambda r: r[-2]["authoritative_state_after"]["game_state"].update(max_hp=99),
            "wrong_floor": lambda r: r[-2]["authoritative_state_after"]["game_state"].update(floor=57),
            "contradictory_victory": lambda r: r[-2]["authoritative_state_after"]["game_state"].update(screen_state={"victory": False}),
            "not_authoritative": lambda r: r[-1].update(authoritative_game_over=False),
            "not_a_victory": lambda r: r[-1].update(victory=False),
            "not_a_heart_win": lambda r: r[-1].update(heart_defeated=False),
            "missing_digest": lambda r: r[-1].pop("selection_digest"),
            "malformed_after": lambda r: r[-3].update(authoritative_state_after=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                rows = copy.deepcopy(FRAMES["records"])
                mutate(rows)
                self.assertIsNone(death_replay._proven_heart_victory_transition(rows, rows[-3], rows[-1]))

    def test_other_combat_findings_are_not_removed_by_victory_bridge(self):
        rows = copy.deepcopy(FRAMES["records"])
        rows[-3]["hp_after"] = 46
        replay = death_replay.build_death_replay(rows, rows[-1], last_turns=1)
        self.assertNotEqual("clear", replay["status"])
        self.assertTrue(replay["issues"] or replay["unknowns"])


if __name__ == "__main__":
    unittest.main()
