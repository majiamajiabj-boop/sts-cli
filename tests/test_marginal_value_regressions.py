"""Historical policy regressions with both failures and valid END counterexamples."""
import copy
import json
import unittest
from pathlib import Path

from tests.test_terminal_survival import game_for, SimpleAgent
from spirecomm.communication.action import EndTurnAction
from spirecomm.spire.power import Power

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/marginal_value_frames.json").read_text(encoding="utf-8"))


def replay(seq):
    game = game_for(FRAMES[str(seq)]["frame"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return game, agent, agent.combat_planner


class MarginalValueRegressions(unittest.TestCase):
    def test_attack_strips_block_for_exact_passive_damage(self):
        for seq, expected_card, damage_gain in [
            (799033, "Strike_B", 6), (799146, "Strike_B", 6),
            (799508, "Mind Blast", 1), (799529, "Strike_B", 6),
        ]:
            with self.subTest(seq=seq):
                game, _, planner = replay(seq)
                before = planner.project_end_turn(game)
                action = planner.choose_card_action(game)
                self.assertEqual(expected_card, action.card.card_id)
                after = planner.last_decision["search"]
                self.assertEqual(0, after["actual_loss"])
                self.assertGreaterEqual(
                    after["passive_hp_damage"] - before["passive_hp_damage"],
                    damage_gain,
                )

    def test_passive_lethal_keeps_all_six_correct_end_decisions(self):
        for seq in (798735, 799101, 799199, 799283, 799365, 799628):
            with self.subTest(seq=seq):
                game, _, planner = replay(seq)
                self.assertIsInstance(planner.choose_card_action(game), EndTurnAction)

    def test_zero_effect_orbs_do_not_outrank_clearing_status(self):
        for seq in (798946, 798992):
            with self.subTest(seq=seq):
                game, _, planner = replay(seq)
                action = planner.choose_card_action(game)
                self.assertEqual("Slimed", action.card.card_id)

    def test_zero_focus_still_values_working_orbs(self):
        game, _, planner = replay(798992)
        game.player.powers = [p for p in game.player.powers if p.power_id not in {"Focus", "Bias"}]
        self.assertGreater(planner._orb_value(game, "zap"), 0)
        self.assertGreater(planner._orb_value(game, "glacier"), 0)

    def test_zero_orbs_do_not_disable_glacier_direct_block(self):
        game, _, planner = replay(799260)
        game.player.powers.append(Power("Focus", "Focus", -12))
        game.monsters[0].move_adjusted_damage = 10
        action = planner.choose_card_action(game)
        self.assertEqual("Glacier", action.card.card_id)
        self.assertEqual(1, action.card.upgrades)

    def test_equivalent_glacier_choice_is_independent_of_hand_order(self):
        for reverse in (False, True):
            game, _, planner = replay(799260)
            if reverse:
                game.hand.reverse()
            action = planner.choose_card_action(game)
            self.assertEqual("Glacier", action.card.card_id)
            self.assertEqual(1, action.card.upgrades)

    def test_bite_package_does_not_automatically_displace_starter_removal(self):
        for seq in (797986, 798172):
            with self.subTest(seq=seq):
                game, agent, _ = replay(seq)
                bite = next(c for c in game.deck if c.card_id == "Bite")
                defend = next(c for c in game.deck if c.card_id == "Defend_G")
                self.assertLess(agent._removal_score(bite), agent._removal_score(defend))
                self.assertLess(agent._removal_score_parts(bite)["lost_sustain_value"], 0)

    def test_last_bite_has_more_retention_value_than_surplus_copy(self):
        game, agent, _ = replay(797986)
        bite = next(c for c in game.deck if c.card_id == "Bite")
        full_score = agent._removal_score(bite)
        single = [c for c in game.deck if c.card_id != "Bite"] + [bite]
        self.assertLess(agent._removal_score(bite, single), full_score)


if __name__ == "__main__":
    unittest.main()
