import copy
import unittest
from math import comb

from tests.test_strength_readiness import historical_agent
from tests.test_agent_policy import build_card, strike, defend
from spirecomm.spire.card import CardType


class EngineAccessTests(unittest.TestCase):
    def access(self, size=20, copies=1, energy=0, draw=0, bottled=False, cost=3):
        agent = historical_agent("79896e7a")
        agent.game.relics = []
        cores = [build_card("Demon Form", CardType.POWER, cost=cost) for _ in range(copies)]
        for core in cores:
            core.in_bottle_tornado = bottled
        deck = cores + [defend() for _ in range(size - copies)]
        return agent._damage_engine_access(deck, {"draw": draw}, energy)["demonform"]

    def test_delayed_core_has_no_first_turn_trigger(self):
        access = self.access()
        self.assertEqual([0, 0.1667, 0.3333], access["online_by_turn"])
        self.assertAlmostEqual(1 / 6, access["early_online_fraction"], places=3)

    def test_multiple_sources_use_sampling_without_replacement(self):
        access = self.access(copies=2, energy=1)
        expected = (1 - comb(18, 5) / comb(20, 5) + 1 - comb(18, 10) / comb(20, 10)) / 3
        self.assertAlmostEqual(expected, access["early_online_fraction"], places=3)
        self.assertGreater(access["early_online_fraction"], self.access(energy=1)["early_online_fraction"])

    def test_draw_and_energy_each_improve_setup(self):
        baseline = self.access()["early_online_fraction"]
        self.assertGreater(self.access(draw=2)["early_online_fraction"], baseline)
        self.assertGreater(self.access(energy=1)["early_online_fraction"], baseline)
        self.assertGreater(self.access(size=10)["early_online_fraction"], baseline)

    def test_unpayable_core_is_not_available(self):
        self.assertEqual(0, self.access(cost=4)["early_online_fraction"])
        self.assertGreater(self.access(cost=4, energy=1)["early_online_fraction"], 0)

    def test_bottled_core_is_guaranteed_but_still_delayed(self):
        access = self.access(bottled=True, energy=1)
        self.assertEqual([0, 1, 1], access["online_by_turn"])

    def test_historical_collector_has_a_startup_gap(self):
        agent = historical_agent("79896e7a")
        profile = agent._deck_profile()
        clock = agent._long_fight_kill_clock(profile)
        self.assertEqual(1, clock["engine_access"]["engine:strength"]["source_copies"])
        self.assertLess(clock["score"], 0.62)
        self.assertGreater(agent._boss_entry_readiness(profile)["gaps"]["kill_clock"], 0)
        # This is a macro estimate, not clairvoyance about the real T6 draw.
        agent.game.draw_pile = list(reversed(agent.game.draw_pile))
        self.assertEqual(clock, agent._long_fight_kill_clock(agent._deck_profile()))

    def test_independent_immediate_source_can_cover_a_late_power(self):
        agent = historical_agent("79896e7a")
        before = agent._deck_profile()
        after = agent._deck_profile(agent.game.deck + [build_card("Inflame", CardType.POWER, cost=1)])
        self.assertGreater(agent._damage_scaling_support(after), agent._damage_scaling_support(before))

    def test_candidate_draw_support_closes_part_of_real_clock_gap(self):
        agent = historical_agent("79896e7a")
        before = agent._deck_profile()
        after = agent._deck_profile(agent.game.deck + [build_card("Battle Trance", cost=0)])
        self.assertGreater(agent._long_fight_kill_clock(after)["score"], agent._long_fight_kill_clock(before)["score"])
        self.assertLess(agent._boss_entry_readiness(after)["gaps"]["kill_clock"], agent._boss_entry_readiness(before)["gaps"]["kill_clock"])


if __name__ == "__main__":
    unittest.main()
