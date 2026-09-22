import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

import bridge

from spirecomm.ai.agent import SimpleAgent
from spirecomm.communication.action import (
    BossRewardAction,
    BuyCardAction,
    BuyPotionAction,
    BuyRelicAction,
    CancelAction,
    CardRewardAction,
    CombatRewardAction,
    PotionAction,
    ProceedAction,
)
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Player, PlayerClass
from spirecomm.spire.potion import Potion
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import (
    BossRewardScreen,
    CardRewardScreen,
    CombatReward,
    CombatRewardScreen,
    RewardType,
    ScreenType,
    ShopScreen,
)


def card(card_id, card_type=CardType.SKILL, rarity=CardRarity.UNCOMMON, price=0, uuid=None):
    return Card(
        card_id,
        card_id,
        card_type,
        rarity,
        cost=1,
        price=price,
        uuid=uuid or f"{card_id}-{price}",
    )


def strike(index):
    return card(
        "Strike_G",
        card_type=CardType.ATTACK,
        rarity=CardRarity.BASIC,
        uuid=f"strike-{index}",
    )


def defend(index):
    return card(
        "Defend_G",
        rarity=CardRarity.BASIC,
        uuid=f"defend-{index}",
    )


class LiveChoiceGame:
    """Small authoritative-state stub that enters the same public path as autoplay."""

    def __init__(self, screen, *, deck=None, gold=0, act=2, hp=60, max_hp=70):
        self.screen = screen
        self.screen_type = screen.screen_type
        self.deck = list(deck or [])
        self.gold = gold
        self.act = act
        self.floor = 18
        self.current_hp = hp
        self.max_hp = max_hp
        self.player = Player(hp, max_hp, block=0, energy=3)
        self.in_combat = False
        self.room_type = "MonsterRoom"
        self.key_system_unlocked = False
        self.has_emerald_key = False
        self.has_sapphire_key = False

        # Public dispatch flags consumed by SimpleAgent.get_next_action_in_game.
        self.choice_available = True
        self.proceed_available = False
        self.play_available = False
        self.end_available = False
        self.cancel_available = False

        self.are_potions_full = lambda: False
        self.get_real_potions = lambda: []


