import json
import unittest
from pathlib import Path

from tests.test_terminal_survival import game_for
from tests.test_agent_policy import GameStub, build_card, strike, defend
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.card import CardType
from spirecomm.spire.character import PlayerClass
from spirecomm.spire.relic import Relic

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/strength_readiness_frames.json").read_text(encoding="utf-8"))


def historical_agent(key):
    game = game_for(FRAMES[key]["frame"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class StrengthReadinessTests(unittest.TestCase):
    def test_historical_flex_relic_decks_have_a_real_damage_gap(self):
        for key in ["c5431feb", "13afd91f"]:
            with self.subTest(attempt=key):
                agent = historical_agent(key)
                profile = agent._deck_profile()
                self.assertLess(agent._damage_scaling_support(profile), 0.5)
                self.assertLess(agent._long_fight_kill_clock(profile)["score"], 0.62)
                self.assertGreater(agent._boss_entry_readiness(profile)["gaps"]["kill_clock"], 0)

    def test_red_skull_credit_requires_low_hp(self):
        agent = historical_agent("13afd91f")
        full = agent._deck_profile()["strength_capacity"]
        self.assertFalse(full["red_skull_active"])
        self.assertEqual(0, full["fixed_strength"])
        agent.game.current_hp = agent.game.max_hp // 2
        low = agent._deck_profile()["strength_capacity"]
        self.assertTrue(low["red_skull_active"])
        self.assertEqual(3, low["fixed_strength"])
        self.assertGreater(low["coverage"], full["coverage"])
        self.assertLess(low["coverage"], 0.58)

    def test_vajra_keeps_bounded_damage_and_archetype_value(self):
        agent = historical_agent("c5431feb")
        before = agent._deck_profile()
        agent.game.relics = [r for r in agent.game.relics if agent._token(r.relic_id) != "vajra"]
        after = agent._deck_profile()
        self.assertGreater(agent._damage_scaling_support(before), agent._damage_scaling_support(after))
        self.assertGreater(before["archetypes"]["strength"]["relic_support"], after["archetypes"]["strength"]["relic_support"])

    def test_real_permanent_sources_remain_useful(self):
        for name in ["Inflame", "Spot Weakness", "Demon Form"]:
            with self.subTest(card=name):
                agent = historical_agent("13afd91f")
                profile = agent._deck_profile(agent.game.deck + [build_card(name, CardType.POWER)])
                self.assertGreaterEqual(profile["strength_capacity"]["coverage"], 0.75)
                self.assertGreater(agent._damage_scaling_support(profile), agent._damage_scaling_support(agent._deck_profile()))

    def test_temporary_sources_do_not_accumulate_into_growth(self):
        agent = historical_agent("13afd91f")
        deck = agent.game.deck + [build_card("Flex", cost=0) for _ in range(5)]
        self.assertEqual(0, agent._deck_profile(deck)["strength_capacity"]["persistent_source_support"])

    def test_fixed_seed_with_repeatable_limit_break_is_an_engine(self):
        agent = historical_agent("c5431feb")
        plain = agent._deck_profile(agent.game.deck + [build_card("Limit Break")])
        repeatable = agent._deck_profile(agent.game.deck + [build_card("Limit Break", upgrades=1)])
        self.assertFalse(plain["strength_capacity"]["repeatable_multiplier"])
        self.assertTrue(repeatable["strength_capacity"]["repeatable_multiplier"])
        self.assertGreater(agent._damage_scaling_support(repeatable), agent._damage_scaling_support(plain))

    def test_real_demon_form_is_not_erased(self):
        agent = historical_agent("79896e7a")
        profile = agent._deck_profile()
        self.assertGreaterEqual(profile["strength_capacity"]["coverage"], 0.9)
        self.assertGreater(agent._damage_scaling_support(profile), 0)


if __name__ == "__main__":
    unittest.main()
