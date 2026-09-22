import unittest
from unittest.mock import patch
from types import SimpleNamespace
from test_live_macro_decisions import LiveDecisionGame, event_screen, defend
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.screen import RestOption

def agent_for():
    game = LiveDecisionGame(event_screen("fixture", ["Leave"]),
                            deck=[defend(upgrades=1)], act=3, floor=35, hp=70, max_hp=70)
    agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent

def node(y, symbol, children=()):
    return SimpleNamespace(x=0, y=y, symbol=symbol, children=list(children), has_emerald_key=False)

class RouteRubyStateTests(unittest.TestCase):
    def test_recall_once_then_later_fire_can_recover(self):
        agent = agent_for()
        last = node(14, "R")
        fight = node(10, "M", [last])
        first = node(8, "R", [fight])
        with patch.object(agent, "_map_node", side_effect=lambda n:n), patch.object(
            agent, "_estimated_survival_room_cost",
            side_effect=lambda n, **kw: 40 if n.symbol == "M" else 0,
        ):
            risk = agent._path_survival_risk(first, emerald_pending=False)
        self.assertEqual(19, risk)  # 70 -> Recall -> 30 -> Rest to 51.
        self.assertFalse(agent.game.has_ruby_key)
        self.assertEqual(35, agent.game.floor)

    def test_projected_final_fire_uses_its_own_deadline(self):
        agent = agent_for()
        action, _, details = agent._route_campfire_choice(node(14, "R"), 10, ruby_pending=True)
        self.assertEqual(RestOption.RECALL, action)
        self.assertTrue(details["mandatory_recall"])
        action, _, details = agent._route_campfire_choice(node(14, "R"), 10, ruby_pending=False)
        self.assertEqual(RestOption.REST, action)
        self.assertFalse(details["mandatory_recall"])

    def test_low_hp_before_deadline_still_heals(self):
        agent = agent_for()
        action, _, _ = agent._route_campfire_choice(node(8, "R"), 10, ruby_pending=True)
        self.assertEqual(RestOption.REST, action)

if __name__ == "__main__":
    unittest.main()
