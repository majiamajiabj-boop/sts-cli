import json
import unittest
from pathlib import Path
from unittest.mock import patch
from test_terminal_survival import game_for, SimpleAgent
from spirecomm.spire.screen import EventScreen, ScreenType

FRAMES = json.loads((Path(__file__).parent / "test_fixtures/optional_sphere_frames.json").read_text(encoding="utf-8"))

def event_agent(key):
    frame = FRAMES[key]["frame"]
    game = game_for(frame)
    screen = dict(frame["game_state"]["screen_state"])
    screen.setdefault("event_name", "")
    screen.setdefault("body_text", "")
    # Compact state omits display prose; recover the same visible option
    # text from the decision's candidate surface, not invented event labels.
    for option in screen["options"]:
        original = next(c for c in FRAMES[key]["visible_options"]
                        if str(c["id"]) == str(option["choice_index"]))
        option["text"], option["label"] = original["text"], original["label"]
    game.screen = EventScreen.from_json(screen)
    game.screen_type = ScreenType.EVENT
    game.in_combat = False
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent

class OptionalSphereRiskTests(unittest.TestCase):
    def test_historical_fights_pay_shared_risk_instead_of_fixed_five(self):
        for key in FRAMES:
            with self.subTest(attempt=key):
                agent = event_agent(key)
                action = agent.handle_screen()
                if key == "90defa5a":
                    self.assertEqual(1, action.choice_index)
                detail = agent.last_noncombat_decision
                self.assertGreater(detail["optional_fight_risk"]["utility_cost"], 5)
                self.assertIn("optional_fight_survival_cost",
                              detail["option_semantics"]["0"])

    def test_strong_healthy_deck_can_still_accept_the_relic(self):
        agent = event_agent("c8e6b292")
        agent.game.current_hp = agent.game.max_hp
        self.assertEqual(0, agent.handle_screen().choice_index)

    def test_forced_fight_does_not_pretend_it_can_be_avoided(self):
        agent = event_agent("90defa5a")
        agent.game.screen.options = agent.game.screen.options[:1]
        self.assertIsNone(agent._generic_optional_fight_risk())
        self.assertEqual(0, agent.handle_screen().choice_index)

    def test_more_hp_never_increases_optional_fight_cost(self):
        agent = event_agent("90defa5a")
        low = agent._generic_optional_fight_risk()["utility_cost"]
        agent.game.current_hp = agent.game.max_hp
        self.assertLessEqual(agent._generic_optional_fight_risk()["utility_cost"], low)

if __name__ == "__main__":
    unittest.main()
