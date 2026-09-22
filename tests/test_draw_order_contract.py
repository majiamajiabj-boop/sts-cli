"""Unknown inventories must never be promoted to a concrete future hand."""
import copy
import unittest

from tests.test_combat_survival_regressions import GameStub, card, monster, PrioritiesStub
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.card import CardType
from spirecomm.spire.character import Intent


class DrawOrderContractTests(unittest.TestCase):
    def game(self):
        offering = card("Offering",CardType.SKILL,cost=0,magic=3)
        game = GameStub([monster("JawWorm",40,intent=Intent.ATTACK,damage=12,hits=1)],
                        [offering,card("Defend_R",CardType.SKILL,block=5)],hp=20,energy=2)
        game.draw_pile = [card("Strike_R",CardType.ATTACK,damage=6,target=True),
                          card("Defend_R",CardType.SKILL,block=5),
                          card("Flame Barrier",CardType.SKILL,cost=2,block=12)]
        for i,c in enumerate(game.hand + game.draw_pile):c.uuid=str(i)
        game.discard_pile=[];game.exhaust_pile=[];game.potions=[]
        game.deck=game.hand + game.draw_pile
        game.draw_pile_order_known=False
        return game

    def test_unknown_offering_cannot_claim_an_exact_drawn_hand(self):
        game=self.game();planner=FastCombatPlanner(PrioritiesStub())
        self.assertFalse(planner._temporary_offering_continuation(game,game.hand[0]))
        self.assertEqual(planner._last_temporary_card_search["reason"],"offering_draw_order_unknown")

    def test_unknown_swift_potion_cannot_claim_a_known_top_rescue(self):
        agent=SimpleAgent();agent.game=self.game()
        self.assertIsNone(agent._hypothetical_known_draw_potion_analysis("swiftpotion"))

    def test_unknown_order_does_not_make_discard_cards_drawable_without_shuffle(self):
        game=self.game()
        game.hand=[card("Wound",CardType.STATUS,cost=-2,playable=False)]
        game.draw_pile=game.draw_pile[:1]
        agent=SimpleAgent();agent.game=game
        without_discard=agent._hand_filter_potion_analysis("gamblersbrew")
        game.discard_pile=[card("Impervious",CardType.SKILL,cost=2,block=30)]
        with_discard=agent._hand_filter_potion_analysis("gamblersbrew")
        self.assertEqual(with_discard["draw_mode"],"unknown_draw_expectation")
        self.assertEqual(without_discard["expected_draw_quality"],with_discard["expected_draw_quality"])

    def test_hidden_inventory_permutation_cannot_change_card_choice(self):
        original=self.game();permuted=copy.deepcopy(original)
        permuted.draw_pile.reverse()
        actions=[]
        for game in (original,permuted):
            planner=FastCombatPlanner(PrioritiesStub())
            action=planner.choose_card_action(game)
            actions.append((action.command,getattr(getattr(action,"card",None),"uuid",None)))
            search=planner.last_decision.get("search",{})
            if search:
                self.assertTrue(search["future_draw_pile_unknown"])
        self.assertEqual(actions[0],actions[1])


if __name__=="__main__":unittest.main()
