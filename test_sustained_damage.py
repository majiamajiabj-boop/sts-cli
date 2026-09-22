import copy
import json
import unittest
from pathlib import Path
from test_terminal_survival import game_for, SimpleAgent

FRAMES = json.loads((Path(__file__).parent / "test_fixtures/sustained_damage_frames.json").read_text(encoding="utf-8"))

def agent_for(key):
    game = game_for(FRAMES[key]["frame"])
    game.in_combat = False
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent

class SustainedDamageTests(unittest.TestCase):
    def test_historical_single_poison_packet_is_not_a_complete_growth_engine(self):
        agent = agent_for("078ac357")
        profile = agent._deck_profile()
        detail = profile["sustained_damage"]
        self.assertGreater(detail["finite_damage_roles"], 0)
        self.assertEqual(0, detail["recurring_poison_sources"])
        self.assertLess(agent._long_fight_kill_clock(profile)["score"],
                        FRAMES["078ac357"]["previous_kill_clock"]["score"])

    def test_block_sources_do_not_guarantee_payoff_access(self):
        agent = agent_for("af024a34")
        profile = agent._deck_profile()
        self.assertLess(profile["sustained_damage"]["body_slam_scaling_ceiling"], 1)
        self.assertLess(agent._long_fight_kill_clock(profile)["score"],
                        FRAMES["af024a34"]["previous_kill_clock"]["score"])

    def test_exhausting_attacks_keep_frontload_but_not_repeatable_credit(self):
        agent = agent_for("078ac357")
        before = agent._deck_profile()
        deck = copy.deepcopy(agent.game.deck)
        for card in deck:
            if agent._token(card.card_id) == "diediedie":
                card.exhausts = False
        after = agent._deck_profile(deck)
        self.assertEqual(before["roles"]["nonbasic_damage"], after["roles"]["nonbasic_damage"])
        self.assertGreater(after["sustained_damage"]["repeatable_damage_roles"],
                           before["sustained_damage"]["repeatable_damage_roles"])

    def test_more_payoff_copies_increase_access_without_mutating_live_deck(self):
        agent = agent_for("af024a34")
        deck = list(agent.game.deck)
        slam = next(c for c in deck if agent._token(c.card_id) == "bodyslam")
        before = agent._deck_profile(deck)["sustained_damage"]["body_slam_damage_per_turn"]
        after = agent._deck_profile(deck + [copy.deepcopy(slam)])["sustained_damage"]["body_slam_damage_per_turn"]
        self.assertGreater(after, before)
        self.assertEqual(len(deck), len(agent.game.deck))

    def test_retained_block_growth_and_recurring_poison_keep_credit(self):
        from test_live_macro_decisions import make_card, CardType
        agent = agent_for("af024a34")
        deck = list(agent.game.deck) + [
            make_card("Barricade", card_type=CardType.POWER), make_card("Entrench"),
        ]
        self.assertEqual(1.0, agent._deck_profile(deck)["sustained_damage"]["body_slam_scaling_ceiling"])
        agent = agent_for("078ac357")
        deck = list(agent.game.deck) + [make_card("Deadly Poison")]
        self.assertEqual(1.0, agent._deck_profile(deck)["sustained_damage"]["poison_scaling_ceiling"])

if __name__ == "__main__":
    unittest.main()
