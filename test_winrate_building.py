"""Regression coverage for shared permanent-deck defense valuation."""
import copy
import json
import unittest
from pathlib import Path

from test_agent_policy import (
    SimpleAgent, PlayerClass, GameStub, build_card, CardType, CardRarity, Relic,
)
from test_winrate_mechanics import Game

FRAMES = json.loads((Path(__file__).parent / "test_fixtures" /
                     "winrate_building_frames.json").read_text(encoding="utf-8"))


def historical_agent(seq):
    frame = FRAMES[str(seq)]
    raw = copy.deepcopy(frame["game_state"])

    def restore(value):
        if isinstance(value, dict):
            if "card_instance_id" in value:
                value.setdefault("uuid", value["card_instance_id"])
            for item in list(value.values()):
                restore(item)
        elif isinstance(value, list):
            for item in value:
                restore(item)

    restore(raw)
    raw.setdefault("map", [])
    game = Game.from_json(raw, frame["available_commands"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class DefenseAccessTests(unittest.TestCase):
    def agent(self, deck, player_class=PlayerClass.THE_SILENT):
        agent = SimpleAgent(player_class, goal_mode="HEART", macro_advisor=None)
        agent.game = GameStub([], deck)
        agent.game.in_combat = False
        agent.game.act = 2
        agent.game.floor = 22
        agent.game.act_boss = "Champ"
        return agent

    def test_historical_defense_rewards_beat_skip(self):
        for seq, expected in ((570768, "Ghostly Armor"),
                              (571858, "Deflect"), (571947, "Leg Sweep")):
            with self.subTest(seq=seq):
                agent = historical_agent(seq)
                self.assertGreater(agent._boss_entry_readiness()["gaps"]["block"], 0)
                agent.choose_card_reward()
                self.assertEqual(expected, agent.last_noncombat_decision["chosen"])

    def test_copy_evaluation_distinguishes_defense_from_more_poison(self):
        agent = historical_agent(572055)
        before = agent._deck_readiness()["coverage"]["block"]
        for name, improves in (("After Image", True), ("Footwork", True),
                               ("Corpse Explosion", False)):
            with self.subTest(name=name):
                card = next(c for c in agent.game.deck if c.card_id == name)
                after = agent._deck_readiness(
                    agent._deck_profile(agent.game.deck + [card]))["coverage"]["block"]
                if improves:
                    self.assertGreater(after, before)
                    self.assertGreater(agent._card_boss_gap_relief(
                        card, agent._deck_profile()), 0)
                else:
                    self.assertLess(after, before)

    def test_more_dead_draws_lower_defense_with_the_same_role_count(self):
        defense = [build_card("Ghostly Armor", block=10) for _ in range(3)]
        fillers = [build_card("Strike_G", CardType.ATTACK, damage=6) for _ in range(7)]
        agent = self.agent(defense + fillers)
        thin = agent._deck_readiness()["coverage"]["block"]
        agent.game.deck.extend(copy.deepcopy(fillers) * 2)
        thick = agent._deck_readiness()["coverage"]["block"]
        self.assertLess(thick, thin)

    def test_better_block_and_upgrades_increase_capacity(self):
        cards = [build_card("Deflect", block=4, cost=0) for _ in range(3)]
        cards += [build_card("Strike_G", CardType.ATTACK, damage=6) for _ in range(17)]
        agent = self.agent(cards)
        before = agent._deck_readiness()["coverage"]["block"]
        agent.game.deck[0].base_block = agent.game.deck[0].block = 7
        agent.game.deck[0].upgrades = 1
        self.assertGreater(agent._deck_readiness()["coverage"]["block"], before)

    def test_energy_limits_paid_defense_and_energy_relic_relaxes_the_limit(self):
        cards = [build_card("Ghostly Armor", block=12, cost=3) for _ in range(4)]
        agent = self.agent(cards)
        low = agent._deck_profile()["defense_access"]
        self.assertLess(low["pay_fraction"], 1)
        self.assertLessEqual(low["direct_block_per_turn"], 8)
        self.assertGreater(low["burst_block_per_turn"], low["direct_block_per_turn"])
        agent.game.relics = [Relic("Cursed Key", "Cursed Key")]
        high = agent._deck_profile()["defense_access"]
        self.assertGreater(high["direct_block_per_turn"], low["direct_block_per_turn"])

    def test_unpayable_card_does_not_invent_defense(self):
        agent = self.agent([build_card("Ghostly Armor", block=30, cost=4)])
        self.assertEqual(0, agent._deck_profile()["defense_access"]["direct_block_per_turn"])

    def test_free_block_is_not_charged_the_paid_cards_energy_budget(self):
        cards = [build_card("Ghostly Armor", block=10, cost=3) for _ in range(4)]
        cards.append(build_card("Deflect", block=4, cost=0))
        agent = self.agent(cards)
        # Four paid cards demand 12 energy; 2 reserved defense energy buys
        # 40 * 2/12 block. The free card contributes all four points.
        self.assertAlmostEqual(10.667,
                               agent._deck_profile()["defense_access"]["direct_block_per_turn"],
                               places=3)

    def test_dexterity_requires_real_block_draws(self):
        agent = self.agent([build_card("Footwork", CardType.POWER)])
        self.assertEqual(0, agent._deck_profile()["defense_access"]["dexterity_block"])
        agent.game.deck.append(build_card("Deflect", block=4, cost=0))
        self.assertGreater(agent._deck_profile()["defense_access"]["dexterity_block"], 0)

    def test_frost_focus_requires_a_channel_source(self):
        agent = self.agent([build_card("Defragment", CardType.POWER)],
                           PlayerClass.DEFECT)
        self.assertEqual(0, agent._deck_profile()["defense_access"]["frost_block"])
        agent.game.deck.append(build_card("Coolheaded"))
        focused = agent._deck_profile()["defense_access"]["frost_block"]
        without = agent._deck_profile([agent.game.deck[-1]])["defense_access"]["frost_block"]
        self.assertGreater(focused, without)

    def test_counterfactual_profile_does_not_borrow_live_deck_defense(self):
        agent = historical_agent(571947)
        unsupported = agent._deck_profile([build_card("Accuracy", CardType.POWER)])
        self.assertEqual(0, agent._deck_readiness(unsupported)["coverage"]["block"])

    def test_existing_saturated_defense_still_has_no_boss_gap_credit(self):
        agent = historical_agent(571947)
        agent.game.deck = [build_card("Ghostly Armor", block=15, cost=1) for _ in range(6)]
        candidate = next(c for c in agent.game.screen.cards if c.card_id == "Leg Sweep")
        self.assertEqual(0, agent._card_boss_gap_relief(candidate, agent._deck_profile()))


if __name__ == "__main__":
    unittest.main()
