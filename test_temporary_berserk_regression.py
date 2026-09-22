import unittest

from test_combat_survival_regressions import GameStub, card, monster

from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import IroncladPriority
from spirecomm.spire.card import CardType
from spirecomm.spire.character import Intent
from spirecomm.spire.power import Power
from spirecomm.spire.relic import Relic


class TemporaryBerserkRegressionTests(unittest.TestCase):
    def _reptomancer_turn_two(self):
        enemies = [
            monster(
                "Dagger",
                23,
                intent=Intent.ATTACK_DEBUFF,
                damage=9,
                hits=1,
                powers=[Power("Minion", "Minion", -1)],
            ),
            monster(
                "Reptomancer",
                184,
                intent=Intent.UNKNOWN,
                damage=-1,
                hits=1,
                powers=[Power("Strength", "Strength", 4)],
            ),
            monster(
                "Dagger",
                15,
                intent=Intent.ATTACK,
                damage=29,
                hits=1,
                powers=[
                    Power("Minion", "Minion", -1),
                    Power("Strength", "Strength", 4),
                ],
            ),
        ]
        enemies[-1].max_hp = 22
        hand = [
            card("Defend_R", CardType.SKILL, cost=1, block=5),
            card(
                "Flame Barrier",
                CardType.SKILL,
                cost=2,
                block=16,
                magic=6,
                upgrades=1,
            ),
            card(
                "Impervious",
                CardType.SKILL,
                cost=2,
                block=40,
                upgrades=1,
            ),
            card(
                "Disarm",
                CardType.SKILL,
                cost=1,
                target=True,
                magic=2,
            ),
            card("Defend_R", CardType.SKILL, cost=1, block=5),
        ]
        hand[2].exhausts = True
        hand[3].exhausts = True
        game = GameStub(
            enemies, hand, hp=45, block=14, energy=3, act=3
        )
        game.current_action = "DiscoveryAction"
        game.turn = 2
        game.room_type = "MonsterRoomElite"
        game.player.powers = [
            Power("Thorns", "Thorns", 3),
            Power("Demon Form", "Demon Form", 2),
            Power("Strength", "Strength", 2),
        ]
        game.draw_pile = [
            card(
                "Whirlwind",
                CardType.ATTACK,
                cost=-1,
                damage=8,
                upgrades=1,
            ),
            card("Cleave", CardType.ATTACK, cost=1, damage=8),
        ]
        game.discard_pile = []
        game.exhaust_pile = []
        game.deck = list(hand) + list(game.draw_pile)
        game.relics = [
            Relic("Bronze Scales", "Bronze Scales"),
            Relic("TungstenRod", "TungstenRod"),
        ]
        return game

    def test_power_potion_prefers_feel_no_pain_over_masked_berserk(self):
        """Replay dea seq399611: base Berserk carries risk past Block."""

        game = self._reptomancer_turn_two()
        berserk = card(
            "Berserk", CardType.POWER, cost=0, magic=2
        )
        feel_no_pain = card(
            "Feel No Pain", CardType.POWER, cost=1, magic=3
        )
        planner = FastCombatPlanner(IroncladPriority())

        berserk_score = planner.score_temporary_card(game, berserk)
        berserk_trace = dict(planner._last_temporary_card_search)
        feel_no_pain_score = planner.score_temporary_card(
            game, feel_no_pain
        )

        self.assertEqual(0, berserk_trace["candidate"]["actual_loss"])
        self.assertEqual(
            ["Impervious", "Disarm", "Berserk"],
            berserk_trace["continuation_card_ids"],
        )
        liability = berserk_trace["cross_turn_liability"]
        self.assertEqual(
            "berserk_carryover_vulnerable_exposure", liability["kind"]
        )
        self.assertEqual(15, liability["observed_vulnerable_attack_delta"])
        self.assertEqual(150.0, liability["score_penalty"])
        self.assertLess(berserk_score, feel_no_pain_score)

    def test_one_turn_berserk_has_no_cross_turn_liability(self):
        game = self._reptomancer_turn_two()
        upgraded_berserk = card(
            "Berserk",
            CardType.POWER,
            cost=0,
            magic=1,
            upgrades=1,
        )
        planner = FastCombatPlanner(IroncladPriority())

        planner.score_temporary_card(game, upgraded_berserk)

        self.assertEqual(
            0.0,
            planner._last_temporary_card_search[
                "cross_turn_liability"
            ]["score_penalty"],
        )

    def test_permanent_berserk_reward_keeps_static_scoring(self):
        game = self._reptomancer_turn_two()
        game.current_action = "CombatRewardAction"
        berserk = card(
            "Berserk", CardType.POWER, cost=0, magic=2
        )
        planner = FastCombatPlanner(IroncladPriority())

        planner.score_temporary_card(game, berserk)

        self.assertEqual(
            "static_fallback",
            planner._last_temporary_card_search["mode"],
        )
        self.assertNotIn(
            "cross_turn_liability", planner._last_temporary_card_search
        )


if __name__ == "__main__":
    unittest.main()
