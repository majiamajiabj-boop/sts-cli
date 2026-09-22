import json
import unittest
from copy import deepcopy
from pathlib import Path

from tests.test_terminal_survival import game_for, SimpleAgent
from spirecomm.spire.screen import CardRewardScreen
from spirecomm.communication.action import CardRewardAction, CancelAction

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/duplicate_gap_frames.json").read_text(encoding="utf-8"))


class DuplicateGapTests(unittest.TestCase):
    def agent_for(self, name="poison_gap"):
        raw = FRAMES[name]["frame"]
        game = game_for(raw)
        screen = deepcopy(raw["game_state"]["screen_state"])
        for card in screen["cards"]:
            card["uuid"] = card["card_instance_id"]
        game.screen = CardRewardScreen.from_json(screen)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_logged_second_poison_competes_above_skip(self):
        agent = self.agent_for()
        action = agent.choose_card_reward()
        self.assertIsInstance(action, CardRewardAction)
        self.assertEqual("Deadly Poison", action.card.card_id)
        self.assertGreater(agent._card_reward_score(action.card), agent._permanent_card_pick_hurdle())

    def test_logged_dash_without_material_repair_still_skips(self):
        agent = self.agent_for("dash_no_repair")
        self.assertIsInstance(agent.choose_card_reward(), CancelAction)

    def test_third_poison_does_not_escape_saturation(self):
        agent = self.agent_for()
        poison = next(c for c in agent.game.screen.cards if c.card_id == "Deadly Poison")
        agent.game.deck.append(deepcopy(poison))
        self.assertLess(agent._card_reward_score(poison), agent._permanent_card_pick_hurdle())
        action = agent.choose_card_reward()
        self.assertFalse(isinstance(action, CardRewardAction) and action.card.card_id == "Deadly Poison")

    def test_satisfied_scaling_does_not_waive_second_copy_penalty(self):
        agent = self.agent_for()
        poison = next(c for c in agent.game.screen.cards if c.card_id == "Deadly Poison")
        before = agent._deck_profile()
        # Counterfactual strong coverage: this exercises the scoring guard,
        # independently of which archetype supplied the already-met need.
        original = agent._deck_readiness
        def covered(profile):
            result = deepcopy(original(profile))
            result["coverage"] = dict.fromkeys(result["coverage"], 1.0)
            return result
        agent._deck_readiness = covered
        self.assertGreater(agent._copy_saturation_penalty(poison, 1, 0.0, profile=before), 0)


if __name__ == "__main__":
    unittest.main()
