import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.agent import SimpleAgent
from spirecomm.communication.action import (
    BuyCardAction,
    BuyPotionAction,
    BuyRelicAction,
    CancelAction,
    CombatRewardAction,
    CardSelectAction,
    CardRewardAction,
    ChooseAction,
    EndTurnAction,
    PlayCardAction,
    PotionAction,
    ProceedAction,
    RestAction,
)
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Orb, Player, PlayerClass
from spirecomm.spire.potion import Potion
from spirecomm.spire.power import Power
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import CardRewardScreen, CombatReward, CombatRewardScreen, EventOption, EventScreen, GridSelectScreen, HandSelectScreen, RestOption, RestScreen, RewardType, ScreenType, ShopScreen


def staged_event_contract(
    event_id, event_class, stage, original_index, option_kind,
    instance_parameters, parameters,
):
    return {
        "contract_version": 1,
        "contract_kind": "BASE_GAME_EVENT_OPTION",
        "event_id": event_id,
        "event_class": event_class,
        "event_stage": stage,
        "original_button_index": original_index,
        "option_kind": option_kind,
        "instance_parameters": dict(instance_parameters),
        "parameters": dict(parameters),
    }


def make_monster(name, hp, damage, poison=0, index=0):
    item = Monster(
        name,
        name,
        hp,
        hp,
        0,
        Intent.ATTACK,
        False,
        False,
        move_adjusted_damage=damage,
        move_hits=1,
    )
    item.monster_index = index
    item.powers = [Power("Poison", "Poison", poison)] if poison else []
    return item


def strike():
    return Card(
        "Strike_G",
        "Strike",
        CardType.ATTACK,
        CardRarity.BASIC,
        cost=1,
        uuid="strike-1",
        has_target=True,
        is_playable=True,
        damage=6,
    )


def defend(block=5):
    return Card(
        "Defend_G",
        "Defend",
        CardType.SKILL,
        CardRarity.BASIC,
        cost=1,
        uuid="defend-1",
        has_target=False,
        is_playable=True,
        block=block,
    )


def slimed():
    return Card(
        "Slimed",
        "Slimed",
        CardType.STATUS,
        CardRarity.SPECIAL,
        cost=1,
        uuid="slimed-1",
        has_target=False,
        is_playable=True,
        exhausts=True,
    )


def build_card(card_id, card_type=CardType.SKILL, *, cost=1, damage=0, block=0, rarity=CardRarity.UNCOMMON, upgrades=0):
    return Card(
        card_id,
        card_id,
        card_type,
        rarity,
        upgrades=upgrades,
        cost=cost,
        uuid=f"build-{card_id}",
        has_target=card_type == CardType.ATTACK,
        is_playable=True,
        damage=damage,
        base_damage=damage,
        block=block,
        base_block=block,
    )


class GameStub:
    def __init__(self, monsters, cards):
        self.monsters = monsters
        self.hand = cards
        self.player = Player(80, 80, block=0, energy=3)
        self.player.powers = []
        self.player.orbs = []
        self.act = 1
        self.floor = 1
        self.turn = 1
        self.room_type = "MonsterRoom"
        self.relics = []
        self.deck = list(cards)
        self.current_hp = self.player.current_hp
        self.max_hp = self.player.max_hp
        self.gold = 0
        self.in_combat = True
        self.get_real_potions = lambda: []
        self.are_potions_full = lambda: False


class SharedAgentPolicyTests(unittest.TestCase):
    def test_noncombat_score_keeps_full_precision_for_replay_contract(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        evidence = agent._linear_score_contract(
            "precision_fixture_v1",
            {"third": 1.0 / 3.0, "penalty": 0.1},
            (
                ("third", "third", 1.0),
                ("penalty", "penalty", -1.0),
            ),
        )
        expected = 1.0 / 3.0 - 0.1

        agent._record_noncombat_decision(
            "precision_fixture",
            [{
                "id": "fixture",
                "choice_id": "fixture",
                "choice_index": 0,
                "action": "choose",
                "score": expected,
                "consequences": {"operation": "fixture"},
                **evidence,
            }],
        )

        row = agent.last_noncombat_decision["candidates"][0]
        self.assertEqual(expected, row["score"])
        self.assertNotEqual(round(expected, 3), row["score"])

    def test_numeric_candidate_subset_cannot_self_attest_audit_completeness(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        agent._record_noncombat_decision(
            "partial_candidate_example", [("only_logged_option", 12.0)]
        )

        contract = agent.last_noncombat_decision["candidate_contract"]
        self.assertFalse(contract["strategy_quality_auditable"])
        self.assertFalse(contract["all_visible_options_scored"])
        self.assertEqual(
            "unverified_candidate_subset", contract["score_source"]
        )

    @staticmethod
    def _enable_combat_dispatch(game):
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False

    @staticmethod
    def _masked_bandits_game(
        cards,
        *,
        hp,
        max_hp,
        gold,
        potions=(),
        option_order=("pay", "fight"),
        player_class=PlayerClass.THE_SILENT,
        floor=24,
    ):
        game = GameStub([], list(cards))
        game.in_combat = False
        game.act = 2
        game.floor = floor
        game.act_boss = "Collector"
        game.current_hp = hp
        game.max_hp = max_hp
        game.player.current_hp = hp
        game.player.max_hp = max_hp
        game.gold = gold
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "Masked Bandits", "Masked Bandits", ""
        )
        options = {
            "pay": EventOption(
                "Pay all Gold", "Pay all Gold", False, 0
            ),
            "fight": EventOption("Fight", "Fight", False, 1),
        }
        game.screen.options = [options[kind] for kind in option_order]
        game.get_real_potions = lambda: list(potions)
        return game, SimpleAgent(player_class, goal_mode="HEART")

    def test_act1_frontload_is_a_soft_score_not_a_forced_gate(self):
        backflip = build_card("Backflip", CardType.SKILL, block=5)
        sucker_punch = build_card(
            "Sucker Punch", CardType.ATTACK, damage=7,
            rarity=CardRarity.COMMON,
        )
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.screen = CardRewardScreen(
            [backflip, sucker_punch], can_bowl=False, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(action.card, backflip)
        self.assertEqual(
            "card_reward_context_score",
            agent.last_noncombat_decision["reason"],
        )
        candidate_ids = {
            item["id"] for item in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(
            {"card:build-Backflip", "card:build-Sucker Punch", "skip"},
            candidate_ids,
        )
        skip = next(
            item for item in agent.last_noncombat_decision["candidates"]
            if item["id"] == "skip"
        )
        self.assertEqual("action:return", skip["choice_id"])

    def test_zero_cost_cards_are_preserved_in_deck_curve_and_tags(self):
        zero = build_card("Finesse", CardType.SKILL, cost=0)
        game = GameStub([], [zero])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        profile = agent._deck_profile()

        self.assertIn("zero_cost", agent._card_build_tags(zero))
        self.assertEqual(1, profile["roles"]["zero_cost"])
        self.assertEqual(0.0, profile["average_cost"])

    def test_conditional_block_is_discounted_in_deck_readiness(self):
        rage = build_card("Rage", cost=0, rarity=CardRarity.UNCOMMON)
        reliable = build_card("Ghostly Armor", block=10)
        game = GameStub([], [rage, reliable, strike(), defend()])
        game.in_combat = False
        game.act = 2
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        profile = agent._deck_profile()

        self.assertAlmostEqual(2.35, profile["roles"]["block"])
        self.assertAlmostEqual(1.35, profile["roles"]["nonbasic_block"])
        self.assertLess(
            agent._deck_plan_summary(profile)["readiness"]["coverage"]["block"],
            0.7,
        )

    def test_cables_internal_relic_id_supports_defect_frost_plan(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.relics = [Relic("Cables", "Gold-Plated Cables")]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        profile = agent._deck_profile()

        self.assertGreater(
            profile["archetypes"]["frost"]["relic_support"], 0
        )

    def test_deck_plan_exposes_normalized_resource_demand(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        demand = agent._deck_plan_summary()["demand"]

        self.assertEqual(1.0, demand["frontload"])
        self.assertEqual(1.0, demand["block"])
        self.assertEqual(1.0, demand["draw"])

    def test_deck_plan_exposes_commitment_and_boss_readiness(self):
        game = GameStub([], [
            build_card("Deadly Poison"),
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Poisoned Stab", CardType.ATTACK, damage=6),
            strike(),
            defend(),
        ])
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.act_boss = "Collector"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        plan = agent._deck_plan_summary()
        readiness = plan["readiness"]
        boss_readiness = agent._boss_entry_readiness()

        self.assertEqual("poison", plan["primary"])
        self.assertEqual("soft", plan["commitment"]["phase"])
        self.assertIn("block", readiness["gaps"])
        self.assertIn("scaling", boss_readiness["gaps"])
        self.assertLess(boss_readiness["score"], 0.55)

    def test_boss_display_names_share_one_canonical_policy(self):
        def snapshot(display_name, act):
            game = GameStub([], [strike(), defend()])
            game.in_combat = False
            game.act = act
            game.floor = 32 if act == 2 else 49
            game.act_boss = display_name
            agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
            agent.game = game
            boss = agent._boss_entry_readiness()
            return (
                boss["boss_id"],
                boss["requirements"],
                agent._campfire_threshold(15),
            )

        self.assertEqual(
            snapshot("Collector", 2), snapshot("The Collector", 2)
        )
        donu_id, requirements, threshold = snapshot("Donu and Deca", 3)
        self.assertEqual("donuanddeca", donu_id)
        self.assertEqual(0.62, requirements["block"])
        self.assertEqual(0.58, requirements["scaling"])
        self.assertEqual(0.92, threshold)

    def test_readiness_average_cannot_hide_a_boss_block_bottleneck(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 2
        game.floor = 32
        game.act_boss = "Champ"
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        # Historical losing-deck shape: plenty of nominal damage, scaling,
        # draw and AOE tags, but only 1.35 reliable non-basic block in a
        # 24-card starter-heavy deck.  The old weighted average called this
        # boss-ready because all stronger roles masked block access.
        profile = {
            "roles": {
                "nonbasic_damage": 3.0,
                "nonbasic_block": 1.35,
                "draw": 1.5,
                "scaling": 1.0,
                "energy": 1.0,
                "aoe": 1.0,
            },
            "size": 24,
            "starters": 7,
            "curses": 0,
            "upgraded": 3,
            "expensive": 2,
            "energy_relic_support": 0.0,
        }

        readiness = agent._deck_readiness(profile)
        boss = agent._boss_entry_readiness(profile)

        self.assertGreater(readiness["raw_role_score"], 0.85)
        self.assertLess(readiness["score"], 0.72)
        self.assertEqual("block", readiness["bottleneck_role"])
        self.assertFalse(boss["ready"])
        self.assertEqual("block", boss["bottleneck_role"])
        self.assertLess(boss["bottleneck_ratio"], 0.90)

    def test_scaling_readiness_requires_source_payoff_mechanics(self):
        cases = (
            (
                PlayerClass.IRONCLAD,
                "Automaton",
                [
                    build_card(
                        "Heavy Blade", CardType.ATTACK, cost=2, damage=14
                    ),
                    build_card("Shrug It Off", block=8),
                    build_card("Cleave", CardType.ATTACK, damage=8),
                    strike(), defend(),
                ],
            ),
            (
                PlayerClass.DEFECT,
                "Hexaghost",
                [
                    build_card("Capacitor", CardType.POWER),
                    build_card("Zap", CardType.SKILL, cost=1),
                    build_card("Cold Snap", CardType.ATTACK, damage=6),
                    strike(), defend(),
                ],
            ),
            (
                PlayerClass.DEFECT,
                "Collector",
                [
                    build_card("Creative AI", CardType.POWER, cost=3),
                    build_card("Creative AI", CardType.POWER, cost=3),
                    build_card("Loop", CardType.POWER),
                    strike(), defend(),
                ],
            ),
        )
        for player_class, boss_id, deck in cases:
            with self.subTest(player_class=player_class, boss_id=boss_id):
                game = GameStub([], deck)
                game.in_combat = False
                game.act = 2
                game.floor = 32
                game.act_boss = boss_id
                if player_class == PlayerClass.DEFECT and boss_id == "Hexaghost":
                    # Historical false positive: starter Zap plus Cracked
                    # Core was treated as enough repeatable channel support
                    # to make Capacitor a complete scaling engine.
                    game.relics = [Relic("CrackedCore", "Cracked Core")]
                agent = SimpleAgent(player_class, goal_mode="HEART")
                agent.game = game

                readiness = agent._deck_readiness()
                boss = agent._boss_entry_readiness()

                self.assertEqual(0.0, readiness["coverage"]["scaling"])
                self.assertFalse(boss["ready"])
                self.assertEqual(0.0, boss["requirement_ratios"]["scaling"])
                plan = agent._deck_plan_summary()
                self.assertIn("scaling", plan["needs"])
                self.assertGreater(plan["demand"]["scaling"], 0.0)

    def test_first_reliable_scaling_closes_hexaghost_reward_gap(self):
        deck = (
            [
                build_card(
                    "Strike_R", CardType.ATTACK, cost=1, damage=9,
                    rarity=CardRarity.BASIC, upgrades=1,
                )
                for _ in range(3)
            ]
            + [
                build_card(
                    "Strike_R", CardType.ATTACK, cost=1, damage=6,
                    rarity=CardRarity.BASIC,
                ),
                build_card(
                    "Defend_R", CardType.SKILL, cost=1, block=8,
                    rarity=CardRarity.BASIC, upgrades=1,
                ),
            ]
            + [
                build_card(
                    "Defend_R", CardType.SKILL, cost=1, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(3)
            ]
            + [
                build_card(
                    "Bash", CardType.ATTACK, cost=2, damage=10,
                    rarity=CardRarity.BASIC, upgrades=1,
                ),
                build_card(
                    "Cleave", CardType.ATTACK, cost=1, damage=11,
                    rarity=CardRarity.COMMON, upgrades=1,
                ),
            ]
        )
        clothesline = build_card(
            "Clothesline", CardType.ATTACK, cost=2, damage=12,
            rarity=CardRarity.COMMON,
        )
        shrug = build_card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8,
            rarity=CardRarity.COMMON,
        )
        demon_form = build_card(
            "Demon Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 7
        game.act_boss = "Hexaghost"
        game.current_hp = game.player.current_hp = 67
        game.screen = CardRewardScreen(
            [clothesline, shrug, demon_form],
            can_bowl=False,
            can_skip=True,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        before = agent._boss_entry_readiness()
        with_demon = agent._boss_entry_readiness(
            agent._deck_profile(deck + [demon_form])
        )
        hexaghost_plan_value = agent._deck_plan_adjustment(demon_form)
        action = agent.choose_card_reward()

        self.assertEqual(0.3, before["gaps"]["scaling"])
        self.assertEqual(0.0, with_demon["gaps"]["scaling"])
        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(demon_form, action.card)
        scores = {
            row["id"]: row["score"]
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertGreater(
            scores[f"card:{demon_form.uuid}"],
            scores[f"card:{shrug.uuid}"],
        )
        game.act_boss = "SlimeBoss"
        slime_plan_value = agent._deck_plan_adjustment(demon_form)
        self.assertNotIn(
            "scaling", agent._boss_entry_readiness()["gaps"]
        )
        self.assertGreater(
            hexaghost_plan_value - slime_plan_value, 7.0
        )
        self.assertLess(
            hexaghost_plan_value - slime_plan_value, 8.0
        )

    def test_supported_strength_source_restores_scaling_coverage(self):
        deck = [
            build_card("Inflame", CardType.POWER),
            build_card("Heavy Blade", CardType.ATTACK, cost=2, damage=14),
            build_card("Shrug It Off", block=8),
            strike(), defend(),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.act_boss = "Automaton"
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        readiness = agent._deck_readiness()

        self.assertGreaterEqual(readiness["coverage"]["scaling"], 0.75)
        self.assertIn(
            "strength",
            readiness["scaling_mechanism"]["paired_engines"],
        )

    def test_non_block_defenses_contribute_explicit_mitigation_coverage(self):
        cases = (
            (PlayerClass.THE_SILENT, "Wraith Form v2", CardType.POWER),
            (PlayerClass.THE_SILENT, "Apparition", CardType.SKILL),
            (PlayerClass.THE_SILENT, "Ghostly", CardType.SKILL),
            (PlayerClass.THE_SILENT, "Piercing Wail", CardType.SKILL),
            (PlayerClass.DEFECT, "Buffer", CardType.POWER),
            (PlayerClass.IRONCLAD, "Disarm", CardType.SKILL),
        )
        for player_class, card_id, card_type in cases:
            with self.subTest(player_class=player_class, card_id=card_id):
                defense = build_card(card_id, card_type)
                game = GameStub([], [defense, strike(), defend()])
                game.in_combat = False
                game.act = 2
                game.act_boss = "Automaton"
                agent = SimpleAgent(player_class, goal_mode="HEART")
                agent.game = game

                readiness = agent._deck_readiness()

                mechanism = readiness["defense_mechanism"]
                self.assertGreater(mechanism["equivalent_block_roles"], 0.0)
                self.assertIn(agent._token(card_id), mechanism["evidence"])
                self.assertGreater(readiness["coverage"]["block"], 0.0)

    def test_long_fight_readiness_requires_a_kill_clock(self):
        profile = {
            "roles": {
                "damage": 7.0,
                "nonbasic_damage": 5.0,
                "nonbasic_block": 3.0,
                "draw": 2.0,
                "scaling": 0.0,
                "energy": 1.0,
                "aoe": 1.0,
                "power": 0.0,
                "zero_cost": 0.0,
            },
            "size": 20,
            "starters": 3,
            "curses": 0,
            "upgraded": 6,
            "expensive": 2,
            "energy_relic_support": 1.0,
        }
        for player_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            with self.subTest(player_class=player_class):
                game = GameStub([], [strike(), defend()])
                game.in_combat = False
                game.act = 2
                game.floor = 32
                game.act_boss = "Collector"
                game.current_hp = game.player.current_hp = 70
                game.max_hp = game.player.max_hp = 70
                agent = SimpleAgent(player_class, goal_mode="HEART")
                agent.game = game

                readiness = agent._deck_readiness(profile)
                boss = agent._boss_entry_readiness(profile)

                self.assertLess(readiness["kill_clock"]["score"], 0.50)
                self.assertLess(readiness["kill_clock_factor"], 0.90)
                self.assertEqual(
                    0.62, boss["requirements"]["kill_clock"]
                )
                self.assertEqual("scaling", boss["bottleneck_role"])
                self.assertFalse(boss["ready"])

    def test_slime_split_clock_rejects_historical_ironclad_false_ready(self):
        deck = [
            *(build_card(
                "Strike_R", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(3)),
            *(build_card(
                "Defend_R", block=5, rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            build_card(
                "Bash", CardType.ATTACK, cost=2, damage=10,
                rarity=CardRarity.BASIC, upgrades=1,
            ),
            build_card("Barricade", CardType.POWER, cost=3,
                       rarity=CardRarity.RARE),
            build_card("Reckless Charge", CardType.ATTACK, cost=0,
                       damage=10, upgrades=1),
            build_card("Battle Trance", cost=0),
            build_card("Whirlwind", CardType.ATTACK, cost=-1, damage=5),
            build_card("Evolve", CardType.POWER, upgrades=1),
            build_card("Whirlwind", CardType.ATTACK, cost=-1, damage=5),
            build_card("Power Through", block=15),
            build_card("Shrug It Off", block=8,
                       rarity=CardRarity.COMMON),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 16
        game.act_boss = "Slime Boss"
        game.current_hp = game.player.current_hp = 77
        game.max_hp = game.player.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        readiness = agent._deck_readiness()
        boss = agent._boss_entry_readiness()

        # Barricade/Evolve are defense/draw, not damage scaling. The Slime
        # Boss still has its separate front-loaded split-clock requirement.
        self.assertLess(readiness["kill_clock"]["score"], 0.50)
        self.assertNotIn("scaling", boss["requirements"])
        self.assertEqual(1.0, boss["requirements"]["split_clock"])
        self.assertAlmostEqual(
            0.874, boss["coverage"]["split_clock"], delta=0.01
        )
        self.assertEqual("split_clock", boss["bottleneck_role"])
        self.assertFalse(boss["ready"])

    def test_slime_split_clock_rejects_historical_silent_false_ready(self):
        deck = [
            *(build_card(
                "Strike_G", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            *(build_card(
                "Defend_G", block=5, rarity=CardRarity.BASIC,
            ) for _ in range(5)),
            build_card("Survivor", block=8, rarity=CardRarity.BASIC),
            build_card(
                "Neutralize", CardType.ATTACK, cost=0, damage=3,
                rarity=CardRarity.BASIC,
            ),
            build_card("Dash", CardType.ATTACK, cost=2, damage=10,
                       block=10),
            build_card("Acrobatics", rarity=CardRarity.COMMON),
            build_card("Dagger Spray", CardType.ATTACK, damage=4,
                       rarity=CardRarity.COMMON),
            build_card("Infinite Blades", CardType.POWER),
            build_card("After Image", CardType.POWER,
                       rarity=CardRarity.RARE),
            build_card("Cloak And Dagger", block=6,
                       rarity=CardRarity.COMMON),
            build_card("PiercingWail", rarity=CardRarity.COMMON),
            build_card("Accuracy", CardType.POWER),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 16
        game.act_boss = "Slime Boss"
        game.current_hp = game.player.current_hp = 49
        game.max_hp = game.player.max_hp = 75
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game

        readiness = agent._deck_readiness()
        boss = agent._boss_entry_readiness()

        self.assertGreater(readiness["kill_clock"]["score"], 0.62)
        self.assertAlmostEqual(
            0.625, boss["coverage"]["split_clock"], delta=0.02
        )
        self.assertEqual("split_clock", boss["bottleneck_role"])
        self.assertFalse(boss["ready"])

    def test_slime_split_clock_is_positive_and_boss_scoped(self):
        complete_readiness = {
            "coverage": {
                "frontload": 1.0, "block": 1.0, "scaling": 1.0,
                "draw": 1.0, "aoe": 1.0,
            },
            "consistency": 0.98,
            "kill_clock": {"score": 1.0},
        }
        for player_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            with self.subTest(player_class=player_class):
                game = GameStub([], [strike(), defend()])
                game.in_combat = False
                game.act = 1
                game.floor = 15
                game.act_boss = "Slime Boss"
                game.current_hp = game.player.current_hp = 70
                game.max_hp = game.player.max_hp = 70
                agent = SimpleAgent(player_class, goal_mode="HEART")
                agent.game = game
                with patch.object(
                    agent, "_deck_readiness", return_value=complete_readiness
                ), patch.object(
                    agent, "_act1_elite_readiness", return_value=0.0
                ):
                    slime = agent._boss_entry_readiness({
                        "roles": {}, "early_offense_readiness": 0.98,
                    })
                self.assertTrue(slime["ready"])
                self.assertAlmostEqual(
                    0.98, slime["coverage"]["split_clock"], delta=0.001
                )

                game.act_boss = "The Guardian"
                with patch.object(
                    agent, "_deck_readiness", return_value=complete_readiness
                ):
                    guardian = agent._boss_entry_readiness({"roles": {}})
                self.assertNotIn("split_clock", guardian["requirements"])
                self.assertIn("scaling", guardian["requirements"])

    def test_boss_readiness_includes_hp_potions_and_awakened_power_tax(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 48
        game.act_boss = "Awakened One"
        game.current_hp = game.player.current_hp = 24
        game.max_hp = game.player.max_hp = 80
        profile = {
            "roles": {
                "nonbasic_damage": 3.0,
                "nonbasic_block": 3.0,
                "draw": 2.0,
                "scaling": 1.0,
                "energy": 1.0,
                "aoe": 1.0,
                "power": 7.0,
                "zero_cost": 0.0,
            },
            "size": 20,
            "starters": 2,
            "curses": 0,
            "upgraded": 8,
            "expensive": 2,
            "energy_relic_support": 1.0,
        }
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        boss = agent._boss_entry_readiness(profile)

        self.assertFalse(boss["ready"])
        self.assertLess(boss["survival"]["ratio"], 0.80)
        self.assertLess(boss["boss_specific_factor"], 1.0)
        self.assertIn("power_density", boss["boss_constraints"])

    def test_boss_survival_credit_distinguishes_defensive_from_elixir(self):
        profile = {
            "roles": {
                "nonbasic_damage": 3.0,
                "nonbasic_block": 3.0,
                "draw": 2.0,
                "scaling": 1.0,
                "energy": 1.0,
                "aoe": 1.0,
                "power": 0.0,
                "zero_cost": 0.0,
            },
            "size": 20,
            "starters": 2,
            "curses": 0,
            "upgraded": 8,
            "expensive": 2,
            "energy_relic_support": 1.0,
        }

        def readiness_for(potion):
            game = GameStub([], [strike(), defend()])
            game.in_combat = False
            game.act = 3
            game.act_boss = "Time Eater"
            game.current_hp = game.player.current_hp = 45
            game.max_hp = game.player.max_hp = 80
            game.get_real_potions = lambda: [potion]
            agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
            agent.game = game
            return agent._boss_entry_readiness(profile)

        ghost = readiness_for(
            Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        )
        elixir = readiness_for(
            Potion("Elixir", "Elixir", True, True, False)
        )

        self.assertEqual(0.08, ghost["survival"]["potion_credit"])
        self.assertEqual(0.0, elixir["survival"]["potion_credit"])
        self.assertGreater(
            ghost["survival"]["ratio"], elixir["survival"]["ratio"]
        )
        fairy = readiness_for(
            Potion("FairyPotion", "Fairy in a Bottle", True, True, False)
        )
        self.assertEqual(0.08, fairy["survival"]["potion_credit"])

    def test_committed_plan_keeps_universal_block_ahead_of_redundant_engine(self):
        game = GameStub([], [
            build_card("Deadly Poison"),
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Poisoned Stab", CardType.ATTACK, damage=6),
            strike(),
            defend(),
        ])
        game.in_combat = False
        game.act = 2
        game.floor = 20
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        profile = agent._deck_profile()

        block = build_card("Backflip", block=5)
        redundant = build_card("Bouncing Flask")

        self.assertGreater(
            agent._deck_plan_adjustment(block, profile),
            agent._deck_plan_adjustment(redundant, profile),
        )

    def test_card_rewards_prefer_closure_over_redundant_resources(self):
        defect_deck = [
            *(build_card(
                "Strike_B", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            *(build_card(
                "Defend_B", CardType.SKILL, block=5,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            build_card("Zap", CardType.SKILL, cost=1,
                       rarity=CardRarity.BASIC),
            build_card("Dualcast", CardType.SKILL, cost=1,
                       rarity=CardRarity.BASIC),
            build_card(
                "Compile Driver", CardType.ATTACK, cost=1, damage=7,
                rarity=CardRarity.COMMON,
            ),
            build_card(
                "Coolheaded", CardType.SKILL, cost=1,
                rarity=CardRarity.COMMON,
            ),
        ]
        defect_game = GameStub([], defect_deck)
        defect_game.in_combat = False
        defect_game.act = 2
        defect_game.floor = 20
        defect_agent = SimpleAgent(PlayerClass.DEFECT)
        defect_agent.game = defect_game
        compile_driver = build_card(
            "Compile Driver", CardType.ATTACK, cost=1, damage=7,
            rarity=CardRarity.COMMON,
        )
        glacier = build_card(
            "Glacier", CardType.SKILL, cost=2, block=7,
            rarity=CardRarity.UNCOMMON,
        )

        self.assertGreater(
            defect_agent._card_reward_score(glacier),
            defect_agent._card_reward_score(compile_driver),
        )

        silent_deck = [
            *(build_card(
                "Strike_G", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            *(build_card(
                "Defend_G", CardType.SKILL, block=5,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            build_card("Noxious Fumes", CardType.POWER, cost=1),
            build_card("Backflip", CardType.SKILL, cost=1, block=5),
            build_card("Backflip", CardType.SKILL, cost=1, block=5),
        ]
        silent_game = GameStub([], silent_deck)
        silent_game.in_combat = False
        silent_game.act = 2
        silent_game.floor = 20
        silent_agent = SimpleAgent(PlayerClass.THE_SILENT)
        silent_agent.game = silent_game
        third_backflip = build_card(
            "Backflip", CardType.SKILL, cost=1, block=5,
            rarity=CardRarity.COMMON,
        )
        deadly_poison = build_card(
            "Deadly Poison", CardType.SKILL, cost=1,
            rarity=CardRarity.COMMON,
        )
        catalyst = build_card(
            "Catalyst", CardType.SKILL, cost=1,
            rarity=CardRarity.UNCOMMON,
        )

        self.assertGreater(
            silent_agent._card_reward_score(deadly_poison),
            silent_agent._card_reward_score(third_backflip),
        )
        self.assertGreater(
            silent_agent._card_reward_score(catalyst),
            silent_agent._card_reward_score(third_backflip),
        )
        self.assertLess(
            silent_agent._card_reward_score(third_backflip),
            silent_agent._permanent_card_pick_hurdle(),
        )

        ironclad_game = GameStub([], [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(4)),
            build_card("Bash", CardType.ATTACK, cost=2, damage=8,
                       rarity=CardRarity.BASIC),
        ])
        ironclad_game.in_combat = False
        ironclad_game.act = 2
        ironclad_game.floor = 20
        ironclad_agent = SimpleAgent(PlayerClass.IRONCLAD)
        ironclad_agent.game = ironclad_game
        impervious = build_card(
            "Impervious", CardType.SKILL, cost=2, block=30,
            rarity=CardRarity.RARE,
        )

        self.assertGreater(
            ironclad_agent._card_reward_score(impervious),
            ironclad_agent._permanent_card_pick_hurdle(),
        )

    def test_reward_dependencies_reject_unsupported_package_payoffs(self):
        perfected = build_card(
            "Perfected Strike", CardType.ATTACK, cost=2, damage=6
        )
        ironclad_game = GameStub([], [
            build_card("Bash", CardType.ATTACK, cost=2, damage=8),
            build_card("Armaments", CardType.SKILL, block=5),
            *(defend() for _ in range(4)),
        ])
        ironclad_game.in_combat = False
        ironclad_game.act = 2
        ironclad_game.floor = 29
        ironclad_agent = SimpleAgent(PlayerClass.IRONCLAD)
        ironclad_agent.game = ironclad_game

        perfected_score = ironclad_agent._card_reward_score(perfected)
        perfected_contract = ironclad_agent._card_reward_score_contracts[
            id(perfected)
        ]

        self.assertLess(
            perfected_score,
            ironclad_agent._permanent_card_pick_hurdle(),
        )
        self.assertEqual(
            -18.0,
            perfected_contract["score_inputs"][
                "structural_dependency_adjustment"
            ],
        )

        defect_deck = [
            *(build_card(
                "Strike_B", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            *(build_card(
                "Defend_B", CardType.SKILL, block=5,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            build_card(
                "Zap", CardType.SKILL, cost=1,
                rarity=CardRarity.BASIC,
            ),
            build_card(
                "Dualcast", CardType.SKILL, cost=1,
                rarity=CardRarity.BASIC,
            ),
            build_card("Coolheaded", CardType.SKILL, cost=1),
        ]
        defect_game = GameStub([], defect_deck)
        defect_game.in_combat = False
        defect_game.act = 1
        defect_game.floor = 3
        defect_agent = SimpleAgent(PlayerClass.DEFECT)
        defect_agent.game = defect_game
        capacitor = build_card(
            "Capacitor", CardType.POWER, cost=1,
            rarity=CardRarity.UNCOMMON,
        )
        seek = build_card(
            "Seek", CardType.SKILL, cost=0,
            rarity=CardRarity.RARE,
        )

        capacitor_score = defect_agent._card_reward_score(capacitor)
        seek_score = defect_agent._card_reward_score(seek)
        capacitor_contract = defect_agent._card_reward_score_contracts[
            id(capacitor)
        ]

        self.assertGreater(seek_score, capacitor_score)
        self.assertEqual(
            -18.0,
            capacitor_contract["score_inputs"][
                "structural_dependency_adjustment"
            ],
        )

        defect_game.deck.append(capacitor)
        storm = build_card(
            "Storm", CardType.POWER, cost=1,
            rarity=CardRarity.UNCOMMON,
        )
        storm_score = defect_agent._card_reward_score(storm)
        storm_contract = defect_agent._card_reward_score_contracts[id(storm)]

        self.assertLess(
            storm_score, defect_agent._permanent_card_pick_hurdle()
        )
        self.assertEqual(
            -24.0,
            storm_contract["score_inputs"][
                "structural_dependency_adjustment"
            ],
        )

    def test_biased_cognition_values_artifact_pairing_both_directions(self):
        biased = build_card("Biased Cognition", CardType.POWER, rarity=CardRarity.RARE)
        core_surge = build_card("Core Surge", CardType.ATTACK, damage=11, rarity=CardRarity.RARE)

        plain_game = GameStub([], [strike(), defend()])
        plain_game.in_combat = False
        paired_game = GameStub([], [core_surge, strike(), defend()])
        paired_game.in_combat = False
        plain_agent = SimpleAgent(PlayerClass.DEFECT)
        plain_agent.game = plain_game
        paired_agent = SimpleAgent(PlayerClass.DEFECT)
        paired_agent.game = paired_game

        self.assertGreater(
            paired_agent._card_reward_score(biased),
            plain_agent._card_reward_score(biased) + 15,
        )

        paired_game.deck = [biased, strike(), defend()]
        self.assertGreater(paired_agent._card_reward_score(core_surge), 20)

    def test_upgraded_fission_and_seek_are_engine_cards_not_static_filler(self):
        deck = [
            build_card("Ball Lightning", CardType.ATTACK, damage=7),
            build_card("Glacier", block=7),
            build_card("Defragment", CardType.POWER),
            strike(),
            defend(),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 24
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        base_fission = build_card("Fission", rarity=CardRarity.RARE)
        upgraded_fission = build_card("Fission", rarity=CardRarity.RARE, upgrades=1)
        seek = build_card("Seek", rarity=CardRarity.RARE)

        self.assertGreater(
            agent._card_reward_score(upgraded_fission),
            agent._card_reward_score(base_fission) + 15,
        )
        self.assertGreater(agent._card_reward_score(upgraded_fission), 0)
        self.assertGreater(agent._card_reward_score(seek), 0)

    def test_soft_saturation_allows_exceptional_second_copy(self):
        owned = build_card("Wraith Form v2", CardType.POWER, rarity=CardRarity.RARE)
        offered = build_card("Wraith Form v2", CardType.POWER, rarity=CardRarity.RARE)
        game = GameStub([], [owned, strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 40
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertTrue(agent._within_copy_limit(offered))
        self.assertGreater(agent._card_reward_score(offered), 0)

    def test_deck_plan_identifies_completed_archetype_for_every_class(self):
        scenarios = [
            (
                PlayerClass.THE_SILENT,
                [build_card("Deadly Poison"), build_card("Catalyst")],
                "poison",
            ),
            (
                PlayerClass.IRONCLAD,
                [build_card("True Grit", block=7), build_card("Feel No Pain", CardType.POWER)],
                "exhaust",
            ),
            (
                PlayerClass.DEFECT,
                [build_card("Cold Snap", CardType.ATTACK, damage=6), build_card("Defragment", CardType.POWER)],
                "frost",
            ),
        ]
        for player_class, deck, expected in scenarios:
            with self.subTest(player_class=player_class):
                game = GameStub([], [*deck, strike(), defend()])
                game.in_combat = False
                game.act = 2
                game.floor = 20
                agent = SimpleAgent(player_class)
                agent.game = game

                plan = agent._deck_plan_summary()

                self.assertEqual(expected, plan["primary"])
                self.assertGreater(plan["confidence"], 0)

    def test_mature_exhaust_plan_can_overturn_low_static_dark_embrace_prior(self):
        dark_embrace = build_card("Dark Embrace", CardType.POWER)
        game = GameStub([], [
            build_card("True Grit", block=7),
            build_card("Feel No Pain", CardType.POWER),
            strike(),
            defend(),
        ])
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.screen = CardRewardScreen([dark_embrace], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(dark_embrace, action.card)
        self.assertEqual("exhaust", agent.last_noncombat_decision["deck_plan"]["primary"])

    def test_payoff_without_enabler_is_not_mistaken_for_a_build(self):
        catalyst = build_card("Catalyst")
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.screen = CardRewardScreen([catalyst], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CancelAction)
        self.assertEqual("balanced", agent.last_noncombat_decision["deck_plan"]["primary"])

    def test_flex_alone_does_not_make_limit_break_a_reliable_boss_pick(self):
        deck = [
            strike(),
            defend(),
            build_card("Flex", cost=0, rarity=CardRarity.COMMON),
            build_card("Heavy Blade", CardType.ATTACK, cost=2, damage=14),
            build_card(
                "Sword Boomerang",
                CardType.ATTACK,
                damage=3,
                rarity=CardRarity.COMMON,
            ),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 16
        game.room_type = "MonsterRoomBoss"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        limit_break = build_card(
            "Limit Break", rarity=CardRarity.RARE
        )
        impervious = build_card(
            "Impervious", cost=2, block=30, rarity=CardRarity.RARE
        )

        plan = agent._deck_plan_summary()

        self.assertEqual("balanced", plan["primary"])
        self.assertEqual(1, plan["archetypes"]["strength"]["sources"])
        self.assertLess(
            plan["archetypes"]["strength"]["source_reliability"],
            0.5,
        )
        self.assertGreater(
            agent._card_reward_score(impervious),
            agent._card_reward_score(limit_break) + 20,
        )

    def test_reliable_strength_engine_still_values_first_limit_break(self):
        deck = [
            strike(),
            defend(),
            build_card("Inflame", CardType.POWER),
            build_card("Spot Weakness"),
            build_card("Heavy Blade", CardType.ATTACK, cost=2, damage=14),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 24
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        limit_break = build_card(
            "Limit Break", rarity=CardRarity.RARE
        )

        self.assertEqual("strength", agent._deck_plan_summary()["primary"])
        self.assertGreater(agent._card_reward_score(limit_break), 20)

    def test_heavy_blade_deck_takes_first_reliable_strength_source(self):
        """Replay Act 2 floor 28: payoff should recruit its missing source."""

        deck = (
            [
                build_card(
                    "Strike_R", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ]
            + [
                build_card(
                    "Defend_R", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ]
            + [
                build_card(
                    "Bash", CardType.ATTACK, cost=2, damage=10,
                    rarity=CardRarity.BASIC, upgrades=1,
                ),
                build_card(
                    "Wild Strike", CardType.ATTACK, damage=12,
                    rarity=CardRarity.COMMON,
                ),
                build_card(
                    "Heavy Blade", CardType.ATTACK, cost=2, damage=14,
                    rarity=CardRarity.COMMON,
                ),
                build_card(
                    "Anger", CardType.ATTACK, cost=0, damage=6,
                    rarity=CardRarity.COMMON,
                ),
                build_card("Disarm", CardType.SKILL),
                build_card(
                    "Flame Barrier", CardType.SKILL, cost=2, block=12,
                ),
                build_card(
                    "Immolate", CardType.ATTACK, cost=2, damage=21,
                    rarity=CardRarity.RARE,
                ),
                build_card(
                    "Offering", CardType.SKILL, cost=0,
                    rarity=CardRarity.RARE, upgrades=1,
                ),
                build_card(
                    "Pommel Strike", CardType.ATTACK, damage=9,
                    rarity=CardRarity.COMMON,
                ),
                build_card(
                    "Impervious", CardType.SKILL, cost=2, block=30,
                    rarity=CardRarity.RARE,
                ),
                build_card("Rage", CardType.SKILL, cost=0),
            ]
        )
        pommel_strike = build_card(
            "Pommel Strike", CardType.ATTACK, damage=9,
            rarity=CardRarity.COMMON,
        )
        power_through = build_card(
            "Power Through", CardType.SKILL, block=15,
        )
        spot_weakness = build_card("Spot Weakness", CardType.SKILL)
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 28
        game.room_type = "MonsterRoomElite"
        game.act_boss = "Collector"
        game.current_hp = game.player.current_hp = 79
        game.max_hp = game.player.max_hp = 90
        game.gold = 93
        game.relics = [
            Relic(relic_id, relic_id)
            for relic_id in (
                "Burning Blood", "NeowsBlessing", "Ancient Tea Set",
                "Pear", "MealTicket", "Blue Candle", "Fusion Hammer",
                "Thread and Needle", "Self Forming Clay",
            )
        ]
        game.screen = CardRewardScreen(
            [pommel_strike, power_through, spot_weakness],
            can_bowl=False,
            can_skip=True,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        strength = agent._deck_profile()["archetypes"]["strength"]
        self.assertEqual(0.0, strength["source_reliability"])
        self.assertEqual(1, strength["payoffs"])
        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(spot_weakness, action.card)
        candidates = {
            row["label"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertGreater(
            candidates["Spot Weakness"]["score"],
            agent.last_noncombat_decision["marginal_hurdle"],
        )
        self.assertEqual(
            9.0,
            candidates["Spot Weakness"]["score_inputs"][
                "class_and_relic_context"
            ],
        )

    def test_active_build_still_prioritizes_missing_draw_over_redundant_source(self):
        game = GameStub([], [
            build_card("Deadly Poison"),
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Poisoned Stab", CardType.ATTACK, damage=6),
            strike(),
            defend(),
        ])
        game.in_combat = False
        game.act = 2
        game.floor = 20
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        profile = agent._deck_profile()
        backflip = build_card("Backflip", block=5)
        redundant_poison = build_card("Bouncing Flask")

        self.assertGreater(
            agent._deck_plan_adjustment(backflip, profile),
            agent._deck_plan_adjustment(redundant_poison, profile),
        )

    def test_relic_score_changes_with_matching_deck_engine(self):
        shuriken = Relic("Shuriken", "Shuriken")
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        shiv_game = GameStub([], [
            build_card("Blade Dance"),
            build_card("Cloak And Dagger", block=6),
            strike(),
            defend(),
        ])
        shiv_game.in_combat = False
        agent.game = shiv_game
        shiv_score = agent._shop_relic_score(shuriken)

        poison_game = GameStub([], [
            build_card("Deadly Poison"),
            build_card("Noxious Fumes", CardType.POWER),
            strike(),
            defend(),
        ])
        poison_game.in_combat = False
        agent.game = poison_game
        poison_score = agent._shop_relic_score(shuriken)

        self.assertGreater(shiv_score, poison_score + 8)

    def test_brimstone_shop_score_prices_known_boss_multihit_risk(self):
        game = GameStub([], [
            build_card("Pummel", CardType.ATTACK, damage=2),
            build_card(
                "Whirlwind", CardType.ATTACK, cost=-1, damage=5
            ),
            build_card("Reaper", CardType.ATTACK, cost=2, damage=4),
            strike(),
            defend(),
            defend(),
        ])
        game.in_combat = False
        game.act = 3
        game.floor = 41
        game.act_boss = "Time Eater"
        game.current_hp = 70
        game.max_hp = 85
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        brimstone = Relic("Brimstone", "Brimstone", price=147)

        score = agent._shop_relic_score(brimstone)
        context = agent._shop_relic_score_contexts[id(brimstone)]

        self.assertTrue(context["evaluated"])
        self.assertEqual("symmetric_strength_growth", context["effect"])
        self.assertEqual("timeeater", context["boss_id"])
        self.assertEqual(3.0, context["enemy_hit_exposure"])
        self.assertGreater(context["downside_score"], 0)
        self.assertEqual(context["risk_adjusted_score"], score)
        self.assertLess(score, context["positive_score"])

        game.gold = 358
        game.screen = ShopScreen([], [brimstone], [], True, 125)
        game.screen.protocol_purge_choice_index = 0
        action = agent.choose_shop_action()
        self.assertIsInstance(action, BuyRelicAction)
        selected = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["choice_id"]
            == agent.last_noncombat_decision["chosen_id"]
        )
        self.assertEqual(context, selected["strategic_risk"])
        self.assertEqual(
            context["downside_score"],
            selected["score_inputs"]["relic_downside_risk"],
        )

    def test_owned_relic_feeds_back_into_future_card_plan(self):
        deck = [
            build_card("Deadly Poison"),
            build_card("Noxious Fumes", CardType.POWER),
            strike(),
            defend(),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 12
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        self.assertEqual("balanced", agent._deck_plan_summary()["primary"])

        game.relics = [Relic("The Specimen", "The Specimen")]
        plan = agent._deck_plan_summary()

        self.assertEqual("poison", plan["primary"])
        self.assertGreater(plan["archetypes"]["poison"]["relic_support"], 0)

    def test_prepared_is_only_full_value_when_discard_payoffs_exist(self):
        prepared = build_card("Prepared", cost=0, rarity=CardRarity.COMMON)
        plain_game = GameStub([], [strike(), defend(), build_card("Survivor", block=8)])
        plain_game.in_combat = False
        plain_game.act = 2
        plain_game.floor = 20
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = plain_game
        plain_score = agent._card_reward_score(prepared)

        discard_game = GameStub([], [
            build_card("Survivor", block=8),
            build_card("Eviscerate", CardType.ATTACK, damage=7),
            build_card("Tactician"),
            strike(),
            defend(),
        ])
        discard_game.in_combat = False
        discard_game.act = 2
        discard_game.floor = 20
        agent.game = discard_game
        payoff_score = agent._card_reward_score(prepared)

        self.assertLess(plain_score, 0)
        self.assertGreater(payoff_score, plain_score + 10)

    def test_unupgraded_prepared_does_not_fill_act1_draw_shortfall_by_itself(self):
        prepared = build_card("Prepared", cost=0, rarity=CardRarity.COMMON)
        game = GameStub([], [
            strike(), strike(), defend(), defend(), build_card("Survivor", block=8),
            build_card("Poisoned Stab", CardType.ATTACK, damage=6),
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Predator", CardType.ATTACK, cost=2, damage=15),
            build_card("Dagger Spray", CardType.ATTACK, damage=4),
            build_card("Escape Plan", block=3),
            build_card("Footwork", CardType.POWER),
        ])
        game.in_combat = False
        game.act = 1
        game.floor = 12
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertLess(agent._card_reward_score(prepared), 0)

    def test_serialized_output_can_rescue_unknown_frontload_card(self):
        strong_attack = build_card("Unknown Frontload", CardType.ATTACK, damage=18)
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 5
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertGreater(agent._card_reward_score(strong_attack), 0)

    def test_seek_selects_a_high_value_card_instead_of_the_worst_card(self):
        echo = Card("Echo Form", "Echo Form", CardType.POWER, CardRarity.RARE, uuid="echo")
        weak = Card("Strike_B", "Strike", CardType.ATTACK, CardRarity.BASIC, uuid="strike-b")
        agent = SimpleAgent(PlayerClass.DEFECT)

        selected = agent.priorities.get_cards_for_action("SeekAction", [weak, echo], 1)

        self.assertEqual([echo], selected)

    def test_every_supported_character_avoids_poison_reserved_target(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                doomed = make_monster("doomed", 5, 30, poison=6, index=0)
                active = make_monster("active", 20, 5, index=1)
                game = GameStub([doomed, active], [strike()])
                agent = SimpleAgent(player_class)
                agent.game = game
                action = agent.get_play_card_action()
                self.assertIsInstance(action, PlayCardAction)
                self.assertIs(active, action.target_monster)

    def test_every_supported_character_ends_when_all_enemies_are_reserved_dead(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                doomed = make_monster("doomed", 5, 30, poison=6)
                game = GameStub([doomed], [strike()])
                agent = SimpleAgent(player_class)
                agent.game = game
                self.assertIsInstance(agent.get_play_card_action(), EndTurnAction)

    def test_end_turn_is_preferred_to_block_with_no_incoming_damage(self):
        game = GameStub([make_monster("idle", 30, 0)], [defend()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        self.assertIsInstance(agent.get_play_card_action(), EndTurnAction)

    def test_unused_energy_exhausts_slimed_instead_of_ending_turn(self):
        game = GameStub([make_monster("idle", 30, 0)], [slimed(), defend()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.get_play_card_action()
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Slimed", action.card.card_id)
        self.assertEqual("exhaust_dead_status", agent.combat_planner.last_decision["reason"])

    def test_status_cleanup_does_not_play_into_time_eater_penalty(self):
        time_eater = make_monster("TimeEater", 456, 0)
        time_eater.powers = [Power("Time Warp", "Time Warp", 0)]
        game = GameStub([time_eater], [slimed()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        self.assertIsInstance(agent.get_play_card_action(), EndTurnAction)

    def test_existing_block_prevents_overblocking(self):
        game = GameStub([make_monster("attacker", 30, 6)], [defend()])
        game.player.block = 10
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        self.assertIsInstance(agent.get_play_card_action(), EndTurnAction)

    def test_small_damage_budget_uses_attack_instead_of_unneeded_block(self):
        game = GameStub([make_monster("attacker", 30, 5)], [defend(), strike()])
        game.player.energy = 1
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.get_play_card_action()
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Strike_G", action.card.card_id)

    def test_unused_energy_prevents_avoidable_hp_loss_before_ending_turn(self):
        game = GameStub([make_monster("attacker", 30, 6)], [defend()])
        game.player.block = 5
        game.player.energy = 1
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.get_play_card_action()
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Defend_G", action.card.card_id)
        self.assertEqual("prevent_avoidable_hp_loss", agent.combat_planner.last_decision["reason"])

    def test_heart_beat_uses_block_when_net_damage_still_falls(self):
        heart = make_monster("CorruptHeart", 699, 72)
        heart.powers = [Power("BeatOfDeath", "BeatOfDeath", 1)]
        block = build_card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8,
            rarity=CardRarity.COMMON, upgrades=1,
        )
        game = GameStub([heart], [block, strike()])
        game.draw_pile = [strike()]
        game.discard_pile = []
        game.exhaust_pile = []
        game.limbo = []
        game.player.current_hp = game.current_hp = 54
        game.player.energy = 5
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.get_play_card_action()

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block)
        decision = agent.combat_planner.last_decision
        self.assertEqual("prevent_avoidable_hp_loss", decision["reason"])
        self.assertEqual(7, decision["card_post_reactive_block"])
        self.assertEqual(7, decision["net_loss_reduction"])

    def test_lethal_incoming_prioritizes_mitigation(self):
        game = GameStub([make_monster("attacker", 30, 30)], [defend(8), strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.player.energy = 1
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.get_play_card_action()
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Defend_G", action.card.card_id)

    def test_near_reserve_nonelite_fight_minimizes_tier_one_loss(self):
        enemy = make_monster("JawWorm", 80, 24)
        immolate = build_card(
            "Immolate", CardType.ATTACK, cost=2, damage=21,
            rarity=CardRarity.RARE,
        )
        block = defend(8)
        game = GameStub([enemy], [immolate, block])
        game.act = 3
        game.floor = 42
        game.player.current_hp = game.current_hp = 25
        game.player.max_hp = game.max_hp = 75
        game.player.energy = 2
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.get_play_card_action()

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, block)
        search = agent.combat_planner.last_decision["search"]
        self.assertEqual(1, search["tier"])
        self.assertEqual(16, search["actual_loss"])
        self.assertGreaterEqual(
            25 - search["actual_loss"],
            agent.combat_planner._reactive_safety_reserve(game, 25),
        )

    def test_after_image_does_not_make_attack_execute_before_real_block(self):
        game = GameStub([make_monster("attacker", 40, 18)], [defend(8), strike()])
        game.player.powers = [Power("AfterImagePower", "After Image", 1)]
        game.player.energy = 2
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.get_play_card_action()
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Defend_G", action.card.card_id)

    def test_focus_target_persists_across_authoritative_state_objects(self):
        first = make_monster("scaler", 30, 4, index=0)
        second = make_monster("scaler", 40, 4, index=1)
        game = GameStub([first, second], [strike()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        first_action = agent.get_play_card_action()
        self.assertEqual(0, first_action.target_monster.monster_index)

        refreshed_first = make_monster("scaler", 24, 4, index=0)
        refreshed_second = make_monster("scaler", 40, 8, index=1)
        refreshed_game = GameStub([refreshed_first, refreshed_second], [strike()])
        refreshed_game.floor = game.floor
        agent.game = refreshed_game
        second_action = agent.get_play_card_action()
        self.assertEqual(0, second_action.target_monster.monster_index)

    def test_all_classes_avoid_sozu_and_runic_dome_when_safe_relic_exists(self):
        relics = [Relic("Sozu", "Sozu"), Relic("Runic Dome", "Runic Dome"), Relic("Tiny House", "Tiny House")]
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                game = GameStub([], [])
                agent = SimpleAgent(player_class)
                agent.game = game
                self.assertEqual("Tiny House", agent.choose_boss_relic(relics).relic_id)

    def test_boss_relic_skip_is_a_typed_visible_candidate(self):
        game = GameStub([], [strike(), defend()])
        game.cancel_available = True
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        relics = [Relic("Tiny House", "Tiny House")]

        with patch.object(agent, "_boss_relic_score", return_value=-1.0):
            chosen = agent.choose_boss_relic(relics)

        self.assertIs(relics[0], chosen)
        decision = agent.last_noncombat_decision
        self.assertEqual("relic:Tiny House:0", decision["chosen_id"])
        self.assertEqual(
            {"relic:Tiny House:0", "action:return"},
            {row["choice_id"] for row in decision["candidates"]},
        )
        skip = next(
            row for row in decision["candidates"]
            if row["choice_id"] == "action:return"
        )
        self.assertEqual("return", skip["action"])
        self.assertEqual("return", skip["consequences"]["operation"])
        self.assertFalse(skip["selection_eligible"])
        self.assertEqual("boss_relic_return_is_noop", skip["veto_reason"])

    def test_snecko_eye_is_rejected_for_low_cost_low_draw_deck(self):
        game = GameStub([], [
            *(strike() for _ in range(4)),
            *(defend() for _ in range(5)),
        ])
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        snecko = Relic("Snecko Eye", "Snecko Eye")
        tiny = Relic("Tiny House", "Tiny House")

        profile = agent._snecko_eye_profile()
        self.assertFalse(profile["recommended"])
        self.assertLess(
            agent._boss_relic_score(snecko),
            agent._boss_relic_score(tiny),
        )
        self.assertEqual(
            "Tiny House",
            agent.choose_boss_relic([snecko, tiny]).relic_id,
        )

    def test_runic_pyramid_loses_to_black_star_when_retained_defends_exceed_control(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                survivor = Card(
                    "Survivor", "Survivor", CardType.SKILL, CardRarity.BASIC,
                    cost=1, uuid="survivor", block=8,
                )
                deck = [
                    *(strike() for _ in range(3)),
                    *(defend() for _ in range(5)),
                    survivor,
                    Card("Deadly Poison", "Deadly Poison", CardType.SKILL, CardRarity.COMMON, cost=1, uuid="poison"),
                ]
                game = GameStub([], deck)
                agent = SimpleAgent(player_class)
                agent.game = game

                chosen = agent.choose_boss_relic([
                    Relic("Runic Pyramid", "Runic Pyramid"),
                    Relic("Black Star", "Black Star"),
                ])

                self.assertEqual("Black Star", chosen.relic_id)

    def test_pyramid_discard_removes_excess_basic_defend_before_damage(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                attack = strike()
                weak_defend = defend()
                deck = [attack, *(defend() for _ in range(5))]
                game = GameStub([make_monster("idle", 30, 0)], deck)
                game.relics = [Relic("Runic Pyramid", "Runic Pyramid")]
                game.screen_type = ScreenType.HAND_SELECT
                game.screen = HandSelectScreen([attack, weak_defend], [], 1, False)
                game.current_action = "DiscardAction"
                game.choice_available = True
                agent = SimpleAgent(player_class)
                agent.game = game

                action = agent.handle_screen()

                self.assertIsInstance(action, CardSelectAction)
                self.assertEqual([weak_defend], action.cards)

    def test_discard_overlay_preserves_future_card_in_combat_plan(self):
        planned_strike = strike()
        planned_strike.uuid = "planned-strike"
        spare_defend = defend()
        spare_defend.uuid = "spare-defend"
        game = GameStub([make_monster("JawWorm", 34, 7)], [
            planned_strike, spare_defend,
        ])
        game.player.block = 8
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(
            [planned_strike, spare_defend], [], 1, False
        )
        game.current_action = "DiscardAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        agent._combat_epoch = 1
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": "Strike_G",
                "card_uuid": planned_strike.uuid,
            }],
            "_combat_context": agent._combat_plan_context(),
        }

        action = agent.handle_screen()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([spare_defend], action.cards)
        candidates = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        preserved = candidates[f"grid:{planned_strike.uuid}"]
        self.assertFalse(preserved["selection_eligible"])
        self.assertEqual(
            "exact_combat_plan_card_preservation",
            preserved["veto_reason"],
        )
        self.assertEqual(
            1000.0,
            preserved["score_inputs"][
                "bound_plan_preservation_value"
            ],
        )
        self.assertLess(
            preserved["score"],
            candidates[f"grid:{spare_defend.uuid}"]["score"],
        )
        protection = agent.last_noncombat_decision["combat_plan_protection"]
        self.assertEqual(1, protection["schema_version"])
        self.assertEqual(
            [planned_strike.uuid],
            protection["protected_card_instance_ids"],
        )
        self.assertEqual(
            agent._combat_plan_context(),
            tuple(protection["source_combat_context"]),
        )

    def test_put_on_deck_preserves_future_card_in_combat_plan(self):
        planned_evolve = build_card(
            "Evolve", CardType.POWER, cost=1,
            rarity=CardRarity.UNCOMMON, upgrades=1,
        )
        planned_evolve.uuid = "planned-evolve"
        spare_defend = defend()
        spare_defend.uuid = "spare-defend"
        game = GameStub(
            [make_monster("SlimeBoss", 70, 0)],
            [planned_evolve, spare_defend],
        )
        game.room_type = "MonsterRoomBoss"
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(
            [planned_evolve, spare_defend], [], 1, False
        )
        game.current_action = "PutOnDeckAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent._combat_epoch = 1
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": planned_evolve.card_id,
                "card_uuid": planned_evolve.uuid,
            }],
            "_combat_context": agent._combat_plan_context(),
        }

        action = agent.handle_screen()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([spare_defend], action.cards)
        candidates = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        planned = candidates[f"grid:{planned_evolve.uuid}"]
        self.assertTrue(planned["selection_eligible"])
        self.assertIsNone(planned["veto_reason"])
        self.assertEqual(
            -1000.0,
            planned["score_inputs"]["bound_plan_disruption_penalty"],
        )
        self.assertLess(
            planned["score"],
            candidates[f"grid:{spare_defend.uuid}"]["score"],
        )

    def test_true_grit_does_not_exhaust_unique_scaling_core_to_preserve_plan(self):
        demon_form = build_card(
            "Demon Form", CardType.POWER, cost=3, rarity=CardRarity.RARE,
        )
        demon_form.uuid = "demon-form-core"
        flex = build_card("Flex", CardType.SKILL, cost=0)
        flex.uuid = "planned-flex"
        defend_card = defend()
        defend_card.uuid = "planned-defend"
        anger = build_card("Anger", CardType.ATTACK, cost=0, damage=6)
        anger.uuid = "planned-anger"
        cards = [demon_form, flex, defend_card, anger]
        game = GameStub([make_monster("SlimeBoss", 140, 0)], cards)
        game.room_type = "MonsterRoomBoss"
        game.turn = 3
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(cards, [], 1, False)
        game.current_action = "ExhaustAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent._combat_epoch = 7
        agent.combat_planner.last_decision = {
            "planned_sequence": [
                {"card_id": card.card_id, "card_uuid": card.uuid}
                for card in (flex, defend_card, anger)
            ],
            "_combat_context": agent._combat_plan_context(),
        }

        action = agent.handle_screen()

        self.assertIsInstance(action, CardSelectAction)
        self.assertNotIn(demon_form, action.cards)
        candidates = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        core = candidates[f"grid:{demon_form.uuid}"]
        self.assertTrue(core["selection_eligible"])
        self.assertEqual(
            40.0,
            core["score_inputs"]["irreversible_scaling_core_value"],
        )
        selected = next(
            row for row in candidates.values()
            if row["choice_id"] == f"grid:{action.cards[0].uuid}"
        )
        self.assertGreater(selected["score"], core["score"])
        self.assertIsNone(
            agent.last_noncombat_decision["combat_plan_protection"]
        )

    def test_demon_form_setup_beats_block_only_progress(self):
        demon_form = build_card(
            "Demon Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        bash = build_card(
            "Bash", CardType.ATTACK, cost=2, damage=10,
            rarity=CardRarity.BASIC,
        )
        strike_card = build_card(
            "Strike_R", CardType.ATTACK, cost=1, damage=6,
            rarity=CardRarity.BASIC,
        )
        guardian = make_monster("Spheric Guardian", 60, 0)
        guardian.block = 24
        guardian.intent = Intent.DEFEND
        guardian.move_hits = 0
        guardian.powers = [Power("Barricade", "Barricade", 1)]
        game = GameStub([guardian], [bash, strike_card, demon_form])
        game.act = 2
        game.floor = 18
        game.player.energy = 3

        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        action = agent.combat_planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, demon_form)
        self.assertEqual(
            "Demon Form",
            agent.combat_planner.last_decision["card_id"],
        )
        lifecycle = agent.combat_planner.last_decision["lifecycle"]
        self.assertGreater(lifecycle["expected_benefit"], 0)
        self.assertGreaterEqual(lifecycle["expected_trigger_count"], 2)

    def test_echo_form_setup_beats_temporary_block_progress(self):
        echo_form = build_card(
            "Echo Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        cold_snap = build_card(
            "Cold Snap", CardType.ATTACK, cost=1, damage=7,
            rarity=CardRarity.COMMON,
        )
        ball_lightning = build_card(
            "Ball Lightning", CardType.ATTACK, cost=1, damage=7,
            rarity=CardRarity.COMMON,
        )
        guardian = make_monster("Spheric Guardian", 60, 0)
        guardian.block = 24
        guardian.intent = Intent.DEFEND
        guardian.move_hits = 0
        guardian.powers = [Power("Barricade", "Barricade", 1)]
        game = GameStub(
            [guardian], [cold_snap, ball_lightning, echo_form]
        )
        game.act = 2
        game.floor = 18
        game.player.energy = 3

        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        action = agent.combat_planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, echo_form)
        lifecycle = agent.combat_planner.last_decision["lifecycle"]
        self.assertEqual("echoform", lifecycle["kind"])
        self.assertGreater(lifecycle["expected_benefit"], 0)
        self.assertGreaterEqual(lifecycle["expected_trigger_count"], 2)

    def test_seek_is_used_before_spending_energy_needed_by_its_target(self):
        seek = build_card(
            "Seek", CardType.SKILL, cost=0, rarity=CardRarity.RARE,
        )
        seek.uuid = "seek-before-spend"
        strike_card = build_card(
            "Strike_B", CardType.ATTACK, cost=1, damage=6,
            rarity=CardRarity.BASIC,
        )
        strike_card.uuid = "seek-existing-strike"
        sunder = build_card(
            "Sunder", CardType.ATTACK, cost=3, damage=32,
            rarity=CardRarity.UNCOMMON,
        )
        sunder.uuid = "seek-draw-sunder"
        enemy = make_monster("Cultist", 80, 0)
        enemy.intent = Intent.BUFF
        game = GameStub([enemy], [strike_card, seek])
        game.player.energy = 3
        game.draw_pile = [sunder]
        game.discard_pile = []
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.combat_planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, seek)
        self.assertEqual(
            "Seek",
            agent.combat_planner.last_decision["planned_sequence"][0][
                "card_id"
            ],
        )

    def test_bullet_time_waits_for_draw_then_unlocks_expensive_hand(self):
        acrobatics = build_card(
            "Acrobatics", CardType.SKILL, cost=1,
            rarity=CardRarity.COMMON,
        )
        acrobatics.uuid = "bullet-acrobatics"
        bullet_time = build_card(
            "Bullet Time", CardType.SKILL, cost=3,
            rarity=CardRarity.RARE,
        )
        bullet_time.uuid = "bullet-time"
        dash = build_card(
            "Dash", CardType.ATTACK, cost=2, damage=10, block=10,
        )
        dash.uuid = "bullet-dash"
        predator = build_card(
            "Predator", CardType.ATTACK, cost=2, damage=15,
        )
        predator.uuid = "bullet-predator"
        enemy = make_monster("Snecko", 120, 20)
        game = GameStub(
            [enemy], [acrobatics, bullet_time, dash, predator]
        )
        game.player.energy = 4
        game.draw_pile = [defend()]
        game.discard_pile = []
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.combat_planner.choose_card_action(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, acrobatics)
        sequence = [
            row["card_id"]
            for row in agent.combat_planner.last_decision[
                "planned_sequence"
            ]
        ]
        self.assertIn("Bullet Time", sequence)
        bullet_index = sequence.index("Bullet Time")
        self.assertLess(bullet_index, sequence.index("Dash"))
        self.assertLess(bullet_index, sequence.index("Predator"))

    def test_retain_selection_keeps_high_value_power(self):
        power = Card(
            "Noxious Fumes", "Noxious Fumes", CardType.POWER,
            CardRarity.UNCOMMON, cost=1, uuid="fumes",
        )
        weak_defend = defend()
        game = GameStub([make_monster("idle", 30, 0)], [power, weak_defend])
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen([weak_defend, power], [], 1, False)
        game.current_action = "RetainCardsAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([power], action.cards)
        selected_candidate = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_index"] == 1
        )
        self.assertEqual(
            "hand_select_card",
            selected_candidate["consequences"]["operation"],
        )
        self.assertEqual(
            "fumes",
            selected_candidate["consequences"]["selected_card"][
                "card_instance_id"
            ],
        )
        self.assertEqual(
            "RetainCardsAction",
            selected_candidate["consequences"]["future_costs"][0][
                "current_action"
            ],
        )

    def test_armaments_does_not_upgrade_innate_only_power_mid_combat(self):
        brutality = build_card(
            "Brutality", CardType.POWER, cost=0,
            rarity=CardRarity.RARE,
        )
        brutality.uuid = "brutality-noop"
        bash = build_card(
            "Bash", CardType.ATTACK, cost=2, damage=8,
            rarity=CardRarity.BASIC,
        )
        bash.uuid = "bash-upgrade"
        game = GameStub(
            [make_monster("JawWorm", 40, 0)], [brutality, bash]
        )
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen([brutality, bash], [], 1, False)
        game.current_action = "ArmamentsAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([bash], action.cards)
        candidates = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        noop = candidates["grid:brutality-noop"]
        self.assertEqual(
            -100.0,
            noop["score_inputs"]["armaments_innate_only_noop_penalty"],
        )
        self.assertLess(noop["score"], candidates["grid:bash-upgrade"]["score"])

    def test_armaments_upgrade_prioritizes_handwide_value_in_raw_deck(self):
        armaments = build_card(
            "Armaments", CardType.SKILL, cost=1, block=5,
        )
        bash = build_card(
            "Bash", CardType.ATTACK, cost=2, damage=8,
            rarity=CardRarity.BASIC,
        )
        shrug = build_card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8,
        )
        pommel = build_card(
            "Pommel Strike", CardType.ATTACK, cost=1, damage=9,
        )
        deck = [
            armaments, bash, shrug, pommel,
            *(strike() for _ in range(5)),
            *(defend() for _ in range(4)),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        parts = agent._upgrade_score_parts(armaments)

        self.assertGreater(parts["armaments_handwide_upgrade_value"], 5.0)
        self.assertGreater(
            agent._upgrade_score(armaments), agent._upgrade_score(bash)
        )
        self.assertGreater(
            agent._upgrade_score(armaments), agent._upgrade_score(shrug)
        )
        self.assertGreater(
            agent._upgrade_score(armaments), agent._upgrade_score(pommel)
        )

        for deck_card in deck:
            if deck_card is not armaments and deck_card is not bash:
                deck_card.upgrades = 1
        mature_parts = agent._upgrade_score_parts(armaments)
        self.assertLess(mature_parts["armaments_handwide_upgrade_value"], 1.0)
        self.assertLess(
            agent._upgrade_score(armaments), agent._upgrade_score(bash)
        )

    def test_optional_retain_skips_negative_value_status_card(self):
        status = slimed()
        status.uuid = "retain-status"
        game = GameStub([make_monster("idle", 30, 0)], [status])
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen([status], [], 1, True)
        game.current_action = "RetainCardsAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ProceedAction)
        decision = agent.last_noncombat_decision
        self.assertEqual(["action:proceed"], decision["chosen_ids"])
        rows = {
            row["choice_id"]: row for row in decision["candidates"]
        }
        self.assertLess(rows["grid:retain-status"]["score"], 0.0)
        self.assertEqual(0.0, rows["action:proceed"]["score"])

    def test_gambling_selection_stops_after_weak_subset(self):
        weak_defend = defend()
        weak_defend.uuid = "gamble-defend"
        weak_strike = strike()
        weak_strike.uuid = "gamble-strike"
        battle_trance = build_card(
            "Battle Trance", CardType.SKILL, cost=0,
        )
        battle_trance.uuid = "gamble-battle-trance"
        battle_trance.magic_number = 3
        shrug = build_card(
            "Shrug It Off", CardType.SKILL, cost=1, block=8,
            rarity=CardRarity.COMMON,
        )
        shrug.uuid = "gamble-shrug"
        cards = [weak_defend, weak_strike, battle_trance, shrug]
        game = GameStub([make_monster("SnakePlant", 77, 21)], cards)
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(cards, [], len(cards), True)
        game.current_action = "GamblingChipAction"
        game.choice_available = True
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        first = agent.handle_screen()

        self.assertIsInstance(first, CardSelectAction)
        selected_ids = {card.uuid for card in first.cards}
        self.assertTrue(selected_ids)
        self.assertNotIn(battle_trance.uuid, selected_ids)
        self.assertNotIn(shrug.uuid, selected_ids)
        first_rows = agent.last_noncombat_decision["candidates"]
        proceed_score = next(
            row["score"] for row in first_rows
            if row["choice_id"] == "action:proceed"
        )
        self.assertTrue(all(
            row["score"] >= proceed_score
            for row in first_rows
            if row["choice_id"].removeprefix("grid:") in selected_ids
        ))
        self.assertTrue(all(
            row["score"] < proceed_score
            for row in first_rows
            if row["choice_id"].removeprefix("grid:")
            in {battle_trance.uuid, shrug.uuid}
        ))

        game.screen = HandSelectScreen(
            [battle_trance, shrug],
            list(first.cards),
            len(cards),
            True,
        )
        second = agent.handle_screen()

        self.assertIsInstance(second, ProceedAction)

    def test_best_potion_is_selected_instead_of_first_usable_potion(self):
        game = GameStub([make_monster("attacker", 40, 20)], [strike()])
        weak = Potion("Weak Potion", "Weak Potion", True, True, True)
        block = Potion("Block Potion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [weak, block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.use_best_potion(20)
        self.assertIsInstance(action, PotionAction)
        self.assertEqual("Block Potion", action.potion.potion_id)

    def test_slime_boss_waits_for_battle_trance_replan_before_potions(self):
        """Replay 8fc20a4a: the hidden draw reveals a zero-loss split."""

        reckless = build_card(
            "Reckless Charge", CardType.ATTACK, cost=0, damage=15,
            upgrades=1,
        )
        reckless.uuid = "slime-reckless"
        battle_trance = build_card(
            "Battle Trance", CardType.SKILL, cost=0
        )
        battle_trance.uuid = "slime-battle-trance"
        battle_trance.magic_number = 3
        power_through = build_card(
            "Power Through", CardType.SKILL, cost=1, block=16
        )
        power_through.uuid = "slime-power-through"
        defend_card = build_card(
            "Defend_R", CardType.SKILL, cost=1, block=6,
            rarity=CardRarity.BASIC,
        )
        defend_card.uuid = "slime-defend"
        slimed_card = slimed()
        slimed_card.uuid = "slime-status"
        boss = make_monster("SlimeBoss", 103, 35)
        boss.max_hp = 140
        boss.powers = [Power("Vulnerable", "Vulnerable", 1)]
        game = GameStub(
            [boss],
            [
                battle_trance, reckless, power_through,
                defend_card, slimed_card,
            ],
        )
        game.player.current_hp = 77
        game.current_hp = 77
        game.room_type = "MonsterRoomBoss"
        game.turn = 3
        block = Potion(
            "BlockPotion", "Block Potion", True, True, False
        )
        skill = Potion(
            "SkillPotion", "Skill Potion", True, True, False
        )
        game.get_real_potions = lambda: [block, skill]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        # Replay the trace's exact pre-potion plan: the first card is an
        # ordinary Attack and Battle Trance is only the second action.
        def traced_plan():
            agent.combat_planner.last_decision = {
                "planned_sequence": [
                    {
                        "card_id": reckless.card_id,
                        "card_uuid": reckless.uuid,
                    },
                    {
                        "card_id": battle_trance.card_id,
                        "card_uuid": battle_trance.uuid,
                    },
                    {
                        "card_id": power_through.card_id,
                        "card_uuid": power_through.uuid,
                    },
                    {
                        "card_id": defend_card.card_id,
                        "card_uuid": defend_card.uuid,
                    },
                ],
                "search": {
                    "actual_loss": 13,
                    "true_combat_end": False,
                    "first_action_resolution_count": 1,
                },
            }
            return PlayCardAction(
                card=reckless, target_monster=boss
            )

        with patch.object(
            agent, "get_play_card_action", side_effect=traced_plan
        ):
            first = agent.get_next_action_in_game(game)

        self.assertIsInstance(first, PlayCardAction)
        self.assertIs(first.card, reckless)
        self.assertEqual(
            "defer_potion_until_authoritative_hand_replan",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            "Battle Trance",
            agent.combat_planner.last_decision[
                "potion_defer_trigger_card_id"
            ],
        )
        self.assertIsNone(agent._pending_potion_use)

        # Reckless resolves. The still-hidden draw keeps Block/Skill deferred
        # for one more authoritative frame.
        boss.current_hp = 88
        game.hand = [
            battle_trance, power_through, defend_card, slimed_card
        ]
        next_action = agent.get_next_action_in_game(game)
        self.assertIsInstance(next_action, PlayCardAction)
        self.assertIs(next_action.card, battle_trance)
        self.assertEqual(
            "defer_potion_until_authoritative_hand_replan",
            agent.combat_planner.last_decision["reason"],
        )

        # The real draw supplies the two attacks that cross the split point.
        drawn_strike = build_card(
            "Strike_R", CardType.ATTACK, cost=1, damage=9,
            rarity=CardRarity.BASIC,
        )
        drawn_strike.uuid = "slime-drawn-strike"
        whirlwind = build_card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=7
        )
        whirlwind.uuid = "slime-drawn-whirlwind"
        game.hand = [
            power_through, defend_card, slimed_card,
            drawn_strike, whirlwind,
        ]
        second = agent.get_next_action_in_game(game)

        self.assertIsInstance(second, PlayCardAction)
        self.assertNotIsInstance(second, PotionAction)
        search = agent.combat_planner.last_decision["search"]
        self.assertEqual(0, search["actual_loss"])
        self.assertEqual([0], search["split_pending_enemy_indexes"])
        self.assertIsNone(agent._pending_potion_use)

    def test_hand_replan_does_not_delay_first_attack_strength_potion(self):
        reckless = build_card(
            "Reckless Charge", CardType.ATTACK, cost=0, damage=10
        )
        battle_trance = build_card(
            "Battle Trance", CardType.SKILL, cost=0
        )
        battle_trance.magic_number = 3
        boss = make_monster("SlimeBoss", 120, 20)
        boss.max_hp = 140
        game = GameStub([boss], [reckless, battle_trance])
        game.room_type = "MonsterRoomBoss"
        strength = Potion(
            "StrengthPotion", "Strength Potion", True, True, False
        )
        game.get_real_potions = lambda: [strength]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        def attack_then_draw_plan():
            agent.combat_planner.last_decision = {
                "planned_sequence": [
                    {
                        "card_id": reckless.card_id,
                        "card_uuid": reckless.uuid,
                    },
                    {
                        "card_id": battle_trance.card_id,
                        "card_uuid": battle_trance.uuid,
                    },
                ],
                "search": {
                    "actual_loss": 5,
                    "true_combat_end": False,
                    "first_action_resolution_count": 1,
                    "final_enemy_hp": [110],
                },
            }
            return PlayCardAction(
                card=reckless, target_monster=boss
            )

        with patch.object(
            agent,
            "get_play_card_action",
            side_effect=attack_then_draw_plan,
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, strength)

    def test_lethal_slime_boss_line_does_not_defer_block_potion(self):
        battle_trance = build_card(
            "Battle Trance", CardType.SKILL, cost=0
        )
        battle_trance.magic_number = 3
        power_through = build_card(
            "Power Through", CardType.SKILL, cost=1, block=16
        )
        defend_card = build_card(
            "Defend_R", CardType.SKILL, cost=1, block=6,
            rarity=CardRarity.BASIC,
        )
        boss = make_monster("SlimeBoss", 103, 35)
        boss.max_hp = 140
        game = GameStub(
            [boss], [battle_trance, power_through, defend_card]
        )
        game.player.current_hp = 13
        game.current_hp = 13
        game.room_type = "MonsterRoomBoss"
        block = Potion(
            "BlockPotion", "Block Potion", True, True, False
        )
        game.get_real_potions = lambda: [block]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertLess(
            agent.combat_planner.last_decision[
                "potion_protected_hp_loss"
            ],
            game.player.current_hp,
        )

    def test_time_warp_copy_boundary_does_not_defer_potions_for_draw(self):
        battle_trance = build_card(
            "Battle Trance", CardType.SKILL, cost=0
        )
        time_eater = make_monster("TimeEater", 456, 30)
        time_eater.powers = [
            Power("TimeWarpPower", "Time Warp", 10)
        ]
        game = GameStub([time_eater], [battle_trance])
        block = Potion(
            "BlockPotion", "Block Potion", True, True, False
        )
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": battle_trance.card_id,
                "card_uuid": battle_trance.uuid,
            }],
            "search": {"first_action_resolution_count": 2},
        }

        evidence = agent._authoritative_hand_replan_before_potions(
            PlayCardAction(card=battle_trance), 5
        )

        self.assertIsNone(evidence)

    def test_lethal_turn_does_not_spend_unproven_persistent_potions(self):
        enemy = make_monster("SnakePlant", 80, 15)
        cards = [strike(), defend()]
        game = GameStub([enemy], cards)
        game.player.current_hp = 3
        game.current_hp = 3
        dexterity = Potion(
            "Dexterity Potion", "Dexterity Potion", True, True, False
        )
        strength = Potion(
            "Strength Potion", "Strength Potion", True, True, False
        )
        game.get_real_potions = lambda: [dexterity, strength]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            15,
            planned_turn_loss=15,
            planned_action=PlayCardAction(card=cards[0], target_monster=enemy),
        )

        self.assertIsNone(action)

    def test_lethal_turn_uses_at_most_one_unproven_random_potion(self):
        enemy = make_monster("SnakePlant", 80, 15)
        cards = [strike()]
        game = GameStub([enemy], cards)
        game.player.current_hp = 3
        game.current_hp = 3
        gamblers = Potion(
            "GamblersBrew", "Gamblers Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent.potions_used_this_turn[(game.act, game.floor, game.turn)] = 1

        action = agent.use_best_potion(
            15,
            planned_turn_loss=15,
            planned_action=PlayCardAction(card=cards[0], target_monster=enemy),
        )

        self.assertIsNone(action)

    def test_lethal_empty_hand_can_use_snecko_after_random_potion(self):
        enemy = make_monster("SnakePlant", 80, 15)
        game = GameStub([enemy], [])
        game.player.current_hp = 3
        game.current_hp = 3
        game.draw_pile = [strike()]
        game.discard_pile = []
        snecko = Potion("SneckoOil", "Snecko Oil", True, True, False)
        game.get_real_potions = lambda: [snecko]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        turn_key = (game.act, game.floor, game.turn)
        agent.potions_used_this_turn[turn_key] = 1
        agent.unproven_potions_used_this_turn[turn_key] = {"colorlesspotion"}

        action = agent.use_best_potion(
            15,
            planned_turn_loss=15,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, snecko)
        self.assertEqual(
            "potion_snecko_oil_emergency_replan",
            agent.combat_planner.last_decision["reason"],
        )

    def test_snecko_followup_stays_blocked_without_draw_source(self):
        enemy = make_monster("SnakePlant", 80, 15)
        game = GameStub([enemy], [])
        game.player.current_hp = 3
        game.current_hp = 3
        game.draw_pile = []
        game.discard_pile = []
        snecko = Potion("SneckoOil", "Snecko Oil", True, True, False)
        game.get_real_potions = lambda: [snecko]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        agent.potions_used_this_turn[(game.act, game.floor, game.turn)] = 1

        self.assertIsNone(agent.use_best_potion(15, planned_turn_loss=15))

    def test_lethal_turn_prefers_proven_swift_draw_over_colorless(self):
        """Replay floor 29: the known top three contain a surviving line."""

        enemy = make_monster("SphericGuardian", 80, 20)
        opening_strike = strike()
        dash = build_card(
            "Dash", CardType.ATTACK, cost=2, damage=10, block=10,
        )
        neutralize = build_card(
            "Neutralize", CardType.ATTACK, cost=0, damage=3,
            rarity=CardRarity.BASIC,
        )
        neutralize.magic_number = 1
        survivor = build_card(
            "Survivor", CardType.SKILL, cost=1, block=8,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([enemy], [opening_strike])
        game.player.current_hp = 5
        game.current_hp = 5
        # CardGroup's top is the end of the serialized list.  Swift draws
        # Survivor, Neutralize, then Dash from this exact suffix.
        game.draw_pile = [dash, neutralize, survivor]
        swift = Potion("SwiftPotion", "Swift Potion", True, True, False)
        colorless = Potion(
            "ColorlessPotion", "Colorless Potion", True, True, False
        )
        game.get_real_potions = lambda: [colorless, swift]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            20,
            planned_turn_loss=20,
            planned_action=PlayCardAction(
                card=opening_strike, target_monster=enemy,
            ),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertEqual("SwiftPotion", action.potion.potion_id)
        decision = agent.combat_planner.last_decision
        self.assertEqual("potion_known_draw_survival", decision["reason"])
        self.assertLess(decision["potion_known_draw_planned_loss"], 5)
        self.assertEqual(
            ["Survivor", "Neutralize", "Dash"],
            decision["potion_known_draw_card_ids"],
        )

    def test_cunning_potion_uses_exact_shiv_defense_to_survive(self):
        """Three Shivs can be a deterministic defensive potion."""

        enemy = make_monster("Centurion", 100, 10)
        game = GameStub([enemy], [])
        game.player.current_hp = 6
        game.current_hp = 6
        game.player.powers = [
            Power("AfterImagePower", "After Image", 1)
        ]
        game.relics = [
            Relic("Ornamental Fan", "Ornamental Fan", counter=0)
        ]
        cunning = Potion(
            "CunningPotion", "Cunning Potion", True, True, False
        )
        game.get_real_potions = lambda: [cunning]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        analysis = agent._hypothetical_cunning_potion_analysis()
        action = agent.use_best_potion(
            10,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
        )

        self.assertEqual(3, analysis["generated_shiv_count"])
        self.assertEqual(3, analysis["generated_shiv_plays"])
        self.assertEqual(3, analysis["loss"])
        self.assertTrue(analysis["survives"])
        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, cunning)
        decision = agent.combat_planner.last_decision
        self.assertEqual(
            "potion_cunning_replan_survival", decision["reason"]
        )
        self.assertEqual(3, decision["potion_candidate_hp_loss"])
        self.assertEqual(
            ["Shiv", "Shiv", "Shiv"],
            decision["potion_cunning_planned_card_ids"],
        )

    def test_cunning_replan_includes_accuracy_and_wrist_blade_damage(self):
        enemy = make_monster("Centurion", 100, 0)
        game = GameStub([enemy], [])
        game.player.powers = [
            Power("AccuracyPower", "Accuracy", 4)
        ]
        game.relics = [Relic("Wrist Blade", "Wrist Blade")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        analysis = agent._hypothetical_cunning_potion_analysis()

        self.assertEqual(3, analysis["generated_shiv_plays"])
        self.assertEqual([64], analysis["final_enemy_hp"])

    def test_cunning_potion_rejects_shivs_that_still_leave_lethal_loss(self):
        enemy = make_monster("Centurion", 100, 10)
        game = GameStub([enemy], [])
        game.player.current_hp = 6
        game.current_hp = 6
        game.player.powers = [
            Power("AfterImagePower", "After Image", 1)
        ]
        cunning = Potion(
            "CunningPotion", "Cunning Potion", True, True, False
        )
        game.get_real_potions = lambda: [cunning]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        analysis = agent._hypothetical_cunning_potion_analysis()
        action = agent.use_best_potion(
            10,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
        )

        self.assertEqual(7, analysis["loss"])
        self.assertFalse(analysis["survives"])
        self.assertIsNone(action)

    def test_cunning_potion_requires_exact_analysis_in_lethal_boss(self):
        enemy = make_monster("GenericBoss", 100, 10)
        game = GameStub([enemy], [])
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = 6
        game.current_hp = 6
        game.player.powers = [
            Power("ConfusionPower", "Confusion", 1)
        ]
        cunning = Potion(
            "CunningPotion", "Cunning Potion", True, True, False
        )
        game.get_real_potions = lambda: [cunning]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsNone(agent._hypothetical_cunning_potion_analysis())
        action = agent.use_best_potion(
            10,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_full_hand_cunning_can_still_use_toy_heal_to_survive(self):
        enemy = make_monster("Centurion", 100, 5)
        full_hand = []
        for index in range(10):
            card = slimed()
            card.uuid = f"full-hand-status-{index}"
            full_hand.append(card)
        game = GameStub([enemy], full_hand)
        game.player.current_hp = 3
        game.current_hp = 3
        game.relics = [
            Relic("Toy Ornithopter", "Toy Ornithopter")
        ]
        cunning = Potion(
            "CunningPotion", "Cunning Potion", True, True, False
        )
        game.get_real_potions = lambda: [cunning]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsNone(agent._hypothetical_cunning_potion_analysis())
        action = agent.use_best_potion(
            5,
            planned_turn_loss=5,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, cunning)
        self.assertEqual(5, agent._immediate_potion_hp_gain("cunningpotion"))

    def test_cunning_potion_restores_low_hp_safety_reserve(self):
        enemy = make_monster("Centurion", 100, 17)
        game = GameStub([enemy], [])
        game.player.current_hp = 20
        game.current_hp = 20
        game.player.powers = [
            Power("AfterImagePower", "After Image", 1)
        ]
        game.relics = [
            Relic("Ornamental Fan", "Ornamental Fan", counter=0)
        ]
        cunning = Potion(
            "CunningPotion", "Cunning Potion", True, True, False
        )
        game.get_real_potions = lambda: [cunning]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            17,
            planned_turn_loss=17,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertEqual(
            "potion_cunning_replan_reserve",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertTrue(
            agent.combat_planner.last_decision[
                "potion_cunning_preserves_reserve"
            ]
        )

    def test_lethal_ordinary_turn_uses_distilled_chaos_as_last_stochastic_rescue(self):
        """HP=1 must not retain the only stochastic rescue for certain death."""
        enemy = make_monster("SnakePlant", 80, 20)
        strike_card = strike()
        game = GameStub([enemy], [strike_card])
        game.player.current_hp = 1
        game.current_hp = 1
        chaos = Potion(
            "DistilledChaos", "Distilled Chaos", True, True, False
        )
        game.get_real_potions = lambda: [chaos]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            20,
            planned_turn_loss=20,
            planned_action=PlayCardAction(
                card=strike_card, target_monster=enemy
            ),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, chaos)
        self.assertEqual(
            "potion_stochastic_emergency",
            agent.combat_planner.last_decision["reason"],
        )

    def test_confirmed_random_potion_blocks_a_different_random_potion_same_turn(self):
        enemy = make_monster("SnakePlant", 80, 15)
        cards = [strike()]
        game = GameStub([enemy], cards)
        game.player.current_hp = 6
        game.current_hp = 6
        skill = Potion("SkillPotion", "Skill Potion", True, True, False)
        gamblers = Potion("GamblersBrew", "Gamblers Brew", True, True, False)
        owned = [skill, gamblers]
        game.get_real_potions = lambda: list(owned)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        first = agent.use_best_potion(
            18,
            planned_turn_loss=9,
            planned_action=PlayCardAction(card=cards[0], target_monster=enemy),
        )
        self.assertIsInstance(first, PotionAction)
        owned.remove(first.potion)
        agent.confirm_potion_use(game)

        second = agent.use_best_potion(
            18,
            planned_turn_loss=9,
            planned_action=PlayCardAction(card=cards[0], target_monster=enemy),
        )
        self.assertIsNone(second)

    def test_low_hp_dangerous_fight_uses_persistent_potion_early(self):
        enemy = make_monster("SnakePlant", 77, 21)
        cards = [defend(), strike()]
        game = GameStub([enemy], cards)
        game.act = 2
        game.turn = 1
        game.player.current_hp = 18
        game.current_hp = 18
        dexterity = Potion(
            "Dexterity Potion", "Dexterity Potion", True, True, False
        )
        game.get_real_potions = lambda: [dexterity]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            21,
            planned_turn_loss=1,
            planned_action=PlayCardAction(card=cards[0]),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, dexterity)

    def test_regen_potion_is_saved_when_guaranteed_combo_ends_combat(self):
        # Exercise the same pre-beam shortcut as the historical Lagavulin
        # failure.  Sacred Bark plus Toy would score highly enough to drink
        # if this shortcut omitted its full-combat terminal proof.
        enemy = make_monster("Lagavulin", 12, 18)
        first = strike()
        second = strike()
        second.uuid = "strike-2"
        game = GameStub([enemy], [first, second])
        game.room_type = "MonsterRoomElite"
        game.turn = 4
        game.player.current_hp = game.current_hp = 20
        game.player.max_hp = game.max_hp = 88
        game.relics = [
            Relic("Sacred Bark", "Sacred Bark"),
            Relic("Toy Ornithopter", "Toy Ornithopter"),
        ]
        regen = Potion(
            "Regen Potion", "Regen Potion", True, True, False
        )
        game.get_real_potions = lambda: [regen]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIn(action.card.uuid, {"strike-1", "strike-2"})
        decision = agent.combat_planner.last_decision
        self.assertEqual(
            "guaranteed_attack_combo_lethal", decision["reason"]
        )
        self.assertTrue(decision["search"]["true_combat_end"])
        projection = agent._regen_potion_projection(
            planned_combat_end=True
        )
        self.assertEqual(0, projection["cashable_turn_ends"])
        self.assertEqual(5, projection["immediate_healing"])
        self.assertEqual(5, projection["projected_healing"])

    def test_regen_potion_uses_cashable_ticks_in_long_low_hp_fight(self):
        enemy = make_monster("GenericBoss", 240, 20)
        cards = [defend(), strike()]
        game = GameStub([enemy], cards)
        game.room_type = "MonsterRoomBoss"
        game.act = 2
        game.player.current_hp = game.current_hp = 18
        game.player.max_hp = game.max_hp = 80
        regen = Potion(
            "Regen Potion", "Regen Potion", True, True, False
        )
        game.get_real_potions = lambda: [regen]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            20,
            planned_turn_loss=15,
            planned_action=PlayCardAction(card=cards[0]),
            planned_combat_end=False,
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, regen)
        decision = agent.combat_planner.last_decision
        self.assertEqual(5, decision["potion_regen_cashable_turn_ends"])
        self.assertEqual(15, decision["potion_regen_projected_healing"])
        self.assertFalse(decision["potion_planned_combat_end"])
        projection = agent._regen_potion_projection(
            planned_combat_end=False
        )
        self.assertEqual(5, projection["cashable_turn_ends"])
        self.assertEqual(15, projection["projected_healing"])

    def test_regen_potion_has_no_projected_healing_with_mark_of_bloom(self):
        enemy = make_monster("GenericBoss", 240, 20)
        cards = [defend(), strike()]
        game = GameStub([enemy], cards)
        game.room_type = "MonsterRoomBoss"
        game.act = 2
        game.player.current_hp = game.current_hp = 18
        game.player.max_hp = game.max_hp = 80
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        regen = Potion(
            "Regen Potion", "Regen Potion", True, True, False
        )
        game.get_real_potions = lambda: [regen]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        projection = agent._regen_potion_projection(
            planned_combat_end=False
        )
        action = agent.use_best_potion(
            20,
            planned_turn_loss=15,
            planned_action=PlayCardAction(card=cards[0]),
            planned_combat_end=False,
        )

        self.assertIsNone(action)
        self.assertTrue(projection["healing_blocked"])
        self.assertEqual(0, projection["cashable_turn_ends"])
        self.assertEqual(0, projection["projected_healing"])

    def test_dexterity_and_energy_combo_is_used_only_when_replan_saves(self):
        enemy = make_monster("SnakePlant", 80, 15)
        first = defend()
        second = defend()
        second.uuid = "defend-2"
        game = GameStub([enemy], [first, second])
        game.player.current_hp = 5
        game.current_hp = 5
        game.player.energy = 1
        dexterity = Potion(
            "DexterityPotion", "Dexterity Potion", True, True, False
        )
        energy = Potion("EnergyPotion", "Energy Potion", True, True, False)
        game.get_real_potions = lambda: [dexterity, energy]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            15,
            planned_turn_loss=10,
            planned_action=PlayCardAction(card=first),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIn(action.potion, (dexterity, energy))
        self.assertEqual(
            "potion_stat_combo_survival",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertLess(
            agent.combat_planner.last_decision["protected_combo_hp_loss"],
            game.player.current_hp,
        )

    def test_strength_potion_analysis_adds_strength_once_per_hit(self):
        enemy = make_monster("BookOfStabbing", 40, 0)
        riddle = Card(
            "Riddle With Holes", "Riddle With Holes", CardType.ATTACK,
            CardRarity.UNCOMMON, cost=1, uuid="riddle", has_target=True,
            is_playable=True, damage=3,
        )
        game = GameStub([enemy], [riddle])
        game.player.energy = 1
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        analysis = agent._hypothetical_stat_potion_analysis(
            "StrengthPotion"
        )

        self.assertEqual([15], analysis["final_enemy_hp"])

    def test_energy_potion_is_saved_when_it_only_adds_weak_chip_damage(self):
        hexaghost = make_monster("Hexaghost", 228, 6)
        hexaghost.max_hp = 250
        hexaghost.move_hits = 6
        cards = [
            build_card("Defend_R", CardType.SKILL, block=5),
            build_card(
                "Pommel Strike", CardType.ATTACK, damage=9,
                rarity=CardRarity.COMMON,
            ),
            build_card(
                "Strike_R", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ),
            build_card(
                "Strike_R-2", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ),
            build_card("Shrug It Off", CardType.SKILL, block=8),
        ]
        game = GameStub([hexaghost], cards)
        game.room_type = "MonsterRoomBoss"
        game.floor = 16
        game.turn = 2
        game.player.current_hp = game.current_hp = 65
        energy = Potion(
            "EnergyPotion", "Energy Potion", True, True, False
        )
        game.get_real_potions = lambda: [energy]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(36)

        self.assertIsNone(action)

    def test_energy_potion_is_saved_when_severe_loss_is_unchanged(self):
        """Replay 499776: 18 chip cannot justify zero survival gain."""

        minion = make_monster("GremlinWarrior", 22, 7, index=0)
        minion.block = 6
        minion.powers = [
            Power("Angry", "Angry", 1),
            Power("Strength", "Strength", 3),
        ]
        leader = make_monster("GremlinLeader", 141, 9, index=1)
        leader.move_hits = 3
        leader.powers = [Power("Strength", "Strength", 3)]
        cards = [
            build_card(
                "Defend_R", CardType.SKILL,
                cost=1, block=5, rarity=CardRarity.BASIC,
            ),
            build_card(
                "Strike_R", CardType.ATTACK,
                cost=1, damage=6, rarity=CardRarity.BASIC,
            ),
            build_card(
                "Strike_R-2", CardType.ATTACK,
                cost=1, damage=6, rarity=CardRarity.BASIC,
            ),
            build_card(
                "Searing Blow", CardType.ATTACK, cost=2, damage=12,
            ),
            build_card("Shockwave", CardType.SKILL, cost=2),
        ]
        cards[-1].magic_number = 3
        cards[-1].exhausts = True
        game = GameStub([minion, leader], cards)
        game.room_type = "MonsterRoomElite"
        game.act = 2
        game.floor = 23
        game.turn = 3
        game.player.current_hp = game.current_hp = 26
        game.player.max_hp = game.max_hp = 90
        energy = Potion(
            "Energy Potion", "Energy Potion", True, True, False
        )
        game.get_real_potions = lambda: [energy]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent.combat_planner.last_decision = {
            "search": {
                "final_enemy_hp": [22, 141],
                "enemies_dead": 0,
            },
            "planned_sequence": [{"card_id": "Shockwave"}],
        }

        action = agent.use_best_potion(
            34,
            planned_turn_loss=18,
            planned_action=PlayCardAction(card=cards[-1]),
        )

        self.assertIsNone(action)

    def test_energy_potion_spends_on_measured_severe_loss_reduction(self):
        enemy = make_monster("GenericElite", 100, 15)
        first = defend()
        second = defend()
        second.uuid = "energy-second-defend"
        game = GameStub([enemy], [first, second])
        game.room_type = "MonsterRoomElite"
        game.player.current_hp = game.current_hp = 18
        game.player.energy = 1
        energy = Potion(
            "EnergyPotion", "Energy Potion", True, True, False
        )
        game.get_real_potions = lambda: [energy]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            15,
            planned_turn_loss=10,
            planned_action=PlayCardAction(card=first),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, energy)
        self.assertEqual(
            5, agent.combat_planner.last_decision["potion_candidate_hp_loss"]
        )
        self.assertEqual(
            5, agent.combat_planner.last_decision["potion_loss_reduction"]
        )
        self.assertTrue(
            agent.combat_planner.last_decision["potion_severe_loss"]
        )

    def test_defect_orb_potions_require_grounded_long_fight_value(self):
        dualcast = build_card("Dualcast", CardType.SKILL, cost=1)
        boss_game = GameStub(
            [make_monster("GenericBoss", 180, 0)], [dualcast]
        )
        boss_game.room_type = "MonsterRoomBoss"
        essence = Potion(
            "EssenceOfDarkness", "Essence of Darkness", True, True, False
        )
        boss_game.get_real_potions = lambda: [essence]
        boss_agent = SimpleAgent(PlayerClass.DEFECT)
        boss_agent.game = boss_game

        boss_action = boss_agent.use_best_potion(0)

        self.assertIsInstance(boss_action, PotionAction)
        self.assertIs(boss_action.potion, essence)
        self.assertEqual(
            3,
            boss_agent.combat_planner.last_decision[
                "potion_occupied_orbs_after"
            ],
        )
        self.assertEqual(
            3,
            boss_agent.combat_planner.last_decision[
                "potion_orb_channels"
            ],
        )
        self.assertEqual(
            ["dark", "dark", "dark"],
            boss_agent.combat_planner.last_decision[
                "potion_orb_ids_after"
            ],
        )
        self.assertGreaterEqual(
            boss_agent.combat_planner.last_decision["potion_marginal_damage"],
            12,
        )

        hallway_game = GameStub(
            [make_monster("GenericHallway", 180, 0)], [strike()]
        )
        unused_essence = Potion(
            "EssenceOfDarkness", "Essence of Darkness", True, True, False
        )
        unused_capacity = Potion(
            "PotionOfCapacity", "Potion of Capacity", True, True, False
        )
        hallway_game.get_real_potions = lambda: [
            unused_essence, unused_capacity
        ]
        hallway_agent = SimpleAgent(PlayerClass.DEFECT)
        hallway_agent.game = hallway_game

        self.assertIsNone(hallway_agent.use_best_potion(0))

    def test_essence_of_darkness_models_full_slot_frost_evocation(self):
        enemy = make_monster("GenericAttacker", 100, 10)
        game = GameStub([enemy], [])
        game.player.current_hp = game.current_hp = 6
        game.player.orbs = [
            Orb("Frost", "Frost", 5, 2),
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Lightning", "Lightning", 8, 3),
        ]
        essence = Potion(
            "EssenceOfDarkness", "Essence of Darkness", True, True, False
        )
        game.get_real_potions = lambda: [essence]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.use_best_potion(
            10,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, essence)
        self.assertEqual(
            5, agent.combat_planner.last_decision["potion_candidate_hp_loss"]
        )
        self.assertEqual(
            3,
            agent.combat_planner.last_decision[
                "potion_occupied_orbs_after"
            ],
        )
        self.assertEqual(
            3,
            agent.combat_planner.last_decision[
                "potion_orb_overflow_evokes"
            ],
        )
        self.assertEqual(
            ["dark", "dark", "dark"],
            agent.combat_planner.last_decision["potion_orb_ids_after"],
        )

    def test_capacity_potion_uses_real_slots_and_does_not_drain_belt(self):
        zap = build_card("Zap", CardType.SKILL, cost=1)
        ball = build_card(
            "Ball Lightning", CardType.ATTACK, cost=1, damage=7
        )
        dualcast = build_card("Dualcast", CardType.SKILL, cost=1)
        game = GameStub(
            [make_monster("GenericBoss", 180, 0)], [zap, ball, dualcast]
        )
        game.room_type = "MonsterRoomBoss"
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Frost", "Frost", 5, 2),
            Orb("Empty", "Empty", 0, 0),
        ]
        capacity = Potion(
            "PotionOfCapacity", "Potion of Capacity", True, True, False
        )
        essence = Potion(
            "EssenceOfDarkness", "Essence of Darkness", True, True, False
        )
        owned = [capacity]
        game.get_real_potions = lambda: list(owned)
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        first = agent.use_best_potion(0)

        self.assertIsInstance(first, PotionAction)
        self.assertIs(first.potion, capacity)
        self.assertEqual(
            5,
            agent.combat_planner.last_decision[
                "potion_orb_slots_after"
            ],
        )
        owned.remove(first.potion)
        agent.confirm_potion_use(game)
        owned.append(essence)

        self.assertIsNone(agent.use_best_potion(0))

    def test_capacity_and_essence_analysis_preserves_potion_order(self):
        game = GameStub([make_monster("GenericBoss", 240, 0)], [])
        game.room_type = "MonsterRoomBoss"
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        capacity_first = agent._hypothetical_stat_potion_analysis((
            "PotionOfCapacity", "EssenceOfDarkness",
        ))
        essence_first = agent._hypothetical_stat_potion_analysis((
            "EssenceOfDarkness", "PotionOfCapacity",
        ))

        self.assertEqual(
            ["potionofcapacity", "essenceofdarkness"],
            capacity_first["potion_sequence"],
        )
        self.assertEqual(5, capacity_first["orb_slots_after"])
        self.assertEqual(5, capacity_first["orb_channels"])
        self.assertEqual(5, capacity_first["occupied_orbs_after"])
        self.assertEqual(["dark"] * 5, capacity_first["orb_ids_after"])

        self.assertEqual(
            ["essenceofdarkness", "potionofcapacity"],
            essence_first["potion_sequence"],
        )
        self.assertEqual(5, essence_first["orb_slots_after"])
        self.assertEqual(3, essence_first["orb_channels"])
        self.assertEqual(3, essence_first["occupied_orbs_after"])
        self.assertEqual(["dark"] * 3, essence_first["orb_ids_after"])

    def test_essence_uses_current_reduced_orb_slot_count(self):
        game = GameStub([make_monster("GenericBoss", 240, 0)], [])
        game.room_type = "MonsterRoomBoss"
        game.player.orbs = [
            Orb("Empty", "Empty", 0, 0),
            Orb("Empty", "Empty", 0, 0),
        ]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        analysis = agent._hypothetical_stat_potion_analysis(
            "EssenceOfDarkness"
        )

        self.assertEqual(2, analysis["orb_slots_before"])
        self.assertEqual(2, analysis["orb_slots_after"])
        self.assertEqual(2, analysis["orb_channels"])
        self.assertEqual(2, analysis["occupied_orbs_after"])
        self.assertEqual(["dark", "dark"], analysis["orb_ids_after"])

    def test_essence_uses_authoritative_reduced_capacity_without_empty_rows(self):
        game = GameStub([make_monster("GenericBoss", 240, 0)], [])
        game.room_type = "MonsterRoomBoss"
        game.player.orbs = [
            Orb("Lightning", "Lightning", 8, 3),
            Orb("Frost", "Frost", 5, 2),
        ]
        game.player.max_orbs = 2
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        analysis = agent._hypothetical_stat_potion_analysis(
            "EssenceOfDarkness"
        )

        self.assertEqual(2, analysis["orb_slots_before"])
        self.assertEqual(2, analysis["orb_channels"])
        self.assertEqual(2, analysis["orb_overflow_evokes"])

    def test_sacred_bark_essence_channels_twice_per_orb_slot(self):
        enemy = make_monster("GenericBoss", 240, 0)
        game = GameStub([enemy], [])
        game.room_type = "MonsterRoomBoss"
        game.relics = [Relic("Sacred Bark", "Sacred Bark")]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        analysis = agent._hypothetical_stat_potion_analysis(
            "EssenceOfDarkness"
        )

        self.assertEqual(3, analysis["orb_slots_after"])
        self.assertEqual(6, analysis["orb_channels"])
        self.assertEqual(3, analysis["orb_overflow_evokes"])
        self.assertEqual(18, analysis["dark_evoke_damage"])
        self.assertEqual(["dark", "dark", "dark"], analysis["orb_ids_after"])
        self.assertEqual([222], analysis["post_potion_enemy_hp"])

    def test_essence_overflow_dark_hits_deterministic_lowest_hp_target(self):
        low = make_monster("Low", 30, 0, index=0)
        high = make_monster("High", 100, 0, index=1)
        game = GameStub([low, high], [])
        game.room_type = "MonsterRoomBoss"
        game.player.orbs = [
            Orb("Dark", "Dark", 20, 6),
            Orb("Frost", "Frost", 5, 2),
            Orb("Frost", "Frost", 5, 2),
        ]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        analysis = agent._hypothetical_stat_potion_analysis(
            "EssenceOfDarkness"
        )

        self.assertEqual(3, analysis["orb_channels"])
        self.assertEqual(3, analysis["orb_overflow_evokes"])
        self.assertEqual(20, analysis["dark_evoke_damage"])
        self.assertEqual([10, 100], analysis["post_potion_enemy_hp"])
        self.assertEqual(["dark", "dark", "dark"], analysis["orb_ids_after"])

    def test_zero_loss_ordered_plan_rejects_unproven_one_turn_potions(self):
        potion_ids = (
            "AttackPotion",
            "ColorlessPotion",
            "EnergyPotion",
            "EntropicBrew",
            "GamblersBrew",
            "SkillPotion",
            "SwiftPotion",
            "SneckoOil",
            "DistilledChaos",
        )

        for potion_id in potion_ids:
            with self.subTest(potion_id=potion_id):
                game = GameStub(
                    [make_monster("Collector", 300, 20)],
                    [strike()],
                )
                game.room_type = "MonsterRoomBoss"
                game.turn = 2
                potion = Potion(
                    potion_id, potion_id, True, True, False
                )
                game.get_real_potions = lambda potion=potion: [potion]
                agent = SimpleAgent(PlayerClass.IRONCLAD)
                agent.game = game

                action = agent.use_best_potion(
                    20,
                    planned_turn_loss=0,
                    planned_action=EndTurnAction(),
                )

                self.assertIsNone(action)

    def test_zero_loss_guard_does_not_block_persistent_stat_potions(self):
        cases = (
            ("StrengthPotion", PlayerClass.IRONCLAD),
            ("DexterityPotion", PlayerClass.THE_SILENT),
            ("FocusPotion", PlayerClass.DEFECT),
            ("CultistPotion", PlayerClass.IRONCLAD),
        )

        for potion_id, player_class in cases:
            with self.subTest(potion_id=potion_id):
                game = GameStub(
                    [make_monster("Collector", 300, 0)],
                    [strike()],
                )
                game.room_type = "MonsterRoomBoss"
                game.turn = 1
                potion = Potion(
                    potion_id, potion_id, True, True, False
                )
                game.get_real_potions = lambda potion=potion: [potion]
                agent = SimpleAgent(player_class)
                agent.game = game

                action = agent.use_best_potion(
                    0,
                    planned_turn_loss=0,
                    planned_action=EndTurnAction(),
                )

                self.assertIsInstance(action, PotionAction)
                self.assertIs(action.potion, potion)

    def test_emergency_skill_potion_is_used_in_lethal_normal_combat(self):
        game = GameStub([make_monster("attacker", 40, 20)], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        skill = Potion("SkillPotion", "Skill Potion", True, True, False)
        game.get_real_potions = lambda: [skill]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.use_best_potion(20)
        self.assertIsInstance(action, PotionAction)
        self.assertEqual("SkillPotion", action.potion.potion_id)

    def test_reserve_breaking_turn_uses_one_draw_potion_before_nonlethal_end(self):
        enemy = make_monster("Collector", 240, 81)
        card = strike()
        game = GameStub([enemy], [card])
        game.player.current_hp = 58
        game.current_hp = 58
        gamblers = Potion(
            "GamblersBrew", "Gamblers Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        # 49 damage is not literally lethal at 58 HP, but leaves 9 HP below
        # the planner's 12-point dynamic reserve. A single draw/resource
        # potion should be used to expose a better authoritative line.
        action = agent.use_best_potion(
            81,
            planned_turn_loss=49,
            planned_action=PlayCardAction(card=card, target_monster=enemy),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, gamblers)
        self.assertTrue(
            agent.combat_planner.last_decision["potion_reserve_breaking_crisis"]
        )

    def test_elixir_cleans_persistent_wounds_in_long_boss_before_recycle(self):
        """Regression: attempt 5f92 carried Elixir through Donu/Deca death.

        Mark of Pain plus repeated Power Through created a hand whose Wounds
        would recycle several times.  This mirrors the authoritative frame
        after seq 188635 played Power Through: three energy, fifteen Block,
        Clothesline/Strike and three Wounds.  A zero-loss current card line
        must not hide that long-fight cleanup value.
        """

        useful = build_card(
            "Clothesline", CardType.ATTACK, cost=2, damage=14,
            rarity=CardRarity.COMMON,
        )
        useful.uuid = "elixir-useful"
        followup = strike()
        followup.uuid = "elixir-followup-strike"
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"elixir-wound-{index}", is_playable=False,
            )
            for index in range(3)
        ]
        donu = make_monster("Donu", 240, 0)
        donu.intent = Intent.BUFF
        deca = make_monster("Deca", 250, 20, index=1)
        game = GameStub([donu, deca], [useful, followup, *wounds])
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.floor = 50
        game.turn = 1
        game.player.current_hp = game.current_hp = 44
        game.player.max_hp = game.max_hp = 81
        game.player.energy = 3
        game.player.block = 15
        game.draw_pile = [
            strike(), defend(), strike(), defend(), strike(), defend(),
            strike(), defend(), strike(), defend(), strike(), defend(),
            strike(), defend(), strike(),
        ]
        game.discard_pile = []
        elixir = Potion(
            "ElixirPotion", "Elixir", True, True, False
        )
        game.get_real_potions = lambda: [elixir]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, elixir)
        decision = agent.combat_planner.last_decision
        self.assertEqual(3, decision["potion_hand_filter_target_count"])
        self.assertEqual(
            3, decision["potion_hand_filter_persistent_clog_count"]
        )
        self.assertEqual(
            ["Wound", "Wound", "Wound"],
            decision["potion_hand_filter_targets"],
        )

    def test_elixir_selection_preserves_the_post_potion_card_plan(self):
        planned_defend = defend()
        planned_defend.uuid = "elixir-planned-defend"
        setup = build_card(
            "Demon Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        setup.uuid = "elixir-valuable-setup"
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"elixir-select-wound-{index}",
                is_playable=False,
            )
            for index in range(2)
        ]
        ethereal_dazed = Card(
            "Dazed", "Dazed", CardType.STATUS, CardRarity.SPECIAL,
            cost=-2, uuid="elixir-select-ethereal-dazed",
            is_playable=False, ethereal=True,
        )
        free_slimed = Card(
            "Slimed", "Slimed", CardType.STATUS, CardRarity.SPECIAL,
            cost=0, uuid="elixir-select-free-slimed",
            is_playable=True, exhausts=True,
        )
        game = GameStub(
            [make_monster("Donu", 400, 12)],
            [
                planned_defend, setup, ethereal_dazed, free_slimed,
                *wounds,
            ],
        )
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.draw_pile = [strike() for _ in range(15)]
        elixir = Potion("ElixirPotion", "Elixir", True, True, False)
        game.get_real_potions = lambda: [elixir]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent._combat_epoch = 1
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": planned_defend.card_id,
                "card_uuid": planned_defend.uuid,
            }],
            "_combat_context": agent._combat_plan_context(),
        }

        potion_action = agent.use_best_potion(
            12,
            planned_turn_loss=0,
            planned_action=PlayCardAction(card=planned_defend),
            planned_combat_end=False,
        )

        self.assertIsInstance(potion_action, PotionAction)
        self.assertEqual(
            planned_defend.uuid,
            agent.combat_planner.last_decision["planned_sequence"][0][
                "card_uuid"
            ],
        )
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(
            [
                planned_defend, setup, ethereal_dazed, free_slimed,
                *wounds,
            ],
            [],
            6,
            True,
        )
        game.current_action = "ExhaustAction"
        game.choice_available = True

        selection = agent.handle_screen()

        self.assertIsInstance(selection, CardSelectAction)
        self.assertEqual(
            {card.uuid for card in wounds},
            {card.uuid for card in selection.cards},
        )
        self.assertNotIn(planned_defend, selection.cards)
        self.assertNotIn(ethereal_dazed, selection.cards)
        self.assertNotIn(free_slimed, selection.cards)

        rows = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        proceed_score = rows["action:proceed"]["score"]
        self.assertEqual(0.0, proceed_score)
        self.assertTrue(all(
            rows[f"grid:{card.uuid}"]["score"] > proceed_score
            for card in wounds
        ))
        self.assertTrue(all(
            rows[f"grid:{card.uuid}"]["score"] < proceed_score
            for card in (
                planned_defend, setup, ethereal_dazed, free_slimed,
            )
        ))
        self.assertTrue(all(
            row["score_inputs"]["expected_redraw_keep_value"] == 0.0
            for row in rows.values()
            if row["choice_id"] != "action:proceed"
        ))
        exact_filter = agent.last_noncombat_decision["candidate_contract"][
            "exact_potion_filter"
        ]
        self.assertEqual(
            {card.uuid for card in wounds},
            set(exact_filter["target_card_instance_ids"]),
        )
        self.assertFalse(exact_filter["replacement_draw"])
        self.assertIsNone(exact_filter["draw_mode"])

    def test_elixir_normality_value_cannot_displace_exact_lethal_block(self):
        normality = Card(
            "Normality", "Normality", CardType.CURSE, CardRarity.CURSE,
            cost=-2, uuid="elixir-lethal-normality", is_playable=False,
        )
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"elixir-lethal-wound-{index}",
                is_playable=False,
            )
            for index in range(2)
        ]
        game = GameStub(
            [make_monster("Donu", 300, 10)], [normality, *wounds]
        )
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 5
        elixir = Potion("ElixirPotion", "Elixir", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [elixir, block]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        analysis = agent._hand_filter_potion_analysis("elixirpotion")
        action = agent.use_best_potion(
            10,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
            planned_combat_end=False,
        )

        self.assertTrue(analysis["worthwhile"])
        self.assertGreaterEqual(analysis["hand_filter_quality_value"], 7)
        self.assertEqual(
            0, analysis["direct_current_turn_hp_loss_prevented"]
        )
        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)

    def test_elixir_fixed_burn_damage_can_prove_a_lethal_rescue(self):
        burn = Card(
            "Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL,
            cost=-2, uuid="elixir-lethal-burn", is_playable=False,
        )
        burn.upgrades = 1
        wound = Card(
            "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
            cost=-2, uuid="elixir-lethal-burn-wound", is_playable=False,
        )
        game = GameStub(
            [make_monster("Donu", 300, 0)], [burn, wound]
        )
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 3
        elixir = Potion("ElixirPotion", "Elixir", True, True, False)
        game.get_real_potions = lambda: [elixir]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        analysis = agent._hand_filter_potion_analysis("elixirpotion")
        action = agent.use_best_potion(
            0,
            planned_turn_loss=4,
            planned_action=EndTurnAction(),
            planned_combat_end=False,
        )

        self.assertEqual(
            4, analysis["direct_current_turn_hp_loss_prevented"]
        )
        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, elixir)
        self.assertGreaterEqual(
            agent.combat_planner.last_decision["potion_score"], 110
        )

    def test_gamblers_brew_redraws_clogged_boss_hand_on_zero_loss_turn(self):
        setup = build_card(
            "Demon Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        setup.uuid = "gambler-protected-setup"
        ordinary = defend()
        ordinary.uuid = "gambler-ordinary-defend"
        normality = Card(
            "Normality", "Normality", CardType.CURSE, CardRarity.CURSE,
            cost=-2, uuid="gambler-normality", is_playable=False,
        )
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"gambler-wound-{index}", is_playable=False,
            )
            for index in range(2)
        ]
        game = GameStub(
            [make_monster("AwakenedOne", 300, 0)],
            [setup, ordinary, normality, *wounds],
        )
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.turn = 1
        draw_cards = [
            build_card("Impervious", CardType.SKILL, cost=2, block=30),
            build_card("Offering", CardType.SKILL, cost=0),
            build_card(
                "Heavy Blade", CardType.ATTACK, cost=2, damage=20,
                rarity=CardRarity.COMMON,
            ),
        ]
        game.draw_pile = draw_cards
        game.discard_pile = []
        gamblers = Potion(
            "GamblersBrew", "Gambler's Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": setup.card_id,
                "card_uuid": setup.uuid,
            }],
        }

        action = agent.use_best_potion(
            0,
            planned_turn_loss=0,
            planned_action=PlayCardAction(card=setup),
            planned_combat_end=False,
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, gamblers)
        decision = agent.combat_planner.last_decision
        self.assertEqual("known_top", decision["potion_hand_filter_draw_mode"])
        self.assertGreater(
            decision["potion_hand_filter_expected_redraw_gain"], 12
        )
        self.assertNotIn(
            setup.card_id, decision["potion_hand_filter_targets"]
        )

    def test_gamblers_brew_is_never_used_while_no_draw_is_active(self):
        normality = Card(
            "Normality", "Normality", CardType.CURSE, CardRarity.CURSE,
            cost=-2, uuid="gambler-no-draw-normality", is_playable=False,
        )
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"gambler-no-draw-wound-{index}",
                is_playable=False,
            )
            for index in range(2)
        ]
        game = GameStub(
            [make_monster("TheGuardian", 200, 12)],
            [defend(), normality, *wounds],
        )
        game.room_type = "MonsterRoomBoss"
        game.draw_pile = [
            build_card("Impervious", CardType.SKILL, cost=2, block=30),
            build_card("Offering", CardType.SKILL, cost=0),
            build_card("Heavy Blade", CardType.ATTACK, cost=2, damage=20),
        ]
        game.discard_pile = []
        game.player.powers = [Power("No Draw", "No Draw", -1)]
        gamblers = Potion(
            "GamblersBrew", "Gambler's Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        analysis = agent._hand_filter_potion_analysis("gamblersbrew")
        action = agent.use_best_potion(
            12,
            planned_turn_loss=12,
            planned_action=EndTurnAction(),
            planned_combat_end=False,
        )

        self.assertFalse(analysis["worthwhile"])
        self.assertEqual("no_draw_blocks_gamblers_brew", analysis["reason"])
        self.assertEqual("blocked_by_no_draw", analysis["draw_mode"])
        self.assertIsNone(action)

    def test_gamblers_brew_exact_filter_survives_decision_refresh(self):
        setup = build_card(
            "Demon Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        setup.uuid = "gambler-refresh-setup"
        strike_card = strike()
        strike_card.uuid = "gambler-refresh-strike"
        normality = Card(
            "Normality", "Normality", CardType.CURSE, CardRarity.CURSE,
            cost=-2, uuid="gambler-refresh-normality",
            is_playable=False,
        )
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS,
                CardRarity.SPECIAL, cost=-2,
                uuid=f"gambler-refresh-wound-{index}",
                is_playable=False,
            )
            for index in range(2)
        ]
        hand = [setup, strike_card, normality, *wounds]
        game = GameStub(
            [make_monster("AwakenedOne", 300, 0)], hand
        )
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.turn = 2
        game.draw_pile = [
            build_card("Impervious", CardType.SKILL, cost=2, block=30),
            build_card("Offering", CardType.SKILL, cost=0),
            build_card(
                "Heavy Blade", CardType.ATTACK, cost=2, damage=20,
                rarity=CardRarity.COMMON,
            ),
        ]
        game.discard_pile = []
        gamblers = Potion(
            "GamblersBrew", "Gambler's Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        agent._combat_epoch = 4

        potion_action = agent.use_best_potion(
            0,
            planned_turn_loss=0,
            planned_action=PlayCardAction(card=setup),
            planned_combat_end=False,
        )

        self.assertIsInstance(potion_action, PotionAction)
        expected_targets = set(
            agent.combat_planner.last_decision[
                "potion_hand_filter_target_uuids"
            ]
        )
        self.assertTrue(
            {normality.uuid, *(card.uuid for card in wounds)}.issubset(
                expected_targets
            )
        )
        self.assertNotIn(setup.uuid, expected_targets)

        # Live potion confirmation may refresh this diagnostic slot before
        # the selection overlay arrives.  The exact transaction plan must
        # still be available independently.
        agent.combat_planner.last_decision = {}
        game.screen_type = ScreenType.HAND_SELECT
        game.screen = HandSelectScreen(hand, [], 99, True)
        game.current_action = "GamblingChipAction"
        game.choice_available = True

        selection = agent.handle_screen()

        self.assertIsInstance(selection, CardSelectAction)
        selected_cards = list(selection.cards)
        self.assertTrue(
            {card.uuid for card in selected_cards}.issubset(
                expected_targets
            )
        )
        rows = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertTrue(all(
            row["score"] < 0.0
            for row in rows.values()
            if row["choice_id"].startswith("grid:")
            and row["choice_id"].removeprefix("grid:")
            not in expected_targets
        ))
        exact_filter = agent.last_noncombat_decision[
            "candidate_contract"
        ]["exact_potion_filter"]
        self.assertEqual(
            expected_targets,
            set(exact_filter["target_card_instance_ids"]),
        )

        remaining = [
            card for card in hand if card not in selected_cards
        ]
        if any(card.uuid in expected_targets for card in remaining):
            game.screen = HandSelectScreen(
                remaining, selected_cards, 99, True
            )
            continuation = agent.handle_screen()
            self.assertIsInstance(continuation, CardSelectAction)
            selected_cards.extend(continuation.cards)
            remaining = [
                card for card in remaining
                if card not in continuation.cards
            ]
        self.assertEqual(
            expected_targets,
            {card.uuid for card in selected_cards},
        )
        game.screen = HandSelectScreen(
            remaining, selected_cards, 99, True
        )
        finished = agent.handle_screen()

        self.assertIsInstance(finished, ProceedAction)
        self.assertIsNone(agent.pending_potion_hand_filter)

    def test_gamblers_brew_is_saved_when_redraw_pool_is_equally_clogged(self):
        useful = strike()
        useful.uuid = "gambler-keep-attack"
        hand_wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"hand-wound-{index}", is_playable=False,
            )
            for index in range(2)
        ]
        draw_wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"draw-wound-{index}", is_playable=False,
            )
            for index in range(2)
        ]
        game = GameStub(
            [make_monster("Donu", 250, 20)], [useful, *hand_wounds]
        )
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.draw_pile = draw_wounds
        game.discard_pile = []
        gamblers = Potion(
            "GamblersBrew", "Gambler's Brew", True, True, False
        )
        game.get_real_potions = lambda: [gamblers]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            20,
            planned_turn_loss=0,
            planned_action=PlayCardAction(
                card=useful, target_monster=game.monsters[0]
            ),
            planned_combat_end=False,
        )

        self.assertIsNone(action)

    def test_elixir_is_saved_for_ethereal_or_free_self_clearing_statuses(self):
        useful = strike()
        useful.uuid = "elixir-keep-attack"
        dazed = Card(
            "Dazed", "Dazed", CardType.STATUS, CardRarity.SPECIAL,
            cost=-2, uuid="elixir-dazed", is_playable=False,
            ethereal=True,
        )
        free_slimed = Card(
            "Slimed", "Slimed", CardType.STATUS, CardRarity.SPECIAL,
            cost=0, uuid="elixir-free-slimed", is_playable=True,
            exhausts=True,
        )
        game = GameStub(
            [make_monster("Donu", 250, 0)],
            [useful, dazed, free_slimed],
        )
        game.room_type = "MonsterRoomBoss"
        game.act = 3
        game.draw_pile = [defend(), strike()]
        game.discard_pile = []
        elixir = Potion(
            "ElixirPotion", "Elixir", True, True, False
        )
        game.get_real_potions = lambda: [elixir]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            0,
            planned_turn_loss=0,
            planned_action=PlayCardAction(
                card=useful, target_monster=game.monsters[0]
            ),
            planned_combat_end=False,
        )

        self.assertIsNone(action)

    def test_hand_filter_potions_are_saved_when_current_plan_ends_combat(self):
        wounds = [
            Card(
                "Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"terminal-wound-{index}", is_playable=False,
            )
            for index in range(3)
        ]
        finisher = build_card(
            "Immolate", CardType.ATTACK, cost=2, damage=28,
            rarity=CardRarity.RARE,
        )
        game = GameStub(
            [make_monster("Donu", 20, 0)], [finisher, *wounds]
        )
        game.room_type = "MonsterRoomBoss"
        game.draw_pile = [strike(), defend(), strike()]
        game.discard_pile = []

        for potion_id in ("ElixirPotion", "GamblersBrew"):
            with self.subTest(potion_id=potion_id):
                potion = Potion(
                    potion_id, potion_id, True, True, False
                )
                game.get_real_potions = lambda potion=potion: [potion]
                agent = SimpleAgent(PlayerClass.IRONCLAD)
                agent.game = game

                action = agent.use_best_potion(
                    0,
                    planned_turn_loss=0,
                    planned_action=PlayCardAction(
                        card=finisher, target_monster=game.monsters[0]
                    ),
                    planned_combat_end=True,
                )

                self.assertIsNone(action)

    def test_end_turn_burn_makes_an_emergency_potion_eligible(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        skill = Potion("SkillPotion", "Skill Potion", True, True, False)
        game.get_real_potions = lambda: [skill]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.use_best_potion(0)
        self.assertIsInstance(action, PotionAction)
        self.assertEqual(2, agent.combat_planner.last_decision["projected_end_turn_hp_loss"])

    def test_stacked_brutality_turn_boundary_loss_enters_potion_threshold(self):
        game = GameStub([make_monster("attacker", 40, 4)], [strike()])
        game.player.current_hp = 7
        game.current_hp = 7
        game.player.powers = [Power("Brutality", "Brutality", 3)]
        skill = Potion("SkillPotion", "Skill Potion", True, True, False)
        game.get_real_potions = lambda: [skill]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(4)

        self.assertIsInstance(action, PotionAction)
        self.assertEqual(
            3,
            agent.combat_planner.last_decision[
                "projected_next_turn_start_hp_loss"
            ],
        )
        self.assertEqual(
            7, agent.combat_planner.last_decision["projected_hp_loss"]
        )

    def test_block_potion_cannot_prevent_brutality_only_lethal(self):
        game = GameStub([make_monster("idle", 40, 0)], [strike()])
        game.player.current_hp = 1
        game.current_hp = 1
        game.player.powers = [Power("Brutality", "Brutality", 1)]
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertIsNone(agent.use_best_potion(0))

    def test_ghost_in_a_jar_scores_only_marginal_hp_mitigation(self):
        game = GameStub([make_monster("attacker", 40, 20)], [strike()])
        game.player.block = 15
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [ghost]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsNone(agent.use_best_potion(20))

    def test_block_potion_preserves_buffer_across_burn_then_attack(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 40, 20)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.use_best_potion(20)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertEqual(
            20, agent.combat_planner.last_decision["projected_hp_loss"]
        )

    def test_ghost_uses_one_buffer_chain_for_burn_then_attack(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 40, 20)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        game.player.powers = [Power("Buffer", "Buffer", 1)]
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [ghost]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(20)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, ghost)
        self.assertEqual(
            20, agent.combat_planner.last_decision["projected_hp_loss"]
        )

    def test_block_potion_clears_threshold_for_small_exact_lethal(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)

    def test_sacred_bark_doubles_block_potion_in_survival_projection(self):
        game = GameStub([make_monster("attacker", 80, 26)], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.relics = [
            Relic("Orichalcum", "Orichalcum"),
            Relic("Sacred Bark", "Sacred Bark"),
        ]
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.use_best_potion(26)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)

    def test_ghost_clears_threshold_for_small_exact_lethal(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [ghost]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, ghost)

    def test_equal_lifesavers_spend_lower_value_block_before_ghost(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [ghost, block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)

    def test_block_and_ghost_pair_can_rescue_ordered_status_lethal(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [ghost, block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertEqual(
            "potion_pair_survival",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            1, agent.combat_planner.last_decision["protected_pair_hp_loss"]
        )

    def test_pair_search_does_not_spend_block_when_ghost_alone_saves(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        wound = Card("Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [regret, wound])
        game.player.current_hp = 2
        game.current_hp = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [block, ghost]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, ghost)
        self.assertEqual(
            "potion_value_threshold",
            agent.combat_planner.last_decision["reason"],
        )

    def test_smoke_bomb_single_escape_precedes_two_potion_survival(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 80, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        smoke = Potion("SmokeBomb", "Smoke Bomb", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [block, ghost, smoke]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, smoke)

    def test_smoke_bomb_escapes_lethal_elite_encounter(self):
        """Seed 6110959102265198865 died with a legal Elite escape."""

        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub(
            [make_monster("Gremlin Leader", 61, 0)], [regret, burn]
        )
        game.room_type = "MonsterRoomElite"
        game.player.current_hp = 9
        game.current_hp = 9
        smoke = Potion("SmokeBomb", "Smoke Bomb", True, True, True)
        game.get_real_potions = lambda: [smoke]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(61, planned_turn_loss=61)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, smoke)
        self.assertIs(action.target_monster, game.monsters[0])

    def test_smoke_bomb_escapes_when_only_attack_is_random_spiker_suicide(self):
        spikers = [
            make_monster(f"Spiker-{index}", 40, 7, index=index)
            for index in range(2)
        ]
        for index, spiker in enumerate(spikers):
            spiker.powers = [Power("Thorns", "Thorns", 5 + index * 2)]
        boomerang = build_card(
            "Sword Boomerang",
            CardType.ATTACK,
            cost=1,
            damage=9,
            upgrades=1,
        )
        boomerang.has_target = False
        game = GameStub(spikers, [boomerang])
        game.player.current_hp = 7
        game.player.energy = 1
        game.current_hp = 7
        smoke = Potion("SmokeBomb", "Smoke Bomb", True, True, True)
        game.get_real_potions = lambda: [smoke]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, smoke)
        self.assertIn(action.target_monster, spikers)

    def test_fire_potion_kill_precedes_two_potion_survival(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("target", 20, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        fire = Potion("FirePotion", "Fire Potion", True, True, True)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [block, ghost, fire]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, fire)
        self.assertIs(action.target_monster, game.monsters[0])
        self.assertEqual(
            20,
            agent.combat_planner.last_decision[
                "potion_immediate_enemy_hp_loss"
            ],
        )

    def test_explosive_potion_exposes_exact_multi_enemy_damage(self):
        monsters = [
            make_monster("left", 30, 0),
            make_monster("right", 40, 0),
        ]
        game = GameStub(monsters, [strike()])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertEqual(
            20,
            agent._direct_damage_potion_hp_loss("ExplosivePotion", None),
        )

    def test_explosive_potion_exposes_bounds_through_hidden_invincible_cap(self):
        heart = make_monster("CorruptHeart", 300, 0)
        heart.powers = [Power("InvinciblePower", "Invincible", 200)]
        game = GameStub([heart], [strike()])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertIsNone(
            agent._direct_damage_potion_hp_loss("ExplosivePotion", None)
        )
        self.assertEqual(
            {"minimum": 0, "maximum": 10},
            agent._direct_damage_potion_hp_loss_bounds(
                "ExplosivePotion", None
            ),
        )

    def test_fire_potion_does_not_claim_awakened_one_first_phase_kill(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        awakened = make_monster("Awakened One", 20, 0)
        awakened.powers = [Power("Unawakened", "Unawakened", 1)]
        game = GameStub([awakened], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        fire = Potion("FirePotion", "Fire Potion", True, True, True)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [fire, block, ghost]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertEqual(
            "potion_pair_survival",
            agent.combat_planner.last_decision["reason"],
        )

    def test_blood_potion_single_heal_precedes_two_potion_survival(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 80, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        blood = Potion("BloodPotion", "Blood Potion", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [block, ghost, blood]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, blood)

    def test_blood_potion_replans_orb_walker_lethal_turn_before_card(self):
        enemy = make_monster("Orb Walker", 29, 22)
        enemy.powers = [
            Power("Generic Strength Up Power", "Scaling", 3),
            Power("Strength", "Strength", 12),
        ]
        cheap_defend = build_card(
            "Defend_R", CardType.SKILL, cost=1, block=5,
            rarity=CardRarity.BASIC,
        )
        cheap_defend.uuid = "orb-walker-cheap-defend"
        expensive_defend = build_card(
            "Defend_R", CardType.SKILL, cost=3, block=5,
            rarity=CardRarity.BASIC,
        )
        expensive_defend.uuid = "orb-walker-expensive-defend"
        expensive_strike = build_card(
            "Strike_R", CardType.ATTACK, cost=3, damage=14,
            rarity=CardRarity.BASIC,
        )
        expensive_strike.uuid = "orb-walker-expensive-strike"
        thunderclap = build_card(
            "Thunderclap", CardType.ATTACK, cost=3, damage=3,
            rarity=CardRarity.COMMON,
        )
        thunderclap.uuid = "orb-walker-thunderclap"
        burns = [
            Card(
                "Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL,
                cost=-2, uuid=f"orb-walker-burn-{index}",
                is_playable=False,
            )
            for index in range(3)
        ]
        game = GameStub(
            [enemy], [
                cheap_defend,
                thunderclap,
                expensive_strike,
                expensive_defend,
                *burns,
            ]
        )
        game.act = 3
        game.floor = 35
        game.turn = 5
        game.player.current_hp = game.current_hp = 20
        game.player.max_hp = game.max_hp = 40
        game.player.energy = 3
        game.player.powers = [
            Power("Confusion", "Confusion", -1),
            Power("Strength", "Strength", 8),
            Power("Demon Form", "Demon Form", 3),
        ]
        blood = Potion(
            "BloodPotion", "Blood Potion", True, True, False
        )
        owned_potions = [blood]
        game.get_real_potions = lambda: list(owned_potions)
        self._enable_combat_dispatch(game)

        baseline_agent = SimpleAgent(PlayerClass.IRONCLAD)
        baseline_action = baseline_agent.combat_planner.choose_card_action(
            game
        )
        self.assertIsInstance(baseline_action, PlayCardAction)
        self.assertGreaterEqual(
            baseline_agent.combat_planner.last_decision["search"][
                "actual_loss"
            ],
            game.player.current_hp,
        )

        agent = SimpleAgent(PlayerClass.IRONCLAD)
        potion_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(potion_action, PotionAction)
        self.assertIs(potion_action.potion, blood)
        decision = agent.combat_planner.last_decision
        self.assertEqual("potion_healing_replan_survival", decision["reason"])
        self.assertEqual(8, decision["potion_healing_hp_gain"])
        self.assertEqual(28, decision["potion_post_heal_hp"])
        self.assertEqual(23, decision["potion_post_heal_planned_loss"])
        self.assertTrue(decision["potion_post_heal_survives"])
        self.assertEqual(
            "Defend_R", decision["potion_post_heal_first_card_id"]
        )

        # Simulate the next authoritative CommunicationMod frame after the
        # potion is removed and healing is applied.  The ordinary dispatcher
        # must now execute the defensive re-plan rather than the old Strike.
        owned_potions.clear()
        game.player.current_hp = game.current_hp = 28
        agent.confirm_potion_use(game)
        card_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(card_action, PlayCardAction)
        self.assertIs(card_action.card, cheap_defend)
        self.assertEqual(
            23, agent.combat_planner.last_decision["search"]["actual_loss"]
        )
        self.assertLess(
            agent.combat_planner.last_decision["search"]["actual_loss"],
            game.player.current_hp,
        )

    def test_magic_flower_rounds_blood_healing_before_survival_replan(self):
        enemy = make_monster("attacker", 80, 41)
        defend_card = build_card(
            "Defend_R", CardType.SKILL, cost=1, block=5,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([enemy], [defend_card])
        game.player.current_hp = game.current_hp = 23
        game.player.max_hp = game.max_hp = 45
        game.player.energy = 1
        game.relics = [Relic("Magic Flower", "Magic Flower")]
        blood = Potion(
            "BloodPotion", "Blood Potion", True, True, False
        )
        game.get_real_potions = lambda: [blood]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertEqual(14, agent._immediate_potion_hp_gain("bloodpotion"))
        action = agent.use_best_potion(36)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, blood)
        self.assertEqual(
            "potion_healing_replan_survival",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            37, agent.combat_planner.last_decision["potion_post_heal_hp"]
        )
        self.assertEqual(
            36,
            agent.combat_planner.last_decision[
                "potion_post_heal_planned_loss"
            ],
        )

    def test_healing_replan_preserves_normality_resolution_count(self):
        enemy = make_monster("attacker", 80, 34)
        first = defend()
        first.uuid = "normality-defend-1"
        second = defend()
        second.uuid = "normality-defend-2"
        normality = Card(
            "Normality",
            "Normality",
            CardType.CURSE,
            CardRarity.CURSE,
            cost=-2,
            uuid="normality-curse",
            has_target=False,
            is_playable=False,
        )
        game = GameStub([enemy], [first, second, normality])
        game.player.current_hp = game.current_hp = 21
        game.player.max_hp = game.max_hp = 40
        game.player.energy = 2
        game.turn = 3
        blood = Potion(
            "BloodPotion", "Blood Potion", True, True, False
        )
        game.get_real_potions = lambda: [blood]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        planner = agent.combat_planner
        planner._card_play_turn_key = planner._combat_turn_key(game)
        planner._confirmed_cards_played = 2
        planner._confirmed_card_resolutions = 2

        baseline_action = planner.choose_card_action(game)
        baseline_loss = planner.last_decision["search"]["actual_loss"]
        analysis = agent._hypothetical_healing_potion_analysis(
            "bloodpotion"
        )

        self.assertIsInstance(baseline_action, PlayCardAction)
        self.assertEqual(29, baseline_loss)
        self.assertEqual(29, analysis["post_potion_hp"])
        self.assertEqual(29, analysis["loss"])
        self.assertFalse(analysis["survives"])
        self.assertEqual(1, len(analysis["planned_card_ids"]))
        self.assertIsNone(
            agent.use_best_potion(
                34,
                planned_turn_loss=baseline_loss,
                planned_action=baseline_action,
            )
        )

    def test_red_skull_crossing_cannot_fabricate_healing_potion_kill(self):
        target = make_monster("target", 12, 30, index=0)
        other = make_monster("other", 80, 14, index=1)
        attack = build_card(
            "Strike_R", CardType.ATTACK, cost=1, damage=12,
            rarity=CardRarity.BASIC,
        )
        attack.base_damage = 9
        game = GameStub([target, other], [attack])
        game.player.current_hp = game.current_hp = 13
        game.player.max_hp = game.max_hp = 40
        game.player.energy = 1
        game.relics = [Relic("Red Skull", "Red Skull")]
        blood = Potion(
            "BloodPotion", "Blood Potion", True, True, False
        )
        block = Potion(
            "BlockPotion", "Block Potion", True, True, False
        )
        game.get_real_potions = lambda: [blood, block]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        baseline_action = agent.combat_planner.choose_card_action(game)
        baseline_loss = agent.combat_planner.last_decision["search"][
            "actual_loss"
        ]

        self.assertEqual(14, baseline_loss)
        blocked = agent._hypothetical_healing_potion_analysis(
            "bloodpotion"
        )
        self.assertTrue(blocked["blocked"])
        self.assertEqual(
            "red_skull_threshold_transition", blocked["blocked_reason"]
        )
        action = agent.use_best_potion(
            44,
            planned_turn_loss=baseline_loss,
            planned_action=baseline_action,
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertNotEqual(
            "potion_healing_replan_survival",
            agent.combat_planner.last_decision["reason"],
        )

    def test_toy_heal_crossing_red_skull_cannot_fabricate_block_save(self):
        target = make_monster("target", 12, 30, index=0)
        other = make_monster("other", 80, 17, index=1)
        attack = build_card(
            "Strike_R", CardType.ATTACK, cost=1, damage=12,
            rarity=CardRarity.BASIC,
        )
        # The live damage includes Red Skull's +3 Strength.  Toy healing
        # after Block Potion crosses half HP and removes that Strength before
        # this card can be played, so carrying damage=12 into a hypothetical
        # re-plan would fabricate a kill on the first monster.
        attack.base_damage = 9
        game = GameStub([target, other], [attack])
        game.player.current_hp = game.current_hp = 16
        game.player.max_hp = game.max_hp = 40
        game.player.energy = 1
        game.relics = [
            Relic("Red Skull", "Red Skull"),
            Relic("Toy Ornithopter", "Toy Ornithopter"),
        ]
        block = Potion(
            "BlockPotion", "Block Potion", True, True, False
        )
        smoke = Potion(
            "SmokeBomb", "Smoke Bomb", True, True, False
        )
        game.get_real_potions = lambda: [block, smoke]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        baseline_action = agent.combat_planner.choose_card_action(game)
        baseline_loss = agent.combat_planner.last_decision["search"][
            "actual_loss"
        ]

        self.assertIsInstance(baseline_action, PlayCardAction)
        self.assertIs(baseline_action.card, attack)
        self.assertEqual(17, baseline_loss)
        self.assertTrue(
            agent._potion_changes_red_skull_state(("blockpotion",))
        )
        self.assertIsNone(
            agent._hypothetical_defensive_potion_loss(("blockpotion",))
        )

        action = agent.use_best_potion(
            47,
            planned_turn_loss=baseline_loss,
            planned_action=baseline_action,
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, smoke)

    def test_ordered_blood_and_fruit_combo_can_rescue_lethal_turn(self):
        game = GameStub([make_monster("attacker", 80, 79)], [])
        game.player.current_hp = game.current_hp = 60
        game.player.max_hp = game.max_hp = 75
        blood = Potion(
            "BloodPotion", "Blood Potion", True, True, False
        )
        fruit = Potion(
            "FruitJuice", "Fruit Juice", True, True, False
        )
        owned_potions = [blood, fruit]
        game.get_real_potions = lambda: list(owned_potions)
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertFalse(
            agent._hypothetical_healing_potion_analysis(
                "bloodpotion"
            )["survives"]
        )
        self.assertFalse(
            agent._hypothetical_healing_potion_analysis(
                "fruitjuice"
            )["survives"]
        )
        combined = agent._hypothetical_healing_potion_analysis(
            ("bloodpotion", "fruitjuice")
        )
        self.assertEqual(80, combined["post_potion_hp"])
        self.assertEqual(80, combined["post_potion_max_hp"])
        self.assertEqual(79, combined["loss"])
        self.assertTrue(combined["survives"])

        action = agent.use_best_potion(79, planned_turn_loss=79)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, blood)
        self.assertEqual(
            "potion_healing_combo_survival",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            ["BloodPotion", "FruitJuice"],
            agent.combat_planner.last_decision["potion_combo"],
        )

        owned_potions.remove(blood)
        game.player.current_hp = game.current_hp = 75
        agent.confirm_potion_use(game)
        second_action = agent.use_best_potion(79, planned_turn_loss=79)

        self.assertIsInstance(second_action, PotionAction)
        self.assertIs(second_action.potion, fruit)
        self.assertEqual(
            "potion_healing_replan_survival",
            agent.combat_planner.last_decision["reason"],
        )
        owned_potions.clear()
        game.player.current_hp = game.current_hp = 80
        game.player.max_hp = game.max_hp = 80
        self.assertEqual(1, game.player.current_hp - 79)

    def test_fruit_juice_single_hp_gain_precedes_two_potion_survival(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 80, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        fruit = Potion("FruitJuice", "Fruit Juice", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [block, ghost, fruit]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, fruit)

    def test_mark_of_bloom_does_not_treat_blood_as_lifesaving(self):
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("attacker", 80, 0)], [regret, burn])
        game.player.current_hp = 2
        game.current_hp = 2
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        blood = Potion("BloodPotion", "Blood Potion", True, True, False)
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        ghost = Potion("GhostInAJar", "Ghost in a Jar", True, True, False)
        game.get_real_potions = lambda: [blood, block, ghost]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)
        self.assertEqual(
            "potion_pair_survival",
            agent.combat_planner.last_decision["reason"],
        )

    def test_two_block_potions_can_rescue_large_attack(self):
        game = GameStub([make_monster("attacker", 80, 32)], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.relics = [Relic("Orichalcum", "Orichalcum")]
        first = Potion("BlockPotion", "Block Potion", True, True, False)
        second = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [first, second]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(32)

        self.assertIsInstance(action, PotionAction)
        self.assertIn(action.potion, (first, second))
        self.assertEqual(
            8, agent.combat_planner.last_decision["protected_pair_hp_loss"]
        )

    def test_three_block_potions_can_rescue_when_two_cannot(self):
        game = GameStub([make_monster("attacker", 80, 50)], [strike()])
        game.player.current_hp = 20
        game.current_hp = 20
        blocks = [
            Potion("BlockPotion", "Block Potion", True, True, False)
            for _ in range(3)
        ]
        game.get_real_potions = lambda: blocks
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(50)

        self.assertIsInstance(action, PotionAction)
        self.assertIn(action.potion, blocks)
        self.assertEqual(
            "potion_combo_survival",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            14, agent.combat_planner.last_decision["protected_combo_hp_loss"]
        )

    def test_toy_ornithopter_can_make_any_usable_potion_lifesaving(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        game.relics = [Relic("Toy Ornithopter", "Toy Ornithopter")]
        ancient = Potion("AncientPotion", "Ancient Potion", True, True, False)
        game.get_real_potions = lambda: [ancient]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.use_best_potion(0)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, ancient)

    def test_end_turn_plan_can_use_block_potion_for_burn_lethal(self):
        burn = Card("Burn", "Burn", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub([make_monster("idle", 40, 0)], [burn])
        game.player.current_hp = 2
        game.current_hp = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, block)

    def test_play_phase_exhausted_keeps_exact_end_search_projection(self):
        game = GameStub([make_monster("attacker", 40, 8)], [])
        game.play_available = False
        game.end_available = True
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.cancel_available = False
        game.get_real_potions = lambda: []
        agent = SimpleAgent(PlayerClass.DEFECT)
        exact = {
            "actual_loss": 3,
            "projected_attack_hp_loss": 3,
            "projected_end_turn_hp_loss": 0,
            "projected_next_turn_start_hp_loss": 0,
            "projected_hp_loss": 3,
            "true_combat_end": False,
            "static_discharge_triggered_hits": 1,
        }

        with patch.object(
            agent.combat_planner, "project_end_turn", return_value=exact
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, EndTurnAction)
        decision = agent.combat_planner.last_decision
        self.assertEqual("play_phase_exhausted", decision["reason"])
        self.assertEqual(3, decision["projected_hp_loss"])
        self.assertEqual(exact, decision["search"])

    def test_defensive_potion_is_saved_when_ordered_plan_ends_combat(self):
        eater = make_monster("Time Eater", 6, 26)
        eater.powers = [Power("Time Warp", "Time Warp", 11)]
        game = GameStub([eater], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.player.energy = 1
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, game.hand[0])

    def test_block_potion_is_saved_when_ordered_block_line_has_zero_loss(self):
        impervious = build_card(
            "Impervious", CardType.SKILL, cost=2, block=30,
            rarity=CardRarity.RARE,
        )
        game = GameStub([make_monster("attacker", 80, 20)], [impervious])
        game.player.current_hp = 10
        game.current_hp = 10
        game.player.energy = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, impervious)

    def test_agent_keeps_safe_high_value_self_damage_plan_at_low_hp(self):
        automaton = make_monster("BronzeAutomaton", 149, 13)
        automaton.max_hp = 300
        automaton.move_hits = 2
        automaton.powers = [Power("Strength", "Strength", 6)]
        hemokinesis = build_card(
            "Hemokinesis", CardType.ATTACK, cost=1, damage=22
        )
        wounds = [
            Card(
                "Wound",
                "Wound",
                CardType.STATUS,
                CardRarity.SPECIAL,
                cost=-2,
                uuid=f"agent-wound-{index}",
                is_playable=False,
            )
            for index in range(3)
        ]
        game = GameStub(
            [automaton], [wounds[0], hemokinesis, *wounds[1:]]
        )
        game.player.current_hp = 5
        game.player.max_hp = 85
        game.player.block = 23
        game.player.energy = 1
        game.player.powers = [Power("Metallicize", "Metallicize", 4)]
        game.current_hp = 5
        game.max_hp = 85
        game.act = 2
        game.floor = 33
        game.turn = 8
        game.room_type = "MonsterRoomBoss"
        fairy = Potion("FairyPotion", "Fairy Potion", False, False, False)
        gamblers = [
            Potion(
                "GamblersBrew",
                f"Gambler's Brew {index}",
                True,
                True,
                False,
            )
            for index in range(2)
        ]
        game.get_real_potions = lambda: [fairy, *gamblers]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, hemokinesis)
        self.assertEqual(
            "ordered_turn_search",
            agent.combat_planner.last_decision["reason"],
        )
        self.assertEqual(
            2,
            agent.combat_planner.last_decision["search"]["actual_loss"],
        )

    def test_block_potion_does_not_claim_it_can_prevent_regret_hp_loss(self):
        impervious = build_card(
            "Impervious", CardType.SKILL, cost=2, block=30,
            rarity=CardRarity.RARE,
        )
        regret = Card("Regret", "Regret", CardType.CURSE, CardRarity.CURSE)
        wound = Card("Wound", "Wound", CardType.STATUS, CardRarity.SPECIAL)
        game = GameStub(
            [make_monster("attacker", 80, 20)],
            [impervious, regret, wound],
        )
        game.player.current_hp = 2
        game.current_hp = 2
        game.player.energy = 2
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIs(action.card, impervious)

    def test_block_potion_fails_closed_for_unquantified_card_plan(self):
        game = GameStub([make_monster("attacker", 80, 20)], [defend(20)])
        game.player.current_hp = 10
        game.current_hp = 10
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        planned = PlayCardAction(card=game.hand[0])

        action = agent.use_best_potion(
            20, planned_turn_loss=None, planned_action=planned
        )

        self.assertIsNone(action)

    def test_all_potions_are_saved_when_passive_kill_ends_combat_safely(self):
        doomed = make_monster("elite", 20, 20, poison=20)
        game = GameStub([doomed], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.room_type = "MonsterRoomElite"
        power = Potion("PowerPotion", "Power Potion", True, True, False)
        game.get_real_potions = lambda: [power]
        game.screen_type = ScreenType.NONE
        game.choice_available = False
        game.proceed_available = False
        game.play_available = True
        game.end_available = True
        game.cancel_available = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, EndTurnAction)

    def test_potion_limit_changes_only_after_authoritative_confirmation(self):
        game = GameStub([make_monster("attacker", 40, 20)], [strike()])
        block = Potion("Block Potion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        turn_key = (game.act, game.floor, game.turn)

        self.assertIsInstance(agent.use_best_potion(20), PotionAction)
        self.assertNotIn(turn_key, agent.potions_used_this_turn)
        # A stale/rejected command is re-decided from authoritative state.
        self.assertIsInstance(agent.use_best_potion(20), PotionAction)
        agent.confirm_potion_use(game)
        self.assertEqual(1, agent.potions_used_this_turn[turn_key])
        self.assertIsNone(agent.use_best_potion(20))

    def test_liquid_memories_is_used_to_recover_lethal_block(self):
        game = GameStub([make_monster("attacker", 80, 20)], [strike()])
        game.player.current_hp = 10
        game.current_hp = 10
        game.discard_pile = [defend(12)]
        potion = Potion("LiquidMemories", "Liquid Memories", True, True, False)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(20)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, potion)

    def test_liquid_memories_is_saved_when_exact_plan_already_loses_zero(self):
        game = GameStub([make_monster("Collector", 100, 30)], [defend(30)])
        game.room_type = "MonsterRoomBoss"
        game.discard_pile = [strike()]
        potion = Potion("LiquidMemories", "Liquid Memories", True, True, False)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            30,
            planned_turn_loss=0,
            planned_action=PlayCardAction(card=game.hand[0]),
        )

        self.assertIsNone(action)

    def test_liquid_memories_grid_uses_bound_tactical_card_not_weakest_card(self):
        basic = strike()
        setup = build_card(
            "Noxious Fumes", CardType.POWER, cost=1,
            rarity=CardRarity.UNCOMMON,
        )
        game = GameStub([make_monster("Collector", 100, 0)], [defend()])
        game.act = 2
        game.floor = 33
        game.turn = 2
        game.screen = GridSelectScreen(
            [basic, setup], [], 1, False, False, False, False, False
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        agent.pending_selection_context = {
            "kind": "liquid_memories",
            "act": 2,
            "floor": 33,
            "turn": 2,
            "card_id": setup.card_id,
            "card_uuid": setup.uuid,
        }

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([setup], action.cards)
        self.assertEqual(
            "grid_liquid_memories_bound_marginal_plan",
            agent.last_noncombat_decision["reason"],
        )
        self.assertIsNotNone(agent.pending_selection_context)
        agent.confirm_card_selection(game, setup.uuid)
        self.assertIsNone(agent.pending_selection_context)

    def test_discard_to_top_grid_recovers_best_card_not_weakest_card(self):
        """Headbutt's exact Grid action is a positive retrieval, not a purge."""

        weak = strike()
        strong = build_card(
            "Bludgeon", CardType.ATTACK, cost=3, damage=32,
            rarity=CardRarity.RARE,
        )
        game = GameStub([make_monster("Cultist", 80, 10)], [weak, strong])
        game.screen = GridSelectScreen(
            [weak, strong], [], 1, False, False, False, False, False
        )
        game.current_action = "DiscardPileToTopOfDeckAction"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([strong], action.cards)
        self.assertEqual(
            "grid_discard_to_top_of_deck_tactical_value",
            agent.last_noncombat_decision["reason"],
        )
        chosen = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["choice_id"] == f"grid:{strong.uuid}"
        )
        self.assertEqual(
            "grid_combat_discard_to_top",
            chosen["consequences"]["operation"],
        )
        self.assertEqual(
            "grid_combat_discard_to_top",
            chosen["consequences"]["future_costs"][0]["operation"],
        )

    def test_combat_hand_retrieval_grids_use_current_turn_tactical_value(self):
        """Hologram and Seek must not use removal scoring on combat Grids."""

        for current_action, expected_reason, expected_operation in (
            (
                "BetterDiscardPileToHandAction",
                "grid_combat_discard_to_hand_tactical_value",
                "grid_combat_discard_to_hand",
            ),
            (
                "BetterDrawPileToHandAction",
                "grid_combat_draw_to_hand_tactical_value",
                "grid_combat_draw_to_hand",
            ),
            (
                "SkillFromDeckToHandAction",
                "grid_combat_draw_to_hand_tactical_value",
                "grid_combat_draw_to_hand",
            ),
        ):
            with self.subTest(current_action=current_action):
                weak = defend(5)
                lethal = build_card(
                    "Ball Lightning", CardType.ATTACK, cost=1, damage=10,
                    rarity=CardRarity.COMMON,
                )
                game = GameStub(
                    [make_monster("TorchHead", 8, 6)], [defend(10)]
                )
                game.player.energy = 1
                game.screen = GridSelectScreen(
                    [weak, lethal], [], 1,
                    False, False, False, False, False,
                )
                game.current_action = current_action
                agent = SimpleAgent(PlayerClass.DEFECT)
                agent.game = game

                action = agent.choose_grid_action()

                self.assertIsInstance(action, CardSelectAction)
                self.assertEqual([lethal], action.cards)
                self.assertEqual(
                    expected_reason,
                    agent.last_noncombat_decision["reason"],
                )
                chosen = next(
                    candidate
                    for candidate in agent.last_noncombat_decision[
                        "candidates"
                    ]
                    if candidate["choice_id"] == f"grid:{lethal.uuid}"
                )
                self.assertEqual(
                    expected_operation,
                    chosen["consequences"]["operation"],
                )

    def test_seek_grid_rejects_target_unplayable_with_remaining_energy(self):
        echo_form = build_card(
            "Echo Form", CardType.POWER, cost=3,
            rarity=CardRarity.RARE,
        )
        echo_form.uuid = "late-seek-echo"
        claw = build_card(
            "Claw", CardType.ATTACK, cost=0, damage=3,
            rarity=CardRarity.COMMON,
        )
        claw.uuid = "late-seek-claw"
        game = GameStub(
            [make_monster("Louse", 3, 6)], [defend(5)]
        )
        game.player.energy = 0
        game.screen = GridSelectScreen(
            [echo_form, claw], [], 1,
            False, False, False, False, False,
        )
        game.current_action = "BetterDrawPileToHandAction"
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([claw], action.cards)
        rows = {
            row["choice_id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(
            -1000000.0, rows["grid:late-seek-echo"]["score"]
        )

    def test_all_bottled_relic_grids_use_typed_opening_value(self):
        cases = (
            (
                "Bottled Flame", "bottle_attack", "ATTACK",
                strike(),
                build_card(
                    "Bludgeon", CardType.ATTACK, cost=3, damage=32,
                    rarity=CardRarity.RARE,
                ),
            ),
            (
                "Bottled Lightning", "bottle_skill", "SKILL",
                defend(5),
                build_card(
                    "Impervious", CardType.SKILL, cost=2, block=30,
                    rarity=CardRarity.RARE,
                ),
            ),
            (
                "Bottled Tornado", "bottle_power", "POWER",
                build_card(
                    "Metallicize", CardType.POWER, cost=1,
                    rarity=CardRarity.UNCOMMON,
                ),
                build_card(
                    "Inflame", CardType.POWER, cost=1,
                    rarity=CardRarity.RARE,
                ),
            ),
        )
        for relic_id, operation, card_type, weak, strong in cases:
            with self.subTest(relic_id=relic_id):
                game = GameStub([], [weak, strong])
                game.screen = GridSelectScreen(
                    [weak, strong], [], 1, False, False, False, False, False,
                    parent_choice_context={
                        "authority": "accepted_protocol_choice",
                        "parent_phase": "COMBAT_REWARD",
                        "relic_id": relic_id,
                        "operation": operation,
                        "card_type": card_type,
                        "select_count": 1,
                    },
                )
                agent = SimpleAgent(PlayerClass.IRONCLAD)
                agent.game = game

                action = agent.choose_grid_action()

                self.assertEqual([strong], action.cards)
                self.assertEqual(
                    (
                        "grid_bottled_lightning_opening_value"
                        if operation == "bottle_skill"
                        else "grid_bottled_relic_opening_value"
                    ),
                    agent.last_noncombat_decision["reason"],
                )
                selected = next(
                    candidate
                    for candidate in agent.last_noncombat_decision["candidates"]
                    if candidate["choice_id"] == f"grid:{strong.uuid}"
                )
                self.assertEqual(
                    f"grid_{operation}",
                    selected["consequences"]["operation"],
                )

    def test_upgrade_role_gap_is_marginal_not_full_card_value(self):
        """A block shortage must not turn starter Defend into a premium fire."""

        cases = (
            (
                "Bash",
                build_card(
                    "Bash", CardType.ATTACK, cost=2, damage=8,
                    rarity=CardRarity.BASIC,
                ),
                [
                    build_card("Headbutt", CardType.ATTACK, damage=9),
                    build_card("Whirlwind", CardType.ATTACK, damage=5),
                    build_card("Flex"),
                ],
            ),
            (
                "Immolate",
                build_card(
                    "Immolate", CardType.ATTACK, cost=2, damage=21,
                    rarity=CardRarity.RARE,
                ),
                [
                    build_card("Cleave", CardType.ATTACK, damage=8),
                    build_card("Perfected Strike", CardType.ATTACK, damage=6),
                    build_card("Pommel Strike", CardType.ATTACK, damage=9),
                ],
            ),
            (
                "Shockwave",
                build_card("Shockwave", cost=2, rarity=CardRarity.UNCOMMON),
                [
                    build_card("Anger", CardType.ATTACK, cost=0, damage=6),
                    build_card("Heavy Blade", CardType.ATTACK, damage=14),
                    build_card("Sever Soul", CardType.ATTACK, damage=16),
                ],
            ),
        )
        for label, premium, supporting_cards in cases:
            with self.subTest(premium=label):
                basic_defend = defend()
                deck = [
                    premium,
                    basic_defend,
                    *supporting_cards,
                    strike(),
                    strike(),
                    defend(),
                    defend(),
                    defend(),
                ]
                game = GameStub([], deck)
                game.in_combat = False
                game.screen = GridSelectScreen(
                    [basic_defend, premium], [], 1, False, False,
                    True, False, False,
                )
                agent = SimpleAgent(PlayerClass.IRONCLAD)
                agent.game = game

                action = agent.choose_grid_action()

                self.assertEqual([premium], action.cards)
                defend_parts = agent._upgrade_score_parts(basic_defend)
                self.assertLessEqual(defend_parts["deck_role_demand"], 2.0)
                self.assertEqual(
                    "grid_upgrade_marginal_value",
                    agent.last_noncombat_decision["reason"],
                )

    def test_slime_boss_upgrade_prefers_whirlwind_over_reckless_charge(self):
        reckless = build_card(
            "Reckless Charge", CardType.ATTACK, cost=0, damage=7,
        )
        whirlwind = build_card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5,
        )
        deck = [
            build_card(
                "Barricade", CardType.POWER, cost=3,
                rarity=CardRarity.RARE,
            ),
            reckless,
            build_card("Battle Trance", CardType.SKILL, cost=0),
            whirlwind,
            build_card("Whirlwind", CardType.ATTACK, cost=-1, damage=5),
            build_card("Evolve", CardType.POWER, cost=1, upgrades=1),
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            build_card("Shrug It Off", CardType.SKILL, cost=1, block=8),
            *(strike() for _ in range(4)),
            *(defend() for _ in range(4)),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 12
        game.act_boss = "Slime Boss"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        whirlwind_parts = agent._upgrade_score_parts(whirlwind)

        self.assertEqual(
            6.0, whirlwind_parts["boss_matchup_context"]
        )
        self.assertGreater(
            agent._upgrade_score(whirlwind),
            agent._upgrade_score(reckless),
        )

        game.act_boss = "The Guardian"
        self.assertEqual(
            0.0,
            agent._upgrade_score_parts(whirlwind)["boss_matchup_context"],
        )

    def test_speed_potion_is_saved_when_existing_block_makes_loss_zero(self):
        game = GameStub([make_monster("BookOfStabbing", 120, 10)], [defend(5)])
        game.room_type = "MonsterRoomElite"
        game.turn = 2
        game.player.block = 10
        speed = Potion("SpeedPotion", "Speed Potion", True, True, False)
        game.get_real_potions = lambda: [speed]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertNotIsInstance(action, PotionAction)

    def test_speed_potion_uses_exact_plus_five_dexterity_to_prevent_lethal(self):
        game = GameStub([make_monster("BookOfStabbing", 120, 14)], [defend(5)])
        game.room_type = "MonsterRoomElite"
        game.act = 3
        game.turn = 2
        game.player.current_hp = 8
        game.current_hp = 8
        speed = Potion("SpeedPotion", "Speed Potion", True, True, False)
        game.get_real_potions = lambda: [speed]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(speed, action.potion)

    def test_speed_potion_rebuilds_block_from_base_dexterity_then_frail(self):
        frail_defend = build_card(
            "Defend_G", CardType.SKILL, block=5, rarity=CardRarity.BASIC
        )
        # Current display: floor((5 base + 2 Dexterity) * 0.75) == 5.
        frail_defend.block = 5
        game = GameStub(
            [make_monster("BookOfStabbing", 120, 14)], [frail_defend]
        )
        game.player.powers = [
            Power("Dexterity", "Dexterity", 2),
            Power("Frail", "Frail", 1),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        # After Speed: floor((5 + 2 + 5) * 0.75) == 9, so five HP is lost.
        self.assertEqual(5, agent._hypothetical_speed_potion_loss())

    def test_speed_potion_does_not_claim_a_frail_rounding_lethal_escape(self):
        frail_defend = build_card(
            "Defend_G", CardType.SKILL, block=5, rarity=CardRarity.BASIC
        )
        frail_defend.block = 3
        game = GameStub(
            [make_monster("BookOfStabbing", 120, 12)], [frail_defend]
        )
        game.room_type = "MonsterRoomElite"
        game.act = 3
        game.turn = 2
        game.player.current_hp = game.current_hp = 5
        game.player.powers = [Power("Frail", "Frail", 1)]
        speed = Potion("SpeedPotion", "Speed Potion", True, True, False)
        game.get_real_potions = lambda: [speed]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        # Correct post-Speed block is floor((5 + 5) * .75) == 7: the
        # resulting five damage is still lethal, so the potion is not falsely
        # promoted to a lifesaver by treating displayed 3 + 5 as eight block.
        self.assertNotIsInstance(action, PotionAction)

    def test_duplication_potion_uses_exact_profitable_bound_card(self):
        heavy = build_card(
            "Heavy Blade", CardType.ATTACK, cost=1, damage=15,
            rarity=CardRarity.UNCOMMON,
        )
        heavy.uuid = "heavy-bound"
        cold_snap = build_card(
            "Cold Snap", CardType.ATTACK, cost=1, damage=6,
            rarity=CardRarity.COMMON,
        )
        cold_snap.uuid = "cold-snap-distractor"
        game = GameStub(
            [make_monster("Collector", 30, 0)], [cold_snap, heavy]
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 2
        game.player.energy = 1
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False
        )
        game.get_real_potions = lambda: [duplication]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        potion_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(potion_action, PotionAction)
        self.assertTrue(potion_action.use)
        self.assertIs(duplication, potion_action.potion)
        self.assertEqual(
            "duplication_potion",
            agent.pending_selection_context["kind"],
        )
        self.assertEqual(
            "heavy-bound", agent.pending_selection_context["card_uuid"]
        )
        decision = agent.combat_planner.last_decision
        self.assertEqual("potion_value_threshold", decision["reason"])
        self.assertGreater(decision["duplication_marginal_kills"], 0)
        rows = decision["potion_operation_candidates"]
        use = next(row for row in rows if row["operation"] == "use")
        discard = next(row for row in rows if row["operation"] == "discard")
        self.assertTrue(use["selection_eligible"])
        self.assertIsNone(use["veto_reason"])
        self.assertTrue(discard["selection_eligible"])

        # The confirmed post-potion frame exposes DuplicationPower.  The
        # bound action must carry a fresh ordered-search proof for both card
        # resolutions rather than a static doubled damage packet.
        agent.confirm_potion_use(game)
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 1)
        ]
        game.get_real_potions = lambda: []
        card_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(card_action, PlayCardAction)
        self.assertIs(card_action.card, heavy)
        bound_decision = agent.combat_planner.last_decision
        self.assertEqual(
            "duplication_potion_bound_next_card",
            bound_decision["reason"],
        )
        self.assertEqual(
            "bound_fresh_turn_search",
            bound_decision["ordered_copy_evidence"],
        )
        self.assertEqual(
            2,
            bound_decision["search"]["first_action_resolution_count"],
        )
        self.assertEqual(
            30,
            bound_decision["search"]["first_action_enemy_hp_loss"],
        )

    def test_duplication_binding_does_not_claim_dynamic_copy_as_exact(self):
        rampage = build_card(
            "Rampage", CardType.ATTACK, cost=1, damage=8,
            rarity=CardRarity.UNCOMMON,
        )
        rampage.uuid = "rampage-dynamic"
        target = make_monster("Collector", 100, 0)
        game = GameStub([target], [rampage])
        game.player.energy = 1
        game.player.powers = [
            Power("DuplicationPower", "Duplication", 1)
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        decision = agent._bound_duplication_card_decision(
            PlayCardAction(card=rampage, target_monster=target)
        )

        self.assertEqual(
            "duplication_potion_bound_next_card", decision["reason"]
        )
        self.assertEqual("unavailable", decision["ordered_copy_evidence"])
        self.assertNotIn("search", decision)

    def test_duplication_binding_cannot_leak_into_same_floor_second_combat(self):
        bound = build_card(
            "Heavy Blade", CardType.ATTACK, damage=15,
            rarity=CardRarity.UNCOMMON,
        )
        bound.uuid = "same-deck-card-uuid"
        first_game = GameStub(
            [make_monster("GremlinNob", 82, 14)], [bound]
        )
        first_game.act = 2
        first_game.floor = 28
        first_game.turn = 1
        first_game.room_type = "MonsterRoomElite"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = first_game
        agent._combat_epoch = 1
        agent.pending_selection_context = {
            "kind": "duplication_potion",
            "act": 2,
            "floor": 28,
            "turn": 1,
            "combat_identity": agent._combat_identity(first_game),
            "card_id": bound.card_id,
            "card_uuid": bound.uuid,
            "target_index": 0,
            "confirmed": True,
        }

        refreshed_bound = build_card(
            "Heavy Blade", CardType.ATTACK, damage=15,
            rarity=CardRarity.UNCOMMON,
        )
        refreshed_bound.uuid = "same-deck-card-uuid"
        second_game = GameStub(
            [make_monster("Pointy", 34, 9)], [refreshed_bound]
        )
        second_game.act = 2
        second_game.floor = 28
        second_game.turn = 1
        second_game.room_type = "MonsterRoomElite"
        agent.game = second_game

        self.assertIsNone(agent._bound_duplication_card_action())
        self.assertIsNone(agent.pending_selection_context)

    def test_same_floor_new_combat_resets_focus_and_reactive_progress(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent._combat_epoch = 1
        agent._combat_was_active = True
        agent.combat_planner.focus_key = ("old-enemy", 0)
        agent.combat_planner.combat_key = (2, 28, "MonsterRoomElite")
        agent.combat_planner._reactive_progress_pending = (
            (2, 28, "MonsterRoomElite", 1),
            "same-deck-card-uuid",
        )
        agent.combat_planner._card_play_turn_key = (
            2, 28, "MonsterRoomElite", 1,
        )
        agent.combat_planner._confirmed_cards_played = 2
        agent.combat_planner._confirmed_card_resolutions = 2
        agent.combat_planner._card_play_pending_uuid = (
            "same-deck-card-uuid"
        )
        agent.combat_planner._card_play_pending_resolutions = 2

        between_fights = GameStub([], [])
        between_fights.in_combat = False
        between_fights.screen_type = ScreenType.NONE
        between_fights.choice_available = False
        between_fights.proceed_available = True
        between_fights.play_available = False
        between_fights.end_available = False
        between_fights.cancel_available = False
        self.assertIsInstance(
            agent.get_next_action_in_game(between_fights), ProceedAction
        )

        second_game = GameStub(
            [make_monster("GremlinNob", 82, 14)], [strike()]
        )
        second_game.act = 2
        second_game.floor = 28
        second_game.turn = 1
        second_game.room_type = "MonsterRoomElite"
        self._enable_combat_dispatch(second_game)
        with patch.object(
            agent, "get_play_card_action", return_value=EndTurnAction()
        ), patch.object(agent, "use_best_potion", return_value=None):
            agent.get_next_action_in_game(second_game)

        self.assertEqual(2, agent._combat_epoch)
        self.assertIsNone(agent.combat_planner.focus_key)
        self.assertIsNone(agent.combat_planner.combat_key)
        self.assertIsNone(agent.combat_planner._reactive_progress_pending)
        self.assertIsNone(agent.combat_planner._card_play_turn_key)
        self.assertEqual(0, agent.combat_planner._confirmed_cards_played)
        self.assertEqual(
            0, agent.combat_planner._confirmed_card_resolutions
        )
        self.assertIsNone(agent.combat_planner._card_play_pending_uuid)
        self.assertEqual(
            1, agent.combat_planner._card_play_pending_resolutions
        )

    def test_duplication_potion_is_kept_without_marginal_line(self):
        cold_snap = build_card(
            "Cold Snap", CardType.ATTACK, cost=1, damage=6,
            rarity=CardRarity.COMMON,
        )
        cold_snap.uuid = "cold-snap-only"
        basic_defend = defend(5)
        basic_defend.uuid = "defend-distractor"
        game = GameStub(
            [make_monster("Collector", 100, 0)],
            [cold_snap, basic_defend],
        )
        game.room_type = "MonsterRoomBoss"
        game.turn = 2
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False
        )
        game.get_real_potions = lambda: [duplication]
        self._enable_combat_dispatch(game)
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, PlayCardAction)
        self.assertIsNone(agent.pending_selection_context)

    def test_premium_block_potion_is_saved_for_small_nonlethal_gain(self):
        game = GameStub([make_monster("attacker", 80, 15)], [strike()])
        block = Potion("BlockPotion", "Block Potion", True, True, False)
        game.get_real_potions = lambda: [block]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            15,
            planned_turn_loss=12,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_weak_potion_uses_exact_replan_before_hp_reaches_lethal(self):
        attacker = make_monster("Champ", 300, 24)
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.turn = 5
        game.player.current_hp = game.current_hp = 56
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            24,
            planned_turn_loss=24,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, weak)
        self.assertIs(action.target_monster, attacker)
        self.assertEqual(
            18,
            agent.combat_planner.last_decision["potion_protected_hp_loss"],
        )
        self.assertEqual(
            6,
            agent.combat_planner.last_decision["potion_loss_reduction"],
        )

    def test_weak_potion_commits_on_large_boss_loss_before_low_hp(self):
        attacker = make_monster("GenericBoss", 300, 22)
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.turn = 4
        game.player.max_hp = game.max_hp = 83
        game.player.current_hp = game.current_hp = 83
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            22,
            planned_turn_loss=22,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, weak)
        self.assertEqual(
            16,
            agent.combat_planner.last_decision["potion_protected_hp_loss"],
        )
        self.assertTrue(
            agent.combat_planner.last_decision["potion_severe_loss"]
        )
        self.assertFalse(
            agent.combat_planner.last_decision[
                "potion_reserve_breaking_crisis"
            ]
        )

    def test_weak_potion_is_saved_when_exact_plan_has_healthy_reserve(self):
        attacker = make_monster("Champ", 300, 24)
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.turn = 3
        game.player.current_hp = game.current_hp = 66
        game.player.block = 14
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            24,
            planned_turn_loss=10,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_weak_potion_targets_largest_provable_damage_reduction(self):
        small = make_monster("Small", 40, 6, index=0)
        large = make_monster("Large", 100, 24, index=1)
        game = GameStub([small, large], [])
        game.room_type = "MonsterRoomElite"
        game.player.current_hp = game.current_hp = 40
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            30,
            planned_turn_loss=30,
            planned_action=EndTurnAction(),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.target_monster, large)
        self.assertEqual(
            24,
            agent.combat_planner.last_decision["potion_protected_hp_loss"],
        )

    def test_weak_potion_replan_applies_reduction_to_every_attack_hit(self):
        attacker = make_monster("BookOfStabbing", 180, 8)
        attacker.move_hits = 3
        game = GameStub([attacker], [])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertEqual(18, agent._hypothetical_weak_potion_loss(attacker))

    def test_weak_potion_replan_honors_paper_krane(self):
        attacker = make_monster("BookOfStabbing", 180, 10)
        attacker.move_hits = 2
        game = GameStub([attacker], [])
        game.relics = [Relic("Paper Crane", "Paper Krane")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertEqual(12, agent._hypothetical_weak_potion_loss(attacker))

    def test_weak_potion_enumerates_targets_after_planned_kill(self):
        doomed_threat = make_monster("Doomed", 6, 40, index=0)
        surviving_threat = make_monster("Survivor", 100, 24, index=1)
        game = GameStub([doomed_threat, surviving_threat], [strike()])
        game.room_type = "MonsterRoomElite"
        game.player.current_hp = game.current_hp = 40
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(
            24,
            planned_turn_loss=24,
            planned_action=PlayCardAction(
                card=game.hand[0], target_monster=doomed_threat
            ),
        )

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.target_monster, surviving_threat)
        self.assertEqual(
            18,
            agent.combat_planner.last_decision["potion_protected_hp_loss"],
        )

    def test_weak_potion_recognizes_protocol_weakened_power(self):
        attacker = make_monster("Champ", 300, 24)
        attacker.powers = [Power("Weakened", "Weak", 2)]
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 20
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertIsNone(agent.use_best_potion(
            24,
            planned_turn_loss=24,
            planned_action=EndTurnAction(),
        ))

    def test_weak_potion_is_not_claimed_through_artifact(self):
        attacker = make_monster("Champ", 300, 24)
        attacker.powers = [Power("Artifact", "Artifact", 1)]
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 20
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            24,
            planned_turn_loss=24,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_weak_potion_is_not_spent_when_exact_replan_still_dies(self):
        attacker = make_monster("Champ", 300, 48)
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 4
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            48,
            planned_turn_loss=48,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_weak_potion_is_saved_when_ordered_plan_loses_zero(self):
        attacker = make_monster("Champ", 300, 24)
        game = GameStub([attacker], [])
        game.room_type = "MonsterRoomBoss"
        game.player.block = 24
        weak = Potion("WeakPotion", "Weak Potion", True, True, True)
        game.get_real_potions = lambda: [weak]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(
            24,
            planned_turn_loss=0,
            planned_action=EndTurnAction(),
        )

        self.assertIsNone(action)

    def test_weak_and_steel_combo_rescues_lethal_for_every_character(self):
        for player_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            with self.subTest(player_class=player_class):
                attacker = make_monster("Byrd", 13, 5)
                attacker.move_hits = 3
                game = GameStub([attacker], [])
                game.turn = 8
                game.player.current_hp = game.current_hp = 6
                weak = Potion(
                    "WeakPotion", "Weak Potion", True, True, True
                )
                steel = Potion(
                    "EssenceOfSteel", "Essence of Steel",
                    True, True, False,
                )
                game.get_real_potions = lambda: [weak, steel]
                agent = SimpleAgent(player_class)
                agent.game = game

                action = agent.use_best_potion(
                    15,
                    planned_turn_loss=15,
                    planned_action=EndTurnAction(),
                )

                self.assertIsInstance(action, PotionAction)
                self.assertIs(action.potion, weak)
                self.assertIs(action.target_monster, attacker)
                decision = agent.combat_planner.last_decision
                self.assertEqual("potion_pair_survival", decision["reason"])
                self.assertEqual(
                    ["WeakPotion", "EssenceOfSteel"],
                    decision["potion_pair"],
                )
                self.assertEqual(5, decision["protected_pair_hp_loss"])

    def test_steel_is_used_before_nonlethal_loss_breaks_hp_reserve(self):
        for player_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            with self.subTest(player_class=player_class):
                attacker = make_monster("Byrd", 25, 5)
                attacker.move_hits = 2
                game = GameStub([attacker], [])
                game.turn = 7
                game.player.current_hp = game.current_hp = 15
                steel = Potion(
                    "EssenceOfSteel", "Essence of Steel",
                    True, True, False,
                )
                game.get_real_potions = lambda: [steel]
                agent = SimpleAgent(player_class)
                agent.game = game

                action = agent.use_best_potion(
                    10,
                    planned_turn_loss=10,
                    planned_action=EndTurnAction(),
                )

                self.assertIsInstance(action, PotionAction)
                self.assertIs(action.potion, steel)
                decision = agent.combat_planner.last_decision
                self.assertEqual(6, decision["potion_protected_hp_loss"])
                self.assertEqual(4, decision["potion_loss_reduction"])
                self.assertTrue(decision["potion_severe_loss"])

    def test_fear_potion_is_used_on_durable_elite_with_attacks_in_hand(self):
        target = make_monster("BookOfStabbing", 120, 15)
        attacks = [strike(), strike()]
        attacks[1].uuid = "strike-2"
        game = GameStub([target], attacks)
        game.room_type = "MonsterRoomElite"
        potion = Potion("FearPotion", "Fear Potion", True, True, True)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(15)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.target_monster, target)

    def test_steroid_potion_counts_multihit_damage(self):
        target = make_monster("BookOfStabbing", 120, 15)
        riddle = Card(
            "Riddle With Holes", "Riddle With Holes", CardType.ATTACK,
            CardRarity.UNCOMMON, cost=1, uuid="riddle", has_target=True,
            is_playable=True, damage=3,
        )
        game = GameStub([target], [riddle])
        game.room_type = "MonsterRoomElite"
        potion = Potion("SteroidPotion", "Steroid Potion", True, True, False)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsInstance(agent.use_best_potion(15), PotionAction)

    def test_snecko_oil_is_used_as_lethal_hand_refill(self):
        game = GameStub([make_monster("attacker", 80, 30)], [defend(5)])
        game.player.current_hp = 10
        game.current_hp = 10
        potion = Potion("SneckoOil", "Snecko Oil", True, True, False)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsInstance(agent.use_best_potion(30), PotionAction)

    def test_ancient_potion_is_not_wasted_without_incoming_debuff(self):
        game = GameStub([make_monster("Sentry", 40, 20)], [strike()])
        potion = Potion("AncientPotion", "Ancient Potion", True, True, False)
        game.get_real_potions = lambda: [potion]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsNone(agent.use_best_potion(20))

    def test_persistent_potion_is_used_early_against_act2_scaling_normal_fight(self):
        cultist = make_monster("Cultist", 54, 12)
        game = GameStub([cultist], [strike(), defend()])
        game.act = 2
        game.floor = 22
        game.turn = 1
        bronze = Potion("LiquidBronze", "Liquid Bronze", True, True, False)
        game.get_real_potions = lambda: [bronze]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.use_best_potion(12)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, bronze)

    def test_persistent_potion_is_saved_against_trivial_one_hp_elite(self):
        elite = make_monster("GremlinNob", 1, 0)
        game = GameStub([elite], [strike()])
        game.room_type = "MonsterRoomElite"
        bronze = Potion("LiquidBronze", "Liquid Bronze", True, True, False)
        game.get_real_potions = lambda: [bronze]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertIsNone(agent.use_best_potion(0))

    def test_heart_of_iron_is_not_retained_through_long_collector_endgame(self):
        collector = make_monster("TheCollector", 123, 30)
        game = GameStub([collector], [strike()])
        game.act = 2
        game.floor = 33
        game.turn = 15
        game.room_type = "MonsterRoomBoss"
        game.player.current_hp = game.current_hp = 10
        game.player.block = 25
        game.player.energy = 0
        game.hand[0].is_playable = False
        heart = Potion("HeartOfIron", "Heart of Iron", True, True, False)
        game.get_real_potions = lambda: [heart]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.use_best_potion(30)

        self.assertIsInstance(action, PotionAction)
        self.assertIs(action.potion, heart)
        self.assertGreaterEqual(
            agent.combat_planner.last_decision["potion_score"], 20.0
        )

    def test_silent_aoe_does_not_satisfy_single_target_offense_or_bypass_copy_cap(self):
        dagger_spray = Card(
            "Dagger Spray", "Dagger Spray", CardType.ATTACK, CardRarity.COMMON, uuid="spray-1"
        )
        duplicate = Card(
            "Dagger Spray", "Dagger Spray", CardType.ATTACK, CardRarity.COMMON, uuid="spray-2"
        )
        predator = Card(
            "Predator", "Predator", CardType.ATTACK, CardRarity.UNCOMMON, uuid="predator-1"
        )
        game = GameStub([], [dagger_spray])
        game.in_combat = False
        game.screen = CardRewardScreen([duplicate, predator], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.choose_card_reward()
        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(predator, action.card)

    def test_unlisted_synergy_card_defaults_to_one_copy(self):
        flask = Card(
            "Bouncing Flask", "Bouncing Flask", CardType.SKILL, CardRarity.UNCOMMON, uuid="flask-1"
        )
        duplicate = Card(
            "Bouncing Flask", "Bouncing Flask", CardType.SKILL, CardRarity.UNCOMMON, uuid="flask-2"
        )
        corpse_explosion = Card(
            "Corpse Explosion", "Corpse Explosion", CardType.SKILL, CardRarity.RARE, uuid="corpse-1"
        )
        game = GameStub([], [flask])
        game.in_combat = False
        game.screen = CardRewardScreen([duplicate, corpse_explosion], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.choose_card_reward()
        self.assertIs(corpse_explosion, action.card)

    def test_permanent_card_reward_skips_a_negative_card(self):
        owned = Card(
            "Dagger Spray", "Dagger Spray", CardType.ATTACK, CardRarity.COMMON, uuid="spray-owned"
        )
        duplicate = Card(
            "Dagger Spray", "Dagger Spray", CardType.ATTACK, CardRarity.COMMON, uuid="spray-reward"
        )
        game = GameStub([], [owned])
        game.in_combat = False
        game.screen = CardRewardScreen([duplicate], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CancelAction)
        self.assertEqual(
            "card_reward_skip_nonpositive",
            agent.last_noncombat_decision["reason"],
        )
        skip = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "skip"
        )
        self.assertEqual("action:return", skip["choice_id"])
        self.assertEqual("return", skip["action"])
        self.assertEqual(0, skip["consequences"]["deck_size_delta"])

    def test_singing_bowl_declares_current_and_max_hp_gain(self):
        filler = build_card("Negative Reward", CardType.SKILL)
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.screen = CardRewardScreen(
            [filler], can_bowl=True, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(agent, "_card_reward_score", return_value=-5.0):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertTrue(action.bowl)
        bowl = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "bowl"
        )
        self.assertEqual(
            {"hp_delta": 2, "max_hp_delta": 2},
            bowl["consequences"],
        )

    def test_large_late_deck_skips_positive_but_submarginal_filler(self):
        filler = build_card("Late Filler", CardType.SKILL)
        game = GameStub([], [
            build_card(f"deck-{index}", CardType.SKILL)
            for index in range(24)
        ])
        game.in_combat = False
        game.act = 3
        game.floor = 40
        game.screen = CardRewardScreen([filler], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(agent, "_card_reward_score", return_value=6.0):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CancelAction)
        self.assertEqual(
            "card_reward_skip_below_marginal_hurdle",
            agent.last_noncombat_decision["reason"],
        )
        self.assertGreater(agent.last_noncombat_decision["marginal_hurdle"], 6)

    def test_small_act_one_deck_skips_positive_submarginal_filler(self):
        filler = build_card("Act One Filler", CardType.SKILL)
        game = GameStub([], [
            build_card(f"starter-{index}", CardType.SKILL)
            for index in range(10)
        ])
        game.in_combat = False
        game.act = 1
        game.floor = 6
        game.screen = CardRewardScreen(
            [filler], can_bowl=False, can_skip=True,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(agent, "_card_reward_score", return_value=0.616):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CancelAction)
        self.assertEqual(
            "card_reward_skip_below_marginal_hurdle",
            agent.last_noncombat_decision["reason"],
        )
        self.assertGreaterEqual(
            agent.last_noncombat_decision["marginal_hurdle"], 2.0,
        )

    def test_small_act_one_deck_still_takes_strong_frontload(self):
        premium = build_card(
            "Act One Frontload", CardType.ATTACK, damage=18,
        )
        game = GameStub([], [
            build_card(f"starter-{index}", CardType.SKILL)
            for index in range(10)
        ])
        game.in_combat = False
        game.act = 1
        game.floor = 6
        game.screen = CardRewardScreen(
            [premium], can_bowl=False, can_skip=True,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(agent, "_card_reward_score", return_value=12.0):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(action.card, premium)

    def test_large_late_deck_still_takes_premium_marginal_card(self):
        premium = build_card("Premium Engine", CardType.POWER)
        game = GameStub([], [
            build_card(f"deck-{index}", CardType.SKILL)
            for index in range(24)
        ])
        game.in_combat = False
        game.act = 3
        game.floor = 40
        game.screen = CardRewardScreen([premium], can_bowl=False, can_skip=True)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(agent, "_card_reward_score", return_value=20.0):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(premium, action.card)

    def test_large_late_deck_takes_adrenaline_over_bowl(self):
        adrenaline = build_card(
            "Adrenaline", CardType.SKILL, cost=0,
            rarity=CardRarity.RARE,
        )
        deck = (
            [
                build_card("Backflip", CardType.SKILL, block=5)
                for _ in range(4)
            ]
            + [
                build_card(
                    "Strike_G", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(10)
            ]
            + [
                build_card(
                    "Defend_G", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(11)
            ]
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 3
        game.floor = 33
        game.current_hp = game.player.current_hp = 39
        game.max_hp = game.player.max_hp = 70
        game.screen = CardRewardScreen(
            [adrenaline], can_bowl=True, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIs(action.card, adrenaline)
        contract = agent._card_reward_score_contracts[id(adrenaline)]
        self.assertGreater(
            contract["score_inputs"]["self_replacing_acceleration_floor"],
            0.0,
        )

    def test_healthy_offering_beats_singing_bowl(self):
        offering = build_card(
            "Offering", CardType.SKILL, cost=0,
            rarity=CardRarity.RARE,
        )
        deck = (
            [
                build_card("Battle Trance", CardType.SKILL, cost=0)
                for _ in range(4)
            ]
            + [
                build_card(
                    "Strike_R", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(7)
            ]
            + [
                build_card(
                    "Defend_R", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(7)
            ]
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 30
        game.current_hp = game.player.current_hp = 63
        game.max_hp = game.player.max_hp = 95
        game.screen = CardRewardScreen(
            [offering], can_bowl=True, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIs(action.card, offering)
        self.assertGreater(agent._card_reward_score(offering), 8.0)

    def test_low_hp_offering_does_not_receive_acceleration_floor(self):
        offering = build_card(
            "Offering", CardType.SKILL, cost=0,
            rarity=CardRarity.RARE,
        )
        deck = (
            [
                build_card("Battle Trance", CardType.SKILL, cost=0)
                for _ in range(4)
            ]
            + [
                build_card(
                    "Strike_R", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(7)
            ]
            + [
                build_card(
                    "Defend_R", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(7)
            ]
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 30
        game.current_hp = game.player.current_hp = 7
        game.max_hp = game.player.max_hp = 95
        game.screen = CardRewardScreen(
            [offering], can_bowl=True, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertTrue(action.bowl)
        contract = agent._card_reward_score_contracts[id(offering)]
        self.assertNotIn(
            "self_replacing_acceleration_floor", contract["score_inputs"]
        )

    def test_collector_adds_replayable_matchup_value_to_aoe_reward(self):
        whirlwind = build_card(
            "Whirlwind", CardType.ATTACK, cost=-1, damage=5,
            rarity=CardRarity.UNCOMMON,
        )

        collector_game = GameStub([], [strike(), defend()])
        collector_game.in_combat = False
        collector_game.act = 2
        collector_game.act_boss = "TheCollector"
        collector_agent = SimpleAgent(PlayerClass.IRONCLAD)
        collector_agent.game = collector_game
        collector_score = collector_agent._card_reward_score(whirlwind)
        collector_contract = collector_agent._card_reward_score_contracts[
            id(whirlwind)
        ]

        neutral_game = GameStub([], [strike(), defend()])
        neutral_game.in_combat = False
        neutral_game.act = 2
        neutral_game.act_boss = "Champ"
        neutral_agent = SimpleAgent(PlayerClass.IRONCLAD)
        neutral_agent.game = neutral_game
        neutral_score = neutral_agent._card_reward_score(whirlwind)

        self.assertAlmostEqual(10.0, collector_score - neutral_score)
        self.assertAlmostEqual(
            10.0,
            collector_contract["score_inputs"]["boss_matchup_context"],
        )
        self.assertTrue(
            collector_agent._is_universal_plan_candidate(
                whirlwind, {"aoe"}
            )
        )

    def test_slime_boss_reward_takes_evolve_for_guaranteed_slimed_draws(self):
        deck = [
            build_card(
                "Strike_R", CardType.ATTACK, cost=1, damage=6,
                rarity=CardRarity.BASIC,
            )
            for _ in range(5)
        ] + [
            build_card(
                "Defend_R", CardType.SKILL, cost=1, block=5,
                rarity=CardRarity.BASIC,
            )
            for _ in range(4)
        ] + [
            build_card(
                "Bash", CardType.ATTACK, cost=2, damage=10,
                rarity=CardRarity.BASIC, upgrades=1,
            ),
            build_card(
                "Pommel Strike", CardType.ATTACK, cost=1, damage=9,
                rarity=CardRarity.COMMON,
            ),
            build_card(
                "Thunderclap", CardType.ATTACK, cost=1, damage=4,
                rarity=CardRarity.COMMON,
            ),
            build_card(
                "Flame Barrier", CardType.SKILL, cost=2, block=12,
                rarity=CardRarity.UNCOMMON,
            ),
        ]
        twin_strike = build_card(
            "Twin Strike", CardType.ATTACK, cost=1, damage=5,
            rarity=CardRarity.COMMON,
        )
        clash = build_card(
            "Clash", CardType.ATTACK, cost=0, damage=14,
            rarity=CardRarity.COMMON,
        )
        evolve = build_card(
            "Evolve", CardType.POWER, cost=1,
            rarity=CardRarity.UNCOMMON,
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 8
        game.current_hp = 54
        game.max_hp = 74
        game.player.current_hp = 54
        game.player.max_hp = 74
        game.act_boss = "Slime Boss"
        game.screen = CardRewardScreen(
            [twin_strike, clash, evolve], can_bowl=False, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(action.card, evolve)
        contract = agent._card_reward_score_contracts[id(evolve)]
        self.assertAlmostEqual(
            30.0, contract["score_inputs"]["boss_matchup_context"]
        )

    def test_champ_reward_takes_disarm_over_late_deck_skip(self):
        def reward_card(
            card_id, card_type=CardType.ATTACK, *, cost=1, damage=0,
            block=0, rarity=CardRarity.COMMON, upgrades=0,
        ):
            return build_card(
                card_id, card_type, cost=cost, damage=damage, block=block,
                rarity=rarity, upgrades=upgrades,
            )

        deck = [
            reward_card(
                "Strike_R", damage=6, rarity=CardRarity.BASIC
            )
            for _ in range(2)
        ] + [
            reward_card(
                "Defend_R", CardType.SKILL, block=5,
                rarity=CardRarity.BASIC,
            )
            for _ in range(4)
        ] + [
            reward_card(
                "Bash", cost=2, damage=10, rarity=CardRarity.BASIC,
                upgrades=1,
            ),
            reward_card("Anger", cost=0, damage=6),
            reward_card(
                "Sever Soul", cost=2, damage=16,
                rarity=CardRarity.UNCOMMON,
            ),
            reward_card("Shrug It Off", CardType.SKILL, block=8),
            reward_card(
                "Inflame", CardType.POWER, rarity=CardRarity.UNCOMMON,
                upgrades=1,
            ),
            reward_card("Pommel Strike", damage=9),
            reward_card(
                "Flame Barrier", CardType.SKILL, cost=2, block=12,
                rarity=CardRarity.UNCOMMON,
            ),
            reward_card(
                "Battle Trance", CardType.SKILL, cost=0,
                rarity=CardRarity.UNCOMMON,
            ),
            reward_card(
                "Impervious", CardType.SKILL, cost=2, block=30,
                rarity=CardRarity.RARE,
            ),
            reward_card("Thunderclap", damage=4),
            reward_card("Sword Boomerang", damage=3),
            reward_card("Twin Strike", damage=5),
        ]
        rupture = reward_card(
            "Rupture", CardType.POWER, rarity=CardRarity.UNCOMMON
        )
        headbutt = reward_card("Headbutt", damage=9)
        disarm = reward_card(
            "Disarm", CardType.SKILL, rarity=CardRarity.UNCOMMON
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 29
        game.current_hp = 60
        game.max_hp = 92
        game.player.current_hp = 60
        game.player.max_hp = 92
        game.act_boss = "Champ"
        game.screen = CardRewardScreen(
            [rupture, headbutt, disarm], can_bowl=False, can_skip=True
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_card_reward()

        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(action.card, disarm)
        contract = agent._card_reward_score_contracts[id(disarm)]
        self.assertAlmostEqual(
            10.0, contract["score_inputs"]["boss_matchup_context"]
        )

    def test_clash_score_accounts_for_nonattack_playability_constraint(self):
        clash = build_card(
            "Clash", CardType.ATTACK, cost=0, damage=14,
            rarity=CardRarity.COMMON,
        )
        deck = [
            build_card(f"Strike_R_{index}", CardType.ATTACK, damage=6)
            for index in range(5)
        ] + [
            build_card(f"Defend_R_{index}", CardType.SKILL, block=5)
            for index in range(4)
        ] + [build_card("Bash", CardType.ATTACK, cost=2, damage=8)]
        game = GameStub([], deck)
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertLess(agent._card_reward_score(clash), 0)

    def test_wrist_blade_support_raises_blade_dance_marginal_value(self):
        blade_dance = build_card("Blade Dance", CardType.SKILL)
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        without_relic = agent._card_reward_score(blade_dance)
        game.relics = [Relic("WristBlade", "Wrist Blade")]

        self.assertGreater(
            agent._card_reward_score(blade_dance), without_relic + 12
        )

    def test_reaper_recognizes_existing_strength_engine(self):
        reaper = build_card(
            "Reaper", CardType.ATTACK, cost=2, damage=4,
            rarity=CardRarity.RARE,
        )
        game = GameStub([], [
            build_card("Demon Form", CardType.POWER, cost=3),
            build_card("Inflame", CardType.POWER, cost=1),
            strike(), defend(),
        ])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        self.assertGreater(agent._card_reward_score(reaper), 0)

    def test_neow_parses_variable_chinese_hp_cost(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        costly = EventOption(
            "稀有牌", "受到 24 点伤害。获得一张稀有牌。", choice_index=0
        )
        free = EventOption(
            "稀有牌", "获得一张稀有牌。", choice_index=1
        )

        costly.neow_contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": "ONE_RARE_RELIC",
            "drawback_kind": "PERCENT_DAMAGE",
            "parameters": {
                "hp_bonus": 0,
                "cursed": False,
                "drawback_def_kind": "PERCENT_DAMAGE",
            },
        }
        free.neow_contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": "ONE_RARE_RELIC",
            "drawback_kind": "NONE",
            "parameters": {
                "hp_bonus": 0,
                "cursed": False,
                "drawback_def_kind": None,
            },
        }

        self.assertLess(
            agent._neow_option_score(costly), agent._neow_option_score(free)
        )

    def test_neow_paid_card_screen_uses_optional_value_not_guaranteed_gain(self):
        game = GameStub([], [
            build_card(
                "Strike_R", CardType.ATTACK, cost=1, damage=6,
                rarity=CardRarity.BASIC,
            ),
            build_card(
                "Defend_R", CardType.SKILL, cost=1, block=5,
                rarity=CardRarity.BASIC,
            ),
        ])
        game.in_combat = False
        game.floor = 0
        game.current_hp = 80
        game.max_hp = 80
        game.gold = 99
        game.act_boss = "Slime Boss"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        def neow_option(index, reward, drawback="NONE"):
            option = EventOption(
                reward, reward, choice_index=index,
                original_button_index=index,
            )
            parameters = {
                "hp_bonus": 8,
                "cursed": False,
            }
            if drawback != "NONE":
                parameters["drawback_def_kind"] = drawback
            option.neow_contract = {
                "contract_version": 1,
                "contract_kind": "NEOW_REWARD",
                "reward_kind": reward,
                "drawback_kind": drawback,
                "parameters": parameters,
            }
            return option

        options = [
            neow_option(0, "THREE_CARDS"),
            neow_option(1, "THREE_SMALL_POTIONS"),
            neow_option(2, "RANDOM_COLORLESS_2", "PERCENT_DAMAGE"),
            neow_option(3, "BOSS_RELIC"),
        ]
        game.screen = EventScreen("Neow", "Neow Event", "")
        game.screen.options = options

        action = agent.choose_event_action(options)
        scores = {
            row["id"]: row["score"]
            for row in agent.last_noncombat_decision["candidates"]
        }

        self.assertEqual(1, action.choice_index)
        self.assertLess(scores["neow:2"], 0)
        self.assertGreater(scores["neow:1"], scores["neow:2"])
        self.assertTrue(
            agent.last_noncombat_decision["typed_contract_complete"]
        )

    def test_neow_untyped_option_cannot_beat_typed_negative_option(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = 80
        game.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        malformed = EventOption(
            "unknown", "unknown", choice_index=0,
            original_button_index=0,
        )
        costly = EventOption(
            "rare colorless", "rare colorless", choice_index=1,
            original_button_index=1,
        )
        costly.neow_contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": "RANDOM_COLORLESS_2",
            "drawback_kind": "PERCENT_DAMAGE",
            "parameters": {
                "hp_bonus": 0,
                "cursed": False,
                "drawback_def_kind": "PERCENT_DAMAGE",
            },
        }
        game.screen = EventScreen("Neow", "Neow Event", "")
        game.screen.options = [malformed, costly]

        action = agent.choose_event_action(game.screen.options)
        candidates = {
            row["id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }

        self.assertEqual(1, action.choice_index)
        self.assertFalse(candidates["neow:0"]["selection_eligible"])
        self.assertTrue(candidates["neow:1"]["selection_eligible"])
        self.assertLess(candidates["neow:1"]["score"], 0)
        self.assertFalse(
            agent.last_noncombat_decision["typed_contract_complete"]
        )

    def test_neow_typed_consequence_matches_resource_and_followup_contract(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = 80
        game.max_hp = 80
        game.gold = 99
        game.screen = SimpleNamespace(event_id="Neow Event")
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": "REMOVE_TWO",
            "drawback_kind": "TEN_PERCENT_HP_LOSS",
            "parameters": {
                "hp_bonus": 8,
                "cursed": False,
                "drawback_def_kind": "TEN_PERCENT_HP_LOSS",
            },
        }

        consequence = agent._neow_typed_consequences(contract)

        self.assertEqual("Neow Event", consequence["event_id"])
        self.assertEqual(-8, consequence["hp_delta"])
        self.assertEqual(-8, consequence["max_hp_delta"])
        self.assertEqual(
            [{
                "kind": "neow_grid_selection",
                "domain": "current_deck",
                "operation": "remove",
                "select_count": 2,
                "selection_mode": "player_choice",
            }],
            consequence["future_costs"],
        )
        self.assertEqual(
            {"gold": 0, "hp": 8, "max_hp": 8},
            consequence["current_cost"],
        )

        rare_contract = {**contract,
            "reward_kind": "THREE_RARE_CARDS",
            "drawback_kind": "NONE",
            "parameters": {
                "hp_bonus": 8, "cursed": False,
                "drawback_def_kind": None,
            },
        }
        rare = agent._neow_typed_consequences(rare_contract)
        self.assertEqual([{
            "kind": "neow_card_reward",
            "domain": "character_cards",
            "rarities": ["RARE"],
            "visible_count": 3,
            "choose_count": 1,
            "selection_mode": "player_choice",
        }], rare["future_costs"])

    def test_neow_parent_context_drives_ambiguous_grid_removal(self):
        weak = strike()
        strong = build_card("Demon Form", CardType.POWER, cost=3)
        game = GameStub([], [weak, strong])
        game.in_combat = False
        game.screen = GridSelectScreen(
            cards=[weak, strong], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=False,
            parent_choice_context={
                "authority": "accepted_protocol_choice",
                "parent_phase": "NEOW",
                "operation": "remove",
                "select_count": 1,
            },
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertIs(weak, action.cards[0])
        self.assertEqual(
            "grid_purge",
            agent.last_noncombat_decision["candidates"][0][
                "consequences"
            ]["operation"],
        )

    def test_duplicator_parent_context_drives_positive_grid_selection(self):
        weak = strike()
        strong = build_card("Demon Form", CardType.POWER, cost=3)
        game = GameStub([], [weak, strong])
        game.in_combat = False
        game.screen = GridSelectScreen(
            cards=[weak, strong], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=False,
            parent_choice_context={
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "operation": "duplicate",
                "select_count": 1,
            },
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertIs(strong, action.cards[0])
        self.assertTrue(all(
            candidate["consequences"]["operation"] == "grid_duplicate"
            for candidate in agent.last_noncombat_decision["candidates"]
        ))

    def test_boss_choker_beats_unsupported_wrist_blade_in_low_draw_deck(self):
        deck = [strike(), defend()]
        for index in range(15):
            card = strike() if index % 2 == 0 else defend()
            card.uuid = f"starter-{index}"
            deck.append(card)
        game = GameStub([], deck)
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertGreater(
            agent._boss_relic_score(Relic("Velvet Choker", "Velvet Choker")),
            agent._boss_relic_score(Relic("WristBlade", "Wrist Blade")),
        )

    def test_boss_choker_loses_to_collar_in_defect_action_chain_deck(self):
        deck = [
            build_card("Zap", CardType.SKILL, cost=0),
            build_card("Reinforced Body", CardType.SKILL, cost=-1, block=7),
            build_card("Sweeping Beam", CardType.ATTACK, cost=1, damage=6),
            build_card("Coolheaded", CardType.SKILL, cost=1, upgrades=1),
            build_card("Coolheaded", CardType.SKILL, cost=1, upgrades=1),
            build_card("Compile Driver", CardType.ATTACK, cost=1, damage=7),
            build_card("Reboot", CardType.SKILL, cost=0, rarity=CardRarity.RARE),
            build_card("Turbo", CardType.SKILL, cost=0),
            build_card("Steam", CardType.SKILL, cost=0, block=6),
            build_card("Hello World", CardType.POWER, cost=1),
            build_card("Loop", CardType.POWER, cost=1),
            build_card("Capacitor", CardType.POWER, cost=1, upgrades=1),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.relics = [
            Relic("Ice Cream", "Ice Cream"),
            Relic("Runic Capacitor", "Runic Capacitor"),
        ]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        choker = Relic("Velvet Choker", "Velvet Choker")
        collar = Relic("SlaversCollar", "Slaver's Collar")

        choker_score = agent._boss_relic_score(choker)
        collar_score = agent._boss_relic_score(collar)
        choker_facts = agent._boss_relic_candidate_facts(choker)

        self.assertGreater(collar_score, choker_score)
        self.assertGreater(
            choker_facts["velvet_choker_constraint"]["chain_pressure"], 0,
        )
        self.assertLess(
            choker_facts["velvet_choker_constraint"]["score_adjustment"], 0,
        )

    def test_defect_orb_engine_rejects_and_prioritizes_purging_reprogram(self):
        reprogram = build_card("Reprogram", CardType.SKILL, cost=1)
        hello_world = build_card("Hello World", CardType.POWER, cost=1)
        orb_deck = [
            reprogram,
            hello_world,
            build_card("Zap", CardType.SKILL, cost=0),
            build_card("Coolheaded", CardType.SKILL, cost=1, upgrades=1),
            build_card("Coolheaded", CardType.SKILL, cost=1, upgrades=1),
            build_card("Loop", CardType.POWER, cost=1),
            build_card("Capacitor", CardType.POWER, cost=1, upgrades=1),
            build_card("Static Discharge", CardType.POWER, cost=1),
        ]
        game = GameStub([], orb_deck)
        game.in_combat = False
        game.act = 2
        game.relics = [Relic("Runic Capacitor", "Runic Capacitor")]
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        reward = build_card("Reprogram", CardType.SKILL, cost=1)

        reward_score = agent._card_reward_score(reward)
        reward_contract = agent._card_reward_score_contracts[id(reward)]
        reprogram_removal = agent._removal_score(reprogram)
        hello_world_removal = agent._removal_score(hello_world)

        self.assertLess(reward_score, agent._permanent_card_pick_hurdle())
        self.assertLessEqual(
            reward_contract["score_inputs"]["defect_orb_reprogram_conflict"],
            -20.0,
        )
        self.assertGreater(reprogram_removal, hello_world_removal)

    def test_boss_coffee_dripper_uses_next_act_full_hp_not_boss_exit_hp(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.current_hp = 30
        game.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        coffee = Relic("Coffee Dripper", "Coffee Dripper")
        low_boss_exit_score = agent._boss_relic_score(coffee)
        game.current_hp = 80
        full_boss_exit_score = agent._boss_relic_score(coffee)

        self.assertEqual(full_boss_exit_score, low_boss_exit_score)

    def test_developing_act_two_deck_prefers_collar_over_coffee_dripper(self):
        deck = [
            *[
                build_card(
                    "Strike_R", CardType.ATTACK, cost=1, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(5)
            ],
            *[
                build_card(
                    "Defend_R", cost=1, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ],
            build_card(
                "Bash", CardType.ATTACK, cost=2, damage=8,
                rarity=CardRarity.BASIC,
            ),
            build_card("Anger", CardType.ATTACK, cost=0, damage=6),
            build_card("Uppercut", CardType.ATTACK, cost=2, damage=13),
            build_card("Disarm"),
            build_card("Shrug It Off", block=11, upgrades=1),
            build_card("Disarm"),
            build_card("Thunderclap", CardType.ATTACK, damage=4),
            build_card("Whirlwind", CardType.ATTACK, cost=-1, damage=5),
            build_card("Flame Barrier", cost=2, block=12),
            build_card("Juggernaut", CardType.POWER, cost=2),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.current_hp = 52
        game.max_hp = 85
        game.relics = [Relic("Burning Blood", "Burning Blood")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        coffee = Relic("Coffee Dripper", "Coffee Dripper")
        collar = Relic("SlaversCollar", "Slaver's Collar")

        coffee_score = agent._boss_relic_score(coffee)
        collar_score = agent._boss_relic_score(collar)
        coffee_facts = agent._boss_relic_candidate_facts(coffee)

        self.assertGreater(collar_score, coffee_score)
        self.assertGreaterEqual(
            coffee_facts["coffee_dripper_consequence"][
                "rest_lock_risk_penalty"
            ],
            7.0,
        )

    def test_boss_coffee_dripper_a5_partial_heal_crosses_exact_safety_boundary(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.max_hp = 80
        game.ascension_level = 5
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        coffee = Relic("Coffee Dripper", "Coffee Dripper")

        # 31 + floor((80 - 31) * .75) == 67, below the 85% gate.
        game.current_hp = 31
        below = agent._boss_relic_score(coffee)
        # 32 + floor((80 - 32) * .75) == 68, exactly at the gate.
        game.current_hp = 32
        at_boundary = agent._boss_relic_score(coffee)

        self.assertEqual(36, at_boundary - below)

    def test_boss_coffee_dripper_mark_of_bloom_blocks_post_boss_heal(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.current_hp = 31
        game.max_hp = 80
        game.ascension_level = 0
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        coffee = Relic("Coffee Dripper", "Coffee Dripper")
        bloom_score = agent._boss_relic_score(coffee)

        game.relics = []
        game.ascension_level = 5
        a5_low_score = agent._boss_relic_score(coffee)

        self.assertEqual(a5_low_score, bloom_score)

    def test_boss_energy_is_priced_by_the_next_permanent_point(self):
        deck = [
            build_card("Immolate", CardType.ATTACK, cost=2, damage=21),
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            strike(), defend(), defend(),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        first = agent._boss_energy_marginal_profile("Coffee Dripper")
        game.relics = [Relic("Ectoplasm", "Ectoplasm")]
        fifth = agent._boss_energy_marginal_profile("Coffee Dripper")
        game.relics.append(Relic("Coffee Dripper", "Coffee Dripper"))
        sixth = agent._boss_energy_marginal_profile("Mark of Pain")

        self.assertEqual((3, 4), (
            first["current_permanent_energy"],
            first["projected_permanent_energy"],
        ))
        self.assertGreater(first["local_marginal_adjustment"], fifth["local_marginal_adjustment"])
        self.assertLess(fifth["local_marginal_adjustment"], 0)
        self.assertLess(
            sixth["local_marginal_adjustment"],
            fifth["local_marginal_adjustment"],
        )

    def test_mark_of_pain_does_not_treat_other_status_sources_as_payoffs(self):
        game = GameStub([], [
            build_card("Immolate", CardType.ATTACK, cost=2, damage=21),
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            strike(), defend(),
        ])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        facts = agent._mark_of_pain_profile()

        self.assertEqual(2, len(facts["existing_status_sources"]))
        self.assertEqual([], facts["observed_status_payoffs"])
        self.assertEqual([], facts["observed_status_cleanup"])
        self.assertTrue(facts["unmitigated_wounds"])
        self.assertEqual(16.0, facts["wound_penalty_estimate"])

    def test_mark_of_pain_rewards_observed_payoff_and_cleanup_only(self):
        game = GameStub([], [
            build_card("Evolve", CardType.POWER, cost=1),
            build_card("Second Wind", CardType.SKILL, cost=1),
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            strike(), defend(),
        ])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        facts = agent._mark_of_pain_profile()

        self.assertEqual(["Evolve"], facts["observed_status_payoffs"])
        self.assertEqual(["Second Wind"], facts["observed_status_cleanup"])
        self.assertFalse(facts["unmitigated_wounds"])
        self.assertLessEqual(facts["wound_penalty_estimate"], 2.0)

    def test_exact_coffee_then_mark_replay_prefers_effective_tiny_house(self):
        game = GameStub([], [
            build_card("Immolate", CardType.ATTACK, cost=2, damage=21),
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            build_card("Reckless Charge", CardType.ATTACK, cost=0, damage=7),
            build_card("Shockwave", CardType.SKILL, cost=2),
            build_card("Shrug It Off", CardType.SKILL, cost=1, block=8),
            strike(), defend(), defend(),
        ])
        game.in_combat = False
        game.act = 2
        game.current_hp = 6
        game.max_hp = 80
        game.relics = [
            Relic("Ectoplasm", "Ectoplasm"),
            Relic("Coffee Dripper", "Coffee Dripper"),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        mark = Relic("Mark of Pain", "Mark of Pain")
        tiny = Relic("Tiny House", "Tiny House")

        self.assertGreater(
            agent._boss_relic_score(tiny),
            agent._boss_relic_score(mark),
        )
        self.assertEqual(
            0, agent._tiny_house_post_state()["effective_gold_gain"]
        )

    def test_coffee_non_rest_sustain_mitigates_rest_lock(self):
        game = GameStub([], [strike(), defend(), defend()])
        game.in_combat = False
        game.current_hp = game.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        coffee = Relic("Coffee Dripper", "Coffee Dripper")
        without_sustain = agent._boss_relic_score(coffee)

        game.relics = [Relic("Burning Blood", "Burning Blood")]
        with_sustain = agent._boss_relic_score(coffee)

        self.assertGreater(with_sustain, without_sustain)

    def test_boss_relic_candidate_facts_expose_effective_tradeoffs(self):
        game = GameStub([], [
            build_card("Power Through", CardType.SKILL, cost=1, block=15),
            strike(), defend(),
        ])
        game.in_combat = False
        game.relics = [Relic("Ectoplasm", "Ectoplasm")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        coffee = agent._boss_relic_candidate_facts(
            Relic("Coffee Dripper", "Coffee Dripper")
        )
        mark = agent._boss_relic_candidate_facts(
            Relic("Mark of Pain", "Mark of Pain")
        )
        tiny = agent._boss_relic_candidate_facts(
            Relic("Tiny House", "Tiny House")
        )

        self.assertEqual(4, coffee["energy_marginal"]["current_permanent_energy"])
        self.assertTrue(coffee["coffee_dripper_consequence"]["rest_disabled"])
        self.assertEqual(
            2, mark["mark_of_pain_consequence"]["wounds_added_each_combat"]
        )
        self.assertEqual(0, tiny["tiny_house_post_state"]["effective_gold_gain"])
        self.assertNotIn(
            "local_marginal_adjustment", coffee["energy_marginal"]
        )

    def test_boss_relic_candidates_bind_exact_followup_grids(self):
        astrolabe = SimpleAgent._boss_relic_candidate_consequences(
            Relic("Astrolabe", "Astrolabe")
        )
        cage = SimpleAgent._boss_relic_candidate_consequences(
            Relic("Empty Cage", "Empty Cage")
        )
        ordinary = SimpleAgent._boss_relic_candidate_consequences(
            Relic("Black Blood", "Black Blood")
        )

        self.assertEqual(
            {
                "kind": "astrolabe_grid_selection",
                "operation": "transform_and_upgrade",
                "select_count": 3,
                "domain": "current_deck",
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            },
            astrolabe["future_costs"][0],
        )
        self.assertEqual(
            {
                "kind": "relic_grid_selection",
                "relic_id": "Empty Cage",
                "domain": "current_deck",
                "operation": "remove",
                "select_count": 2,
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            },
            cage["future_costs"][0],
        )
        self.assertEqual(
            "classified_future",
            astrolabe["uncertainty_classification"]["status"],
        )
        self.assertEqual(
            {"operation": "gain_boss_relic", "relic_id": "Black Blood"},
            ordinary,
        )

    def test_hexaghost_rest_values_only_net_divider_survival_gain(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = 51
        game.max_hp = 75
        game.act_boss = "Hexaghost"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.TOKE, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        chosen, values, details = agent._choose_campfire_option(
            game.screen.rest_options, 51, 15
        )

        self.assertEqual(12, details["hexaghost_divider_penalty"])
        self.assertEqual(10, details["effective_recovery"])
        self.assertEqual(RestOption.TOKE, chosen)
        self.assertLess(values[RestOption.REST], values[RestOption.TOKE])

    def test_low_hp_rest_does_not_override_higher_value_smith(self):
        """The low-HP safety override must still respect local value."""

        game = GameStub([], [
            build_card("Apotheosis", CardType.SKILL, cost=2),
            build_card("Strike_A", CardType.ATTACK, cost=1, damage=6),
            build_card("Defend_A", CardType.SKILL, cost=1, block=5),
            build_card("Strike_B", CardType.ATTACK, cost=1, damage=6),
            build_card("Defend_B", CardType.SKILL, cost=1, block=5),
        ])
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = 30
        game.max_hp = 70
        game.player.current_hp = 30
        game.player.max_hp = 70
        game.act_boss = "Hexaghost"
        game.screen = RestScreen(False, [RestOption.REST, RestOption.SMITH])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        chosen, values, details = agent._choose_campfire_option(
            game.screen.rest_options, 30, 15
        )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])
        self.assertFalse(details["mandatory_rest"])
        self.assertFalse(details["rest_is_local_best"])

    def test_healthy_final_fire_smiths_against_slime_boss(self):
        game = GameStub([], [build_card("Defragment", CardType.POWER)])
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = 60
        game.max_hp = 75
        game.act_boss = "Slime Boss"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        chosen, values, details = agent._choose_campfire_option(
            game.screen.rest_options, 60, 15
        )

        self.assertEqual(0.74, details["threshold"])
        self.assertEqual("slimeboss", details["boss_id"])
        self.assertEqual("low", details["boss_risk"])
        self.assertEqual(RestOption.SMITH, chosen)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])

    def test_healthy_unready_guardian_final_fire_respects_better_smith(self):
        deck = [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(4)),
            build_card(
                "Bash", CardType.ATTACK, cost=2, damage=8,
                rarity=CardRarity.BASIC, upgrades=1,
            ),
            build_card("Rage", CardType.SKILL, cost=0),
            build_card("Wild Strike", CardType.ATTACK, damage=12),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = game.player.current_hp = 62
        game.max_hp = game.player.max_hp = 72
        game.act_boss = "The Guardian"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        chosen, values, details = agent._choose_campfire_option(
            game.screen.rest_options, 62, 15
        )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertFalse(details["final_act_one_survival_rest"])
        self.assertFalse(details["mandatory_rest"])
        self.assertLess(details["best_upgrade"], 14.0)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])

    def test_healthy_unready_slime_final_fire_respects_better_smith(self):
        deck = [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(4)),
            build_card("Survivor", CardType.SKILL, block=8),
            build_card("Acrobatics", CardType.SKILL, cost=1),
            build_card("Dagger Throw", CardType.ATTACK, damage=9),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = game.player.current_hp = 52
        game.max_hp = game.player.max_hp = 65
        game.act_boss = "Slime Boss"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        chosen, values, details = agent._choose_campfire_option(
            game.screen.rest_options, 52, 15
        )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertFalse(details["final_act_one_survival_rest"])
        self.assertFalse(details["mandatory_rest"])
        self.assertLess(details["best_upgrade"], 14.0)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])

    def test_recent_57_of_75_slime_fire_uses_local_smith_argmax(self):
        game = GameStub([], [build_card("Defragment", CardType.POWER)])
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = game.player.current_hp = 57
        game.max_hp = game.player.max_hp = 75
        game.act_boss = "Slime Boss"
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        with patch.object(
            agent, "_upgrade_score", return_value=12.446
        ), patch.object(
            agent,
            "_boss_entry_readiness",
            return_value={"ready": False, "score": 0.55},
        ), patch.object(
            agent, "_deck_readiness", return_value={"score": 0.80}
        ):
            chosen, values, details = agent._choose_campfire_option(
                [RestOption.REST, RestOption.SMITH], 57, 15
            )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])
        self.assertFalse(details["mandatory_rest"])

    def test_injured_final_fire_still_rests_against_slime_boss(self):
        game = GameStub([], [build_card("Defragment", CardType.POWER)])
        game.in_combat = False
        game.act = 1
        game.floor = 15
        game.current_hp = 35
        game.max_hp = 75
        game.act_boss = "Slime Boss"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        chosen, _, _ = agent._choose_campfire_option(
            game.screen.rest_options, 35, 15
        )

        self.assertEqual(RestOption.REST, chosen)

    def test_low_max_hp_final_donu_deca_fire_banks_absolute_survival(self):
        """Replay run 5 F49: 29/36 is not safe merely because it is 81%."""

        game = GameStub([], [
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Noxious Fumes 2", CardType.POWER),
        ])
        game.in_combat = False
        game.act = 3
        game.floor = 49
        game.current_hp = game.player.current_hp = 29
        game.max_hp = game.player.max_hp = 36
        game.act_boss = "Donu and Deca"
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.SMITH]
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(
            agent, "_upgrade_score", return_value=17.062
        ), patch.object(
            agent,
            "_boss_entry_readiness",
            return_value={"ready": False, "score": 0.55},
        ), patch.object(
            agent, "_deck_readiness", return_value={"score": 0.90}
        ):
            chosen, values, details = agent._choose_campfire_option(
                game.screen.rest_options, 29, 16
            )

        self.assertEqual(RestOption.REST, chosen)
        self.assertEqual(36, details["boss_survival_reserve"])
        self.assertEqual(7, details["boss_survival_gap_closed"])
        self.assertGreater(
            values[RestOption.REST], values[RestOption.SMITH]
        )

    def test_shop_compares_relic_against_purge(self):
        game = GameStub([], [strike(), strike(), strike(), strike(), defend(), defend(), defend(), defend()])
        relic = Relic("Bag of Preparation", "Bag of Preparation", price=100)
        game.screen = ShopScreen([], [relic], [], True, 75)
        game.screen.protocol_purge_choice_index = 0
        game.gold = 150
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        action = agent.choose_shop_action()
        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(relic, action.relic)

    def test_shop_buys_membership_card_before_profitable_basket(self):
        curse = build_card(
            "Injury", CardType.CURSE, rarity=CardRarity.CURSE
        )
        game = GameStub(
            [],
            [strike(), strike(), defend(), defend(), curse],
        )
        game.in_combat = False
        game.act = 1
        game.floor = 10
        game.current_hp = game.player.current_hp = 36
        game.max_hp = game.player.max_hp = 75
        membership = Relic(
            "Membership Card", "Membership Card", price=155
        )
        meal_ticket = Relic("MealTicket", "Meal Ticket", price=149)
        vajra = Relic("Vajra", "Vajra", price=145)
        dexterity = Potion(
            "Dexterity Potion", "Dexterity Potion",
            True, True, False, price=49,
        )
        game.screen = ShopScreen(
            [], [meal_ticket, vajra, membership], [dexterity], True, 50
        )
        game.screen.protocol_purge_choice_index = 0
        game.gold = 510
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(membership, action.relic)
        selected = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == agent.last_noncombat_decision["chosen_id"]
        )
        plan = selected["strategic_risk"]["discount_purchase_order"]
        self.assertTrue(plan["should_buy_first"])
        self.assertGreater(plan["basket_advantage"], 0)
        self.assertIn(
            "shop:relic:MealTicket:1",
            plan["discounted_basket"]["choice_ids"],
        )

    def test_shop_does_not_buy_membership_card_in_unprofitable_final_shop(self):
        curse = build_card(
            "Shame", CardType.CURSE, rarity=CardRarity.CURSE
        )
        game = GameStub([], [strike(), defend(), curse])
        game.in_combat = False
        game.act = 4
        game.floor = 53
        game.current_hp = game.player.current_hp = 76
        game.max_hp = game.player.max_hp = 76
        membership = Relic(
            "Membership Card", "Membership Card", price=153
        )
        colorless = Potion(
            "ColorlessPotion", "Colorless Potion",
            True, True, False, price=50,
        )
        game.screen = ShopScreen(
            [], [membership], [colorless], True, 125
        )
        game.screen.protocol_purge_choice_index = 0
        game.gold = 260
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual("purge", action.name)
        membership_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "Membership Card"
        )
        plan = membership_row["strategic_risk"][
            "discount_purchase_order"
        ]
        self.assertFalse(plan["should_buy_first"])
        self.assertLess(plan["basket_advantage"], 0)

    def test_shop_discount_relic_does_not_block_decisive_purchase(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 4
        membership = Relic(
            "Membership Card", "Membership Card", price=150
        )
        preparation = Relic(
            "Bag of Preparation", "Bag of Preparation", price=145
        )
        game.screen = ShopScreen(
            [], [preparation, membership], [], False, 75
        )
        game.gold = 208
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(preparation, action.relic)
        membership_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "Membership Card"
        )
        self.assertFalse(
            membership_row["strategic_risk"][
                "discount_purchase_order"
            ]["should_buy_first"]
        )

    def test_shop_model_and_audit_use_exact_protocol_choice_ids(self):
        first = build_card("ShopA")
        second = build_card("ShopB")
        first.uuid = "shop-a"
        second.uuid = "shop-b"
        first.price = second.price = 50
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.screen = ShopScreen([first, second], [], [], False, 75)
        game.gold = 100
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(
            agent,
            "_card_reward_score",
            side_effect=lambda card: 20.0 if card is first else 19.0,
        ):
            action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyCardAction)
        decision = agent.last_noncombat_decision
        choice_ids = {row["choice_id"] for row in decision["candidates"]}
        self.assertEqual(
            {"shop:card:shop-a:0", "shop:card:shop-b:1", "action:return"},
            choice_ids,
        )
        selected = next(
            row for row in decision["candidates"]
            if row["choice_id"] == decision["chosen_id"]
        )
        self.assertEqual(
            "buy_card_then_rerank_shop",
            selected["consequences"]["operation"],
        )

    def test_shop_orrery_candidate_publishes_price_and_reward_sequence(self):
        tea_set = Relic("Ancient Tea Set", "Ancient Tea Set", price=150)
        orrery = Relic("Orrery", "Orrery", price=153)
        tea_set.protocol_choice_index = 8
        orrery.protocol_choice_index = 9
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.screen = ShopScreen([], [tea_set, orrery], [], False, 75)
        game.gold = 211
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(
            agent,
            "_shop_relic_score",
            side_effect=lambda relic: 40.0 if relic is tea_set else 8.0,
        ):
            action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(tea_set, action.relic)
        orrery_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == "shop:relic:Orrery:9"
        )
        self.assertTrue(orrery_row["selection_eligible"])
        self.assertEqual(8.0 - 153.0 / 16.0, orrery_row["score"])
        consequences = orrery_row["consequences"]
        self.assertEqual(
            {"gold": 153, "hp": 0, "max_hp": 0},
            consequences["current_cost"],
        )
        self.assertEqual(-153, consequences["gold_delta"])
        self.assertEqual(
            {"gain": [], "remove": [], "upgrade": [], "transform": []},
            consequences["card_changes"],
        )
        self.assertEqual(
            {
                "kind": "optional_card_reward_sequence",
                "relic_id": "Orrery",
                "card_pool": "CHARACTER",
                "reward_count": 5,
                "candidates_per_reward": 3,
                "select_count_per_reward": 1,
                "can_skip": True,
                "timing": "after_relic_purchase",
            },
            consequences["future_costs"][0],
        )

        game.relics = [
            Relic("QuestionCard", "Question Card"),
            Relic("BustedCrown", "Busted Crown"),
        ]
        modified_agent = SimpleAgent(PlayerClass.IRONCLAD)
        modified_agent.game = game
        with patch.object(
            modified_agent,
            "_shop_relic_score",
            side_effect=lambda relic: 40.0 if relic is tea_set else 8.0,
        ):
            modified_agent.choose_shop_action()
        modified_orrery = next(
            row for row in modified_agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == "shop:relic:Orrery:9"
        )
        self.assertEqual(
            2,
            modified_orrery["consequences"]["future_costs"][0][
                "candidates_per_reward"
            ],
        )

    def test_shop_buys_affordable_low_floor_potion_when_gold_remains(self):
        game = GameStub([], [strike(), defend()])
        potion = Potion(
            "BlessingOfTheForge",
            "Blessing of the Forge",
            True,
            True,
            False,
            price=52,
        )
        game.screen = ShopScreen([], [], [potion], False, 75)
        game.gold = 124
        game.potion_available = True
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(action.potion, potion)
        selected = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == agent.last_noncombat_decision["chosen_id"]
        )
        self.assertEqual(
            {
                "kind": "potion",
                "id": "BlessingOfTheForge",
                "potion_id": "BlessingOfTheForge",
            },
            selected["consequences"]["acquired_benefit"],
        )

    def test_shop_values_real_lees_waffle_id_over_focus_potion(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 12
        game.current_hp = game.player.current_hp = 25
        game.max_hp = game.player.max_hp = 80
        waffle = Relic("Lee's Waffle", "Lee's Waffle", price=143)
        focus = Potion(
            "FocusPotion", "Focus Potion",
            True, True, False, price=50,
        )
        game.screen = ShopScreen([], [waffle], [focus], False, 75)
        game.gold = 185
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(waffle, action.relic)
        candidates = {
            row["id"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertGreater(
            candidates["Lee's Waffle"]["score"],
            candidates["FocusPotion"]["score"],
        )

    def test_shop_potion_does_not_block_affordable_permanent_relic(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 10
        game.current_hp = game.player.current_hp = 56
        game.max_hp = game.player.max_hp = 80
        ink_bottle = Relic("InkBottle", "Ink Bottle", price=257)
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion",
            True, True, False, price=75,
        )
        game.screen = ShopScreen(
            [], [ink_bottle], [duplication], False, 75
        )
        game.gold = 325
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(ink_bottle, action.relic)
        potion_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "DuplicationPotion"
        )
        blocked = potion_row["strategic_risk"][
            "blocked_permanent_purchase"
        ]
        self.assertEqual("InkBottle", blocked["relic_id"])
        self.assertGreater(blocked["opportunity_penalty"], 0)

    def test_shop_potion_remains_available_when_relic_stays_affordable(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 1
        game.floor = 10
        game.current_hp = game.player.current_hp = 56
        game.max_hp = game.player.max_hp = 80
        ink_bottle = Relic("InkBottle", "Ink Bottle", price=257)
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion",
            True, True, False, price=75,
        )
        game.screen = ShopScreen(
            [], [ink_bottle], [duplication], False, 75
        )
        game.gold = 400
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(duplication, action.potion)
        potion_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "DuplicationPotion"
        )
        self.assertNotIn("strategic_risk", potion_row)

    def test_shop_stops_after_first_marginal_potion_purchase(self):
        first = Potion(
            "AttackPotion", "Attack Potion",
            True, True, False, price=50,
        )
        second = Potion(
            "Fire Potion", "Fire Potion",
            True, True, False, price=50,
        )
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.screen = ShopScreen([], [], [first, second], False, 75)
        game.gold = 150
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        first_action = agent.choose_shop_action()
        bought = first_action.potion
        game.screen.potions = [
            potion for potion in game.screen.potions if potion is not bought
        ]
        game.gold -= bought.price
        second_action = agent.choose_shop_action()

        self.assertIsInstance(first_action, BuyPotionAction)
        self.assertIsInstance(second_action, CancelAction)
        remaining = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] != "leave"
        )
        self.assertEqual(
            1.0,
            remaining["score_inputs"]["prior_potion_purchases"],
        )
        self.assertLess(remaining["score"], 6.5)

    def test_shop_can_buy_high_value_duplication_potion(self):
        game = GameStub([], [strike(), defend()])
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False,
            price=1,
        )
        game.in_combat = False
        game.screen = ShopScreen([], [], [duplication], False, 75)
        game.gold = 999
        game.potion_available = True
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(duplication, action.potion)
        row = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["choice_id"].startswith("shop:potion:")
        )
        self.assertTrue(row["visible"])
        self.assertTrue(row["selection_eligible"])
        self.assertIsNone(row["veto_reason"])

    def test_shop_can_resume_bound_duplication_purchase(self):
        game = GameStub([], [strike(), defend()])
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False,
            price=1,
        )
        game.in_combat = False
        game.screen = ShopScreen([], [], [duplication], False, 75)
        game.gold = 999
        game.potion_available = True
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        visit_key = (game.act, game.floor)
        agent.shop_visit_key = visit_key
        agent.pending_shop_potion_purchase = {
            "visit_key": visit_key,
            "potion_id": "duplicationpotion",
            "name": "Duplication Potion",
            "occurrence": 0,
            "quoted_price": 1,
            "replaced": "FirePotion",
            "gold_before": game.gold,
        }

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyPotionAction)
        self.assertIs(duplication, action.potion)
        row = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["choice_id"].startswith("shop:potion:")
        )
        self.assertTrue(row["selection_eligible"])
        self.assertIsNone(row["veto_reason"])

    def test_shop_uses_affordable_low_floor_purge(self):
        # Purge scored 7.75 in the live trace, just below the generic Leave
        # floor.  Seven starter cards make the removal materially useful.
        game = GameStub(
            [],
            [strike(), strike(), strike(), strike(), defend(), defend(), defend()],
        )
        game.screen = ShopScreen([], [], [], True, 75)
        game.screen.protocol_purge_choice_index = 0
        game.gold = 150
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual("purge", action.name)

    def test_low_hp_shop_spends_surplus_gold_on_near_floor_relic(self):
        game = GameStub(
            [],
            [
                strike(), strike(),
                defend(), defend(), defend(), defend(),
                *[
                    build_card(
                        f"NonStarter{index}",
                        CardType.ATTACK,
                        damage=8,
                    )
                    for index in range(17)
                ],
            ],
        )
        torii = Relic("Torii", "Torii", price=298)
        game.screen = ShopScreen([], [torii], [], True, 75)
        game.screen.protocol_purge_choice_index = 0
        game.gold = 618
        game.current_hp = game.player.current_hp = 13
        game.max_hp = game.player.max_hp = 74
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(agent, "_shop_relic_score", return_value=26.492):
            action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(torii, action.relic)
        self.assertEqual(
            "shop_global_comparison",
            agent.last_noncombat_decision["reason"],
        )

    def test_low_absolute_hp_shop_buys_waffle_before_incremental_relics(self):
        game = GameStub([], [strike(), defend()])
        waffle = Relic("Waffle", "Waffle", price=150)
        dream_catcher = Relic("Dream Catcher", "Dream Catcher", price=100)
        game.screen = ShopScreen([], [dream_catcher, waffle], [], True, 75)
        game.screen.protocol_purge_choice_index = 0
        game.gold = 495
        game.current_hp = 27
        game.max_hp = 35
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, BuyRelicAction)
        self.assertIs(waffle, action.relic)

    def test_shop_potion_replacement_binds_the_exact_planned_purchase(self):
        game = GameStub([], [strike(), defend()])
        owned = Potion("FirePotion", "Fire Potion", True, True, True)
        ghost = Potion(
            "GhostInAJar", "Ghost in a Jar", True, True, False, price=90
        )
        distracting_speed = Potion(
            "SpeedPotion", "Speed Potion", True, True, False, price=45
        )
        game.screen = ShopScreen(
            [], [], [ghost, distracting_speed], False, 75
        )
        game.gold = 200
        game.potion_available = True
        belt_full = {"value": True}
        game.are_potions_full = lambda: belt_full["value"]
        game.get_real_potions = lambda: [owned] if belt_full["value"] else []
        for index, potion in enumerate((ghost, distracting_speed)):
            potion.protocol_choice_index = index
            potion.protocol_option_id = f"option:shop-potion:{index}"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(
            agent,
            "_shop_potion_value",
            side_effect=lambda potion: (
                100.0 if potion is distracting_speed else 20.0
            ),
        ):
            discard_action = agent.choose_shop_action()
            self.assertIsInstance(discard_action, PotionAction)
            self.assertFalse(discard_action.use)
            self.assertIs(owned, discard_action.potion)
            preparation = agent.last_noncombat_decision
            self.assertEqual(
                "shop_potion_replacement_resource_preparation",
                preparation["reason"],
            )
            self.assertEqual(
                ["potion:discard:0:FirePotion"],
                [row["choice_id"] for row in preparation["candidates"]],
            )
            self.assertTrue(
                preparation["candidate_contract"][
                    "parent_screen_resolution_pending"
                ]
            )

            belt_full["value"] = False
            refreshed_ghost = Potion(
                "GhostInAJar", "Ghost in a Jar", True, True, False,
                price=65,
            )
            refreshed_speed = Potion(
                "SpeedPotion", "Speed Potion", True, True, False,
                price=45,
            )
            game.screen = ShopScreen(
                [], [], [refreshed_ghost, refreshed_speed], False, 75
            )
            purchase_action = agent.choose_shop_action()

        self.assertIsInstance(purchase_action, BuyPotionAction)
        self.assertIs(refreshed_ghost, purchase_action.potion)
        self.assertEqual(
            "shop_bound_potion_replacement_purchase",
            agent.last_noncombat_decision["reason"],
        )
        self.assertEqual(
            3,
            len(agent.last_noncombat_decision["candidates"]),
        )
        self.assertEqual(
            {"shop:potion:GhostInAJar:0", "shop:potion:SpeedPotion:1", "action:return"},
            {
                row["choice_id"]
                for row in agent.last_noncombat_decision["candidates"]
            },
        )

    def test_shop_retains_every_visible_listing_and_vetoes_in_place(self):
        game = GameStub([], [strike(), defend()])
        card = build_card("DuplicateLimitCard")
        card.price = 5
        relic = Relic("Anchor", "Anchor", price=50)
        affordable_potion = Potion(
            "DexterityPotion", "Dexterity Potion", True, True, False, price=5
        )
        unaffordable_duplicate = Potion(
            "DexterityPotion", "Dexterity Potion", True, True, False, price=50
        )
        game.screen = ShopScreen(
            [card], [relic], [affordable_potion, unaffordable_duplicate], True, 75
        )
        game.gold = 10
        game.relics = [Relic("Sozu", "Sozu")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(agent, "_within_copy_limit", return_value=False):
            action = agent.choose_shop_action()

        self.assertIsInstance(action, CancelAction)
        rows = agent.last_noncombat_decision["candidates"]
        self.assertEqual([0, 1, 2, 3, None], [row["choice_index"] for row in rows])
        self.assertEqual(5, len({row["choice_id"] for row in rows}))
        vetoes = {row["choice_index"]: row.get("veto_reason") for row in rows}
        self.assertEqual("copy_limit", vetoes[0])
        self.assertEqual("insufficient_gold", vetoes[1])
        self.assertEqual("sozu_prevents_potion_purchase", vetoes[2])
        self.assertEqual("insufficient_gold", vetoes[3])
        self.assertNotIn("purge", {row["choice_id"] for row in rows})

    def test_shop_uses_authoritative_protocol_choice_indexes(self):
        game = GameStub([], [strike(), defend()])
        relic = Relic("OrangePellets", "Orange Pellets", price=145)
        potion = Potion(
            "SpeedPotion", "Speed Potion", True, True, False, price=50
        )
        relic.protocol_choice_index = 8
        potion.protocol_choice_index = 11
        game.screen = ShopScreen([], [relic], [potion], True, 75)
        game.screen.protocol_purge_choice_index = 0
        game.gold = 20
        game.relics = [Relic("Sozu", "Sozu")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.choose_shop_action()

        self.assertIsInstance(action, CancelAction)
        self.assertEqual(
            [0, 8, 11, None],
            [
                row["choice_index"]
                for row in agent.last_noncombat_decision["candidates"]
            ],
        )

    def test_potion_preparation_typed_key_distinguishes_use_and_discard(self):
        game = GameStub([], [strike(), defend()])
        game.current_hp = 40
        game.max_hp = 80
        fruit = Potion(
            "Fruit Juice", "Fruit Juice", True, True, False
        )
        targeted = Potion(
            "FirePotion", "Fire Potion", True, True, True
        )
        replacement = Potion(
            "GhostInAJar", "Ghost in a Jar", True, True, False
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        rows, selected_id = agent._potion_resource_preparation_candidates(
            [fruit, targeted],
            replacement,
            PotionAction(True, potion=fruit),
            fruit,
            parent_operation="bound_purchase",
        )

        fruit_rows = [row for row in rows if row["choice_index"] == 0]
        self.assertEqual({"use", "discard"}, {row["operation"] for row in fruit_rows})
        self.assertEqual(2, len({
            (row["choice_index"], row["action"], row["operation"])
            for row in fruit_rows
        }))
        self.assertEqual("potion:use:0:Fruit Juice", selected_id)
        targeted_rows = [row for row in rows if row["choice_index"] == 1]
        self.assertEqual(["discard"], [row["operation"] for row in targeted_rows])
        selected = next(row for row in rows if row["choice_id"] == selected_id)
        self.assertEqual(
            selected["score"],
            sum(component["value"] for component in selected["score_components"]),
        )
        self.assertTrue(
            selected["semantic_id"].startswith("potion-use:potion:")
        )

    def test_duplication_potion_preparation_keeps_unproven_use_ineligible(self):
        game = GameStub([], [strike(), defend()])
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False
        )
        replacement = Potion(
            "GhostInAJar", "Ghost in a Jar", True, True, False
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        rows, selected_id = agent._potion_resource_preparation_candidates(
            [duplication],
            replacement,
            PotionAction(False, potion=duplication),
            duplication,
            parent_operation="bound_purchase",
        )

        self.assertEqual({"use", "discard"}, {row["operation"] for row in rows})
        use = next(row for row in rows if row["operation"] == "use")
        discard = next(row for row in rows if row["operation"] == "discard")
        self.assertFalse(use["selection_eligible"])
        self.assertEqual(
            "use_has_no_verified_preparation_value",
            use["veto_reason"],
        )
        self.assertTrue(discard["selection_eligible"])
        self.assertEqual(discard["choice_id"], selected_id)

    def test_confirm_actions_are_retained_with_chest_and_shop_room_choices(self):
        game = GameStub([], [strike(), defend()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        game.screen_type = ScreenType.CHEST
        agent.handle_screen()
        self.assertEqual(
            {"chest:open", "action:proceed"},
            {row["choice_id"] for row in agent.last_noncombat_decision["candidates"]},
        )

        game.screen_type = ScreenType.SHOP_ROOM
        game.screen = SimpleNamespace()
        agent.handle_screen()
        self.assertEqual(
            {"shop_room:enter", "action:proceed"},
            {row["choice_id"] for row in agent.last_noncombat_decision["candidates"]},
        )
        agent.shop_completed_visit_key = (game.act, game.floor)
        action = agent.handle_screen()
        self.assertIsInstance(action, ProceedAction)
        self.assertEqual(
            {"shop_room:enter", "action:proceed"},
            {row["choice_id"] for row in agent.last_noncombat_decision["candidates"]},
        )

    def test_combat_reward_scores_are_preselection_and_confirm_is_always_visible(self):
        gold_reward = CombatReward(RewardType.GOLD, gold=1)
        relic_reward = CombatReward(
            RewardType.RELIC, relic=Relic("Anchor", "Anchor")
        )
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([gold_reward, relic_reward])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        before = agent._combat_reward_audit_candidates()
        claimed_selected = agent._combat_reward_audit_candidates(gold_reward)
        self.assertEqual(
            [row["score"] for row in before],
            [row["score"] for row in claimed_selected],
        )
        self.assertEqual("action:proceed", before[-1]["choice_id"])
        gold_claim = before[0]["consequences"]
        relic_claim = before[1]["consequences"]
        self.assertEqual("collect_combat_reward", gold_claim["operation"])
        self.assertEqual("gold", gold_claim["reward_type"])
        self.assertEqual(1, gold_claim["gold_delta"])
        self.assertNotIn("relic_id", gold_claim)
        self.assertNotIn("potion_id", gold_claim)
        self.assertEqual("Anchor", relic_claim["relic_id"])
        self.assertNotIn("gold_delta", relic_claim)
        self.assertNotIn("potion_id", relic_claim)

        action = agent.handle_screen()
        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(relic_reward, action.combat_reward)
        rows = agent.last_noncombat_decision["candidates"]
        selected = next(
            row for row in rows
            if row["choice_id"] == agent.last_noncombat_decision["chosen_id"]
        )
        self.assertEqual(
            max(row["score"] for row in rows if row["selection_eligible"]),
            selected["score"],
        )

    def test_bloody_idol_gold_reward_exposes_and_values_capped_heal(self):
        gold_reward = CombatReward(RewardType.GOLD, gold=19)
        game = GameStub([], [strike(), defend()])
        game.current_hp = 78
        game.max_hp = 80
        game.relics = [Relic("Bloody Idol", "Bloody Idol")]
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([gold_reward])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        gold_row = agent._combat_reward_audit_candidates()[0]

        self.assertEqual(21.0, gold_row["score"])
        self.assertEqual(19, gold_row["consequences"]["gold_delta"])
        self.assertEqual(2, gold_row["consequences"]["hp_delta"])
        self.assertEqual(
            "combat_reward_gold_and_heal_v2", gold_row["score_rule_id"]
        )
        self.assertEqual(
            2.0, gold_row["score_inputs"]["bloody_idol_hp_gain"]
        )

    def test_free_relic_reward_always_outranks_early_confirm(self):
        relic_reward = CombatReward(
            RewardType.RELIC, relic=Relic("Pantograph", "Pantograph")
        )
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([relic_reward])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        with patch.object(
            agent, "_relic_acquisition_score", return_value=-2.0
        ):
            rows = agent._combat_reward_audit_candidates()

        relic_row = rows[0]
        proceed_row = rows[-1]
        self.assertEqual(1.0, relic_row["score"])
        self.assertGreater(relic_row["score"], proceed_row["score"])
        self.assertEqual(
            "combat_reward_free_relic_collection_v2",
            relic_row["score_rule_id"],
        )

    def test_combat_reward_can_collect_duplication_potion(self):
        duplication = Potion(
            "DuplicationPotion", "Duplication Potion", True, True, False
        )
        potion_reward = CombatReward(
            RewardType.POTION, potion=duplication
        )
        gold_reward = CombatReward(RewardType.GOLD, gold=1)
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([potion_reward, gold_reward])
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(potion_reward, action.combat_reward)
        potion_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["consequences"].get("potion_id")
            == "DuplicationPotion"
        )
        self.assertTrue(potion_row["visible"])
        self.assertTrue(potion_row["selection_eligible"])
        self.assertIsNone(potion_row["veto_reason"])

    def test_match_scores_are_not_rewritten_after_selection(self):
        options = [
            EventOption("Backflip", "Backflip", False, 3),
            EventOption("Backflip", "Backflip", False, 6),
            EventOption("card8", "card8", False, 8),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.choose_match_game_action(options)

        self.assertEqual(3, action.choice_index)
        rows = agent.last_noncombat_decision["candidates"]
        selected_score = next(
            row for row in rows if row["choice_index"] == 3
        )["score"]
        peer_score = next(
            row for row in rows if row["choice_index"] == 6
        )["score"]
        self.assertEqual(89.997, selected_score)
        self.assertEqual(89.994, peer_score)
        self.assertGreater(selected_score, peer_score)

    def test_trivial_recovery_does_not_consume_campfire_for_low_max_hp(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(False, [RestOption.REST, RestOption.SMITH])
        game.act = 2
        game.floor = 28
        game.current_hp = 32
        game.max_hp = 35
        game.key_system_unlocked = False
        game.has_ruby_key = False
        game.act_boss = "Collector"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.SMITH, action.rest_option)

    def test_low_hp_with_meaningful_recovery_still_rests(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(False, [RestOption.REST, RestOption.SMITH])
        game.act = 2
        game.floor = 28
        game.current_hp = 24
        game.max_hp = 35
        game.key_system_unlocked = False
        game.has_ruby_key = False
        game.act_boss = "Collector"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.REST, action.rest_option)

    def test_act_three_36_of_37_hp_smiths_instead_of_healing_one(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                game = GameStub([], [strike(), defend()])
                game.screen = RestScreen(False, [RestOption.REST, RestOption.SMITH])
                game.act = 3
                game.floor = 49
                game.current_hp = 36
                game.max_hp = 37
                game.key_system_unlocked = False
                game.has_ruby_key = False
                game.act_boss = "Time Eater"
                agent = SimpleAgent(player_class)
                agent.game = game

                action = agent.choose_rest_option()

                self.assertIsInstance(action, RestAction)
                self.assertEqual(RestOption.SMITH, action.rest_option)

    def test_act_three_sapphire_key_precedes_linked_relic(self):
        relic_reward = CombatReward(RewardType.RELIC)
        key_reward = CombatReward(RewardType.SAPPHIRE_KEY)
        game = GameStub([], [])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([relic_reward, key_reward])
        game.key_system_unlocked = True
        game.has_emerald_key = True
        game.has_sapphire_key = False
        game.act = 3
        game.are_potions_full = lambda: False

        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        action = agent.handle_screen()
        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(key_reward, action.combat_reward)

    def test_act_two_sapphire_uses_linked_relic_opportunity_cost(self):
        for relic, expect_key in (
            (Relic("Juzu Bracelet", "Juzu Bracelet"), True),
            (Relic("Tungsten Rod", "Tungsten Rod"), False),
        ):
            with self.subTest(relic=relic.relic_id):
                relic_reward = CombatReward(RewardType.RELIC, relic=relic)
                key_reward = CombatReward(RewardType.SAPPHIRE_KEY, link=relic)
                game = GameStub([], [strike(), defend()])
                game.screen_type = ScreenType.COMBAT_REWARD
                game.screen = CombatRewardScreen([relic_reward, key_reward])
                game.key_system_unlocked = True
                game.has_emerald_key = False
                game.has_sapphire_key = False
                game.has_ruby_key = False
                game.act = 2
                game.are_potions_full = lambda: False
                agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
                agent.game = game

                action = agent.handle_screen()

                self.assertIsInstance(action, CombatRewardAction)
                self.assertIs(
                    key_reward if expect_key else relic_reward,
                    action.combat_reward,
                )
                decision = agent.last_noncombat_decision
                key_row = next(
                    row for row in decision["candidates"]
                    if row["consequences"].get("reward_type")
                    == "sapphire_key"
                )
                self.assertEqual(decision["key_value"], key_row["score"])
                self.assertEqual(
                    (
                        "sapphire_key_opportunity_value_v1"
                        if expect_key else
                        "sapphire_linked_reward_opportunity_v1"
                    ),
                    key_row["score_rule_id"],
                )

    def test_sapphire_first_order_outscores_independent_chest_relic(self):
        meal_ticket = Relic("MealTicket", "Meal Ticket")
        darkstone = Relic("Darkstone Periapt", "Darkstone Periapt")
        meal_reward = CombatReward(RewardType.RELIC, relic=meal_ticket)
        darkstone_reward = CombatReward(RewardType.RELIC, relic=darkstone)
        key_reward = CombatReward(
            RewardType.SAPPHIRE_KEY, link=darkstone
        )
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([
            meal_reward, darkstone_reward, key_reward,
        ])
        game.key_system_unlocked = True
        game.has_emerald_key = False
        game.has_sapphire_key = False
        game.has_ruby_key = False
        game.act = 2
        game.current_hp = game.player.current_hp = 69
        game.max_hp = game.player.max_hp = 85
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIs(key_reward, action.combat_reward)
        decision = agent.last_noncombat_decision
        rows = decision["candidates"]
        key_row = next(
            row for row in rows
            if row["consequences"].get("reward_type") == "sapphire_key"
        )
        meal_row = next(
            row for row in rows
            if row["consequences"].get("relic_id") == "MealTicket"
        )
        self.assertGreater(key_row["score"], meal_row["score"])
        self.assertEqual(
            "sapphire_key_first_collection_order_v1",
            key_row["score_rule_id"],
        )
        self.assertGreater(
            key_row["score_inputs"]["collection_order_priority"], 0
        )

    def test_sapphire_surface_marks_unlinked_rewards_independent(self):
        linked = Relic("DataDisk", "Data Disk")
        key_reward = CombatReward(RewardType.SAPPHIRE_KEY, link=linked)
        relic_reward = CombatReward(RewardType.RELIC, relic=linked)
        gold_reward = CombatReward(RewardType.GOLD, gold=25)
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        game.screen = CombatRewardScreen([
            key_reward, relic_reward, gold_reward,
        ])
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        rows = agent._combat_reward_audit_candidates()
        key_row = next(
            row for row in rows
            if row.get("consequences", {}).get("reward_type")
            == "sapphire_key"
        )
        by_type = {
            row["consequences"]["reward_type"]: row["consequences"]
            for row in rows if "reward_type" in row["consequences"]
        }

        self.assertEqual(
            "gain_sapphire_key", by_type["sapphire_key"]["operation"]
        )
        self.assertEqual(
            "gain_linked_relic", by_type["relic"]["operation"]
        )
        self.assertEqual(
            "collect_independent_reward", by_type["gold"]["operation"]
        )
        self.assertEqual(4.0, key_row["score"])
        self.assertEqual(
            "combat_reward_key_opportunity_v2", key_row["score_rule_id"]
        )

    def test_tactical_card_reward_retains_and_can_select_skip(self):
        bad = build_card("Temporary Liability")
        bad.uuid = "temporary-liability"
        game = GameStub([], [strike(), defend()])
        game.in_combat = True
        game.screen = CardRewardScreen([bad], can_bowl=False, can_skip=True)
        game.cancel_available = True
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(
            agent.combat_planner,
            "score_temporary_card",
            return_value=-5.0,
        ):
            action = agent.choose_card_reward()

        self.assertIsInstance(action, CancelAction)
        decision = agent.last_noncombat_decision
        self.assertEqual("action:return", decision["chosen_id"])
        self.assertEqual(
            {"card:temporary-liability", "action:return"},
            {row["choice_id"] for row in decision["candidates"]},
        )

    def test_ice_cream_x_cost_synergy_uses_cost_property_and_is_capped(self):
        ice_cream = Relic("Ice Cream", "Ice Cream")
        control = Relic("Anchor", "Anchor")
        scores = []
        control_scores = []
        for x_cost_count in (0, 1, 2, 3, 5):
            deck = [
                build_card(
                    f"Protocol X Card {index}",
                    CardType.SKILL,
                    cost=-1,
                )
                if index < x_cost_count
                else build_card(f"Ordinary Card {index}")
                for index in range(5)
            ]
            agent = SimpleAgent(PlayerClass.DEFECT)
            agent.game = GameStub([], deck)

            scores.append(agent._relic_acquisition_score(ice_cream))
            control_scores.append(agent._relic_acquisition_score(control))

        self.assertEqual([43.0, 49.0, 55.0, 61.0, 61.0], scores)
        self.assertEqual([28.0] * 5, control_scores)

    def test_act_one_ice_cream_beats_sapphire_for_every_role_and_reward_order(self):
        for player_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            for key_first in (False, True):
                with self.subTest(
                    player_class=player_class,
                    key_first=key_first,
                ):
                    deck = [
                        build_card(
                            "Arbitrary X Attack",
                            CardType.ATTACK,
                            cost=-1,
                        ),
                        build_card(
                            "Arbitrary X Skill",
                            CardType.SKILL,
                            cost=-1,
                        ),
                        strike(),
                        defend(),
                    ]
                    ice_cream = Relic("Ice Cream", "Ice Cream")
                    relic_reward = CombatReward(
                        RewardType.RELIC,
                        relic=ice_cream,
                    )
                    key_reward = CombatReward(
                        RewardType.SAPPHIRE_KEY,
                        link=ice_cream,
                    )
                    rewards = (
                        [key_reward, relic_reward]
                        if key_first
                        else [relic_reward, key_reward]
                    )
                    game = GameStub([], deck)
                    game.screen_type = ScreenType.COMBAT_REWARD
                    game.screen = CombatRewardScreen(rewards)
                    game.key_system_unlocked = True
                    game.has_ruby_key = False
                    game.has_emerald_key = False
                    game.has_sapphire_key = False
                    game.act = 1
                    game.floor = 9
                    agent = SimpleAgent(
                        player_class,
                        goal_mode="HEART",
                    )
                    agent.game = game

                    action = agent.handle_screen()

                    self.assertIsInstance(action, CombatRewardAction)
                    self.assertIs(relic_reward, action.combat_reward)
                    self.assertEqual(
                        4.0,
                        agent.last_noncombat_decision["key_value"],
                    )
                    self.assertEqual(
                        2,
                        agent.last_noncombat_decision[
                            "x_cost_card_count"
                        ],
                    )

    def test_sapphire_macro_replay_binds_decision_surface_scores(self):
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent._pending_macro_advice = {
            "decision_type": "SAPPHIRE_KEY",
            "rule_choice_id": "sapphire_key",
            "model_choice_id": "linked_relic:DataDisk",
            "final_choice_ids": ["sapphire_key"],
            "rankings": [
                {"candidate_id": "sapphire_key", "score": 1.0},
                {"candidate_id": "linked_relic:DataDisk", "score": 2.0},
            ],
            "replay": {
                "rule_choice_ids": ["sapphire_key"],
                "candidates": [
                    {"candidate_id": "sapphire_key", "local_score": 30.0},
                    {
                        "candidate_id": "linked_relic:DataDisk",
                        "local_score": 30.0,
                    },
                ],
            },
        }
        rows = [
            {
                "choice_id": "reward:sapphire_key:1", "score": 78.0,
                "consequences": {"reward_type": "sapphire_key"},
            },
            {
                "choice_id": "reward:relic:0", "score": 52.0,
                "consequences": {
                    "reward_type": "relic", "relic_id": "DataDisk",
                },
            },
        ]

        agent._bind_sapphire_macro_advice_to_reward_surface(rows)

        replay = agent._pending_macro_advice["replay"]
        by_id = {
            row["candidate_id"]: row for row in replay["candidates"]
        }
        self.assertEqual(
            78.0,
            by_id["reward:sapphire_key:1"]["decision_surface_score"],
        )
        self.assertEqual(
            52.0,
            by_id["reward:relic:0"]["decision_surface_score"],
        )
        self.assertEqual(30.0, by_id["reward:relic:0"]["local_score"])

    def test_sapphire_act_boundaries_preserve_low_value_and_deadline_cases(self):
        cases = (
            (1, Relic("Hand Drill", "Hand Drill"), [], True, 4.0),
            (2, Relic("Ice Cream", "Ice Cream"), [], False, 30.0),
            (
                3,
                Relic("Ice Cream", "Ice Cream"),
                [
                    build_card(
                        f"Deadline X Card {index}",
                        cost=-1,
                    )
                    for index in range(4)
                ],
                True,
                None,
            ),
        )
        for act, relic, deck, expect_key, expected_key_value in cases:
            with self.subTest(act=act, relic=relic.relic_id):
                relic_reward = CombatReward(
                    RewardType.RELIC,
                    relic=relic,
                )
                key_reward = CombatReward(
                    RewardType.SAPPHIRE_KEY,
                    link=relic,
                )
                game = GameStub([], deck or [strike(), defend()])
                game.screen_type = ScreenType.COMBAT_REWARD
                game.screen = CombatRewardScreen(
                    [key_reward, relic_reward]
                )
                game.key_system_unlocked = True
                game.has_ruby_key = False
                game.has_emerald_key = False
                game.has_sapphire_key = False
                game.act = act
                agent = SimpleAgent(
                    PlayerClass.IRONCLAD,
                    goal_mode="HEART",
                )
                agent.game = game

                action = agent.handle_screen()

                self.assertIsInstance(action, CombatRewardAction)
                self.assertIs(
                    key_reward if expect_key else relic_reward,
                    action.combat_reward,
                )
                self.assertEqual(
                    expected_key_value,
                    agent.last_noncombat_decision["key_value"],
                )

    def test_zero_benefit_random_upgrade_relic_does_not_defer_sapphire(self):
        for relic_id, card_type in (
            ("Whetstone", CardType.ATTACK),
            ("War Paint", CardType.SKILL),
        ):
            with self.subTest(relic_id=relic_id):
                already_upgraded = build_card(
                    f"{relic_id} target",
                    card_type,
                    upgrades=1,
                )
                relic = Relic(relic_id, relic_id)
                relic_reward = CombatReward(RewardType.RELIC, relic=relic)
                key_reward = CombatReward(
                    RewardType.SAPPHIRE_KEY,
                    link=relic,
                )
                game = GameStub([], [already_upgraded])
                game.screen_type = ScreenType.COMBAT_REWARD
                game.screen = CombatRewardScreen(
                    [relic_reward, key_reward]
                )
                game.key_system_unlocked = True
                game.has_sapphire_key = False
                game.act = 2
                game.are_potions_full = lambda: False
                agent = SimpleAgent(
                    PlayerClass.DEFECT,
                    goal_mode="HEART",
                )
                agent.game = game

                action = agent.handle_screen()

                self.assertIsInstance(action, CombatRewardAction)
                self.assertIs(key_reward, action.combat_reward)

    def test_random_upgrade_relic_has_zero_score_without_eligible_cards(self):
        game = GameStub(
            [],
            [
                build_card("Upgraded Attack", CardType.ATTACK, upgrades=1),
                build_card("Upgraded Skill", CardType.SKILL, upgrades=1),
            ],
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertEqual(
            0.0,
            agent._relic_acquisition_score(
                Relic("Whetstone", "Whetstone")
            ),
        )
        self.assertEqual(
            0.0,
            agent._relic_acquisition_score(
                Relic("War Paint", "War Paint")
            ),
        )

    def test_random_upgrade_relic_values_actual_upgrade_quality(self):
        cases = (
            (
                "Whetstone",
                [strike(), strike()],
                [
                    build_card("Glass Knife", CardType.ATTACK),
                    build_card("Glass Knife", CardType.ATTACK),
                ],
            ),
            (
                "War Paint",
                [defend(), defend()],
                [
                    build_card("Catalyst"),
                    build_card("Catalyst"),
                ],
            ),
        )
        for relic_id, basic_cards, premium_cards in cases:
            with self.subTest(relic_id=relic_id):
                relic = Relic(relic_id, relic_id)
                agent = SimpleAgent(PlayerClass.THE_SILENT)
                agent.game = GameStub([], basic_cards)
                basic_score = agent._relic_acquisition_score(relic)
                agent.game = GameStub([], premium_cards)
                premium_score = agent._relic_acquisition_score(relic)

                self.assertEqual(
                    sum(agent._upgrade_score(card) for card in premium_cards),
                    premium_score,
                )
                self.assertGreater(premium_score, basic_score)

    def test_random_upgrade_relic_uses_two_card_random_expectation(self):
        cards = [
            strike(),
            build_card("Glass Knife", CardType.ATTACK),
            build_card("Searing Blow", CardType.ATTACK),
        ]
        game = GameStub([], cards)
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        eligible_values = [agent._upgrade_score(card) for card in cards]

        score = agent._relic_acquisition_score(
            Relic("Whetstone", "Whetstone")
        )

        self.assertAlmostEqual(
            2.0 * sum(eligible_values) / len(eligible_values),
            score,
        )

    def test_act_one_sapphire_does_not_misclassify_unclaimed_mango(self):
        mango = Relic("Mango", "Mango")
        relic_reward = CombatReward(RewardType.RELIC, relic=mango)
        key_reward = CombatReward(RewardType.SAPPHIRE_KEY, link=mango)
        game = GameStub([], [strike(), defend()])
        game.screen_type = ScreenType.COMBAT_REWARD
        # Put the key first to verify the generic reward loop cannot bypass
        # HeartPlan's opportunity-cost decision.
        game.screen = CombatRewardScreen([key_reward, relic_reward])
        game.key_system_unlocked = True
        game.has_sapphire_key = False
        game.act = 1
        game.are_potions_full = lambda: False
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(relic_reward, action.combat_reward)

    def test_fusion_hammer_does_not_count_illegal_smith_against_recall(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(False, [RestOption.REST, RestOption.RECALL])
        game.relics = [Relic("Fusion Hammer", "Fusion Hammer")]
        game.act = 2
        game.floor = 28
        game.current_hp = game.max_hp
        game.has_ruby_key = False
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.RECALL, action.rest_option)

    def test_peace_pipe_toke_competes_with_smith_by_value(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False,
            [RestOption.REST, RestOption.SMITH, RestOption.TOKE],
        )
        game.relics = [Relic("Peace Pipe", "Peace Pipe")]
        game.act = 2
        game.floor = 25
        game.current_hp = game.max_hp
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.TOKE, action.rest_option)

    def test_act_two_pain_removal_beats_optional_recall(self):
        pain = build_card(
            "Pain",
            CardType.CURSE,
            rarity=CardRarity.CURSE,
        )
        game = GameStub([], [pain, strike(), defend()])
        game.screen = RestScreen(
            False,
            [
                RestOption.REST,
                RestOption.SMITH,
                RestOption.TOKE,
                RestOption.LIFT,
                RestOption.DIG,
                RestOption.RECALL,
            ],
        )
        game.relics = [Relic("Peace Pipe", "Peace Pipe")]
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 2
        game.floor = 28
        game.current_hp = game.max_hp
        agent = SimpleAgent(
            PlayerClass.THE_SILENT,
            goal_mode="HEART",
        )
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.TOKE, action.rest_option)

    def test_healthy_act_two_small_heal_is_used_for_early_recall(self):
        """Regression: 71/75 at floor 23 must not defer Ruby to floor 49."""

        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False,
            [RestOption.REST, RestOption.SMITH, RestOption.RECALL],
        )
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 2
        game.floor = 23
        game.current_hp = 71
        game.max_hp = 75
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertEqual(RestOption.RECALL, action.rest_option)
        self.assertEqual(
            "heart_plan_ruby_safe_window",
            agent.last_noncombat_decision["reason"],
        )
        recall = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == "rest:RECALL"
        )
        self.assertEqual(
            {"gain": ["ruby_key"]},
            recall["consequences"]["key_changes"],
        )

    def test_coffee_dripper_uses_healthy_act_two_recall_window(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False, [RestOption.SMITH, RestOption.RECALL]
        )
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 2
        game.floor = 25
        game.current_hp = 52
        game.max_hp = 56
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertEqual(RestOption.RECALL, action.rest_option)

    def test_act_three_first_survivable_fire_pays_ruby_debt(self):
        """Regression: 56/81 at floor 40 must leave later fires to heal."""

        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False,
            [RestOption.REST, RestOption.SMITH, RestOption.RECALL],
        )
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 3
        game.floor = 40
        game.current_hp = 56
        game.max_hp = 81
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertEqual(RestOption.RECALL, action.rest_option)
        self.assertTrue(agent.last_noncombat_decision["scheduled_recall"])

    def test_low_hp_act_three_fire_still_heals_before_ruby_deadline(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.RECALL]
        )
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 3
        game.floor = 40
        game.current_hp = 22
        game.max_hp = 75
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertEqual(RestOption.REST, action.rest_option)
        self.assertFalse(agent.last_noncombat_decision["scheduled_recall"])
        self.assertTrue(agent.last_noncombat_decision["mandatory_rest"])

    def test_act_one_recall_is_visible_only_at_low_cost_window(self):
        game = GameStub([], [strike(), defend()])
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 1
        game.floor = 6
        game.max_hp = 75
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        healthy_values, healthy_details = agent._campfire_option_values(
            [RestOption.REST, RestOption.SMITH, RestOption.RECALL],
            hp=71,
            floor_in_act=6,
        )
        injured_values, injured_details = agent._campfire_option_values(
            [RestOption.REST, RestOption.SMITH, RestOption.RECALL],
            hp=50,
            floor_in_act=6,
        )

        self.assertEqual(7.0, healthy_values[RestOption.RECALL])
        self.assertTrue(healthy_details["act_one_recall_candidate"])
        self.assertLess(injured_values[RestOption.RECALL], -900)
        self.assertFalse(injured_details["act_one_recall_candidate"])

    def test_healthy_act_two_final_fire_recalls_instead_of_small_heal(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False,
            [RestOption.REST, RestOption.SMITH, RestOption.RECALL],
        )
        game.relics = [Relic("Regal Pillow", "Regal Pillow")]
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 2
        game.floor = 32
        game.current_hp = 59
        game.max_hp = 70
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.RECALL, action.rest_option)

    def test_injured_act_two_final_fire_may_still_rest_for_boss(self):
        game = GameStub([], [strike(), defend()])
        game.screen = RestScreen(
            False, [RestOption.REST, RestOption.RECALL]
        )
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 2
        game.floor = 32
        game.current_hp = 35
        game.max_hp = 70
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_rest_option()

        self.assertEqual(RestOption.REST, action.rest_option)

    def test_mark_of_the_bloom_never_values_rest_as_recovery(self):
        game = GameStub([], [build_card("Wraith Form"), strike()])
        game.screen = RestScreen(
            False,
            [RestOption.REST, RestOption.SMITH],
        )
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        game.act = 3
        game.floor = 42
        game.current_hp = 12
        game.max_hp = 70
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.SMITH, action.rest_option)
        self.assertTrue(agent.last_noncombat_decision["healing_blocked"])

    def test_regal_pillow_adds_fifteen_to_normal_rest_recovery(self):
        game = GameStub([], [strike(), defend()])
        game.relics = [Relic("Regal Pillow", "Regal Pillow")]
        game.max_hp = 70
        game.current_hp = 20
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        _, details = agent._campfire_option_values(
            [RestOption.REST],
            hp=20,
            floor_in_act=10,
        )

        self.assertEqual(36, details["actual_recovery"])

    def test_final_act_three_campfire_recalls_for_ruby_key_for_every_class(self):
        for player_class in (PlayerClass.IRONCLAD, PlayerClass.THE_SILENT, PlayerClass.DEFECT):
            with self.subTest(player_class=player_class):
                game = GameStub([], [])
                game.screen = RestScreen(False, [RestOption.REST, RestOption.SMITH, RestOption.RECALL])
                game.key_system_unlocked = True
                game.has_ruby_key = False
                game.act = 3
                game.floor = 49
                game.current_hp = 70
                game.max_hp = 70
                agent = SimpleAgent(player_class, goal_mode="HEART")
                agent.game = game
                action = agent.choose_rest_option()
                self.assertIsInstance(action, RestAction)
                self.assertEqual(RestOption.RECALL, action.rest_option)

    def test_final_act_three_recall_deadline_overrides_pain_removal(self):
        pain = build_card(
            "Pain",
            CardType.CURSE,
            rarity=CardRarity.CURSE,
        )
        game = GameStub([], [pain])
        game.screen = RestScreen(
            False,
            [RestOption.TOKE, RestOption.RECALL],
        )
        game.relics = [Relic("Peace Pipe", "Peace Pipe")]
        game.key_system_unlocked = True
        game.has_ruby_key = False
        game.act = 3
        game.floor = 49
        game.current_hp = game.max_hp
        agent = SimpleAgent(
            PlayerClass.IRONCLAD,
            goal_mode="HEART",
        )
        agent.game = game

        action = agent.choose_rest_option()

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.RECALL, action.rest_option)

    @staticmethod
    def _masked_bandits_strong_defect_deck():
        return [
            build_card(
                "Ball Lightning", CardType.ATTACK,
                damage=7, upgrades=1,
            ),
            build_card("Cold Snap", CardType.ATTACK, damage=6),
            build_card("Glacier", CardType.SKILL, block=7),
            build_card("Coolheaded", CardType.SKILL, block=5),
            build_card("Defragment", CardType.POWER),
            build_card("Electrodynamics", CardType.POWER),
        ]

    def test_masked_bandits_replays_recent_full_34_hp_199_gold_frame(self):
        game, agent = self._masked_bandits_game(
            self._masked_bandits_strong_defect_deck(),
            hp=34,
            max_hp=34,
            gold=199,
            player_class=PlayerClass.DEFECT,
            floor=21,
        )
        game.act_boss = "Bronze Automaton"
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertEqual(
            "unified_net_utility_v2", decision["decision_model"]
        )
        self.assertEqual("fight", decision["chosen_kind"])
        self.assertEqual(
            decision["chosen_kind"], decision["candidate_argmax_kind"]
        )
        scores = {
            int(row["id"]): row["score"]
            for row in decision["candidates"]
        }
        self.assertEqual(
            action.choice_index,
            max(scores, key=lambda choice_index: scores[choice_index]),
        )
        self.assertEqual(
            scores[action.choice_index], decision["chosen_score"]
        )
        for row in decision["candidates"]:
            self.assertTrue(row["score_rule_id"].startswith(
                "masked_bandits_"
            ))
            self.assertEqual(
                "sum_components_v1", row["score_formula"]["kind"]
            )
            self.assertAlmostEqual(
                row["score"],
                sum(component["value"] for component in row[
                    "score_components"
                ]),
                places=7,
            )
        self.assertFalse(decision["hard_survival_veto"])

    def test_masked_bandits_gold_increase_cannot_reverse_fight_to_pay(self):
        cards = [
            build_card(
                "Predator", CardType.ATTACK,
                damage=15, upgrades=1,
            ),
            build_card("Dash", CardType.ATTACK, damage=10),
            build_card(
                "Backflip", CardType.SKILL,
                block=5, rarity=CardRarity.COMMON,
            ),
            build_card("Footwork", CardType.POWER),
        ]
        choices = []
        fight_scores = []
        pay_scores = []
        for gold in (0, 40, 85, 199, 500):
            game, agent = self._masked_bandits_game(
                cards, hp=65, max_hp=70, gold=gold
            )
            agent.game = game
            action = agent.handle_screen()
            profile = agent.last_noncombat_decision[
                "masked_bandits_profile"
            ]
            choices.append(action.choice_index)
            fight_scores.append(profile["scores"]["fight"])
            pay_scores.append(profile["scores"]["pay"])

        self.assertEqual(sorted(fight_scores), fight_scores)
        self.assertEqual(
            sorted(pay_scores, reverse=True), pay_scores
        )
        self.assertEqual(sorted(choices), choices)
        self.assertIn(0, choices)
        self.assertIn(1, choices)

    def test_masked_bandits_hp_increase_cannot_reverse_fight_to_pay(self):
        cards = self._masked_bandits_strong_defect_deck()
        choices = []
        fight_scores = []
        for hp in (8, 16, 24, 34, 48, 68):
            game, agent = self._masked_bandits_game(
                cards,
                hp=hp,
                max_hp=68,
                gold=199,
                player_class=PlayerClass.DEFECT,
                floor=25,
            )
            agent.game = game
            action = agent.handle_screen()
            choices.append(action.choice_index)
            fight_scores.append(
                agent.last_noncombat_decision[
                    "masked_bandits_profile"
                ]["scores"]["fight"]
            )

        self.assertEqual(sorted(fight_scores), fight_scores)
        self.assertEqual(sorted(choices), choices)
        self.assertIn(0, choices)
        self.assertIn(1, choices)

    def test_masked_bandits_strength_increase_cannot_reverse_fight_to_pay(self):
        weak = [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(5)),
        ]
        medium = [
            build_card("Ball Lightning", CardType.ATTACK, damage=7),
            build_card("Cold Snap", CardType.ATTACK, damage=6),
            build_card("Glacier", CardType.SKILL, block=7),
            build_card("Coolheaded", CardType.SKILL, block=5),
            *(strike() for _ in range(3)),
            *(defend() for _ in range(3)),
        ]
        strong = self._masked_bandits_strong_defect_deck() + [
            build_card("Sweeping Beam", CardType.ATTACK, damage=6),
            build_card("Charge Battery", CardType.SKILL, block=7),
            build_card("Compile Driver", CardType.ATTACK, damage=7),
            build_card("Hologram", CardType.SKILL, block=5),
        ]
        choices = []
        fight_scores = []
        strengths = []
        for deck in (weak, medium, strong):
            game, agent = self._masked_bandits_game(
                deck,
                hp=52,
                max_hp=68,
                gold=240,
                player_class=PlayerClass.DEFECT,
            )
            agent.game = game
            action = agent.handle_screen()
            profile = agent.last_noncombat_decision[
                "masked_bandits_profile"
            ]
            choices.append(action.choice_index)
            fight_scores.append(profile["scores"]["fight"])
            strengths.append(profile["combat_strength"])

        self.assertEqual(sorted(strengths), strengths)
        self.assertEqual(sorted(fight_scores), fight_scores)
        self.assertEqual(sorted(choices), choices)
        self.assertEqual(0, choices[0])
        self.assertEqual(1, choices[-1])

    def test_masked_bandits_effective_potions_only_improve_fight_score(self):
        cards = [
            build_card("Predator", CardType.ATTACK, damage=15),
            build_card("Dash", CardType.ATTACK, damage=10),
            build_card("Backflip", CardType.SKILL, block=5),
            build_card("Footwork", CardType.POWER),
        ]
        potion_sets = [
            [],
            [Potion("FirePotion", "Fire Potion", True, True, True)],
            [
                Potion("FirePotion", "Fire Potion", True, True, True),
                Potion("BlockPotion", "Block Potion", True, True, False),
            ],
            [
                Potion("FirePotion", "Fire Potion", True, True, True),
                Potion("BlockPotion", "Block Potion", True, True, False),
                Potion("GhostInAJar", "Ghost in a Jar", True, True, False),
            ],
        ]
        choices = []
        fight_scores = []
        for potions in potion_sets:
            game, agent = self._masked_bandits_game(
                cards,
                hp=36,
                max_hp=68,
                gold=180,
                potions=potions,
            )
            agent.game = game
            action = agent.handle_screen()
            choices.append(action.choice_index)
            fight_scores.append(
                agent.last_noncombat_decision[
                    "masked_bandits_profile"
                ]["scores"]["fight"]
            )

        self.assertEqual(sorted(fight_scores), fight_scores)
        self.assertEqual(sorted(choices), choices)

    def test_masked_bandits_option_order_does_not_change_choice_or_scores(self):
        results = []
        for option_order in (("pay", "fight"), ("fight", "pay")):
            game, agent = self._masked_bandits_game(
                self._masked_bandits_strong_defect_deck(),
                hp=40,
                max_hp=40,
                gold=213,
                option_order=option_order,
                player_class=PlayerClass.DEFECT,
                floor=20,
            )
            agent.game = game
            action = agent.handle_screen()
            results.append((
                action.choice_index,
                {
                    row["id"]: row["score"]
                    for row in agent.last_noncombat_decision["candidates"]
                },
            ))

        self.assertEqual(results[0], results[1])
        self.assertEqual(1, results[0][0])

    def test_masked_bandits_one_hp_hard_veto_is_logged_and_is_argmax(self):
        game, agent = self._masked_bandits_game(
            self._masked_bandits_strong_defect_deck(),
            hp=1,
            max_hp=68,
            gold=500,
            player_class=PlayerClass.DEFECT,
        )
        agent.game = game

        action = agent.handle_screen()

        self.assertEqual(0, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertTrue(decision["hard_survival_veto"])
        self.assertEqual(
            "one_hp_without_revival_or_escape",
            decision["hard_survival_veto_reason"],
        )
        scores = {
            int(row["id"]): row["score"]
            for row in decision["candidates"]
        }
        self.assertEqual(
            action.choice_index,
            max(scores, key=lambda choice_index: scores[choice_index]),
        )
        fight_row = next(
            row for row in decision["candidates"]
            if row["semantic_kind"] == "fight"
        )
        self.assertAlmostEqual(
            fight_row["score"],
            sum(component["value"] for component in fight_row[
                "score_components"
            ]),
            places=7,
        )

    def test_masked_bandits_certain_death_cannot_be_bought_by_extreme_gold(self):
        game, agent = self._masked_bandits_game(
            [*(strike() for _ in range(5)), *(defend() for _ in range(5))],
            hp=2,
            max_hp=70,
            gold=9999,
            player_class=PlayerClass.THE_SILENT,
        )
        agent.game = game

        action = agent.handle_screen()

        self.assertEqual(0, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertTrue(decision["hard_survival_veto"])
        self.assertEqual(
            "predicted_certain_death_without_revival_or_escape",
            decision["hard_survival_veto_reason"],
        )
        self.assertEqual(1.0, decision["masked_bandits_profile"]["death_risk"])

    def test_masked_bandits_spent_lizard_tail_is_not_a_revival_out(self):
        cards = [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(5)),
        ]
        outcomes = []
        for counter in (-2, -1):
            game, agent = self._masked_bandits_game(
                cards,
                hp=2,
                max_hp=70,
                gold=9999,
                player_class=PlayerClass.THE_SILENT,
            )
            game.relics = [
                Relic("Lizard Tail", "Lizard Tail", counter=counter)
            ]
            agent.game = game

            action = agent.handle_screen()
            outcomes.append((
                action.choice_index,
                agent.last_noncombat_decision["masked_bandits_profile"],
            ))

        spent_action, spent = outcomes[0]
        self.assertEqual(0, spent_action)
        self.assertTrue(spent["hard_survival_veto"])
        self.assertNotIn("lizardtail", spent["emergency_outs"])
        self.assertEqual(1.0, spent["death_risk"])

        active_action, active = outcomes[1]
        self.assertEqual(1, active_action)
        self.assertFalse(active["hard_survival_veto"])
        self.assertIn("lizardtail", active["emergency_outs"])
        self.assertLess(active["death_risk"], 1.0)

    def test_masked_bandits_ghost_is_mitigation_not_a_survival_exit(self):
        cards = [
            *(strike() for _ in range(5)),
            *(defend() for _ in range(5)),
        ]
        outcomes = {}
        for potion_id in ("GhostInAJar", "SmokeBomb"):
            potion = Potion(
                potion_id, potion_id, True, True, False
            )
            game, agent = self._masked_bandits_game(
                cards,
                hp=1,
                max_hp=70,
                gold=9999,
                potions=[potion],
                player_class=PlayerClass.THE_SILENT,
            )
            agent.game = game

            action = agent.handle_screen()
            outcomes[potion_id] = (
                action.choice_index,
                agent.last_noncombat_decision["masked_bandits_profile"],
            )

        ghost_action, ghost = outcomes["GhostInAJar"]
        self.assertEqual(0, ghost_action)
        self.assertTrue(ghost["hard_survival_veto"])
        self.assertIn("ghostinajar", ghost["mitigation_outs"])
        self.assertNotIn("ghostinajar", ghost["survival_exit_outs"])

        smoke_action, smoke = outcomes["SmokeBomb"]
        self.assertEqual(1, smoke_action)
        self.assertFalse(smoke["hard_survival_veto"])
        self.assertIn("smokebomb", smoke["survival_exit_outs"])

    def test_masked_bandits_extra_glacier_does_not_make_eight_hp_safe(self):
        base = []
        for _ in range(3):
            base.extend(self._masked_bandits_strong_defect_deck())
        base.extend([
            build_card("Charge Battery", CardType.SKILL, block=7),
            build_card("Sweeping Beam", CardType.ATTACK, damage=6),
        ])
        results = []
        for deck in (
            base,
            base + [
                build_card(
                    "Glacier", CardType.SKILL, block=10, upgrades=1,
                )
            ],
        ):
            game, agent = self._masked_bandits_game(
                deck,
                hp=8,
                max_hp=70,
                gold=110,
                player_class=PlayerClass.DEFECT,
            )
            agent.game = game
            action = agent.handle_screen()
            profile = agent.last_noncombat_decision["masked_bandits_profile"]
            results.append((
                profile["combat_strength"],
                profile["scores"]["fight"],
                action.choice_index,
            ))

        self.assertGreaterEqual(results[1][0], results[0][0])
        # More block does not guarantee faster access to offense. Both
        # bloated decks must preserve their eight HP instead of gambling.
        self.assertEqual([0, 0], [row[2] for row in results])

    def test_masked_bandits_upgraded_noncombat_cards_do_not_fake_a_win_path(self):
        deck = [
            build_card(
                "Prepared" if index % 2 == 0 else "Setup",
                CardType.SKILL,
                upgrades=1,
            )
            for index in range(20)
        ]
        game, agent = self._masked_bandits_game(
            deck,
            hp=20,
            max_hp=70,
            gold=500,
            player_class=PlayerClass.THE_SILENT,
        )
        agent.game = game

        action = agent.handle_screen()

        self.assertEqual(0, action.choice_index)
        profile = agent.last_noncombat_decision["masked_bandits_profile"]
        self.assertEqual(0.0, profile["reliable_damage_sources"])
        self.assertTrue(profile["hard_survival_veto"])
        self.assertEqual(
            "no_reliable_damage_or_escape",
            profile["hard_survival_veto_reason"],
        )

    def test_masked_bandits_accepts_proven_indirect_damage_engines(self):
        cases = [
            (
                PlayerClass.DEFECT,
                [
                    build_card("Zap", CardType.SKILL, upgrades=1),
                    build_card("Electrodynamics", CardType.POWER),
                    build_card("Defragment", CardType.POWER),
                    build_card("Tempest", CardType.SKILL),
                    build_card("Glacier", CardType.SKILL, block=7),
                ],
            ),
            (
                PlayerClass.THE_SILENT,
                [
                    build_card("Noxious Fumes", CardType.POWER),
                    build_card("Deadly Poison", CardType.SKILL),
                    build_card("Bouncing Flask", CardType.SKILL),
                    build_card("Catalyst", CardType.SKILL),
                    build_card("Footwork", CardType.POWER),
                ],
            ),
        ]
        for player_class, deck in cases:
            with self.subTest(player_class=player_class):
                game, agent = self._masked_bandits_game(
                    deck,
                    # This checks that indirect offense is eligible, not
                    # that an under-defended deck must fight at reduced HP.
                    hp=70,
                    max_hp=70,
                    gold=200,
                    player_class=player_class,
                )
                agent.game = game

                action = agent.handle_screen()

                profile = agent.last_noncombat_decision[
                    "masked_bandits_profile"
                ]
                self.assertGreater(profile["reliable_damage_sources"], 0.0)
                self.assertNotEqual(
                    "no_reliable_damage_or_escape",
                    profile["hard_survival_veto_reason"],
                )
                self.assertEqual(1, action.choice_index)

    @staticmethod
    def _mushrooms_game(hp):
        deck = [
            *(build_card(
                "Strike_R", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            ) for _ in range(5)),
            *(build_card(
                "Defend_R", CardType.SKILL, block=5,
                rarity=CardRarity.BASIC,
            ) for _ in range(4)),
            build_card(
                "Bash", CardType.ATTACK, cost=2, damage=8,
                rarity=CardRarity.BASIC,
            ),
            build_card("Hemokinesis", CardType.ATTACK, damage=15),
            build_card("Uppercut", CardType.ATTACK, cost=2, damage=13),
            build_card("Whirlwind", CardType.ATTACK, cost=-1, damage=5),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 1
        game.floor = 10
        game.act_boss = "The Guardian"
        game.current_hp = game.player.current_hp = hp
        game.max_hp = game.player.max_hp = 96
        game.gold = 172
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("garbled", "Mushrooms", "")
        parasite = build_card(
            "Parasite", CardType.CURSE, cost=-2,
            rarity=CardRarity.CURSE,
        )
        fight = EventOption(
            "[garbled] unreadable", "garbled", False, 0,
            original_button_index=0,
        )
        recover = EventOption(
            "[garbled] unreadable 24", "garbled", False, 1,
            card=parasite, original_button_index=1,
        )
        # The visible container is deliberately reversed.  Original button
        # identity, not localized text or list position, binds the outcome.
        game.screen.options = [recover, fight]
        return game, SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

    def test_mushrooms_live_frame_uses_exact_indexed_reward_and_curse_model(self):
        game, agent = self._mushrooms_game(73)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertEqual(
            "mushrooms_reward_vs_recovery", decision["reason"]
        )
        self.assertEqual(
            "exact_event_id_original_button_index_parasite_preview",
            decision["protocol_identity"],
        )
        candidates = {
            int(row["id"]): row for row in decision["candidates"]
        }
        fight = candidates[0]
        recover = candidates[1]
        self.assertEqual(
            "mushrooms_prepare_combat",
            fight["consequences"]["operation"],
        )
        self.assertEqual(
            "Odd Mushroom",
            fight["consequences"]["future_costs"][0]["reward"]["relic_id"],
        )
        self.assertGreaterEqual(
            fight["score_inputs"]["odd_mushroom_value"], 28.0
        )
        self.assertEqual(23, recover["consequences"]["hp_delta"])
        self.assertEqual("Parasite", recover["consequences"]["curse_id"])
        self.assertEqual(1, recover["consequences"]["raw_curse_delta"])
        for candidate in candidates.values():
            self.assertAlmostEqual(
                candidate["score"],
                sum(item["value"] for item in candidate["score_components"]),
            )

    def test_mushrooms_low_hp_prefers_recovery_over_optional_fight(self):
        game, agent = self._mushrooms_game(8)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertEqual(
            "heal_and_take_parasite", decision["chosen_kind"]
        )
        self.assertTrue(
            decision["mushrooms_profile"]["hard_survival_veto"]
        )

    def test_mushrooms_mark_of_the_bloom_does_not_claim_healing(self):
        game, agent = self._mushrooms_game(8)
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        agent.game = game

        agent.handle_screen()

        recover = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }[1]
        self.assertEqual(0, recover["consequences"]["hp_delta"])
        self.assertNotIn("requested_hp_delta", recover["consequences"])

    def test_library_uses_exact_rounded_heal_and_mark_of_bloom_lock(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = game.player.current_hp = 10
        game.max_hp = game.player.max_hp = 50
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("garbled", "The Library", "")
        game.screen.options = [
            EventOption("garbled", "garbled", False, 0,
                        original_button_index=0),
            EventOption("garbled", "garbled", False, 1,
                        original_button_index=1),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        sleep = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }[1]
        self.assertEqual(17, sleep["consequences"]["hp_delta"])

        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        agent.handle_screen()
        sleep = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }[1]
        self.assertEqual(0, sleep["consequences"]["hp_delta"])

    def test_winding_halls_applies_tungsten_and_mark_of_the_bloom(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 44
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 30
        game.max_hp = game.player.max_hp = 80
        game.relics = [
            Relic("Tungsten Rod", "Tungsten Rod"),
            Relic("Mark of the Bloom", "Mark of the Bloom"),
        ]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "Winding Halls", "",
            event_class=(
                "com.megacrit.cardcrawl.events.beyond.WindingHalls"
            ),
            screen_num=1,
        )
        game.screen.options = [
            EventOption("garbled", "garbled", False, index,
                        original_button_index=index)
            for index in range(3)
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        candidates = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(-9, candidates[0]["consequences"]["hp_delta"])
        self.assertEqual(
            10, candidates[0]["consequences"]["nominal_hp_loss"]
        )
        self.assertEqual(0, candidates[1]["consequences"]["hp_delta"])

    def test_winding_halls_uses_game_half_up_percentage_rounding(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 36
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 64
        game.max_hp = game.player.max_hp = 90
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "Winding Halls", "",
            event_class=(
                "com.megacrit.cardcrawl.events.beyond.WindingHalls"
            ),
            screen_num=1,
        )
        game.screen.options = [
            EventOption("garbled", "garbled", False, index,
                        original_button_index=index)
            for index in range(3)
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        candidates = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(-11, candidates[0]["consequences"]["hp_delta"])
        self.assertEqual(23, candidates[1]["consequences"]["hp_delta"])
        self.assertEqual(-5, candidates[2]["consequences"]["max_hp_delta"])

    def test_winding_halls_counts_darkstone_hp_and_max_hp_gain(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 39
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 11
        game.max_hp = game.player.max_hp = 88
        game.relics = [
            Relic("Darkstone Periapt", "Darkstone Periapt")
        ]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "Winding Halls", "",
            event_class=(
                "com.megacrit.cardcrawl.events.beyond.WindingHalls"
            ),
            screen_num=1,
        )
        game.screen.options = [
            EventOption(
                "garbled", "garbled", False, index,
                original_button_index=index,
            )
            for index in range(3)
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        focus = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }[1]
        self.assertEqual(28, focus["consequences"]["hp_delta"])
        self.assertEqual(6, focus["consequences"]["max_hp_delta"])

    def test_moai_head_values_full_heal_after_max_hp_loss(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.act = 3
        game.floor = 39
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 39
        game.max_hp = game.player.max_hp = 94
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "The Moai Head", "",
            event_class=(
                "com.megacrit.cardcrawl.events.beyond.MoaiHead"
            ),
        )
        game.screen.options = [
            EventOption(
                "jump", "jump", False, 0,
                original_button_index=0,
            ),
            EventOption(
                "leave", "leave", False, 1,
                original_button_index=2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        candidates = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        jump = candidates[0]
        self.assertEqual(43, jump["consequences"]["hp_delta"])
        self.assertEqual(-12, jump["consequences"]["max_hp_delta"])
        self.assertGreater(jump["score"], candidates[1]["score"])

    def test_moai_head_singleton_is_dialog_advance_not_leave_choice(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = game.player.current_hp = 50
        game.max_hp = game.player.max_hp = 80
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("localized", "The Moai Head", "")
        game.screen.options = [
            EventOption(
                "continue", "continue", False, 0,
                original_button_index=0,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        candidate = agent.last_noncombat_decision["candidates"][0]
        self.assertEqual(
            "themoaihead_dialog_advance_noop",
            candidate["consequences"]["operation"],
        )
        self.assertEqual(
            "dialog_advance",
            candidate["consequences"]["event_outcome_id"],
        )

    def test_nloth_trade_candidates_are_typed_and_recomputable(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        game.relics = [
            Relic("Whetstone", "Whetstone"),
            Relic("PreservedInsect", "Preserved Insect"),
        ]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("localized", "N'loth", "")
        game.screen.options = [
            EventOption(
                "Trade Whetstone", "Trade Whetstone", False, 0,
                original_button_index=0,
            ),
            EventOption(
                "Trade Preserved Insect", "Trade Preserved Insect",
                False, 1, original_button_index=1,
            ),
            EventOption(
                "Leave", "Leave", False, 2,
                original_button_index=2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        agent.handle_screen()

        candidates = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        for index in (0, 1):
            candidate = candidates[index]
            self.assertEqual(
                "nloth_trade_value_v1", candidate["score_rule_id"]
            )
            self.assertEqual(
                [{"id": "Nloth's Gift"}],
                candidate["consequences"]["relic_changes"]["gain"],
            )
            self.assertAlmostEqual(
                candidate["score"],
                sum(
                    component["value"]
                    for component in candidate["score_components"]
                ),
            )
        self.assertEqual("nloth_leave_v1", candidates[2]["score_rule_id"])

    def test_event_screen_preserves_reflected_progress_metadata(self):
        screen = EventScreen.from_json({
            "event_name": "localized",
            "event_id": "Shining Light",
            "body_text": "localized",
            "event_class": (
                "com.megacrit.cardcrawl.events.exordium.ShiningLight"
            ),
            "event_stage": "INTRO",
            "options": [],
        })

        self.assertEqual(
            "com.megacrit.cardcrawl.events.exordium.ShiningLight",
            screen.event_class,
        )
        self.assertEqual("INTRO", screen.event_stage)
        self.assertIsNone(screen.screen_num)

    def test_event_damage_candidates_apply_exact_passive_relics(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = game.player.current_hp = 20
        game.max_hp = game.player.max_hp = 20
        game.relics = [
            Relic("Torii", "Torii"),
            Relic("Tungsten Rod", "Tungsten Rod"),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.screen = EventScreen(
            "localized", "Shining Light", "",
            event_class=(
                "com.megacrit.cardcrawl.events.exordium.ShiningLight"
            ),
            event_stage="INTRO",
        )
        enter = EventOption(
            "garbled", "garbled", False, 0, original_button_index=0
        )
        leave = EventOption(
            "garbled", "garbled", False, 1, original_button_index=1
        )
        game.screen.options = [enter, leave]
        shining = agent._event_option_consequences(enter)
        self.assertEqual(4, shining["nominal_hp_loss"])
        self.assertEqual(0, shining["hp_delta"])

        game.screen = EventScreen(
            "localized", "Sensory Stone", "",
            event_class=(
                "com.megacrit.cardcrawl.events.beyond.SensoryStone"
            ),
            event_stage="INTRO_2",
        )
        game.screen.options = [
            EventOption("garbled", "garbled", False, index,
                        original_button_index=index)
            for index in range(3)
        ]
        sensory = agent._event_option_consequences(game.screen.options[2])
        self.assertEqual(10, sensory["nominal_hp_loss"])
        self.assertEqual(-9, sensory["hp_delta"])

    def test_living_wall_scores_the_actual_deck_instead_of_fixed_order(self):
        weak = strike()
        strong = build_card(
            "Bash", CardType.ATTACK, cost=2, damage=8,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([], [weak, strong, defend()])
        game.in_combat = False
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("Living Wall", "Living Wall", "")
        game.screen.options = [
            EventOption("Remove", "Remove", False, 7,
                        original_button_index=0),
            EventOption("Transform", "Transform", False, 3,
                        original_button_index=1),
            EventOption("Upgrade", "Upgrade", False, 5,
                        original_button_index=2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        removal_values = {id(weak): 70.0, id(strong): 4.0}
        with (
            patch.object(
                agent, "_removal_score",
                side_effect=lambda card: removal_values.get(id(card), 2.0),
            ),
            patch.object(agent, "_upgrade_score", return_value=20.0),
        ):
            action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(7, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertEqual(
            "living_wall_deck_specific_operation_value",
            decision["reason"],
        )
        rows = {
            row["consequences"]["operation"]: row
            for row in decision["candidates"]
        }
        self.assertEqual(
            {"remove", "transform", "upgrade"},
            set(rows),
        )
        self.assertEqual(70.0, rows["remove"]["score"])
        self.assertEqual(14.0, rows["transform"]["score"])
        self.assertEqual(20.0, rows["upgrade"]["score"])
        for row in rows.values():
            self.assertTrue(row["reason"])
            self.assertAlmostEqual(
                row["score"],
                sum(component["value"] for component in row["score_components"]),
            )

    def test_golden_wing_prices_hp_against_actual_removal_value(self):
        weak = strike()
        game = GameStub([], [weak, defend()])
        game.in_combat = False
        game.current_hp = game.player.current_hp = 40
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("Golden Wing", "Golden Wing", "")
        game.screen.options = [
            EventOption("Pray", "Pray", False, 4,
                        original_button_index=0),
            EventOption("Destroy", "Destroy", False, 8,
                        original_button_index=1),
            EventOption("Leave", "Leave", False, 2,
                        original_button_index=2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        with patch.object(agent, "_removal_score", return_value=50.0):
            action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(4, action.choice_index)
        rows = {
            row["consequences"]["operation"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        pray = rows["golden_wing_pray_remove"]
        self.assertEqual(34.25, pray["score"])
        self.assertEqual(-7, pray["consequences"]["hp_delta"])
        self.assertEqual(
            "remove", pray["consequences"]["future_costs"][0]["operation"]
        )
        self.assertEqual(7.8, rows["golden_wing_destroy_for_gold"]["score"])
        for row in rows.values():
            self.assertTrue(row["reason"])
            self.assertAlmostEqual(
                row["score"],
                sum(component["value"] for component in row["score_components"]),
            )

    def test_match_game_explores_then_uses_a_remembered_pair(self):
        game = GameStub([], [])
        game.screen_type = ScreenType.EVENT
        game.current_hp = 1  # Low HP must not trigger the generic last-option rule.
        game.max_hp = 80
        game.screen = EventScreen("Match and Keep", "Match and Keep!", "")
        game.screen.options = [
            EventOption("card0", "card0", False, 0),
            EventOption("card1", "card1", False, 1),
            EventOption("card2", "card2", False, 2),
            EventOption("card3", "card3", False, 3),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        first = agent.handle_screen()
        self.assertIsInstance(first, ChooseAction)
        self.assertEqual(0, first.choice_index)
        first_decision = agent.last_noncombat_decision
        self.assertEqual(4, len(first_decision["candidates"]))
        self.assertTrue(
            first_decision["candidate_contract"][
                "all_visible_options_scored"
            ]
        )
        self.assertEqual(
            {"match:0", "match:1", "match:2", "match:3"},
            {row["choice_id"] for row in first_decision["candidates"]},
        )
        for row in first_decision["candidates"]:
            self.assertIn("choice_index", row)
            self.assertIn("consequences", row)
            self.assertTrue(row["reason"])

        game.screen.options[0] = EventOption("Strike_G", "Strike_G", False, 0)
        second = agent.handle_screen()
        self.assertIsInstance(second, ChooseAction)
        self.assertEqual(1, second.choice_index)
        retry = agent.handle_screen()
        self.assertIsInstance(retry, ChooseAction)
        self.assertEqual(1, retry.choice_index)

        # The first attempt revealed different cards. Explore a new card,
        # then immediately pair it with the remembered matching Strike.
        game.screen.options[1] = EventOption("Defend_G", "Defend_G", False, 1)
        self.assertIsNone(agent.handle_screen())
        third = agent.handle_screen()
        self.assertEqual(2, third.choice_index)
        game.screen.options[2] = EventOption("Strike_G", "Strike_G", False, 2)
        fourth = agent.handle_screen()
        self.assertEqual(0, fourth.choice_index)

    def test_vampires_declines_when_only_one_strike_remains(self):
        strike_r = build_card(
            "Strike_R", CardType.ATTACK, damage=6,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([], [strike_r, defend(), defend(), defend(), defend()])
        game.in_combat = False
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("Vampires", "Vampires", "")
        game.screen.options = [
            EventOption(
                "Remove all Strikes. Obtain 5 Bites.",
                "Accept",
                False,
                0,
                build_card("Bite", CardType.ATTACK, damage=7),
            ),
            EventOption("Refuse", "Refuse", False, 1),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "vampires_signed_post_state",
            agent.last_noncombat_decision["reason"],
        )
        self.assertEqual(
            "decline", agent.last_noncombat_decision["chosen_outcome"]
        )

    def test_vampires_keeps_max_hp_and_blood_vial_accepts_distinct(self):
        strikes = [
            build_card(
                "Strike_R", CardType.ATTACK, damage=6,
                rarity=CardRarity.BASIC,
            )
            for _ in range(5)
        ]
        game = GameStub([], strikes + [defend(), defend()])
        game.in_combat = False
        game.current_hp = 60
        game.max_hp = 70
        game.relics = [Relic("Blood Vial", "Blood Vial")]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("Vampires", "Vampires", "")
        game.screen.options = [
            EventOption(
                "Remove all Strikes. Receive 5 Bites. Lose 30% Max HP.",
                "Accept",
                False,
                0,
            ),
            EventOption(
                "Lose Blood Vial. Remove all Strikes. Receive 5 Bites.",
                "Lose Blood Vial",
                False,
                1,
            ),
            EventOption("Refuse", "Refuse", False, 2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        outcomes = agent.last_noncombat_decision["outcome_facts"]
        self.assertEqual("accept_bites", outcomes["0"]["outcome_id"])
        self.assertEqual(-21, outcomes["0"]["consequences"]["max_hp_delta"])
        self.assertEqual(0, outcomes["0"]["consequences"]["relic_delta"])
        self.assertEqual(
            "trade_blood_vial_for_bites", outcomes["1"]["outcome_id"]
        )
        self.assertEqual(0, outcomes["1"]["consequences"]["max_hp_delta"])
        self.assertEqual(-1, outcomes["1"]["consequences"]["relic_delta"])
        self.assertEqual("decline", outcomes["2"]["outcome_id"])

    def test_coffee_dripper_protects_vampires_bites_from_later_grid_purge(self):
        """Bites are the compensation for Vampires' max-HP payment."""
        bite = build_card("Bite", CardType.ATTACK, damage=7)
        strike_r = build_card(
            "Strike_R", CardType.ATTACK, damage=6,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([], [bite, strike_r, defend()])
        game.in_combat = False
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        game.screen_type = ScreenType.GRID
        game.screen = GridSelectScreen(
            cards=[bite, strike_r], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=True,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([strike_r], action.cards)
        self.assertEqual(
            "grid_remove_or_transform_protect_vampires_bites",
            agent.last_noncombat_decision["reason"],
        )

    def test_coffee_dripper_protection_survives_untyped_grid_surface(self):
        """Unknown GRID frames must not bypass the Bite retention rule."""
        bite = build_card("Bite", CardType.ATTACK, damage=7)
        strike_r = build_card(
            "Strike_R", CardType.ATTACK, damage=6,
            rarity=CardRarity.BASIC,
        )
        game = GameStub([], [bite, strike_r, defend()])
        game.in_combat = False
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        game.screen_type = ScreenType.GRID
        game.screen = GridSelectScreen(
            cards=[bite, strike_r], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=False,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_grid_action()

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([strike_r], action.cards)
        self.assertEqual(
            "grid_remove_or_transform_protect_vampires_bites",
            agent.last_noncombat_decision["reason"],
        )

    def test_mind_bloom_awake_is_not_healthy_below_full_hp(self):
        game = GameStub([], [
            build_card("Limit Break", CardType.SKILL),
            build_card("Corruption", CardType.POWER),
            build_card("Demon Form", CardType.POWER),
        ])
        game.in_combat = False
        game.current_hp = 70
        game.max_hp = 80
        game.floor = 40
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("MindBloom", "Mind Bloom", "")
        game.screen.options = [
            EventOption(
                "Fight a boss and obtain a rare relic.",
                "I am War",
                False,
                0,
            ),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake",
                False,
                1,
            ),
            EventOption(
                "Gain 999 gold. Receive two Normality curses.",
                "I am Rich",
                False,
                2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(
            "mind_bloom_signed_post_state",
            agent.last_noncombat_decision["reason"],
        )
        awake = agent.last_noncombat_decision["outcome_facts"]["1"]
        self.assertEqual("awake", awake["outcome_id"])
        self.assertTrue(awake["consequences"]["healing_locked"])
        self.assertEqual(10, awake["post_state"]["missing_hp_locked_in"])
        self.assertEqual(3, awake["post_state"]["eligible_upgrade_count"])
        self.assertEqual(
            3,
            awake["post_state"]["upgrade_value_groups"]["non_basic"][
                "count"
            ],
        )
        self.assertIn(
            "upgrade_score_parts",
            awake["post_state"]["top_upgrade_targets"][0],
        )
        self.assertIn("upgrade_realization", awake["post_state"])
        self.assertGreater(
            next(
                row["score"]
                for row in agent.last_noncombat_decision["candidates"]
                if row["id"] == "1"
            ),
            -900.0,
        )
        rich = agent.last_noncombat_decision["outcome_facts"]["2"]
        self.assertEqual("rich", rich["outcome_id"])
        self.assertEqual(999, rich["consequences"]["gold_delta"])

    def test_heart_route_prices_act_four_transition_heal_before_awake(self):
        """Replay floor 39: full HP must not make Mark's future heal cost zero."""

        deck = [
            *[build_card("Strike_G", CardType.ATTACK, damage=9, upgrades=1)
              for _ in range(3)],
            *[build_card("Defend_G", CardType.SKILL, block=8, upgrades=1)
              for _ in range(4)],
            build_card("Survivor", CardType.SKILL, block=11, upgrades=1),
            build_card("Neutralize", CardType.ATTACK, cost=0, damage=3,
                       rarity=CardRarity.BASIC),
            build_card("Noxious Fumes", CardType.POWER, upgrades=1),
            build_card("Noxious Fumes", CardType.POWER),
            build_card("Bane", CardType.ATTACK, damage=7),
            build_card("Backflip", CardType.SKILL, block=5),
            build_card("Deadly Poison", CardType.SKILL),
            build_card("Corpse Explosion", CardType.SKILL, cost=2, upgrades=1),
            build_card("Backstab", CardType.ATTACK, cost=0, damage=11),
            build_card("Footwork", CardType.POWER),
            build_card("Backflip", CardType.SKILL, block=8, upgrades=1),
            build_card("Bouncing Flask", CardType.SKILL, cost=2, upgrades=1),
            build_card("Crippling Poison", CardType.SKILL, cost=2),
            build_card("Poisoned Stab", CardType.ATTACK, damage=6),
            build_card("Footwork", CardType.POWER, upgrades=1),
            build_card("After Image", CardType.POWER,
                       rarity=CardRarity.RARE),
            build_card("CurseOfTheBell", CardType.CURSE,
                       rarity=CardRarity.SPECIAL),
            build_card("Catalyst", CardType.SKILL),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 3
        game.floor = 39
        game.current_hp = 72
        game.max_hp = 72
        game.player.current_hp = 72
        game.player.max_hp = 72
        game.act_boss = "Donu and Deca"
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("MindBloom", "Mind Bloom", "")
        game.screen.options = [
            EventOption("Fight a boss for a rare relic.", "I am War", False, 0),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake", False, 1,
            ),
            EventOption(
                "Gain 999 gold. Receive two Normality curses.",
                "I am Rich", False, 2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        awake = agent.last_noncombat_decision["outcome_facts"]["1"]
        transition_cost = awake["post_state"][
            "disabled_healing_by_source"
        ]["act3_to_act4_transition"]
        self.assertGreaterEqual(transition_cost, 25.0)
        candidates = {
            row["id"]: row for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertGreater(candidates["0"]["score"], candidates["1"]["score"])

    def test_mind_bloom_war_values_rare_relic_without_double_counting_low_hp(self):
        """Replay floor 38: low HP is risk, not evidence of a weak deck too."""

        deck = [
            *[
                build_card(
                    "Strike_R", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ],
            *[
                build_card(
                    "Defend_R", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ],
            build_card(
                "Bash", CardType.ATTACK, damage=8, cost=2,
                rarity=CardRarity.BASIC,
            ),
            build_card("Flame Barrier", CardType.SKILL, block=12, cost=2),
            build_card("Shrug It Off", CardType.SKILL, block=8),
            build_card("Whirlwind", CardType.ATTACK, damage=5, cost=-1),
            build_card("Pommel Strike", CardType.ATTACK, damage=9),
            build_card("Spot Weakness", CardType.SKILL),
            build_card("Metallicize", CardType.POWER),
            build_card("Thunderclap", CardType.ATTACK, damage=4),
            build_card(
                "Demon Form", CardType.POWER, cost=3,
                rarity=CardRarity.RARE,
            ),
            build_card("Heavy Blade", CardType.ATTACK, damage=14, cost=2),
            build_card("Sword Boomerang", CardType.ATTACK, damage=3),
            build_card(
                "Offering", CardType.SKILL, cost=0,
                rarity=CardRarity.RARE,
            ),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 3
        game.floor = 38
        game.current_hp = 11
        game.max_hp = 85
        game.gold = 487
        game.relics = [
            Relic("Burning Blood", "Burning Blood"),
            Relic("Regal Pillow", "Regal Pillow"),
        ]
        fairy = Potion("FairyPotion", "Fairy Potion", True, True, False)
        skill = Potion("SkillPotion", "Skill Potion", True, True, False)
        game.get_real_potions = lambda: [fairy, skill]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("MindBloom", "Mind Bloom", "")
        game.screen.options = [
            EventOption("Fight a boss for a rare relic.", "I am War", False, 0),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake", False, 1,
            ),
            EventOption(
                "Gain 999 gold. Receive two Normality curses.",
                "I am Rich", False, 2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        war = agent.last_noncombat_decision["outcome_facts"]["0"]
        post_state = war["post_state"]
        self.assertTrue(post_state["deck_strength_ready"])
        self.assertFalse(post_state["survival_ready"])
        self.assertEqual(0.0, post_state["weak_deck_penalty"])
        self.assertGreater(post_state["rare_relic_expected_value"], 0.0)
        random_relic = war["consequences"]["random_effects"][0]
        self.assertEqual("rare_relics", random_relic["domain"])
        war_candidate = next(
            row
            for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "0"
        )
        self.assertEqual(
            "mind_bloom_war_expected_utility_v2",
            war_candidate["score_rule_id"],
        )
        self.assertAlmostEqual(
            war_candidate["score"],
            sum(part["value"] for part in war_candidate["score_components"]),
        )

    def test_mind_bloom_decomposed_scores_are_shared_by_all_characters(self):
        options = [
            EventOption("Fight a boss for a rare relic.", "I am War", False, 0),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake", False, 1,
            ),
            EventOption(
                "Gain 999 gold. Receive two Normality curses.",
                "I am Rich", False, 2,
            ),
        ]
        for chosen_class in (
            PlayerClass.IRONCLAD,
            PlayerClass.THE_SILENT,
            PlayerClass.DEFECT,
        ):
            with self.subTest(chosen_class=chosen_class):
                deck = [strike(), defend()] + [
                    build_card("Adrenaline", CardType.SKILL, cost=0),
                    build_card("Backflip", CardType.SKILL, block=5),
                    build_card("Demon Form", CardType.POWER, cost=3),
                    build_card("Ball Lightning", CardType.ATTACK, damage=7),
                ]
                game = GameStub([], deck)
                game.in_combat = False
                game.act = 3
                game.floor = 40
                game.current_hp = 70
                game.max_hp = 80
                game.screen_type = ScreenType.EVENT
                game.screen = EventScreen("MindBloom", "Mind Bloom", "")
                game.screen.options = list(options)
                agent = SimpleAgent(chosen_class, goal_mode="HEART")
                agent.game = game

                action = agent.handle_screen()

                self.assertIsInstance(action, ChooseAction)
                candidates = agent.last_noncombat_decision["candidates"]
                self.assertEqual(3, len(candidates))
                for candidate in candidates:
                    self.assertTrue(
                        candidate["score_rule_id"].startswith(
                            "mind_bloom_"
                        )
                    )
                    self.assertAlmostEqual(
                        candidate["score"],
                        sum(
                            part["value"]
                            for part in candidate["score_components"]
                        ),
                    )

    def test_mind_bloom_prices_black_blood_future_triggers(self):
        """Replay floor 36: Black Blood is a healing stream, not six points."""

        deck = [
            build_card("Inflame", CardType.POWER),
            build_card("Heavy Blade", CardType.ATTACK, damage=14, cost=2),
            build_card("Rage", CardType.SKILL, cost=0),
            build_card("Armaments", CardType.SKILL),
            build_card("Bash", CardType.ATTACK, damage=8, cost=2),
            build_card("Battle Trance", CardType.SKILL, cost=0),
            build_card("Demon Form", CardType.POWER, cost=3),
            build_card("Whirlwind", CardType.ATTACK, damage=5, cost=-1),
            build_card("Shockwave", CardType.SKILL, cost=2),
            build_card("Pommel Strike", CardType.ATTACK, damage=9),
            build_card("Flash of Steel", CardType.ATTACK, damage=3, cost=0),
            *[build_card("Strike_R", CardType.ATTACK, damage=6) for _ in range(3)],
            *[build_card("Defend_R", CardType.SKILL) for _ in range(3)],
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.current_hp = 45
        game.max_hp = 45
        game.floor = 36
        game.relics = [Relic("Black Blood", "Black Blood")]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("MindBloom", "Mind Bloom", "")
        game.screen.options = [
            EventOption("Fight a boss for a rare relic.", "I am War", False, 0),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake", False, 1,
            ),
            EventOption(
                "Gain 999 gold. Receive two Normality curses.",
                "I am Rich", False, 2,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        awake = agent.last_noncombat_decision["outcome_facts"]["1"]
        post_state = awake["post_state"]
        self.assertGreater(
            post_state["disabled_healing_by_source"]["blackblood"], 30.0
        )
        self.assertGreater(
            post_state["healing_lock_opportunity_cost"], 35.0
        )

    def test_mind_bloom_floor_41_third_option_is_healthy_not_awake(self):
        game = GameStub([], [
            build_card("Strike_R", CardType.ATTACK, upgrades=1),
            build_card("Defend_R", CardType.SKILL, upgrades=1),
        ])
        game.in_combat = False
        game.current_hp = 30
        game.max_hp = 80
        game.floor = 42
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("MindBloom", "Mind Bloom", "")
        game.screen.options = [
            EventOption("Fight a boss for a rare relic.", "I am War", False, 0),
            EventOption(
                "Upgrade all cards. You can no longer heal.",
                "I am Awake",
                False,
                1,
            ),
            EventOption("Heal to full. Become cursed - Doubt.", "I am Healthy", False, 2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(2, action.choice_index)
        healthy = agent.last_noncombat_decision["outcome_facts"]["2"]
        self.assertEqual("healthy", healthy["outcome_id"])
        self.assertEqual(50, healthy["consequences"]["hp_delta"])
        self.assertEqual(1, healthy["consequences"]["curse_delta"])
        awake = agent.last_noncombat_decision["outcome_facts"]["1"]
        self.assertEqual("awake", awake["outcome_id"])
        self.assertTrue(awake["consequences"]["healing_locked"])
        healthy_candidate = next(
            row
            for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "2"
        )
        self.assertEqual(
            "mind_bloom_healthy_expected_utility_v2",
            healthy_candidate["score_rule_id"],
        )
        self.assertAlmostEqual(
            healthy_candidate["score"],
            sum(
                component["value"]
                for component in healthy_candidate["score_components"]
            ),
        )

        game.relics = [
            Relic("Darkstone Periapt", "Darkstone Periapt")
        ]
        agent.handle_screen()
        healthy = agent.last_noncombat_decision["outcome_facts"]["2"]
        self.assertEqual(56, healthy["consequences"]["hp_delta"])
        self.assertEqual(6, healthy["consequences"]["max_hp_delta"])
        self.assertEqual(86, healthy["post_state"]["current_hp"])
        self.assertEqual(86, healthy["post_state"]["max_hp"])
        healthy_candidate = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "2"
        )
        self.assertAlmostEqual(
            healthy_candidate["score"],
            sum(
                component["value"]
                for component in healthy_candidate["score_components"]
            ),
        )

    def test_event_half_hp_and_healing_lock_are_not_scored_as_free_rewards(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = 30
        game.max_hp = 80
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game
        costly = EventOption(
            "Lose half your HP",
            "Lose half your current HP. You can no longer heal.",
            False,
            0,
        )
        safe = EventOption(
            "Leave",
            "Leave without losing HP.",
            False,
            1,
        )

        costly_score, signals = agent._generic_event_option_score(
            costly, game.current_hp / game.max_hp
        )
        safe_score, _ = agent._generic_event_option_score(
            safe, game.current_hp / game.max_hp
        )

        self.assertIn("healing_lock", signals)
        self.assertLess(costly_score, safe_score)
        profile = agent._optional_event_fight_profile()
        self.assertFalse(profile["ready"])
        self.assertEqual(50, profile["missing_hp"])

    def test_shining_light_values_two_random_upgrades_from_live_pool(self):
        deck = (
            [
                build_card(
                    "Strike_R", CardType.ATTACK, damage=6,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(5)
            ]
            + [
                build_card(
                    "Defend_R", CardType.SKILL, block=5,
                    rarity=CardRarity.BASIC,
                )
                for _ in range(4)
            ]
            + [
                build_card(
                    "Bash", CardType.ATTACK, cost=2, damage=8,
                    rarity=CardRarity.BASIC,
                ),
                build_card("Thunderclap", CardType.ATTACK, damage=4),
                build_card(
                    "Clothesline", CardType.ATTACK, cost=2, damage=12,
                ),
                build_card(
                    "Uppercut", CardType.ATTACK, cost=2, damage=13,
                ),
                build_card("Armaments", CardType.SKILL, block=5),
                build_card("Rage", CardType.SKILL, cost=0),
            ]
        )
        game = GameStub([], deck)
        game.in_combat = False
        game.current_hp = game.player.current_hp = 63
        game.max_hp = game.player.max_hp = 80
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "Shining Light", "",
            event_class=(
                "com.megacrit.cardcrawl.events.exordium.ShiningLight"
            ),
            event_stage="INTRO",
        )
        game.screen.options = [
            EventOption(
                "[Enter] Upgrade 2 random cards. Lose 16 HP.",
                "Enter", False, 0, original_button_index=0,
            ),
            EventOption(
                "[Leave]", "Leave", False, 1, original_button_index=1,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        upgradable = agent._event_upgradable_cards()
        expected = 2.0 * sum(
            max(0.0, min(30.0, float(agent._upgrade_score(card))))
            for card in upgradable
        ) / len(upgradable)
        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)
        enter = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_index"] == 0
        )
        evaluation = enter["consequences"]["score_components"][
            "deck_operation_evaluation"
        ]
        self.assertAlmostEqual(expected, evaluation["upgrade"], places=3)
        self.assertEqual(2, evaluation["random_upgrade_effective_count"])
        self.assertEqual(
            "uniform_without_replacement",
            evaluation["random_upgrade_sampling"],
        )
        self.assertLess(enter["score"], 1.0)

    def test_shining_light_caps_random_upgrade_count_to_available_cards(self):
        lone_target = build_card("Uppercut", CardType.ATTACK)
        deck = [
            lone_target,
            build_card(
                "Strike_R", CardType.ATTACK,
                rarity=CardRarity.BASIC, upgrades=1,
            ),
            build_card(
                "Defend_R", CardType.SKILL,
                rarity=CardRarity.BASIC, upgrades=1,
            ),
            build_card("Bash", CardType.ATTACK, upgrades=1),
            build_card("Rage", CardType.SKILL, cost=0, upgrades=1),
        ]
        game = GameStub([], deck)
        game.in_combat = False
        game.current_hp = game.player.current_hp = 70
        game.max_hp = game.player.max_hp = 80
        game.screen = EventScreen(
            "localized", "Shining Light", "",
            event_class=(
                "com.megacrit.cardcrawl.events.exordium.ShiningLight"
            ),
            event_stage="INTRO",
        )
        enter = EventOption(
            "[Enter] Upgrade 2 random cards. Lose 16 HP.",
            "Enter", False, 0, original_button_index=0,
        )
        game.screen.options = [
            enter,
            EventOption(
                "[Leave]", "Leave", False, 1, original_button_index=1,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        consequences = agent._event_option_consequences(enter)
        score, signals = agent._generic_event_option_score(
            enter, game.current_hp / game.max_hp
        )
        evaluation = agent._event_deck_operation_values_for_option(enter)

        self.assertEqual(1, consequences["upgrade_delta"])
        self.assertEqual(1, evaluation["random_upgrade_available_count"])
        self.assertEqual(1, evaluation["random_upgrade_effective_count"])
        self.assertAlmostEqual(
            max(0.0, min(30.0, agent._upgrade_score(lone_target))),
            evaluation["upgrade"],
            places=3,
        )
        self.assertIn("upgrade", signals)
        self.assertGreater(score, -1000.0)

    def test_we_meet_again_scores_exact_losses_as_signed_costs(self):
        compile_driver = build_card(
            "Compile Driver", CardType.ATTACK, damage=7
        )
        game = GameStub([], [compile_driver, strike(), defend()])
        game.in_combat = False
        game.gold = 200
        focus = Potion(
            "FocusPotion", "Focus Potion", True, True, False
        )
        game.get_real_potions = lambda: [focus]
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game
        options = {
            "potion": EventOption(
                "Give him a potion",
                "Lose Focus Potion. Gain a relic.",
                False,
                0,
            ),
            "gold": EventOption(
                "Give him gold",
                "Lose 84 gold. Gain a relic.",
                False,
                1,
            ),
            "card": EventOption(
                "Give him a card",
                "Lose Compile Driver. Gain a relic.",
                False,
                2,
            ),
        }

        scored = {
            name: agent._generic_event_option_score(option, 1.0)
            for name, option in options.items()
        }
        consequences = {
            name: agent._event_option_consequences(option)
            for name, option in options.items()
        }

        self.assertEqual(-84, consequences["gold"]["gold_delta"])
        self.assertEqual(1, consequences["gold"]["relic_delta"])
        self.assertEqual(-1, consequences["card"]["card_delta"])
        self.assertEqual(
            "Compile Driver",
            consequences["card"]["lost_card"]["card"]["card_id"],
        )
        self.assertGreater(
            consequences["card"]["lost_card"]["opportunity_cost"], 0
        )
        self.assertEqual(-1, consequences["potion"]["potion_delta"])
        self.assertEqual(
            "FocusPotion",
            consequences["potion"]["lost_potion"]["id"],
        )
        self.assertGreater(
            consequences["potion"]["lost_potion"]["opportunity_cost"], 0
        )
        self.assertIn("card_cost", scored["card"][1])
        self.assertNotIn("card", scored["card"][1])
        self.assertIn("potion_cost", scored["potion"][1])
        self.assertNotIn("potion", scored["potion"][1])
        self.assertGreater(scored["gold"][0], scored["card"][0])
        self.assertGreater(scored["gold"][0], scored["potion"][0])

    def test_event_consequences_bind_each_resource_to_its_own_direction(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.gold = 200
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        consequences = agent._event_option_consequences(
            EventOption(
                "Trade",
                "Lose 84 Gold. Gain a card and a relic.",
                False,
                0,
            )
        )

        self.assertEqual(-84, consequences["gold_delta"])
        self.assertEqual(1, consequences["card_delta"])
        self.assertTrue(consequences["card_gain"])
        self.assertFalse(consequences["card_loss"])
        self.assertNotIn("lost_card", consequences)
        self.assertEqual(1, consequences["relic_delta"])

    def test_event_consequences_bind_loss_and_gain_to_their_clauses(self):
        compile_driver = build_card(
            "Compile Driver", CardType.ATTACK, damage=7
        )
        game = GameStub([], [compile_driver, strike(), defend()])
        game.in_combat = False
        focus = Potion(
            "FocusPotion", "Focus Potion", True, True, False
        )
        game.get_real_potions = lambda: [focus]
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        gold_for_potion = agent._event_option_consequences(EventOption(
            "Trade",
            "Lose 84 gold. Gain a Focus Potion.",
            False,
            0,
        ))
        card_exchange = agent._event_option_consequences(EventOption(
            "Exchange",
            "Lose Compile Driver. Gain a card.",
            False,
            1,
        ))

        # The owned Focus Potion appears in the gain clause, so it must never
        # be rebound as the object of the earlier gold loss.
        self.assertEqual(-84, gold_for_potion["gold_delta"])
        self.assertTrue(gold_for_potion["potion_gain"])
        self.assertFalse(gold_for_potion["potion_loss"])
        self.assertEqual(1, gold_for_potion["potion_delta"])
        self.assertNotIn("lost_potion", gold_for_potion)

        # Mixed same-resource directions remain explicit even though their
        # signed net count is zero.
        self.assertTrue(card_exchange["card_loss"])
        self.assertTrue(card_exchange["card_gain"])
        self.assertEqual(0, card_exchange["card_delta"])
        self.assertEqual("exchange", card_exchange["card_change_kind"])
        self.assertEqual(
            "Compile Driver",
            card_exchange["lost_card"]["card"]["card_id"],
        )

    def test_event_remove_all_curses_has_negative_signed_delta(self):
        curses = [
            build_card("Doubt", CardType.CURSE, rarity=CardRarity.CURSE),
            build_card("Writhe", CardType.CURSE, rarity=CardRarity.CURSE),
        ]
        game = GameStub([], curses + [strike(), defend()])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game
        option = EventOption(
            "Remove all Curses from your deck.",
            "Cleanse",
            False,
            0,
        )

        consequences = agent._event_option_consequences(option)
        score, signals = agent._generic_event_option_score(option, 1.0)

        self.assertEqual(-2, consequences["curse_delta"])
        self.assertEqual("remove_all", consequences["curse_change_kind"])
        self.assertIn("curse_removal", signals)
        self.assertNotIn("curse_cost", signals)
        self.assertGreater(score, 0.0)

    def test_mausoleum_omamori_resolves_probability_before_model_facts(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.current_hp = 76
        game.max_hp = 94
        game.ascension_level = 0
        game.relics = [Relic("Omamori", "Omamori", counter=2)]
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen("陵墓", "The Mausoleum", "")
        writhe = build_card(
            "Writhe", CardType.CURSE, rarity=CardRarity.CURSE
        )
        game.screen.options = [
            EventOption(
                "[打开棺材] 获得一件遗物。 50%: 被诅咒——苦恼。",
                "打开棺材",
                False,
                0,
                writhe,
            ),
            EventOption("[离开]", "离开", False, 1),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        action = agent.handle_screen()

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)
        opening = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["choice_index"] == 0
        )
        consequences = opening["consequences"]
        self.assertEqual(
            "base_game_the_mausoleum_v1",
            consequences["mechanism_id"],
        )
        self.assertEqual(
            "base_game_non_boss_relic_pool",
            consequences["relic_changes"]["random_gain"][0]["domain"],
        )
        self.assertEqual([], consequences["card_changes"]["conditional_gain"])
        self.assertEqual(0.5, consequences["curse"]["probability"])
        self.assertEqual(
            0.0, consequences["curse"]["effective_gain_probability"]
        )
        self.assertEqual(2, consequences["curse"]["omamori_charges_before"])
        self.assertEqual(
            {"writhe_blocked_by_omamori", "no_writhe"},
            {item["id"] for item in consequences["probabilistic_outcomes"]},
        )
        blocked = next(
            item for item in consequences["probabilistic_outcomes"]
            if item["id"] == "writhe_blocked_by_omamori"
        )
        self.assertEqual(
            [{"id": "Omamori", "before": 2, "after": 1, "delta": -1}],
            blocked["relic_changes"]["counter"],
        )
        self.assertEqual(
            {"gold": 0, "hp": 0, "max_hp": 0},
            consequences["current_cost"],
        )
        self.assertEqual([], consequences["future_costs"])
        self.assertEqual(14.5, opening["score"])

    def test_mausoleum_without_omamori_keeps_expected_curse_risk(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        game.relics = [Relic("Omamori", "Omamori", counter=0)]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        open_option = EventOption(
            "Open the coffin. Gain a relic. 50%: Become cursed - Writhe.",
            "Open the coffin",
            False,
            0,
            build_card(
                "Writhe", CardType.CURSE, rarity=CardRarity.CURSE
            ),
        )
        leave_option = EventOption("Leave", "Leave", False, 1)
        game.screen = EventScreen("Mausoleum", "The Mausoleum", "")
        # Container order is deliberately reversed; semantic binding is the
        # authoritative choice_index plus Writhe preview, not list position.
        game.screen.options = [leave_option, open_option]

        consequences = agent._event_option_consequences(open_option)
        open_score, signals = agent._generic_event_option_score(
            open_option, 1.0
        )
        leave_score, _ = agent._generic_event_option_score(
            leave_option, 1.0
        )

        self.assertEqual(
            "mausoleum_open_coffin", consequences["operation"]
        )
        self.assertEqual(0.5, consequences["curse"]["probability"])
        self.assertEqual(
            0.5, consequences["curse"]["effective_gain_probability"]
        )
        self.assertEqual(
            [0.5, 0.5],
            sorted(
                item["probability"]
                for item in consequences["probabilistic_outcomes"]
            ),
        )
        self.assertEqual(
            ["Writhe"],
            [
                item["card_id"]
                for item in consequences["card_changes"]["conditional_gain"]
            ],
        )
        self.assertIn("curse_cost", signals)
        self.assertIn("curse_risk", signals)
        self.assertAlmostEqual(4.2, open_score, places=3)
        self.assertGreater(open_score, leave_score)

    def test_mausoleum_darkstone_gain_is_bound_to_writhe_branch(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        game.relics = [
            Relic("Darkstone Periapt", "Darkstone Periapt")
        ]
        open_option = EventOption(
            "ignored", "ignored", False, 0,
            build_card(
                "Writhe", CardType.CURSE, rarity=CardRarity.CURSE
            ),
        )
        game.screen = EventScreen("localized", "The Mausoleum", "")
        game.screen.options = [
            open_option, EventOption("leave", "leave", False, 1),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        consequence = agent._event_option_consequences(open_option)

        writhe = next(
            item for item in consequence["probabilistic_outcomes"]
            if item["id"] == "writhe_added"
        )
        no_writhe = next(
            item for item in consequence["probabilistic_outcomes"]
            if item["id"] == "no_writhe"
        )
        self.assertEqual((6, 6), (
            writhe["hp_delta"], writhe["max_hp_delta"],
        ))
        self.assertEqual((0, 0), (
            no_writhe["hp_delta"], no_writhe["max_hp_delta"],
        ))

    def test_event_multiple_curses_use_only_available_omamori_charges(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.relics = [Relic("Omamori", "Omamori", counter=1)]
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game
        option = EventOption(
            "Gain a relic. Receive 3 curses.",
            "Accept",
            False,
            0,
        )

        consequences = agent._event_option_consequences(option)
        score, signals = agent._generic_event_option_score(option, 1.0)

        self.assertEqual(3, consequences["raw_curse_delta"])
        self.assertEqual(1, consequences["omamori_prevented_curse_delta"])
        self.assertEqual(2, consequences["effective_curse_delta"])
        self.assertEqual(2, consequences["curse_delta"])
        self.assertEqual(
            2.0, consequences["expected_effective_curse_delta"]
        )
        self.assertEqual(1.0, consequences["expected_omamori_charge_use"])
        self.assertEqual(
            0, consequences["omamori_charges_after_if_triggered"]
        )
        self.assertIn("omamori_prevents_curse", signals)
        self.assertIn("curse_cost", signals)
        self.assertLess(score, 0.0)

    def test_curse_trade_profile_is_specific_and_probability_monotone(self):
        deck = [strike(), defend()] * 6
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        def cost(curse_id, probability=1.0):
            consequences = agent._event_curse_gain_consequences(
                1, probability=probability, curse_id=curse_id
            )
            return consequences["curse_trade_profile"][
                "total_opportunity_cost"
            ]

        normality = cost("Normality")
        writhe = cost("Writhe")
        injury = cost("Injury")

        self.assertGreater(normality, writhe)
        self.assertGreater(writhe, injury)
        self.assertLess(cost("Writhe", 0.5), writhe)

    def test_curse_trade_profile_models_clog_synergy_and_removal_monotonicity(self):
        deck = [strike(), defend()] * 6
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        def profile(relics=(), gold=0, extra_cards=()):
            game.relics = list(relics)
            game.gold = gold
            game.deck = deck + list(extra_cards)
            return agent._event_curse_gain_consequences(
                1, curse_id="Writhe"
            )["curse_trade_profile"]

        baseline = profile()
        pyramid = profile([Relic("Runic Pyramid", "Runic Pyramid")])
        supported = profile([
            Relic("Blue Candle", "Blue Candle"),
            Relic("Du-Vu Doll", "Du-Vu Doll"),
            Relic("Darkstone Periapt", "Darkstone Periapt"),
        ])
        piped = profile([Relic("Peace Pipe", "Peace Pipe")])
        affordable = profile([], gold=75)

        self.assertGreater(
            pyramid["total_opportunity_cost"],
            baseline["total_opportunity_cost"],
        )
        self.assertLess(
            supported["total_opportunity_cost"],
            baseline["total_opportunity_cost"],
        )
        self.assertLess(
            piped["total_opportunity_cost"],
            baseline["total_opportunity_cost"],
        )
        self.assertLess(
            affordable["total_opportunity_cost"],
            baseline["total_opportunity_cost"],
        )

    def test_cursed_key_increases_only_the_omamori_charge_opportunity_cost(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.relics = [Relic("Omamori", "Omamori", counter=2)]
        ordinary = agent._event_curse_gain_consequences(
            1, curse_id="Writhe"
        )["curse_trade_profile"]
        game.relics = [
            Relic("Omamori", "Omamori", counter=2),
            Relic("Cursed Key", "Cursed Key"),
        ]
        reserved = agent._event_curse_gain_consequences(
            1, curse_id="Writhe"
        )["curse_trade_profile"]

        self.assertEqual(0.0, ordinary["expected_curse_harm"])
        self.assertEqual(0.0, reserved["expected_curse_harm"])
        self.assertGreater(
            reserved["expected_omamori_cost"],
            ordinary["expected_omamori_cost"],
        )
        self.assertGreater(reserved["future_cursed_key_chests"], 0)

    def test_historical_big_fish_and_golden_idol_use_omamori_context(self):
        def curse(card_id):
            return build_card(
                card_id, CardType.CURSE, rarity=CardRarity.CURSE
            )

        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.current_hp = game.max_hp = 80
        game.player.current_hp = game.player.max_hp = 80
        game.relics = [Relic("Omamori", "Omamori", counter=2)]
        game.screen = EventScreen("Big Fish", "Big Fish", "")
        game.screen.options = [
            EventOption("Banana", "Banana", False, 0),
            EventOption("Donut", "Donut", False, 1),
            EventOption("Box", "Box", False, 2, curse("Regret")),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        box = agent.choose_event_action(game.screen.options)
        self.assertEqual(2, box.choice_index)
        box_facts = agent._event_option_consequences(game.screen.options[2])
        self.assertEqual(
            0, box_facts["effective_curse_delta"]
        )
        self.assertIn("curse_trade_profile", box_facts)

        game.screen = EventScreen("Golden Idol", "Golden Idol", "")
        game.screen.options = [
            EventOption(
                "Take Injury", "Take Injury", False, 0, curse("Injury")
            ),
            EventOption("Take damage", "Take damage", False, 1),
            EventOption("Lose max HP", "Lose max HP", False, 2),
        ]
        injury = agent.choose_event_action(game.screen.options)
        self.assertEqual(0, injury.choice_index)

    def test_big_fish_banana_claim_is_capped_by_missing_hp(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.current_hp = 67
        game.max_hp = 74
        game.player.current_hp = 67
        game.player.max_hp = 74
        game.screen = EventScreen("Big Fish", "Big Fish", "")
        banana = EventOption("Banana", "Heal 24 HP", False, 0)
        game.screen.options = [
            banana,
            EventOption("Donut", "Gain 5 Max HP", False, 1),
            EventOption("Box", "Obtain a relic", False, 2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        consequences = agent._event_option_consequences(banana)

        self.assertEqual(7, consequences["hp_delta"])

    def test_historical_cursed_tome_and_gold_curse_trades_are_contextual(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.ascension_level = 0
        game.current_hp = game.max_hp = 80
        game.player.current_hp = game.player.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.screen = EventScreen("Cursed Tome", "Cursed Tome", "")
        tome_instance = {
            "final_hp_loss": 10,
            "damage_taken": 0,
            "random_relic_pool": [
                "Necronomicon", "Enchiridion", "Nilry's Codex",
            ],
        }
        game.screen.options = [
            EventOption(
                "Read", "Read", False, 0,
                original_button_index=0,
                event_contract=staged_event_contract(
                    "Cursed Tome",
                    "com.megacrit.cardcrawl.events.city.CursedTome",
                    "INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN",
                    tome_instance,
                    {
                        "future_hp_loss_to_complete": 16,
                        "random_relic_count": 1,
                        "reward_surface": "COMBAT_REWARD",
                        "selection_mode": "UNIFORM_MISC_RNG",
                    },
                ),
            ),
            EventOption(
                "Leave", "Leave", False, 1,
                original_button_index=1,
                event_contract=staged_event_contract(
                    "Cursed Tome",
                    "com.megacrit.cardcrawl.events.city.CursedTome",
                    "INTRO", 1, "LEAVE", tome_instance, {},
                ),
            ),
        ]
        self.assertEqual(
            1, agent.choose_event_action(game.screen.options).choice_index
        )

        tome_facts = agent._event_option_consequences(
            game.screen.options[0]
        )
        self.assertEqual(
            "cursed_tome_enter_random_book_chain",
            tome_facts["operation"],
        )
        self.assertEqual(
            "exhaustive_domain",
            tome_facts["uncertainty_classification"]["status"],
        )
        read = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_index"] == 0
        )
        self.assertFalse(read["selection_eligible"])
        self.assertEqual(
            "unsupported_necronomicon_activation_state",
            read["veto_reason"],
        )

        game.current_hp = game.player.current_hp = 15
        self.assertEqual(
            1, agent.choose_event_action(game.screen.options).choice_index
        )

        game.current_hp = game.player.current_hp = 80
        game.gold = 100
        game.relics = [Relic("Peace Pipe", "Peace Pipe")]
        game.screen = EventScreen("Golden Shrine", "Golden Shrine", "")
        game.screen.options = [
            EventOption("Pray", "Pray", False, 0),
            EventOption("Desecrate", "Desecrate", False, 1),
            EventOption("Leave", "Leave", False, 2),
        ]
        self.assertEqual(
            1, agent.choose_event_action(game.screen.options).choice_index
        )

        game.relics = [Relic("Omamori", "Omamori", counter=2)]
        game.screen = EventScreen(
            "Accursed Blacksmith", "Accursed Blacksmith", ""
        )
        game.screen.options = [
            EventOption("Forge", "Forge", False, 0),
            EventOption("Rummage", "Rummage", False, 1),
            EventOption("Leave", "Leave", False, 2),
        ]
        self.assertEqual(
            1, agent.choose_event_action(game.screen.options).choice_index
        )

    def test_reflected_progress_types_serpent_golden_shrine_and_vampires(self):
        game = GameStub([], [strike(), defend()] * 3)
        game.in_combat = False
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 70
        game.max_hp = game.player.max_hp = 71
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.screen = EventScreen(
            "localized", "Liars Game", "",
            event_class=(
                "com.megacrit.cardcrawl.events.exordium.Sssserpent"
            ),
            event_stage="INTRO",
        )
        game.screen.options = [
            EventOption("leave", "leave", False, index,
                        original_button_index=index)
            for index in range(2)
        ]
        serpent = agent._event_option_consequences(
            game.screen.options[0]
        )
        self.assertEqual(0, serpent["gold_delta"])
        self.assertEqual(
            175, serpent["future_costs"][0]["gold_delta"]
        )
        self.assertEqual(
            "liars_game_prepare_agreement", serpent["operation"]
        )

        game.relics = [Relic("Darkstone Periapt", "Darkstone Periapt")]
        game.screen = EventScreen(
            "localized", "Golden Shrine", "",
            event_class=(
                "com.megacrit.cardcrawl.events.shrines.GoldShrine"
            ),
            event_stage="INTRO",
        )
        game.screen.options = [
            EventOption("contradictory", "contradictory", False, index,
                        original_button_index=index)
            for index in range(3)
        ]
        desecrate = agent._event_option_consequences(
            game.screen.options[1]
        )
        self.assertEqual(275, desecrate["gold_delta"])
        self.assertEqual(6, desecrate["hp_delta"])
        self.assertEqual(6, desecrate["max_hp_delta"])
        self.assertEqual(
            "Regret", desecrate["card_changes"]["gain"][0]["card_id"]
        )

        game.relics = []
        game.screen = EventScreen(
            "localized", "Vampires", "",
            event_class="com.megacrit.cardcrawl.events.city.Vampires",
            screen_num=0,
        )
        game.screen.options = [
            EventOption("contradictory", "contradictory", False, index,
                        original_button_index=index)
            for index in range(2)
        ]
        bites = agent._event_option_consequences(game.screen.options[0])
        self.assertEqual(-22, bites["max_hp_delta"])
        self.assertEqual(-21, bites["hp_delta"])
        self.assertEqual(
            5, bites["card_changes"]["gain"][0]["count"]
        )
        self.assertEqual(3, len(bites["card_changes"]["remove"]))
        self.assertEqual(
            "vampires_trade_max_hp_for_bites", bites["operation"]
        )

    def test_duplicator_contract_emits_classified_future_grid_selection(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        instance = {"screen_num": 0}
        options = [
            EventOption(
                "leave", "leave", False, 0,
                original_button_index=0,
                event_contract=staged_event_contract(
                    "Duplicator",
                    "com.megacrit.cardcrawl.events.shrines.Duplicator",
                    "MAIN", 0, "DUPLICATE", instance,
                    {
                        "duplicate_select_count": 1,
                        "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
                    },
                ),
            ),
            EventOption(
                "duplicate", "duplicate", False, 1,
                original_button_index=1,
                event_contract=staged_event_contract(
                    "Duplicator",
                    "com.megacrit.cardcrawl.events.shrines.Duplicator",
                    "MAIN", 1, "LEAVE", instance, {},
                ),
            ),
        ]
        game.screen = EventScreen("localized", "Duplicator", "")
        game.screen.options = list(reversed(options))

        duplicate = agent._event_option_consequences(options[0])
        leave = agent._event_option_consequences(options[1])

        self.assertEqual(
            "duplicator_open_duplicate_grid", duplicate["operation"]
        )
        self.assertEqual(
            "classified_future",
            duplicate["uncertainty_classification"]["status"],
        )
        self.assertEqual(
            "grid_duplicate", duplicate["future_costs"][0]["operation"]
        )
        self.assertEqual("duplicator_leave", leave["operation"])

    def test_designer_contract_classifies_all_deferred_card_operations(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        event_class = (
            "com.megacrit.cardcrawl.events.shrines.Designer"
        )
        instance = {
            "adjust_cost": 40,
            "adjustment_upgrades_one": True,
            "clean_up_cost": 60,
            "clean_up_removes_cards": True,
            "full_service_cost": 90,
            "hp_loss": 3,
        }
        specs = [
            (0, "ADJUSTMENT_GRID_UPGRADE", {
                "gold_cost": 40,
                "upgrade_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            (1, "CLEAN_UP_GRID_PURGE", {
                "gold_cost": 60,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            (2, "FULL_SERVICE", {
                "gold_cost": 90,
                "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                ),
            }),
            (3, "PUNCH_AND_LEAVE", {"hp_loss": 3}),
        ]
        options = [
            EventOption(
                "localized", "localized", False, index,
                original_button_index=index,
                event_contract=staged_event_contract(
                    "Designer", event_class, "MAIN", index, kind,
                    instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]
        game.screen = EventScreen("localized", "Designer", "")
        game.screen.options = options

        consequences = [
            agent._event_option_consequences(option)
            for option in options[:3]
        ]

        self.assertEqual(
            [
                "typed Designer GRID upgrade contract",
                "typed Designer cleanup GRID contract",
                (
                    "Full Service purge and random-upgrade domain "
                    "are typed"
                ),
            ],
            [
                consequence["uncertainty_classification"]["reason"]
                for consequence in consequences
            ],
        )
        self.assertTrue(all(
            consequence["uncertainty_classification"]["status"]
            == "classified_future"
            for consequence in consequences
        ))
        self.assertTrue(all(
            consequence["uncertainty_classification"]["authority"]
            == "protocol_multistage_operation"
            for consequence in consequences
        ))

    def test_face_trader_contract_types_localized_surface_and_random_pool(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 64
        game.max_hp = game.player.max_hp = 80
        game.relics = [Relic("FaceOfCleric", "Face of Cleric")]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        event_class = (
            "com.megacrit.cardcrawl.events.shrines.FaceTrader"
        )
        pool = [
            "CultistMask", "GremlinMask", "NlothsMask",
            "SsserpentHead",
        ]
        instance = {
            "gold_reward": 75,
            "damage": 8,
            "random_face_pool": pool,
        }
        specs = [
            (0, "TOUCH", {"hp_loss": 8, "gold_gain": 75}),
            (1, "TRADE", {
                "random_relic_pool": pool,
                "random_relic_count": 1,
                "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
            }),
            (2, "LEAVE", {}),
        ]
        options = [
            EventOption(
                f"contradictory-{2 - index}",
                f"untrusted-localized-{2 - index}",
                False,
                index,
                original_button_index=index,
                event_contract=staged_event_contract(
                    "Face Trader", event_class, "MAIN", index, kind,
                    instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]
        game.screen = EventScreen("localized", "FaceTrader", "")
        game.screen.options = list(reversed(options))

        touch = agent._event_option_consequences(options[0])
        trade = agent._event_option_consequences(options[1])
        leave = agent._event_option_consequences(options[2])

        self.assertEqual("face_trader_touch", touch["operation"])
        self.assertEqual("FaceTrader", touch["event_id"])
        self.assertEqual(-8, touch["hp_delta"])
        self.assertEqual(75, touch["gold_delta"])
        self.assertEqual("face_trader_trade", trade["operation"])
        self.assertEqual(4, len(trade["probabilistic_outcomes"]))
        self.assertAlmostEqual(
            1.0,
            sum(
                outcome["probability"]
                for outcome in trade["probabilistic_outcomes"]
            ),
        )
        self.assertEqual(
            pool, trade["random_effects"][0]["domain"]
        )
        self.assertEqual(
            [
                {
                    "gain": [{"id": relic_id}],
                    "remove": [],
                    "counter": [],
                }
                for relic_id in pool
            ],
            trade["relic_changes"],
        )
        self.assertEqual(
            "exhaustive_probability",
            trade["uncertainty_classification"]["status"],
        )
        self.assertEqual("face_trader_leave", leave["operation"])

        for stage, kind in (("INTRO", "OPEN"), ("RESULT", "CONTINUE")):
            option = EventOption(
                "untrusted", "untrusted", False, 0,
                original_button_index=0,
                event_contract=staged_event_contract(
                    "Face Trader", event_class, stage, 0, kind,
                    instance, {},
                ),
            )
            game.screen.options = [option]
            consequence = agent._event_option_consequences(option)
            self.assertEqual(
                "face_trader_dialog_advance_noop",
                consequence["operation"],
            )

    def test_bonfire_contract_types_all_three_forced_stages(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        event_class = "com.megacrit.cardcrawl.events.shrines.Bonfire"
        instance = {"card_select": False}

        def consequence(stage, kind, parameters):
            option = EventOption(
                "untrusted", "untrusted", False, 0,
                original_button_index=0,
                event_contract=staged_event_contract(
                    "Bonfire Elementals", event_class, stage, 0, kind,
                    instance, parameters,
                ),
            )
            game.screen = EventScreen(
                "localized", "Bonfire Elementals", ""
            )
            game.screen.options = [option]
            return agent._event_option_consequences(option)

        intro = consequence("INTRO", "CONTINUE", {})
        choose = consequence("CHOOSE", "OFFER_CARD", {
            "offer_select_count": 1,
            "selection_mode": (
                "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
            ),
        })
        complete = consequence("COMPLETE", "CONTINUE", {})

        self.assertEqual("bonfire_dialog_advance_noop", intro["operation"])
        self.assertEqual("bonfire_open_offer_grid", choose["operation"])
        self.assertEqual(
            "classified_future",
            choose["uncertainty_classification"]["status"],
        )
        self.assertEqual(
            "grid_offer_card", choose["future_costs"][0]["operation"]
        )
        self.assertEqual(
            "bonfire_dialog_advance_noop", complete["operation"]
        )

        game.screen.options[0].event_contract[
            "instance_parameters"
        ]["card_select"] = True
        failed = agent._event_option_consequences(game.screen.options[0])
        self.assertTrue(failed["operation"].startswith("unclassified_"))

    def test_knowing_skull_dialog_noop_has_deterministic_uncertainty_contract(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game
        instance = {
            "potion_cost": 6,
            "card_cost": 6,
            "gold_cost": 6,
            "leave_cost": 6,
            "gold_reward": 90,
        }
        option = EventOption(
            "untrusted", "untrusted", False, 0,
            original_button_index=0,
            event_contract=staged_event_contract(
                "Knowing Skull",
                "com.megacrit.cardcrawl.events.city.KnowingSkull",
                "INTRO_1", 0, "OPEN_QUESTIONS", instance, {},
            ),
        )
        game.screen = EventScreen("localized", "Knowing Skull", "")
        game.screen.options = [option]

        consequence = agent._event_option_consequences(option)

        self.assertEqual(
            "knowing_skull_dialog_advance_noop", consequence["operation"]
        )
        self.assertEqual([], consequence["uncertainty"])
        self.assertEqual(
            {
                "status": "none",
                "authority": "typed_event_contract",
                "reason": "exact typed event consequence is deterministic",
            },
            consequence["uncertainty_classification"],
        )

    def test_knowing_skull_reserves_typed_leave_cost_before_reward(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 29
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 12
        game.max_hp = game.player.max_hp = 80
        game.gold = 600
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        instance = {
            "potion_cost": 6,
            "card_cost": 8,
            "gold_cost": 9,
            "leave_cost": 6,
            "gold_reward": 90,
        }
        specs = [
            (0, "TAKE_POTION", {
                "hp_loss": 6,
                "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            }),
            (1, "TAKE_GOLD", {"hp_loss": 9, "gold_gain": 90}),
            (2, "TAKE_CARD", {
                "hp_loss": 8,
                "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            }),
            (3, "LEAVE", {"hp_loss": 6}),
        ]
        # Display strings deliberately contradict the mechanics.  Only the
        # original button and private-cost contract may classify a choice.
        labels = ["leave", "free potion", "gain 999 gold", "take reward"]
        options = [
            EventOption(
                labels[index], labels[index], False, index,
                original_button_index=index,
                event_contract=staged_event_contract(
                    "Knowing Skull",
                    "com.megacrit.cardcrawl.events.city.KnowingSkull",
                    "ASK", index, kind, instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]
        game.screen = EventScreen("localized", "Knowing Skull", "")
        game.screen.options = list(reversed(options))

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(3, action.choice_index)
        self.assertEqual(
            "knowingskull_typed_a0_resource_trade",
            agent.last_noncombat_decision["reason"],
        )
        rows = {
            row["consequences"]["original_button_index"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        for original in (0, 1, 2):
            self.assertFalse(rows[original]["selection_eligible"])
            self.assertEqual(
                "knowing_skull_exit_reserve_insufficient",
                rows[original]["veto_reason"],
            )
            self.assertEqual(
                6.0,
                rows[original]["score_inputs"][
                    "reserved_exit_hp_cost"
                ],
            )
            self.assertEqual(
                -1000000.0,
                rows[original]["score_inputs"][
                    "survival_veto_adjustment"
                ],
            )
            veto_component = next(
                component
                for component in rows[original]["score_components"]
                if component["name"] == "survival_veto"
            )
            self.assertEqual(-1000000.0, veto_component["value"])
            self.assertAlmostEqual(
                rows[original]["score"],
                sum(
                    component["value"]
                    for component in rows[original]["score_components"]
                ),
            )
        self.assertTrue(rows[3]["selection_eligible"])
        self.assertEqual(
            "knowing_skull_leave",
            rows[3]["consequences"]["operation"],
        )
        self.assertGreater(
            rows[3]["score"],
            max(rows[original]["score"] for original in (0, 1, 2)),
        )

    def test_knowing_skull_high_hp_uses_typed_reward_and_tamper_fails_closed(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 80
        game.max_hp = game.player.max_hp = 80
        game.gold = 100
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        instance = {
            "potion_cost": 6,
            "card_cost": 6,
            "gold_cost": 6,
            "leave_cost": 6,
            "gold_reward": 90,
        }
        specs = [
            (0, "TAKE_POTION", {
                "hp_loss": 6, "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            }),
            (1, "TAKE_GOLD", {"hp_loss": 6, "gold_gain": 90}),
            (2, "TAKE_CARD", {
                "hp_loss": 6, "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            }),
            (3, "LEAVE", {"hp_loss": 6}),
        ]
        options = [
            EventOption(
                f"untrusted-{3 - index}", f"untrusted-{3 - index}",
                False, index, original_button_index=index,
                event_contract=staged_event_contract(
                    "Knowing Skull",
                    "com.megacrit.cardcrawl.events.city.KnowingSkull",
                    "ASK", index, kind, instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]
        game.screen = EventScreen("localized", "Knowing Skull", "")
        game.screen.options = list(reversed(options))

        self.assertEqual(
            1, agent.choose_event_action(game.screen.options).choice_index
        )
        gold = agent._event_option_consequences(options[1])
        self.assertEqual("knowing_skull_take_gold", gold["operation"])
        self.assertEqual(
            6, gold["future_costs"][0]["reserved_hp_loss"]
        )
        self.assertNotIn("nominal_gold_gain", gold)

        potion = agent._event_option_consequences(options[0])
        random_potion_gain = {
            "kind": "random_potion_gain",
            "count": 1,
            "domain": "base_game_potion_pool",
            "selection_mode": "random",
        }
        self.assertNotIn("potion_reward_obtainable", potion)
        self.assertEqual(
            [random_potion_gain], potion["potion_changes"]["gain"]
        )
        self.assertEqual([random_potion_gain], potion["random_effects"])
        self.assertEqual(
            "classified_random_domain",
            potion["uncertainty_classification"]["status"],
        )

        card = agent._event_option_consequences(options[2])
        random_card_gain = {
            "kind": "random_card_gain",
            "count": 1,
            "domain": "base_game_colorless_uncommon_pool",
            "selection_mode": "random",
        }
        self.assertEqual(
            [random_card_gain], card["card_changes"]["gain"]
        )
        self.assertEqual([random_card_gain], card["random_effects"])
        self.assertEqual(
            "classified_random_domain",
            card["uncertainty_classification"]["status"],
        )

        options[0].event_contract["instance_parameters"]["leave_cost"] = 7
        rejected = agent.choose_event_action(game.screen.options)
        self.assertIsNone(rejected)
        self.assertTrue(agent.last_noncombat_decision["fail_closed"])
        self.assertTrue(all(
            row["selection_eligible"] is False
            for row in agent.last_noncombat_decision["candidates"]
        ))

    def test_knowing_skull_stops_before_spending_combat_survival_floor(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 59
        game.max_hp = game.player.max_hp = 80
        game.gold = 370
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        instance = {
            "potion_cost": 6,
            "card_cost": 6,
            "gold_cost": 9,
            "leave_cost": 6,
            "gold_reward": 90,
        }
        specs = [
            (0, "TAKE_POTION", {
                "hp_loss": 6, "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            }),
            (1, "TAKE_GOLD", {"hp_loss": 9, "gold_gain": 90}),
            (2, "TAKE_CARD", {
                "hp_loss": 6, "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            }),
            (3, "LEAVE", {"hp_loss": 6}),
        ]
        game.screen = EventScreen("localized", "Knowing Skull", "")
        game.screen.options = [
            EventOption(
                f"untrusted-{index}", f"untrusted-{index}", False, index,
                original_button_index=index,
                event_contract=staged_event_contract(
                    "Knowing Skull",
                    "com.megacrit.cardcrawl.events.city.KnowingSkull",
                    "ASK", index, kind, instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(3, action.choice_index)
        gold_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["consequences"]["operation"]
            == "knowing_skull_take_gold"
        )
        self.assertFalse(gold_row["selection_eligible"])
        self.assertEqual(
            "knowing_skull_post_exit_survival_floor",
            gold_row["veto_reason"],
        )
        self.assertEqual(44.0, gold_row["score_inputs"]["post_exit_hp"])
        self.assertEqual(
            48.0,
            gold_row["score_inputs"]["post_exit_survival_floor"],
        )
        self.assertEqual(
            -1000000.0,
            gold_row["score_inputs"]["survival_veto_adjustment"],
        )
        veto_component = next(
            component for component in gold_row["score_components"]
            if component["name"] == "survival_veto"
        )
        self.assertEqual(-1000000.0, veto_component["value"])
        leave_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["consequences"]["operation"] == "knowing_skull_leave"
        )
        self.assertGreater(leave_row["score"], gold_row["score"])

    def test_knowing_skull_counts_forced_campfire_relic_healing(self):
        deck = [strike(), defend()] * 7 + [strike()]
        game = GameStub([], deck)
        game.in_combat = False
        game.act = 2
        game.floor = 31
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 54
        game.max_hp = game.player.max_hp = 80
        game.gold = 152
        game.relics = [
            Relic("Eternal Feather", "Eternal Feather"),
            Relic("Pantograph", "Pantograph"),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        instance = {
            "potion_cost": 6,
            "card_cost": 6,
            "gold_cost": 7,
            "leave_cost": 6,
            "gold_reward": 90,
        }
        specs = [
            (0, "TAKE_POTION", {
                "hp_loss": 6, "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            }),
            (1, "TAKE_GOLD", {"hp_loss": 7, "gold_gain": 90}),
            (2, "TAKE_CARD", {
                "hp_loss": 6, "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            }),
            (3, "LEAVE", {"hp_loss": 6}),
        ]
        game.screen = EventScreen("localized", "Knowing Skull", "")
        game.screen.options = [
            EventOption(
                f"untrusted-{index}", f"untrusted-{index}", False, index,
                original_button_index=index,
                event_contract=staged_event_contract(
                    "Knowing Skull",
                    "com.megacrit.cardcrawl.events.city.KnowingSkull",
                    "ASK", index, kind, instance, parameters,
                ),
            )
            for index, kind, parameters in specs
        ]

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(1, action.choice_index)
        gold_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["consequences"]["operation"]
            == "knowing_skull_take_gold"
        )
        self.assertTrue(gold_row["selection_eligible"])
        self.assertIsNone(gold_row["veto_reason"])
        self.assertEqual(41.0, gold_row["score_inputs"]["post_exit_hp"])
        self.assertEqual(
            1.0,
            gold_row["score_inputs"]["next_room_is_forced_campfire"],
        )
        self.assertEqual(
            9.0,
            gold_row["score_inputs"]["guaranteed_eternal_feather_heal"],
        )
        self.assertEqual(
            25.0,
            gold_row["score_inputs"]["guaranteed_pantograph_heal"],
        )
        self.assertEqual(
            34.0,
            gold_row["score_inputs"]["guaranteed_future_healing"],
        )
        self.assertEqual(
            75.0,
            gold_row["score_inputs"]["post_guaranteed_heal_hp"],
        )
        self.assertEqual(
            0.0,
            gold_row["score_inputs"]["survival_veto_adjustment"],
        )

    def test_the_joust_a0_uses_typed_expected_gold_not_localized_text(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 21
        game.ascension_level = 0
        game.gold = 252
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        # Neither label nor text is used to classify the bets.  Reversing the
        # container also must not change the stable original-index choice.
        against = EventOption(
            "localized-or-modded-display-a", "display-a", False, 0,
            original_button_index=0,
        )
        for_bet = EventOption(
            "localized-or-modded-display-b", "display-b", False, 1,
            original_button_index=1,
        )
        game.screen = EventScreen("The Joust", "The Joust", "")
        game.screen.options = [for_bet, against]

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "the_joust_a0_expected_gold",
            agent.last_noncombat_decision["reason"],
        )
        rows = {
            int(row["choice_index"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(20.0, rows[0]["score"])
        self.assertEqual(25.0, rows[1]["score"])
        self.assertEqual(
            ["exact_base_game_the_joust_a0_expected_gold"],
            rows[1]["reason_codes"],
        )

    def test_high_ascension_cursed_tome_and_gold_events_use_exact_values(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.ascension_level = 15
        game.current_hp = game.player.current_hp = 80
        game.max_hp = game.player.max_hp = 80
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        # The private Cursed Tome stage is not protocol-visible.  Even at
        # high ascension the random-book chain must remain vetoed instead of
        # deriving HP costs or Necronomicon activation from localized text.
        game.screen = EventScreen("Cursed Tome", "Cursed Tome", "")
        game.screen.options = [
            EventOption("Read", "Read", False, 0),
            EventOption("Leave", "Leave", False, 1),
        ]
        action = agent.choose_event_action(game.screen.options)
        self.assertIsNone(action)
        rows = {
            int(row.get("choice_index", row["id"])): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertFalse(rows[0]["selection_eligible"])
        self.assertEqual(
            "typed_event_requires_exact_a0",
            rows[0]["veto_reason"],
        )
        self.assertFalse(rows[1]["selection_eligible"])
        self.assertIn(
            "typed_event_requires_exact_a0",
            rows[1]["consequences"]["reason_codes"],
        )

        game.screen = EventScreen("Golden Shrine", "Golden Shrine", "")
        pray = EventOption("Pray", "Pray", False, 0)
        self.assertEqual(
            50, agent._event_option_consequences(pray)["gold_delta"]
        )
        game.screen = EventScreen("Sssserpent", "The Sssserpent", "")
        accept = EventOption("Agree", "Agree", False, 0)
        serpent = agent._event_option_consequences(accept)
        self.assertEqual(150, serpent["gold_delta"])
        self.assertEqual("Doubt", serpent["curse_id"])

    def test_high_ascension_golden_idol_and_forgotten_altar_costs_are_exact(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.ascension_level = 15
        game.current_hp = game.player.current_hp = 75
        game.max_hp = game.player.max_hp = 75
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.screen = EventScreen("Golden Idol", "Golden Idol", "")
        game.screen.options = [
            EventOption("Take Injury", "Take Injury", False, 0),
            EventOption("Take damage", "Take damage", False, 1),
            EventOption("Lose max HP", "Lose max HP", False, 2),
        ]
        self.assertEqual(
            -26,
            agent._event_option_consequences(
                game.screen.options[1]
            )["hp_delta"],
        )
        self.assertEqual(
            -7,
            agent._event_option_consequences(
                game.screen.options[2]
            )["max_hp_delta"],
        )
        self.assertEqual(
            -7,
            agent._event_option_consequences(
                game.screen.options[2]
            )["hp_delta"],
        )

        game.screen = EventScreen(
            "Forgotten Altar", "Forgotten Altar", ""
        )
        game.current_hp = game.player.current_hp = 70
        game.screen.options = [
            EventOption("Offer", "Offer", False, 0),
            EventOption("Sacrifice", "Sacrifice", False, 1),
            EventOption("Desecrate", "Desecrate", False, 2),
        ]
        sacrifice = agent._event_option_consequences(
            game.screen.options[1]
        )
        self.assertEqual(5, sacrifice["max_hp_delta"])
        self.assertEqual(26, sacrifice["damage_amount"])
        self.assertEqual(-21, sacrifice["hp_delta"])

        game.ascension_level = 0
        sacrifice = agent._event_option_consequences(
            game.screen.options[1]
        )
        self.assertEqual(19, sacrifice["damage_amount"])
        self.assertEqual(-14, sacrifice["hp_delta"])

    def test_mausoleum_probability_is_explicit_at_high_ascension(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 15
        game.screen = EventScreen("陵墓", "The Mausoleum", "")
        game.screen.options = [
            EventOption(
                "Open the coffin. 50%: Become cursed - Writhe.",
                "Open",
                False,
                0,
                build_card(
                    "Writhe", CardType.CURSE, rarity=CardRarity.CURSE
                ),
            ),
            EventOption("Leave", "Leave", False, 1),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        facts = agent._event_option_consequences(game.screen.options[0])

        self.assertEqual(1.0, facts["curse"]["probability"])
        self.assertEqual(1.0, facts["curse"]["effective_gain_probability"])
        self.assertEqual(1, len(facts["probabilistic_outcomes"]))
        self.assertEqual(
            "writhe_added", facts["probabilistic_outcomes"][0]["id"]
        )
        self.assertEqual(
            1.0, facts["probabilistic_outcomes"][0]["probability"]
        )

        leave = agent._event_option_consequences(game.screen.options[1])
        self.assertEqual("mausoleum_leave", leave["operation"])
        self.assertEqual((0, 0, 0), (
            leave["hp_delta"], leave["max_hp_delta"], leave["gold_delta"],
        ))
        self.assertEqual([], leave["probabilistic_outcomes"])
        self.assertEqual([], leave["random_effects"])

    def test_mausoleum_typed_contract_rejects_wrong_event_index_and_preview(self):
        writhe = build_card(
            "Writhe", CardType.CURSE, rarity=CardRarity.CURSE
        )
        open_option = EventOption("ignored", "ignored", False, 0, writhe)
        leave_option = EventOption("ignored", "ignored", False, 1)
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        game.screen = EventScreen("lookalike", "Other Event", "")
        game.screen.options = [open_option, leave_option]
        wrong_event = agent._event_option_consequences(open_option)
        self.assertNotIn("mechanism_id", wrong_event)
        self.assertNotEqual(
            "mausoleum_open_coffin", wrong_event.get("operation")
        )

        game.screen = EventScreen("Mausoleum", "The Mausoleum", "")
        game.screen.options = [open_option, leave_option]
        phantom = EventOption("ignored", "ignored", False, 0, writhe)
        wrong_binding = agent._event_option_consequences(phantom)
        self.assertEqual(
            "unclassified_mausoleum_option", wrong_binding["operation"]
        )
        self.assertEqual(
            ["mausoleum_choice_index_binding_mismatch"],
            wrong_binding["reason_codes"],
        )
        self.assertNotIn("relic_changes", wrong_binding)

        game.screen.options[0].card = build_card(
            "Doubt", CardType.CURSE, rarity=CardRarity.CURSE
        )
        wrong_preview = agent._event_option_consequences(
            game.screen.options[0]
        )
        self.assertEqual(
            ["mausoleum_writhe_preview_mismatch"],
            wrong_preview["reason_codes"],
        )

    def test_cleric_typed_contract_uses_floor_heal_and_rejects_phantom_binding(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 60
        game.max_hp = game.player.max_hp = 71
        game.gold = 100
        game.screen = EventScreen("The Cleric", "The Cleric", "")
        cleric_instance = {
            "heal_amount": 17,
            "heal_gold_cost": 35,
            "purify_cost": 50,
        }
        options = [
            EventOption(
                "Leave", "Leave", False, 0,
                original_button_index=0,
                event_contract=staged_event_contract(
                    "The Cleric",
                    "com.megacrit.cardcrawl.events.exordium.Cleric",
                    "MAIN", 0, "HEAL", cleric_instance,
                    {"gold_cost": 35, "heal_amount": 17},
                ),
            ),
            EventOption(
                "Heal", "Heal", False, 1,
                original_button_index=1,
                event_contract=staged_event_contract(
                    "The Cleric",
                    "com.megacrit.cardcrawl.events.exordium.Cleric",
                    "MAIN", 1, "PURIFY", cleric_instance,
                    {
                        "gold_cost_if_purgeable": 50,
                        "purge_select_count": 1,
                        "selection_mode": "PLAYER_SELECT",
                    },
                ),
            ),
            EventOption(
                "Purify", "Purify", False, 2,
                original_button_index=2,
                event_contract=staged_event_contract(
                    "The Cleric",
                    "com.megacrit.cardcrawl.events.exordium.Cleric",
                    "MAIN", 2, "LEAVE", cleric_instance, {},
                ),
            ),
        ]
        game.screen.options = list(reversed(options))
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        heal = agent._event_option_consequences(options[0])

        self.assertEqual("cleric_heal", heal["operation"])
        self.assertEqual(11, heal["hp_delta"])
        self.assertEqual(-35, heal["gold_delta"])
        purge = agent._event_option_consequences(options[1])
        self.assertEqual("cleric_open_purge_grid", purge["operation"])
        self.assertEqual(-50, purge["gold_delta"])
        self.assertEqual(
            "classified_future",
            purge["uncertainty_classification"]["status"],
        )
        self.assertEqual(
            "protocol_multistage_operation",
            purge["uncertainty_classification"]["authority"],
        )
        self.assertEqual(
            "after_grid_confirmation",
            purge["future_costs"][0]["commit_timing"],
        )
        phantom = EventOption("ignored", "ignored", False, 0)
        rejected = agent._event_option_consequences(phantom)
        self.assertEqual(
            "unclassified_thecleric_option", rejected["operation"]
        )
        self.assertEqual(
            ["typed_event_choice_index_binding_mismatch"],
            rejected["reason_codes"],
        )

    def test_mausoleum_followup_single_button_is_exact_zero_delta(self):
        game = GameStub([], [strike(), defend()])
        game.in_combat = False
        game.ascension_level = 0
        option = EventOption("fake reward", "fake reward", False, 0)
        game.screen = EventScreen("Mausoleum", "The Mausoleum", "")
        game.screen.options = [option]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        consequence = agent._event_option_consequences(option)

        self.assertEqual(
            "mausoleum_dialog_advance_noop", consequence["operation"]
        )
        self.assertEqual((0, 0, 0), (
            consequence["hp_delta"],
            consequence["max_hp_delta"],
            consequence["gold_delta"],
        ))

    def test_forgotten_altar_low_hp_does_not_bypass_golden_idol_trade(self):
        game = GameStub([], [strike(), defend()] * 6)
        game.in_combat = False
        game.act = 2
        game.floor = 20
        game.current_hp = game.player.current_hp = 30
        game.max_hp = game.player.max_hp = 80
        game.relics = [Relic("Golden Idol", "Golden Idol")]
        game.screen = EventScreen("Forgotten Altar", "Forgotten Altar", "")
        game.screen.options = [
            EventOption("Offer Golden Idol", "Offer", False, 0),
            EventOption("Gain 5 Max HP. Lose 20 HP", "Sacrifice", False, 1),
            EventOption("Become cursed - Decay", "Desecrate", False, 2),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(0, action.choice_index)
        facts = agent._event_option_consequences(game.screen.options[0])
        self.assertEqual("Golden Idol", facts["lost_relic_id"])
        self.assertEqual("Bloody Idol", facts["relic_id"])

    def test_forgotten_altar_prices_net_sacrifice_hp_before_decay(self):
        """Typed +5 HP must offset the altar's displayed gross damage."""
        game = GameStub([], [strike(), defend()] * 8)
        game.in_combat = False
        game.act = 2
        game.floor = 28
        game.ascension_level = 0
        game.current_hp = game.player.current_hp = 31
        game.max_hp = game.player.max_hp = 80
        sacrifice = EventOption(
            "Gain 5 Max HP. Lose 20 HP", "Sacrifice", False, 0,
            original_button_index=1,
        )
        desecrate = EventOption(
            "Become cursed - Decay", "Desecrate", False, 1,
            build_card("Decay", CardType.CURSE, rarity=CardRarity.CURSE),
            original_button_index=2,
        )
        game.screen = EventScreen("Forgotten Altar", "Forgotten Altar", "")
        game.screen.options = [sacrifice, desecrate]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(0, action.choice_index, agent.last_noncombat_decision)
        candidates = agent.last_noncombat_decision["candidates"]
        sacrifice_candidate = next(row for row in candidates if row["id"] == "0")
        decay_candidate = next(row for row in candidates if row["id"] == "1")
        self.assertEqual(-15, sacrifice_candidate["consequences"]["hp_delta"])
        self.assertGreater(sacrifice_candidate["score"], decay_candidate["score"])
        hp_cost = next(
            component["value"]
            for component in sacrifice_candidate["score_components"]
            if component["name"].startswith("hp_cost_")
        )
        self.assertAlmostEqual(-21.0, hp_cost)

    def test_nest_and_transmorgrifier_publish_typed_event_outcomes(self):
        game = GameStub([], [strike(), defend()] * 5)
        game.in_combat = False
        game.ascension_level = 0
        ritual_dagger = build_card("RitualDagger", CardType.ATTACK)
        game.screen = EventScreen("The Nest", "Nest", "")
        game.screen.options = [
            EventOption("Steal 99 Gold", "Steal", False, 0),
            EventOption(
                "Lose 6 HP. Obtain Ritual Dagger.", "Join", False, 1,
                ritual_dagger,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        join = agent._event_option_consequences(game.screen.options[1])

        self.assertEqual(-6, join["hp_delta"])
        self.assertEqual("RitualDagger", join["card_id"])
        self.assertEqual(1, join["card_delta"])

        game.screen = EventScreen(
            "The Transmorgrifier", "Transmorgrifier", ""
        )
        game.screen.options = [
            EventOption("Transform a Card", "Transform", False, 0),
            EventOption("Leave", "Leave", False, 1),
        ]
        transform = agent._event_option_consequences(
            game.screen.options[0]
        )
        self.assertEqual("transform_card", transform["operation"])
        self.assertEqual(1, transform["card_transform_delta"])

    def test_match_game_does_not_prefer_a_known_curse_pair(self):
        game = GameStub([], [])
        game.in_combat = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game
        options = [
            EventOption("Injury", "Injury", False, 0),
            EventOption("Injury", "Injury", False, 1),
            EventOption("Backflip", "Backflip", False, 2),
            EventOption("Backflip", "Backflip", False, 3),
        ]

        action = agent.choose_match_game_action(options)

        self.assertEqual(2, action.choice_index)

    def test_match_game_intro_buttons_do_not_become_card_memory(self):
        game = GameStub([], [])
        game.screen_type = ScreenType.EVENT
        game.current_hp = 70
        game.max_hp = 70
        game.screen = EventScreen("Match and Keep", "Match and Keep!", "")
        game.screen.options = [
            EventOption("Continue", "Continue", False, 0),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        intro = agent.handle_screen()
        self.assertIsInstance(intro, ChooseAction)
        self.assertEqual(0, intro.choice_index)
        self.assertIsNone(agent.match_game_first_choice)

        game.screen.options = [
            EventOption("Play mini-game", "Play mini-game", False, 0),
        ]
        start = agent.handle_screen()
        self.assertIsInstance(start, ChooseAction)
        self.assertEqual(0, start.choice_index)
        self.assertIsNone(agent.match_game_first_choice)

        game.screen.options = [
            EventOption(f"card{index}", f"card{index}", False, index)
            for index in range(12)
        ]
        first_card = agent.handle_screen()
        self.assertIsInstance(first_card, ChooseAction)
        self.assertEqual(0, first_card.choice_index)
        self.assertEqual(0, agent.match_game_first_choice)
        self.assertEqual("card0", agent.match_game_first_label)

    def test_match_game_compacted_choices_do_not_hide_the_first_flip(self):
        game = GameStub([], [])
        game.screen_type = ScreenType.EVENT
        game.current_hp = 70
        game.max_hp = 70
        game.screen = EventScreen("Match and Keep", "Match and Keep!", "")
        game.screen.options = [
            EventOption(f"card{index}", f"card{index}", False, index)
            for index in range(12)
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        first = agent.handle_screen()
        self.assertEqual(0, first.choice_index)
        self.assertEqual("card0", agent.match_game_first_label)

        # While card0 is face up, CommunicationMod omits it and compacts the
        # remaining list. Index 0 now identifies card1.
        game.screen.options = [
            EventOption(f"card{index}", f"card{index}", False, index - 1)
            for index in range(1, 12)
        ]
        second = agent.handle_screen()
        self.assertIsInstance(second, ChooseAction)
        self.assertEqual(0, second.choice_index)
        self.assertTrue(agent.match_game_waiting_for_resolution)
        retry = agent.handle_screen()
        self.assertIsInstance(retry, ChooseAction)
        self.assertEqual(0, retry.choice_index)

    def test_match_game_known_pair_survives_same_index_compaction(self):
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        options = [
            EventOption("Malaise", "Malaise", False, 0),
            EventOption("Bandage Up", "Bandage Up", False, 1),
            EventOption("Bandage Up", "Bandage Up", False, 2),
            EventOption("Deflect", "Deflect", False, 3),
            EventOption("card6", "card6", False, 4),
        ]

        first = agent.choose_match_game_action(options)
        self.assertEqual(1, first.choice_index)

        # The clicked copy disappears. The other Bandage Up compacts from
        # index 2 to index 1, the same numeric index as the first click.
        compacted = [
            EventOption("Malaise", "Malaise", False, 0),
            EventOption("Bandage Up", "Bandage Up", False, 1),
            EventOption("Deflect", "Deflect", False, 2),
            EventOption("card6", "card6", False, 3),
        ]
        second = agent.choose_match_game_action(compacted)

        self.assertIsInstance(second, ChooseAction)
        self.assertEqual(1, second.choice_index)
        self.assertTrue(agent.match_game_waiting_for_resolution)

    def test_match_game_semantic_choice_ignores_container_order(self):
        options = [
            EventOption("card8", "card8", False, 8),
            EventOption("Backflip", "Backflip", False, 3),
            EventOption("Backflip", "Backflip", False, 6),
            EventOption("card1", "card1", False, 1),
        ]
        first_agent = SimpleAgent(PlayerClass.THE_SILENT)
        second_agent = SimpleAgent(PlayerClass.THE_SILENT)

        first = first_agent.choose_match_game_action(options)
        second = second_agent.choose_match_game_action(
            list(reversed(options))
        )

        self.assertEqual(3, first.choice_index)
        self.assertEqual(first.choice_index, second.choice_index)
        self.assertEqual(
            [row["choice_id"] for row in first_agent.last_noncombat_decision["candidates"]],
            [row["choice_id"] for row in second_agent.last_noncombat_decision["candidates"]],
        )


class NoteForYourselfAuditRegressionTests(unittest.TestCase):
    def test_note_choice_emits_score_receipts_for_every_option(self):
        game = GameStub([], [strike(), defend()] * 5)
        game.in_combat = False
        game.screen_type = ScreenType.EVENT
        game.screen = EventScreen(
            "localized", "NoteForYourself", ""
        )
        game.screen.options = [
            EventOption(
                "take", "take", False, 0, card=strike(),
                original_button_index=0,
            ),
            EventOption(
                "leave", "leave", False, 1,
                original_button_index=1,
            ),
        ]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        agent.game = game

        action = agent.choose_event_action(game.screen.options)

        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "note_for_yourself_reject_low_value_card",
            agent.last_noncombat_decision["reason"],
        )
        for row in agent.last_noncombat_decision["candidates"]:
            self.assertNotEqual("unclassified", row["score_rule_id"])
            self.assertTrue(row["score_components"])
            if row["choice_index"] == 0:
                self.assertEqual(
                    "note_for_yourself_prepare_exchange",
                    row["consequences"]["operation"],
                )
            else:
                self.assertEqual(
                    "note_for_yourself_leave",
                    row["consequences"]["operation"],
                )
                self.assertIs(True, row["consequences"]["leave"])


if __name__ == "__main__":
    unittest.main()
