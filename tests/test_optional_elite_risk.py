import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_terminal_survival import game_for
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.screen import EventScreen, ScreenType

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/optional_fight_frames.json").read_text(encoding="utf-8"))


def agent_for(key):
    frame = FRAMES[key]["frame"]
    game = game_for(frame)
    game.screen_type = ScreenType.EVENT
    screen = dict(frame["game_state"]["screen_state"])
    # Compact traces omit display-only event prose.
    screen.setdefault("event_name", "")
    screen.setdefault("body_text", "")
    game.screen = EventScreen.from_json(screen)
    game.in_combat = False
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class OptionalEliteRiskTests(unittest.TestCase):
    def test_two_historical_losses_leave_instead_of_buying_another_fight(self):
        for key in FRAMES:
            with self.subTest(attempt=key):
                agent = agent_for(key)
                action = agent.handle_screen()
                detail = agent.last_noncombat_decision
                self.assertEqual(0, action.choice_index)
                self.assertEqual("cower", detail["chosen_kind"])
                self.assertGreater(detail["death_risk"], 0)
                self.assertGreater(detail["upper_hp_loss"], agent.game.current_hp)
                self.assertEqual(detail["upper_hp_loss"], detail["elite_survival_hp_loss"])

    def test_healthy_version_of_same_deck_still_accepts_reward(self):
        agent = agent_for("09199f92")
        agent.game.current_hp = agent.game.max_hp
        action = agent.handle_screen()
        self.assertEqual(1, action.choice_index)
        self.assertEqual("fight", agent.last_noncombat_decision["chosen_kind"])

    def test_card_count_cannot_override_shared_capacity(self):
        agent = agent_for("09199f92")
        profile = agent._optional_event_fight_profile()
        self.assertGreater(profile["tag_score"], 24)
        self.assertLess(profile["capacity"], 1)
        with patch.object(agent, "_deck_readiness", return_value={"score": 0.1}):
            action = agent.handle_screen()
        self.assertEqual(0, action.choice_index)
        self.assertEqual(0.1, agent.last_noncombat_decision["combat_readiness"]["capacity"])

    def test_map_and_optional_elite_use_identical_survival_budget(self):
        agent = agent_for("09199f92")
        node = SimpleNamespace(symbol="E", has_emerald_key=False)
        budget = agent._estimated_survival_room_cost(node)
        agent.handle_screen()
        self.assertEqual(budget, agent.last_noncombat_decision["elite_survival_hp_loss"])
        node.has_emerald_key = True
        self.assertGreater(agent._estimated_survival_room_cost(node), budget)


if __name__ == "__main__":
    unittest.main()
