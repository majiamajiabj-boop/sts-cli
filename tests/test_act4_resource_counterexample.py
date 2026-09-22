import copy
import json
import unittest
from pathlib import Path
from tests.test_terminal_survival import game_for, SimpleAgent

FRAME = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/act4_resource_counterexample.json").read_text(encoding="utf-8"))

class Act4ResourceCounterexampleTests(unittest.TestCase):
    def test_purchased_glacier_reduces_shield_spear_turn_two_loss(self):
        game = game_for(FRAME["shield_turn2"])
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        action = agent.combat_planner.choose_card_action(game)
        with_glacier = agent._planned_turn_hp_loss(action, agent.combat_planner.last_decision)
        alternative = copy.deepcopy(game)
        alternative.hand = [card for card in alternative.hand if card.card_id != "Glacier"]
        other = SimpleAgent(alternative.character, goal_mode="HEART", macro_advisor=None)
        other.game = alternative
        action = other.combat_planner.choose_card_action(alternative)
        without_glacier = other._planned_turn_hp_loss(action, other.combat_planner.last_decision)
        self.assertIsNotNone(with_glacier)
        self.assertIsNotNone(without_glacier)
        self.assertLess(with_glacier, without_glacier)

if __name__ == "__main__":
    unittest.main()
