import unittest

from test_combat_survival_regressions import (
    CardType,
    EndTurnAction,
    FastCombatPlanner,
    GameStub,
    Power,
    PrioritiesStub,
    Relic,
    card,
    monster,
)
from spirecomm.spire.character import Intent


class CalculatedGambleExactnessTests(unittest.TestCase):
    def setUp(self):
        self.planner = FastCombatPlanner(PrioritiesStub())

    @staticmethod
    def lethal_redraw_game():
        attacker = monster(
            "Attacker", 100, intent=Intent.ATTACK, damage=18, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = [
            card(
                f"Dazed-{index}", CardType.STATUS, cost=-2,
                playable=False,
            )
            for index in range(2)
        ]
        for status in dazed:
            status.card_id = "Dazed"
        survivor = card("Survivor", CardType.SKILL, cost=1, block=8)
        deflect = card("Deflect", CardType.SKILL, cost=0, block=4)
        game = GameStub(
            [attacker], [gamble, *dazed], hp=7, block=6, energy=1, act=2
        )
        game.draw_pile = [deflect, survivor]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0
        return game

    def test_drawn_void_cannot_be_used_as_an_exact_energy_proof(self):
        game = self.lethal_redraw_game()
        void = card("Void", CardType.STATUS, cost=-2, playable=False)
        game.draw_pile = [game.draw_pile[-1], void]

        self.assertIsInstance(
            self.planner.choose_card_action(game), EndTurnAction
        )

    def test_any_ink_bottle_counter_rejects_single_redraw_proof(self):
        game = self.lethal_redraw_game()
        game.relics = [Relic("Ink Bottle", "Ink Bottle", counter=8)]

        self.assertIsInstance(
            self.planner.choose_card_action(game), EndTurnAction
        )

    def test_strange_spoon_random_exhaust_rejects_single_redraw_proof(self):
        game = self.lethal_redraw_game()
        game.relics = [Relic("Strange Spoon", "Strange Spoon")]
        game.player.powers = [Power("FeelNoPainPower", "Feel No Pain", 3)]

        self.assertIsInstance(
            self.planner.choose_card_action(game), EndTurnAction
        )

    def test_no_block_power_rejects_defensive_redraw_proof(self):
        game = self.lethal_redraw_game()
        game.player.powers = [Power("NoBlockPower", "No Block", 2)]

        self.assertIsInstance(
            self.planner.choose_card_action(game), EndTurnAction
        )

    def test_targeted_kill_is_not_accepted_as_unbound_redraw_continuation(self):
        attacker = monster(
            "Attacker", 6, intent=Intent.ATTACK, damage=10, hits=1
        )
        gamble = card("Calculated Gamble", CardType.SKILL, cost=0)
        dazed = card("Dazed", CardType.STATUS, cost=-2, playable=False)
        strike = card(
            "Strike_G", CardType.ATTACK, cost=1, damage=6, target=True
        )
        game = GameStub([attacker], [gamble, dazed], hp=5, energy=1, act=2)
        game.draw_pile = [strike]
        game.discard_pile = []
        game.cards_discarded_this_turn = 0

        self.assertIsInstance(
            self.planner.choose_card_action(game), EndTurnAction
        )


if __name__ == "__main__":
    unittest.main()
