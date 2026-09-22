import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.agent import SimpleAgent
from spirecomm.communication.action import BuyRelicAction, CancelAction, CombatRewardAction
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Player, PlayerClass
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import (
    CombatReward,
    CombatRewardScreen,
    RewardType,
    ScreenType,
    ShopScreen,
)


def starter_card(card_id, card_type):
    return Card(
        card_id,
        card_id,
        card_type,
        CardRarity.BASIC,
        cost=1,
        uuid=f"starter-{card_id}",
        has_target=card_type == CardType.ATTACK,
        is_playable=True,
        damage=6 if card_type == CardType.ATTACK else 0,
        block=5 if card_type == CardType.SKILL else 0,
    )


class MacroGameStub:
    def __init__(self):
        self.player = Player(70, 70, block=0, energy=3)
        self.current_hp = 70
        self.max_hp = 70
        self.act = 1
        self.floor = 6
        self.gold = 500
        self.deck = [
            starter_card("Strike_G", CardType.ATTACK),
            starter_card("Defend_G", CardType.SKILL),
        ]
        self.relics = []
        self.screen = ShopScreen([], [], [], False, 75)
        self.screen_type = ScreenType.SHOP_SCREEN
        self.key_system_unlocked = True
        self.has_ruby_key = False
        self.has_emerald_key = True
        self.has_sapphire_key = False
        self.are_potions_full = lambda: False
        self.get_real_potions = lambda: []