class LivePolicyRoutingTests(unittest.TestCase):
    def test_live_boss_reward_routes_through_dynamic_relic_selector(self):
        cursed_key = Relic("Cursed Key", "Cursed Key")
        tiny_house = Relic("Tiny House", "Tiny House")
        relics = [cursed_key, tiny_house]
        game = LiveChoiceGame(BossRewardScreen(relics))
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        # Cursed Key wins the old static ordering. Returning Tiny House from
        # the dynamic selector makes this test specifically cover live wiring.
        with patch.object(agent, "choose_boss_relic", return_value=tiny_house) as selector:
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BossRewardAction)
        self.assertIs(tiny_house, action.relic)
        selector.assert_called_once_with(relics)

    def test_live_shop_allows_a_premium_relic_to_beat_card_removal(self):
        deck = [*(strike(index) for index in range(4)), *(defend(index) for index in range(4))]
        bag = Relic("Bag of Preparation", "Bag of Preparation", price=100)
        screen = ShopScreen([], [bag], [], purge_available=True, purge_cost=75)
        game = LiveChoiceGame(screen, deck=deck, gold=150)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(bag, action.relic)

    def test_live_shop_can_buy_a_high_value_potion(self):
        ghost = Potion(
            "GhostInAJar",
            "Ghost in a Jar",
            can_use=True,
            can_discard=True,
            requires_target=False,
            price=60,
        )
        screen = ShopScreen([], [], [ghost], purge_available=False, purge_cost=0)
        game = LiveChoiceGame(screen, gold=100, hp=22, max_hp=70)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(ghost, action.potion)

    def test_full_shop_belt_discards_weak_potion_before_buying_ghost(self):
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        ghost = Potion(
            "GhostInAJar", "Ghost in a Jar", True, True, False, price=60
        )
        game = LiveChoiceGame(
            ShopScreen([], [], [ghost], purge_available=False, purge_cost=0),
            gold=100,
            hp=20,
            max_hp=70,
        )
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        raw_options = bridge.build_options({
            "game_state": {
                "gold": game.gold,
                "screen_type": "SHOP_SCREEN",
                "choice_list": [ghost.name],
                "screen_state": {
                    "purge_available": False,
                    "purge_cost": 0,
                    "cards": [],
                    "relics": [],
                    "potions": [{
                        "id": ghost.potion_id,
                        "name": ghost.name,
                        "price": ghost.price,
                        "can_use": ghost.can_use,
                        "can_discard": ghost.can_discard,
                        "requires_target": ghost.requires_target,
                    }],
                },
            },
        })
        self.assertEqual(1, len(raw_options))
        raw_listing = raw_options[0]
        self.assertEqual(ghost.potion_id, raw_listing["target"]["item"]["id"])
        self.assertEqual(ghost.price, raw_listing["target"]["item"]["price"])
        ghost.protocol_option_id = raw_listing["option_id"]
        ghost.protocol_choice_index = raw_listing["choice_index"]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        discard = agent.get_next_action_in_game(game)

        self.assertIsInstance(discard, PotionAction)
        self.assertFalse(discard.use)
        self.assertIs(weak, discard.potion)

        game.potions = []
        game.are_potions_full = lambda: False
        purchase = agent.get_next_action_in_game(game)
        self.assertIsInstance(purchase, BuyPotionAction)
        self.assertIs(ghost, purchase.potion)

    def test_full_reward_belt_replaces_weak_potion_but_keeps_fairy(self):
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", False, False, False)
        reward = CombatReward(RewardType.POTION, potion=ghost)
        game = LiveChoiceGame(CombatRewardScreen([reward]), hp=18, max_hp=70)
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        discard = agent.get_next_action_in_game(game)

        self.assertIsInstance(discard, PotionAction)
        self.assertFalse(discard.use)
        self.assertIs(weak, discard.potion)

        game.potions = []
        game.are_potions_full = lambda: False
        collect = agent.get_next_action_in_game(game)
        self.assertIsInstance(collect, CombatRewardAction)
        self.assertIs(reward, collect.combat_reward)

        fairy = Potion("FairyPotion", "Fairy in a Bottle", False, True, False)
        game.potions = [fairy]
        game.are_potions_full = lambda: True
        kept = agent.get_next_action_in_game(game)
        self.assertIsInstance(kept, ProceedAction)

    def test_full_reward_uses_fruit_juice_instead_of_discarding_it(self):
        fruit = Potion("Fruit Juice", "Fruit Juice", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", False, False, False)
        reward = CombatReward(RewardType.POTION, potion=ghost)
        game = LiveChoiceGame(CombatRewardScreen([reward]), hp=50, max_hp=70)
        game.potions = [fruit]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertTrue(action.use)
        self.assertIs(fruit, action.potion)

    def test_sozu_never_discards_or_buys_for_an_unobtainable_potion(self):
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        ghost = Potion(
            "GhostInAJar", "Ghost in a Jar", False, False, False, price=60
        )
        game = LiveChoiceGame(
            ShopScreen([], [], [ghost], purge_available=False, purge_cost=0),
            gold=100,
            hp=18,
            max_hp=70,
        )
        game.relics = [Relic("Sozu", "Sozu")]
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        full_action = agent.get_next_action_in_game(game)
        self.assertIsInstance(full_action, CancelAction)

        game.potions = []
        game.are_potions_full = lambda: False
        empty_action = agent.get_next_action_in_game(game)
        self.assertIsInstance(empty_action, CancelAction)

        reward = CombatReward(RewardType.POTION, potion=ghost)
        reward_game = LiveChoiceGame(CombatRewardScreen([reward]), hp=18, max_hp=70)
        reward_game.relics = [Relic("Sozu", "Sozu")]
        reward_game.potions = [weak]
        reward_game.potion_available = True
        reward_game.are_potions_full = lambda: True
        reward_game.get_real_potions = lambda: list(reward_game.potions)
        reward_action = agent.get_next_action_in_game(reward_game)
        self.assertIsInstance(reward_action, ProceedAction)

    def test_full_shop_belt_does_not_discard_when_relic_is_better(self):
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        ghost = Potion(
            "GhostInAJar", "Ghost in a Jar", False, False, False, price=60
        )
        bag = Relic("Bag of Preparation", "Bag of Preparation", price=100)
        game = LiveChoiceGame(
            ShopScreen([], [bag], [ghost], purge_available=False, purge_cost=0),
            gold=120,
            hp=50,
            max_hp=70,
        )
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(bag, action.relic)

    def test_multiple_potion_rewards_collect_the_one_that_justified_discard(self):
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        attack = Potion("AttackPotion", "Attack Potion", False, False, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", False, False, False)
        attack_reward = CombatReward(RewardType.POTION, potion=attack)
        ghost_reward = CombatReward(RewardType.POTION, potion=ghost)
        game = LiveChoiceGame(
            CombatRewardScreen([attack_reward, ghost_reward]), hp=18, max_hp=70
        )
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        discard = agent.get_next_action_in_game(game)
        self.assertIsInstance(discard, PotionAction)
        self.assertIs(weak, discard.potion)

        game.potions = []
        game.are_potions_full = lambda: False
        collect = agent.get_next_action_in_game(game)
        self.assertIsInstance(collect, CombatRewardAction)
        self.assertIs(ghost_reward, collect.combat_reward)

    def test_full_reward_belt_keeps_the_exact_replaceable_bottle_eligible(self):
        """A full belt must not blanket-veto the reward that frees a slot."""
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        attack = Potion("AttackPotion", "Attack Potion", False, False, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", False, False, False)
        attack_reward = CombatReward(RewardType.POTION, potion=attack)
        ghost_reward = CombatReward(RewardType.POTION, potion=ghost)
        game = LiveChoiceGame(
            CombatRewardScreen([attack_reward, ghost_reward]), hp=18, max_hp=70
        )
        game.potions = [weak]
        game.potion_available = True
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(game.potions)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        rows = agent._combat_reward_audit_candidates()

        self.assertFalse(rows[0]["selection_eligible"])
        self.assertEqual(
            "potion_slots_full_requires_resource_preparation",
            rows[0]["veto_reason"],
        )
        self.assertTrue(rows[1]["selection_eligible"])
        self.assertIsNone(rows[1]["veto_reason"])

    def test_live_shop_does_not_buy_a_card_at_its_copy_cap(self):
        owned = card("Predator", card_type=CardType.ATTACK, uuid="predator-owned")
        duplicate = card(
            "Predator",
            card_type=CardType.ATTACK,
            price=50,
            uuid="predator-shop",
        )
        screen = ShopScreen([duplicate], [], [], purge_available=False, purge_cost=0)
        game = LiveChoiceGame(screen, deck=[owned], gold=100)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CancelAction)

    def test_live_shop_prefers_mummified_hand_to_three_filler_cards(self):
        powers = [card("A Thousand Cuts", card_type=CardType.POWER, uuid="cuts-owned")]
        cloak = card("Cloak And Dagger", price=50, uuid="cloak-shop")
        escape = card("Escape Plan", price=75, uuid="escape-shop")
        caltrops = card("Caltrops", card_type=CardType.POWER, price=75, uuid="caltrops-shop")
        hand = Relic("Mummified Hand", "Mummified Hand", price=160)
        screen = ShopScreen(
            [cloak, escape, caltrops], [hand], [], purge_available=True, purge_cost=75
        )
        game = LiveChoiceGame(screen, deck=[*powers, strike(0), defend(0)], gold=300, hp=28)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(hand, action.relic)

    def test_boss_shop_prefers_survival_potion_to_duplicate_predator(self):
        owned_predator = card("Predator", card_type=CardType.ATTACK, uuid="predator-owned")
        duplicate_predator = card(
            "Predator", card_type=CardType.ATTACK, price=73, uuid="predator-shop"
        )
        deflect = card("Deflect", rarity=CardRarity.COMMON, price=26, uuid="deflect-shop")
        dexterity = Potion(
            "DexterityPotion", "Dexterity Potion", True, True, False, price=51
        )
        screen = ShopScreen(
            [duplicate_predator, deflect], [], [dexterity], purge_available=False, purge_cost=0
        )
        game = LiveChoiceGame(
            screen, deck=[owned_predator], gold=100, act=2, hp=12, max_hp=75
        )
        game.floor = 31
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(dexterity, action.potion)

    def test_shop_does_not_chain_multiple_moderate_filler_cards(self):
        first = card("Cloak And Dagger", price=50, uuid="first-filler")
        second = card("Escape Plan", price=50, uuid="second-filler")
        screen = ShopScreen([first, second], [], [], purge_available=False, purge_cost=0)
        game = LiveChoiceGame(screen, gold=150, act=2)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        with patch.object(agent, "_card_reward_score", return_value=20):
            first_action = agent.get_next_action_in_game(game)
            self.assertIsInstance(first_action, BuyCardAction)
            game.deck.append(first_action.card)
            game.gold -= first_action.card.price
            game.screen.cards = [card for card in game.screen.cards if card is not first_action.card]
            second_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(second_action, CancelAction)

    def test_live_normal_card_reward_uses_dynamic_scores(self):
        footwork = card("Footwork", rarity=CardRarity.UNCOMMON, uuid="footwork-reward")
        backflip = card("Backflip", rarity=CardRarity.COMMON, uuid="backflip-reward")
        screen = CardRewardScreen([footwork, backflip], can_bowl=False, can_skip=True)
        game = LiveChoiceGame(screen, act=2)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        scores = {"Footwork": 1.0, "Backflip": 20.0}

        with patch.object(agent, "_card_reward_score", side_effect=lambda item: scores[item.card_id]):
            action = agent.get_next_action_in_game(game)

        # Footwork wins the old static priority list; Backflip only wins when
        # the live reward path compares the context-sensitive scores.
        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(backflip, action.card)

    def test_live_normal_card_reward_skips_when_all_choices_are_negative(self):
        footwork = card("Footwork", rarity=CardRarity.UNCOMMON, uuid="footwork-skip")
        backflip = card("Backflip", rarity=CardRarity.COMMON, uuid="backflip-skip")
        screen = CardRewardScreen([footwork, backflip], can_bowl=False, can_skip=True)
        game = LiveChoiceGame(screen, act=2)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        scores = {"Footwork": -1.0, "Backflip": -2.0}
        with patch.object(agent, "_card_reward_score", side_effect=lambda item: scores[item.card_id]):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CancelAction)
        self.assertTrue(agent.skipped_cards)

    def test_event_card_skip_does_not_suppress_next_combat_card_reward(self):
        event_card = card("Setup", uuid="event-card")
        event_game = LiveChoiceGame(
            CardRewardScreen([event_card], can_bowl=False, can_skip=True),
            act=1,
        )
        event_game.room_type = "EventRoom"
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        with patch.object(agent, "_card_reward_score", return_value=-1.0):
            selected = agent.get_next_action_in_game(event_game)
        self.assertIsInstance(selected, CancelAction)

        card_reward = CombatReward(RewardType.CARD)
        combat_game = LiveChoiceGame(CombatRewardScreen([card_reward]), act=1)
        combat_game.room_type = "MonsterRoom"
        action = agent.get_next_action_in_game(combat_game)

        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(action.combat_reward, card_reward)

    def test_prayer_wheel_second_card_reward_is_not_suppressed_after_first_disappears(self):
        second_card_reward = CombatReward(RewardType.CARD)
        game = LiveChoiceGame(
            # The authoritative parent screen has already removed the first
            # skipped item; only Prayer Wheel's second CARD remains.
            CombatRewardScreen([second_card_reward]),
            act=1,
        )
        game.room_type = "MonsterRoom"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.skipped_cards = True
        agent.skipped_card_rewards_remaining = 1
        agent.skipped_card_reward_key = (game.act, game.floor, game.room_type)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(action.combat_reward, second_card_reward)
        self.assertEqual(0, agent.skipped_card_rewards_remaining)

    def test_act_one_boss_reward_does_not_force_frontloaded_damage_gate(self):
        glass_knife = card(
            "Glass Knife",
            card_type=CardType.ATTACK,
            rarity=CardRarity.RARE,
            uuid="glass-boss",
        )
        wraith = card(
            "Wraith Form v2",
            card_type=CardType.POWER,
            rarity=CardRarity.RARE,
            uuid="wraith-boss",
        )
        game = LiveChoiceGame(
            CardRewardScreen([glass_knife, wraith], can_bowl=False, can_skip=True),
            act=1,
        )
        game.floor = 16
        game.room_type = "MonsterRoomBoss"
        game.act_boss = "SlimeBoss"
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(action.card, wraith)

    def test_silent_boss_reward_prefers_wraith_form_over_a_thousand_cuts(self):
        thousand_cuts = Card(
            "A Thousand Cuts", "A Thousand Cuts", CardType.POWER, CardRarity.RARE,
            cost=2, uuid="cuts",
        )
        glass_knife = Card(
            "Glass Knife", "Glass Knife", CardType.ATTACK, CardRarity.RARE,
            cost=1, uuid="glass",
        )
        wraith_form = Card(
            "Wraith Form v2", "Wraith Form", CardType.POWER, CardRarity.RARE,
            cost=3, uuid="wraith",
        )
        established_offense = [
            card("Blade Dance", uuid="blade"),
            card("Flying Knee", card_type=CardType.ATTACK, uuid="knee"),
            card("Backstab", card_type=CardType.ATTACK, uuid="backstab"),
        ]
        screen = CardRewardScreen(
            [thousand_cuts, glass_knife, wraith_form], can_bowl=False, can_skip=True
        )
        game = LiveChoiceGame(screen, deck=established_offense, act=1, hp=19, max_hp=70)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(wraith_form, action.card)

    def test_poison_synergy_does_not_bypass_better_contextual_reward(self):
        poison_owned = card("Deadly Poison", rarity=CardRarity.COMMON, uuid="poison-owned")
        flask = card("Bouncing Flask", rarity=CardRarity.UNCOMMON, uuid="flask-reward")
        wraith = Card(
            "Wraith Form v2", "Wraith Form", CardType.POWER, CardRarity.RARE,
            cost=3, uuid="wraith-reward",
        )
        screen = CardRewardScreen([flask, wraith], can_bowl=False, can_skip=True)
        game = LiveChoiceGame(screen, deck=[poison_owned], act=2)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(wraith, action.card)

    def test_power_potion_reward_uses_current_fight_value_not_deck_tier(self):
        wraith = Card(
            "Wraith Form v2", "Wraith Form", CardType.POWER, CardRarity.RARE,
            cost=3, uuid="temporary-wraith",
        )
        plans = Card(
            "Well-Laid Plans", "Well-Laid Plans", CardType.POWER,
            CardRarity.UNCOMMON, cost=1, uuid="temporary-plans",
        )
        game = LiveChoiceGame(
            CardRewardScreen([wraith, plans], can_bowl=False, can_skip=False),
            hp=57,
            max_hp=70,
        )
        game.in_combat = True
        game.room_type = "MonsterRoomElite"
        game.turn = 2
        game.hand = []
        game.relics = []
        game.player.powers = []
        game.player.orbs = []
        game.monsters = [
            Monster(
                "GremlinLeader", "Gremlin Leader", 140, 140, 0,
                Intent.BUFF, False, False,
                move_adjusted_damage=0, move_hits=0,
            )
        ]
        game.monsters[0].powers = []
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(plans, action.card)
        self.assertEqual(
            "card_reward_combat_tactical_score",
            agent.last_noncombat_decision["reason"],
        )


if __name__ == "__main__":
    unittest.main()
