import json
import unittest
from copy import deepcopy
from pathlib import Path
from test_terminal_survival import game_for, SimpleAgent
from spirecomm.spire.screen import HandSelectScreen, ScreenType

FRAMES = json.loads((Path(__file__).parent / "test_fixtures/exhaust_plan_frames.json").read_text(encoding="utf-8"))


class ExhaustPlanTests(unittest.TestCase):
    def replay(self, hp=None, lethal=False, operation="ExhaustAction"):
        raw = FRAMES["exhaust_select"]["frame"]
        game = game_for(raw)
        data = deepcopy(raw["game_state"]["screen_state"])
        for card in data["hand"]:
            card["uuid"] = card["card_instance_id"]
        game.screen = HandSelectScreen.from_json(data)
        game.screen_type = ScreenType.HAND_SELECT
        game.current_action = operation
        game.choice_available = True
        if hp is not None:
            game.player.current_hp = game.current_hp = hp
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        source = deepcopy(FRAMES["exhaust_play"]["decision"])
        agent._combat_epoch = source["_combat_context"][0]
        source["search"]["true_combat_end"] = lethal
        agent.combat_planner.last_decision = source
        return agent, agent.handle_screen()

    def test_logged_safe_exhaust_preserves_high_damage_card(self):
        agent, action = self.replay()
        self.assertEqual("Defend_R", action.cards[0].card_id)
        self.assertIsNone(agent.last_noncombat_decision["combat_plan_protection"])
        self.assertIsNotNone(agent.last_noncombat_decision["exhaust_plan_reconsideration"])

    def test_immediate_survival_keeps_exact_plan(self):
        agent, action = self.replay(hp=1)
        self.assertEqual("Hemokinesis", action.cards[0].card_id)
        self.assertIsNotNone(agent.last_noncombat_decision["combat_plan_protection"])

    def test_verified_lethal_keeps_exact_plan(self):
        _, action = self.replay(lethal=True)
        self.assertEqual("Hemokinesis", action.cards[0].card_id)

    def test_reversible_discard_keeps_exact_plan(self):
        _, action = self.replay(operation="DiscardAction")
        self.assertEqual("Hemokinesis", action.cards[0].card_id)


if __name__ == "__main__":
    unittest.main()