class RelicPolicyRegressionTests(unittest.TestCase):
    def make_agent(self, player_class=PlayerClass.THE_SILENT):
        agent = SimpleAgent(player_class, goal_mode="HEART")
        agent.game = MacroGameStub()
        return agent

    def test_paper_crane_aliases_both_defer_act_two_sapphire_key(self):
        for relic_id in ("Paper Crane", "PaperKrane"):
            with self.subTest(relic_id=relic_id):
                agent = self.make_agent()
                game = agent.game
                game.act = 2
                game.floor = 24
                relic = Relic(relic_id, "Paper Crane")
                relic_reward = CombatReward(RewardType.RELIC, relic=relic)
                key_reward = CombatReward(
                    RewardType.SAPPHIRE_KEY,
                    link=relic,
                )
                game.screen_type = ScreenType.COMBAT_REWARD
                game.screen = CombatRewardScreen([relic_reward, key_reward])

                action = agent.handle_screen()

                self.assertGreater(agent._relic_acquisition_score(relic), 30)
                self.assertIsInstance(action, CombatRewardAction)
                self.assertIs(action.combat_reward, relic_reward)

    def test_agent_unusable_and_low_value_relics_are_not_auto_bought(self):
        relic_ids = (
            "Prismatic Shard",
            "Frozen Eye",
            "Juzu Bracelet",
            "Hand Drill",
        )
        for relic_id in relic_ids:
            with self.subTest(relic_id=relic_id):
                agent = self.make_agent()
                relic = Relic(relic_id, relic_id, price=145)
                agent.game.screen = ShopScreen([], [relic], [], False, 75)

                action = agent.choose_shop_action()

                self.assertIsInstance(action, CancelAction)

    def test_unknown_relic_uses_cautious_baseline(self):
        agent = self.make_agent()
        relic = Relic("Unmodelled Relic", "Unmodelled Relic", price=100)
        agent.game.screen = ShopScreen([], [relic], [], False, 75)

        action = agent.choose_shop_action()

        self.assertIsInstance(action, CancelAction)

    def test_premium_relics_remain_shop_buys(self):
        for relic_id, price in (
            ("Bag of Preparation", 100),
            ("Tungsten Rod", 150),
        ):
            with self.subTest(relic_id=relic_id):
                agent = self.make_agent()
                relic = Relic(relic_id, relic_id, price=price)
                agent.game.screen = ShopScreen([], [relic], [], False, 75)

                action = agent.choose_shop_action()

                self.assertIsInstance(action, BuyRelicAction)
                self.assertIs(action.relic, relic)

    def test_damage_and_reactive_relic_aliases_are_not_unknown(self):
        agent = self.make_agent(PlayerClass.IRONCLAD)
        for relic_id in ("Paper Frog", "Paper Phrog", "Boot", "Bronze Scales"):
            with self.subTest(relic_id=relic_id):
                relic = Relic(relic_id, relic_id, price=100)
                score = agent._shop_relic_score(relic)
                self.assertGreaterEqual(score, 20)

    def test_role_relics_are_priced_from_the_current_deck(self):
        agent = self.make_agent(PlayerClass.THE_SILENT)
        low_support = agent._shop_relic_score(
            Relic("Calipers", "Calipers", price=100)
        )
        agent.game.deck.extend(
            [
                starter_card("Defend_G", CardType.SKILL),
                starter_card("Defend_G", CardType.SKILL),
                starter_card("Defend_G", CardType.SKILL),
                starter_card("Defend_G", CardType.SKILL),
            ]
        )
        high_support = agent._shop_relic_score(
            Relic("Calipers", "Calipers", price=100)
        )
        self.assertGreater(high_support, low_support)

    def test_new_reactive_relics_clear_the_shop_unknown_gate(self):
        agent = self.make_agent(PlayerClass.DEFECT)
        for relic_id in (
            "Akabeko",
            "Kunai",
            "Nunchaku",
            "Letter Opener",
            "Mercury Hourglass",
            "Calipers",
            "Thread and Needle",
            "Charon's Ashes",
        ):
            with self.subTest(relic_id=relic_id):
                relic = Relic(relic_id, relic_id, price=100)
                self.assertGreaterEqual(agent._shop_relic_score(relic), 20)

    def test_relic_score_is_discounted_as_run_nears_its_end(self):
        agent = self.make_agent()
        relic = Relic("Bag of Preparation", "Bag of Preparation")

        act_one_score = agent._shop_relic_score(relic)
        agent.game.act = 2
        agent.game.floor = 24
        act_two_score = agent._shop_relic_score(relic)
        agent.game.act = 3
        agent.game.floor = 47
        late_act_three_score = agent._shop_relic_score(relic)

        self.assertGreater(act_one_score, act_two_score)
        self.assertGreater(act_two_score, late_act_three_score)
        self.assertGreater(late_act_three_score, 20)

    def test_second_relic_needs_more_value_but_premium_relic_still_clears_gate(self):
        standalone_agent = self.make_agent()
        standalone_lantern = Relic("Lantern", "Lantern", price=150)
        standalone_agent.game.screen = ShopScreen(
            [], [standalone_lantern], [], False, 75,
        )
        self.assertIsInstance(
            standalone_agent.choose_shop_action(), BuyRelicAction,
        )

        agent = self.make_agent()
        bag = Relic("Bag of Preparation", "Bag of Preparation", price=100)
        marginal = Relic("Lantern", "Lantern", price=150)
        agent.game.screen = ShopScreen([], [bag, marginal], [], False, 75)

        first_action = agent.choose_shop_action()
        agent.game.gold -= bag.price
        agent.game.screen = ShopScreen([], [marginal], [], False, 75)
        second_action = agent.choose_shop_action()

        self.assertIs(first_action.relic, bag)
        self.assertEqual(1, agent.shop_relics_bought)
        self.assertIsInstance(second_action, CancelAction)

        premium_agent = self.make_agent()
        premium_bag = Relic(
            "Bag of Preparation", "Bag of Preparation", price=100,
        )
        tungsten = Relic("Tungsten Rod", "Tungsten Rod", price=150)
        premium_agent.game.screen = ShopScreen(
            [], [premium_bag, tungsten], [], False, 75,
        )

        first_action = premium_agent.choose_shop_action()
        premium_agent.game.gold -= premium_bag.price
        premium_agent.game.screen = ShopScreen([], [tungsten], [], False, 75)
        second_action = premium_agent.choose_shop_action()

        self.assertIs(first_action.relic, premium_bag)
        self.assertEqual(1, premium_agent.shop_relics_bought)
        self.assertIsInstance(second_action, BuyRelicAction)
        self.assertIs(second_action.relic, tungsten)


if __name__ == "__main__":
    unittest.main()
