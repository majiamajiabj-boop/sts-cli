import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deepseek_macro import AdvisorResult
from macro_policy import MacroPolicyConfig


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.agent import SimpleAgent
from spirecomm.communication.action import (
    CancelAction,
    BossRewardAction,
    BuyCardAction,
    CardRewardAction,
    CombatRewardAction,
    CardSelectAction,
    ChooseAction,
    ChooseMapBossAction,
    ChooseMapNodeAction,
    ChooseShopkeeperAction,
    ProceedAction,
    RestAction,
)
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Player, PlayerClass
from spirecomm.spire.map import Map, Node
from spirecomm.spire.potion import Potion
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import (
    EventOption,
    EventScreen,
    BossRewardScreen,
    CardRewardScreen,
    CombatReward,
    CombatRewardScreen,
    GridSelectScreen,
    MapScreen,
    RestOption,
    RestScreen,
    RewardType,
    ShopRoomScreen,
    ShopScreen,
)


def make_card(
    card_id,
    *,
    card_type=CardType.SKILL,
    rarity=CardRarity.UNCOMMON,
    upgrades=0,
    uuid=None,
):
    return Card(
        card_id,
        card_id,
        card_type,
        rarity,
        upgrades=upgrades,
        cost=1,
        uuid=uuid or f"{card_id}-{upgrades}",
    )


def strike(index=0, upgrades=0):
    return make_card(
        "Strike_G",
        card_type=CardType.ATTACK,
        rarity=CardRarity.BASIC,
        upgrades=upgrades,
        uuid=f"strike-{index}-{upgrades}",
    )


def defend(index=0, upgrades=0):
    return make_card(
        "Defend_G",
        rarity=CardRarity.BASIC,
        upgrades=upgrades,
        uuid=f"defend-{index}-{upgrades}",
    )


class LiveDecisionGame:
    """Authoritative-state stub that exercises the public live dispatch path."""

    def __init__(
        self,
        screen,
        *,
        deck=None,
        gold=0,
        act=1,
        floor=1,
        hp=70,
        max_hp=70,
        ascension_level=0,
        dungeon_map=None,
    ):
        self.deck = list(deck or [])
        self.gold = gold
        self.act = act
        self.floor = floor
        self.current_hp = hp
        self.max_hp = max_hp
        self.ascension_level = ascension_level
        self.player = Player(hp, max_hp, block=0, energy=3)
        self.in_combat = False
        self.relics = []
        self.map = dungeon_map or Map()

        self.key_system_unlocked = False
        self.has_emerald_key = False
        self.has_sapphire_key = False
        self.has_ruby_key = False

        self.choice_available = True
        self.proceed_available = False
        self.play_available = False
        self.end_available = False
        self.cancel_available = False

        self.are_potions_full = lambda: False
        self.get_real_potions = lambda: []
        self.set_screen(screen)

    def set_screen(self, screen):
        self.screen = screen
        self.screen_type = screen.screen_type


def event_screen(event_id, labels):
    screen = EventScreen(event_id, event_id, "")
    screen.options = [
        EventOption(
            label, label, disabled=False, choice_index=index,
            original_button_index=index,
        )
        for index, label in enumerate(labels)
    ]
    return screen


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


def typed_cleric_screen(labels, *, max_hp=70, stage="MAIN"):
    screen = event_screen("The Cleric", labels)
    heal_amount = int(max_hp * 0.25)
    instance = {
        "heal_amount": heal_amount,
        "heal_gold_cost": 35,
        "purify_cost": 50,
    }
    rows = (
        [
            ("HEAL", {"gold_cost": 35, "heal_amount": heal_amount}),
            ("PURIFY", {
                "gold_cost_if_purgeable": 50,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("LEAVE", {}),
        ]
        if stage == "MAIN" else [("CONTINUE", {})]
    )
    for option, (kind, parameters) in zip(screen.options, rows):
        option.event_contract = staged_event_contract(
            "The Cleric",
            "com.megacrit.cardcrawl.events.exordium.Cleric",
            stage, option.original_button_index, kind, instance, parameters,
        )
    return screen


def typed_designer_screen(
    labels, *, stage="MAIN", adjustment_upgrades_one=True,
    clean_up_removes_cards=True,
):
    screen = event_screen("Designer", labels)
    instance = {
        "adjustment_upgrades_one": adjustment_upgrades_one,
        "clean_up_removes_cards": clean_up_removes_cards,
        "adjust_cost": 40,
        "clean_up_cost": 60,
        "full_service_cost": 90,
        "hp_loss": 3,
    }
    if stage in {"INTRO", "DONE"}:
        rows = [("OPEN_SERVICES" if stage == "INTRO" else "CONTINUE", {})]
    else:
        rows = [
            (
                "ADJUSTMENT_GRID_UPGRADE"
                if adjustment_upgrades_one else
                "ADJUSTMENT_RANDOM_UPGRADE",
                {
                    "gold_cost": 40,
                    **({
                        "upgrade_select_count": 1,
                        "selection_mode": "PLAYER_SELECT",
                    } if adjustment_upgrades_one else {
                        "upgrade_max_count": 2,
                        "selection_mode": "RANDOM_UP_TO_AVAILABLE",
                    }),
                },
            ),
            (
                "CLEAN_UP_GRID_PURGE"
                if clean_up_removes_cards else
                "CLEAN_UP_GRID_TRANSFORM",
                {
                    "gold_cost": 60,
                    "selection_mode": "PLAYER_SELECT",
                    **({"purge_select_count": 1}
                       if clean_up_removes_cards else {
                           "transform_select_count": 2,
                           "transform_result": "RANDOM",
                       }),
                },
            ),
            ("FULL_SERVICE", {
                "gold_cost": 90,
                "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                ),
            }),
            ("PUNCH_AND_LEAVE", {"hp_loss": 3}),
        ]
    for option, (kind, parameters) in zip(screen.options, rows):
        option.event_contract = staged_event_contract(
            "Designer",
            "com.megacrit.cardcrawl.events.shrines.Designer",
            stage, option.original_button_index, kind, instance, parameters,
        )
    return screen


def typed_cursed_tome_intro_screen(labels):
    screen = event_screen("Cursed Tome", labels)
    instance = {
        "final_hp_loss": 10,
        "damage_taken": 0,
        "random_relic_pool": [
            "Necronomicon", "Enchiridion", "Nilry's Codex",
        ],
    }
    rows = [
        ("ENTER_RANDOM_BOOK_CHAIN", {
            "future_hp_loss_to_complete": 16,
            "random_relic_count": 1,
            "reward_surface": "COMBAT_REWARD",
            "selection_mode": "UNIFORM_MISC_RNG",
        }),
        ("LEAVE", {}),
    ]
    for option, (kind, parameters) in zip(screen.options, rows):
        option.event_contract = staged_event_contract(
            "Cursed Tome",
            "com.megacrit.cardcrawl.events.city.CursedTome",
            "INTRO", option.original_button_index, kind,
            instance, parameters,
        )
    return screen


def typed_goop_screen(*, gold_loss=27, continue_only=False):
    labels = ["adversarial continue"] if continue_only else [
        "claims this is Leave", "claims free gold",
    ]
    screen = event_screen("World of Goop", labels)
    for option in screen.options:
        original_index = option.original_button_index
        if continue_only:
            option_kind = "CONTINUE"
            parameters = {}
        elif original_index == 0:
            option_kind = "GATHER"
            parameters = {"gold_gain": 75, "hp_damage": 11}
        else:
            option_kind = "LEAVE"
            parameters = {"gold_loss": gold_loss}
        option.event_contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "World of Goop",
            "event_class": (
                "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
            ),
            "original_button_index": original_index,
            "option_kind": option_kind,
            "parameters": parameters,
        }
    return screen


def neow_reward_contract(
    reward_kind, drawback_kind="NONE", *, hp_bonus=0, cursed=False
):
    return {
        "contract_version": 1,
        "contract_kind": "NEOW_REWARD",
        "reward_kind": reward_kind,
        "drawback_kind": drawback_kind,
        "parameters": {
            "hp_bonus": hp_bonus,
            "cursed": cursed,
            "drawback_def_kind": (
                None if drawback_kind == "NONE" else drawback_kind
            ),
        },
    }


def typed_neow_screen(labels, contracts):
    screen = event_screen("Neow Event", labels)
    for option, contract in zip(screen.options, contracts):
        option.neow_contract = contract
    return screen


def grid_screen(
    cards, *, for_upgrade=False, for_transform=False, for_purge=False,
    num_cards=1, parent_choice_context=None,
):
    return GridSelectScreen(
        cards=cards,
        selected_cards=[],
        num_cards=num_cards,
        any_number=False,
        confirm_up=False,
        for_upgrade=for_upgrade,
        for_transform=for_transform,
        for_purge=for_purge,
        parent_choice_context=parent_choice_context,
    )


def make_map(*nodes):
    dungeon_map = Map()
    for node in nodes:
        dungeon_map.add_node(node)
    return dungeon_map


class LiveMacroDecisionTests(unittest.TestCase):
    def test_forced_map_boss_entry_records_one_typed_candidate(self):
        game = LiveDecisionGame(
            MapScreen(None, [], boss_available=True),
            act=2,
            floor=33,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapBossAction)
        decision = agent.last_noncombat_decision
        self.assertEqual("map_forced_boss_entry", decision["reason"])
        self.assertEqual("map:boss", decision["chosen_id"])
        self.assertEqual(1, len(decision["candidates"]))
        candidate = decision["candidates"][0]
        self.assertEqual(0, candidate["choice_index"])
        self.assertEqual("map_boss:0", candidate["semantic_id"])
        self.assertEqual(
            {"boss": True, "act": 2},
            candidate["consequences"]["route"],
        )
        self.assertEqual(
            candidate["score"],
            sum(row["value"] for row in candidate["score_components"]),
        )

    def test_late_act_three_avoids_optional_elite_after_emerald_key(self):
        current = Node(0, 5, "M")
        elite = Node(0, 6, "E")
        normal = Node(1, 6, "M")
        current.children = [elite, normal]
        dungeon_map = make_map(current, elite, normal)
        game = LiveDecisionGame(
            MapScreen(current, [elite, normal], boss_available=False),
            dungeon_map=dungeon_map,
            hp=80,
            max_hp=80,
            act=3,
            floor=40,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = True
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: (
                500 if node.symbol == "E" else 0
            ),
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(normal, action.node)
        rows = agent.last_noncombat_decision["candidates"]
        self.assertEqual(2, len(rows))
        by_semantic = {row["semantic_id"]: row for row in rows}
        self.assertFalse(by_semantic["E@0,6"]["selection_eligible"])
        self.assertIn(
            "emerald_key_secured_avoid_late_elite",
            by_semantic["E@0,6"]["veto_reason"],
        )
        self.assertTrue(by_semantic["M@1,6"]["selection_eligible"])

    def test_late_act_three_keeps_forced_elite_legal(self):
        current = Node(0, 5, "M")
        elite = Node(0, 6, "E")
        current.children = [elite]
        game = LiveDecisionGame(
            MapScreen(current, [elite], boss_available=False),
            dungeon_map=make_map(current, elite),
            hp=80,
            max_hp=80,
            act=3,
            floor=40,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = True
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(elite, action.node)

    def test_map_semantic_choice_ignores_visible_container_order(self):
        left = Node(0, 0, "?")
        right = Node(1, 0, "$")
        dungeon_map = make_map(left, right)

        def run(order):
            game = LiveDecisionGame(
                MapScreen(None, order, boss_available=False),
                dungeon_map=dungeon_map,
                hp=70,
                max_hp=70,
                act=1,
                floor=0,
            )
            agent = SimpleAgent(PlayerClass.THE_SILENT)
            with patch.object(
                agent,
                "score_map_node",
                side_effect=lambda node, projected_hp=None: (
                    20 if node.x == 1 else 10
                ),
            ):
                action = agent.get_next_action_in_game(game)
            return action, agent.last_noncombat_decision

        first, first_decision = run([left, right])
        second, second_decision = run([right, left])

        self.assertEqual((1, 0), (first.node.x, first.node.y))
        self.assertEqual(
            (first.node.x, first.node.y), (second.node.x, second.node.y)
        )
        self.assertEqual(
            [row["choice_id"] for row in first_decision["candidates"]],
            [row["choice_id"] for row in second_decision["candidates"]],
        )

    def test_developed_act_one_deck_prefers_event_over_extra_hallway(self):
        event = Node(0, 0, "?")
        hallway = Node(1, 0, "M")
        game = LiveDecisionGame(
            MapScreen(
                None, [event, hallway], boss_available=False
            ),
            dungeon_map=make_map(event, hallway),
            hp=70,
            max_hp=70,
            act=1,
            floor=0,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")

        with patch.object(
            agent, "_act1_elite_readiness", return_value=1.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(event, action.node)
        event_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["semantic_id"] == "?@0,0"
        )
        components = event_row["consequences"][
            "question_room_value_components"
        ]
        self.assertGreater(components["relic_opportunity_value"], 0)
        self.assertEqual(
            sum(components.values()),
            agent.score_map_node(event),
        )

    def test_undeveloped_act_one_deck_still_values_hallway_rewards(self):
        event = Node(0, 0, "?")
        hallway = Node(1, 0, "M")
        game = LiveDecisionGame(
            MapScreen(
                None, [event, hallway], boss_available=False
            ),
            dungeon_map=make_map(event, hallway),
            hp=70,
            max_hp=70,
            act=1,
            floor=0,
        )
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")

        with patch.object(
            agent, "_act1_elite_readiness", return_value=0.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(hallway, action.node)

    def test_low_hp_act_one_prefers_event_even_with_development_gap(self):
        event = Node(0, 0, "?")
        hallway = Node(1, 0, "M")
        game = LiveDecisionGame(
            MapScreen(
                None, [event, hallway], boss_available=False
            ),
            dungeon_map=make_map(event, hallway),
            hp=30,
            max_hp=70,
            act=1,
            floor=0,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_act1_elite_readiness", return_value=0.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(event, action.node)

    def test_unknown_event_does_not_treat_take_relic_as_hp_loss(self):
        game = LiveDecisionGame(
            event_screen("Modded Gift", ["Leave", "Take a relic"]),
            hp=18,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_designer_records_all_four_typed_a0_outcomes(self):
        screen = typed_designer_screen(
            [
                "Adjustments. Lose 40 Gold. Upgrade a card.",
                "Clean Up. Lose 60 Gold. Remove a card.",
                (
                    "Full Service. Lose 90 Gold. Remove a card, then "
                    "upgrade a random card."
                ),
                "Punch. Lose 3 HP.",
            ],
        )
        screen.options.reverse()
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            gold=200,
            hp=70,
            max_hp=70,
            act=2,
            floor=20,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        agent.get_next_action_in_game(game)

        by_index = {
            row["choice_index"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual({0, 1, 2, 3}, set(by_index))
        self.assertEqual(-40, by_index[0]["consequences"]["gold_delta"])
        self.assertEqual(
            "designer_adjustment_grid_upgrade",
            by_index[0]["consequences"]["operation"],
        )
        self.assertTrue(by_index[0]["selection_eligible"])
        self.assertEqual(
            "grid_upgrade",
            by_index[0]["consequences"]["future_costs"][0]["operation"],
        )
        self.assertEqual(-60, by_index[1]["consequences"]["gold_delta"])
        self.assertTrue(by_index[1]["selection_eligible"])
        self.assertEqual(
            "designer_clean_up_grid_purge",
            by_index[1]["consequences"]["operation"],
        )
        full_service = by_index[2]["consequences"]
        self.assertEqual(-90, full_service["gold_delta"])
        self.assertEqual(
            ["grid_purge", "random_upgrade"],
            [row["operation"] for row in full_service["future_costs"]],
        )
        self.assertEqual(
            "random_card_upgrade", full_service["random_effects"][0]["kind"]
        )
        self.assertEqual(-3, by_index[3]["consequences"]["hp_delta"])
        self.assertTrue(by_index[3]["consequences"]["leave"])
        for row in by_index.values():
            self.assertNotEqual("unclassified", row["score_rule_id"])
            self.assertTrue(row["score_formula"])
            self.assertTrue(row["score_inputs"])
            self.assertTrue(row["score_components"])

    def test_unknown_event_rejects_explicit_lethal_composite_reward(self):
        game = LiveDecisionGame(
            event_screen(
                "Modded Trap",
                ["Take 999 damage. Gain a relic", "Leave"],
            ),
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_real_repeatable_search_event_is_taken_only_at_safe_hp(self):
        game = LiveDecisionGame(
            event_screen("Dead Adventurer", ["Search", "Leave"]),
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)

    def test_dead_adventurer_chinese_repeat_screen_keeps_searching(self):
        screen = EventScreen("Dead Adventurer", "Dead Adventurer", "")
        screen.options = [
            EventOption(
                "[继续] 寻找东西。50%：遇见回来的怪物。",
                "继续",
                disabled=False,
                choice_index=0,
            ),
            EventOption("[离开]", "离开", disabled=False, choice_index=1),
        ]
        game = LiveDecisionGame(screen, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        self.assertEqual(
            "event_repeatable_reward_at_safe_hp",
            agent.last_noncombat_decision["reason"],
        )

    def test_scrap_ooze_chinese_repeat_screen_keeps_reaching(self):
        screen = EventScreen("Scrap Ooze", "Scrap Ooze", "")
        screen.options = [
            EventOption(
                "[接着往里伸] 失去4生命。35%：找到遗物。",
                "接着往里伸",
                disabled=False,
                choice_index=0,
            ),
            EventOption("[离开]", "离开", disabled=False, choice_index=1),
        ]
        game = LiveDecisionGame(screen, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        self.assertEqual(
            "event_repeatable_reward_at_safe_hp",
            agent.last_noncombat_decision["reason"],
        )

    def test_all_unknown_event_tie_is_not_an_index_zero_default(self):
        game = LiveDecisionGame(
            event_screen("Modded Unknown", ["Alpha", "Zulu"]),
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_note_for_yourself_accepts_apotheosis_from_card_preview(self):
        apotheosis = make_card(
            "Apotheosis", rarity=CardRarity.RARE, uuid="note-apotheosis"
        )
        screen = EventScreen("A Note For Yourself", "A Note For Yourself", "")
        screen.options = [
            EventOption(
                "[Take] Trade a card.",
                "Take",
                disabled=False,
                choice_index=0,
                card=apotheosis,
            ),
            EventOption("[Leave]", "Leave", disabled=False, choice_index=1),
        ]
        game = LiveDecisionGame(
            screen, deck=[strike(), defend()], hp=70, max_hp=70
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        self.assertEqual(
            "note_for_yourself_accept_positive_card",
            agent.last_noncombat_decision["reason"],
        )
        self.assertEqual(
            "Apotheosis", agent.last_noncombat_decision["offered_card"]
        )

    def test_event_option_deserializes_card_preview(self):
        option = EventOption.from_json({
            "text": "[Take] Trade a card.",
            "label": "Take",
            "disabled": False,
            "choice_index": 0,
            "card": {
                "id": "Apotheosis",
                "name": "Apotheosis",
                "type": "SKILL",
                "rarity": "RARE",
                "upgrades": 0,
                "has_target": False,
                "cost": 2,
                "uuid": "note-apotheosis",
            },
        })

        self.assertEqual("Apotheosis", option.card.card_id)
        self.assertEqual("note-apotheosis", option.card.uuid)

    def test_event_option_deserializes_typed_neow_contract(self):
        contract = neow_reward_contract("HUNDRED_GOLD")
        option = EventOption.from_json({
            "text": "localized display only",
            "label": "opaque label",
            "disabled": False,
            "choice_index": 2,
            "neow_contract": contract,
        })

        self.assertEqual(contract, option.neow_contract)
        self.assertEqual(2, option.choice_index)

    def test_note_for_yourself_accepts_visible_apotheosis_label_without_preview(self):
        screen = event_screen(
            "A Note For Yourself", ["[Take] Apotheosis", "[Leave]"]
        )
        game = LiveDecisionGame(screen, deck=[strike(), defend()])
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        self.assertEqual(
            "note_for_yourself_accept_positive_card",
            agent.last_noncombat_decision["reason"],
        )

    def test_unknown_event_uses_visible_outcome_semantics_not_option_zero(self):
        game = LiveDecisionGame(
            event_screen(
                "Modded Crossroads",
                ["Lose 18 HP", "Gain a relic", "Leave"],
            ),
            hp=30,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "event_semantic_outcome_score",
            agent.last_noncombat_decision["reason"],
        )

    def test_unknown_event_prefers_leave_to_low_hp_optional_fight(self):
        game = LiveDecisionGame(
            event_screen(
                "Modded Arena",
                ["Fight for a card", "Leave"],
            ),
            hp=18,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        self.assertIn(
            "combat_cost",
            agent.last_noncombat_decision["option_semantics"]["0"],
        )

    def test_sensory_stone_real_frame_values_optional_colorless_rewards(self):
        screen = event_screen(
            "SensoryStone",
            [
                "[回忆] 在你的牌组中加入 1 张无色牌。",
                "[回忆] 在你的牌组中加入 2 张无色牌。 失去 5 点生命。",
                "[回忆] 在你的牌组中加入 3 张无色牌。 失去 10 点生命。",
            ],
        )
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            hp=76,
            max_hp=80,
            act=3,
            floor=38,
        )
        game.has_ruby_key = True
        game.has_sapphire_key = True
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_deck_readiness", return_value={"score": 0.904}
        ):
            action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)
        candidates = {
            int(row["id"]): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        for index, (count, hp_delta) in enumerate(
            ((1, 0), (2, -5), (3, -10))
        ):
            facts = candidates[index]["consequences"]
            self.assertEqual(count, facts["optional_card_reward_count"])
            self.assertEqual(3, facts["card_reward_candidates_per_screen"])
            self.assertTrue(facts["selection_optional"])
            self.assertEqual(0, facts["card_delta_min"])
            self.assertEqual(count, facts["card_delta_max"])
            self.assertEqual(hp_delta, facts["hp_delta"])
            self.assertNotIn(
                "unknown", candidates[index]["semantic_signals"]
            )
        self.assertGreater(
            candidates[2]["consequences"]["expected_card_option_value"],
            candidates[1]["consequences"]["expected_card_option_value"],
        )
        marginal = candidates[2]["consequences"][
            "marginal_card_option_values"
        ]
        self.assertGreater(marginal[0], marginal[1])
        self.assertGreater(marginal[1], marginal[2])

    def test_sensory_stone_preserves_hp_on_injured_emerald_route(self):
        game = LiveDecisionGame(
            event_screen(
                "SensoryStone",
                [
                    "Add 1 Colorless card to your deck.",
                    "Add 2 Colorless cards to your deck. Lose 5 HP.",
                    "Add 3 Colorless cards to your deck. Lose 10 HP.",
                ],
            ),
            deck=[strike(), defend()],
            hp=40,
            max_hp=80,
            act=3,
            floor=38,
        )
        game.has_ruby_key = True
        game.has_sapphire_key = True
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_deck_readiness", return_value={"score": 0.904}
        ):
            action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        self.assertTrue(
            agent.last_noncombat_decision["candidates"][0][
                "consequences"
            ]["emerald_route_pressure"]
        )

    def test_sensory_stone_uses_choice_index_when_options_are_reordered(self):
        screen = event_screen(
            "SensoryStone",
            [
                "Add 1 Colorless card to your deck.",
                "Add 2 Colorless cards to your deck. Lose 5 HP.",
                "Add 3 Colorless cards to your deck. Lose 10 HP.",
            ],
        )
        screen.options = list(reversed(screen.options))
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            hp=76,
            max_hp=80,
            act=3,
            floor=38,
        )
        game.has_ruby_key = True
        game.has_sapphire_key = True
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_deck_readiness", return_value={"score": 0.904}
        ):
            action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)

    def test_shop_room_decision_is_idempotent_on_protocol_retry(self):
        game = LiveDecisionGame(ShopRoomScreen(), gold=100)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        first = agent.get_next_action_in_game(game)
        retry = agent.get_next_action_in_game(game)

        self.assertIsInstance(first, ChooseShopkeeperAction)
        self.assertIsInstance(retry, ChooseShopkeeperAction)

    def test_shop_room_proceeds_after_confirmed_shop_exit(self):
        game = LiveDecisionGame(ShopRoomScreen(), gold=29, floor=3)
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        self.assertIsInstance(
            agent.get_next_action_in_game(game),
            ChooseShopkeeperAction,
        )

        game.set_screen(ShopScreen([], [], [], False, 75))
        # This is the real CommunicationMod shape after spending down below
        # every listed price: the shop remains open but only CANCEL is legal.
        game.choice_available = False
        game.cancel_available = True
        self.assertIsInstance(agent.get_next_action_in_game(game), CancelAction)
        leave = agent.last_noncombat_decision["candidates"][0]
        self.assertEqual("leave", leave["id"])
        self.assertEqual("action:return", leave["choice_id"])
        self.assertEqual("return", leave["action"])

        game.set_screen(ShopRoomScreen())
        game.choice_available = True
        game.cancel_available = False
        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ProceedAction)
        self.assertEqual(
            "shop_room_proceed_after_confirmed_exit",
            agent.last_noncombat_decision["reason"],
        )

    def test_cleric_healthy_with_gold_selects_removal_then_removes_strike(self):
        cards = [strike(), defend()]
        game = LiveDecisionGame(
            typed_cleric_screen(["Heal", "Purify", "Leave"]),
            deck=cards,
            gold=80,
            hp=64,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        event_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(event_action, ChooseAction)
        self.assertEqual(1, event_action.choice_index, "healthy Cleric visit should buy Purify, not Heal")

        game.set_screen(grid_screen(cards, for_purge=True))
        grid_action = agent.get_next_action_in_game(game)

        self.assertIsInstance(grid_action, CardSelectAction)
        self.assertEqual(["Strike_G"], [card.card_id for card in grid_action.cards])

    def test_vampires_bites_are_preserved_with_coffee_dripper(self):
        """The Vampires max-HP trade is a package, not five purge targets."""
        bites = [
            make_card(
                "Bite", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid=f"bite-{index}",
            )
            for index in range(5)
        ]
        strike_card = strike(index=9)
        defend_card = defend(index=9)
        game = LiveDecisionGame(
            grid_screen([bites[0], strike_card, defend_card], for_purge=True),
            deck=[*bites, strike_card, defend_card],
            act=2,
            floor=25,
            hp=56,
            max_hp=56,
        )
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertNotEqual("Bite", action.cards[0].card_id)
        self.assertEqual(
            "grid_remove_or_transform_protect_vampires_bites",
            agent.last_noncombat_decision["reason"],
        )

    def test_bite_removal_without_coffee_dripper_prices_healing_before_duplicates(self):
        bites = [
            make_card(
                "Bite", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid=f"bite-no-coffee-{index}",
            )
            for index in range(5)
        ]
        bite = bites[0]
        game = LiveDecisionGame(
            grid_screen([bite, defend(index=10)], for_purge=True),
            deck=bites + [defend(index=10)],
            act=2,
            floor=25,
            hp=56,
            max_hp=56,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual("Defend_G", action.cards[0].card_id)
        self.assertLess(agent._removal_score_parts(bite)["lost_sustain_value"], 0)

    def test_cleric_localized_options_bind_by_stable_choice_index(self):
        screen = typed_cleric_screen(["治疗", "净化", "离开"])
        screen.options = list(reversed(screen.options))
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            gold=100,
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "remove_card", agent.last_noncombat_decision["chosen_kind"]
        )
        self.assertEqual(
            {"heal", "remove_card", "leave"},
            {
                row["semantic_kind"]
                for row in agent.last_noncombat_decision["candidates"]
            },
        )

    def test_typed_cleric_ignores_adversarial_labels_and_records_score_rules(self):
        screen = typed_cleric_screen(
            ["Leave", "Heal", "Purify"], max_hp=71
        )
        screen.options.reverse()
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            gold=100,
            hp=70,
            max_hp=71,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        rows = {
            row["choice_index"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(-50, rows[1]["consequences"]["gold_delta"])
        self.assertEqual(
            "cleric_grid_selection",
            rows[1]["consequences"]["future_costs"][0]["kind"],
        )
        self.assertEqual(1, rows[0]["consequences"]["hp_delta"])
        for row in rows.values():
            self.assertNotEqual("unclassified", row["score_rule_id"])
            self.assertAlmostEqual(
                row["score"],
                sum(component["value"] for component in row["score_components"]),
            )

    def test_world_of_goop_private_loss_uses_typed_contract(self):
        screen = typed_goop_screen(gold_loss=27)
        screen.options.reverse()
        game = LiveDecisionGame(screen, gold=36, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        rows = {
            row["choice_index"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(75, rows[0]["consequences"]["gold_delta"])
        self.assertEqual(-11, rows[0]["consequences"]["hp_delta"])
        self.assertTrue(rows[1]["selection_eligible"])
        self.assertEqual(-27, rows[1]["consequences"]["gold_delta"])
        self.assertEqual(
            "world_of_goop_leave",
            rows[1]["consequences"]["operation"],
        )

    def test_world_of_goop_low_gold_cap_makes_leave_exact(self):
        game = LiveDecisionGame(
            typed_goop_screen(gold_loss=17),
            gold=17,
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        agent.get_next_action_in_game(game)

        leave = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_index"] == 1
        )
        self.assertTrue(leave["selection_eligible"])
        self.assertEqual(-17, leave["consequences"]["gold_delta"])
        self.assertEqual("world_of_goop_leave", leave["consequences"]["operation"])

    def test_world_of_goop_wrong_original_contract_fails_closed(self):
        screen = typed_goop_screen(gold_loss=27)
        screen.options[1].event_contract["original_button_index"] = 0
        game = LiveDecisionGame(screen, gold=36, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsNone(action)
        self.assertTrue(all(
            not row["selection_eligible"]
            for row in agent.last_noncombat_decision["candidates"]
        ))
        self.assertTrue(all(
            "typed_event_contract_missing_invalid_or_mismatched"
            in row["consequences"]["reason_codes"]
            for row in agent.last_noncombat_decision["candidates"]
        ))

    def test_cursed_tome_vetoes_book_chain_without_using_labels(self):
        screen = typed_cursed_tome_intro_screen(["Leave", "Read"])
        screen.options.reverse()
        for choice_index, option in enumerate(screen.options):
            option.choice_index = choice_index
        game = LiveDecisionGame(screen, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        rows = {
            row["choice_index"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertTrue(rows[0]["selection_eligible"])
        self.assertEqual(
            "cursed_tome_leave",
            rows[0]["consequences"]["operation"],
        )
        self.assertFalse(rows[1]["selection_eligible"])
        self.assertEqual(
            "unsupported_necronomicon_activation_state",
            rows[1]["veto_reason"],
        )

    def test_typed_event_single_dialog_noops_do_not_read_labels(self):
        expected = {
            "The Cleric": "cleric_dialog_advance_noop",
            "Designer": "designer_dialog_advance_noop",
            "World of Goop": "world_of_goop_dialog_advance_noop",
            "The Mausoleum": "mausoleum_dialog_advance_noop",
        }
        for event_id, operation in expected.items():
            with self.subTest(event_id=event_id):
                game = LiveDecisionGame(
                    (
                        typed_goop_screen(continue_only=True)
                        if event_id == "World of Goop" else
                        typed_cleric_screen(
                            ["claims a large reward"], stage="RESULT"
                        )
                        if event_id == "The Cleric" else
                        typed_designer_screen(
                            ["claims a large reward"], stage="DONE"
                        )
                        if event_id == "Designer" else
                        event_screen(event_id, ["claims a large reward"])
                    )
                )
                agent = SimpleAgent(PlayerClass.IRONCLAD)

                action = agent.get_next_action_in_game(game)

                self.assertEqual(0, action.choice_index)
                row = agent.last_noncombat_decision["candidates"][0]
                self.assertEqual(operation, row["consequences"]["operation"])
                self.assertEqual((0, 0, 0), (
                    row["consequences"]["hp_delta"],
                    row["consequences"]["max_hp_delta"],
                    row["consequences"]["gold_delta"],
                ))

    def test_compacted_designer_surface_uses_original_index(self):
        screen = typed_designer_screen(["a", "b", "c", "d"])
        screen.options[0].disabled = True
        screen.options[0].choice_index = None
        for compact_index, option in enumerate(screen.options[1:]):
            option.choice_index = compact_index
        game = LiveDecisionGame(
            screen, deck=[strike(), defend()], gold=200
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        rows = agent.last_noncombat_decision["candidates"]
        self.assertEqual(3, len(rows))
        chosen = next(row for row in rows if row["choice_index"] == 0)
        self.assertEqual(
            "designer_clean_up_grid_purge",
            chosen["consequences"]["operation"],
        )
        self.assertEqual(1, chosen["consequences"]["original_button_index"])
        full_service = next(
            row for row in rows if row["choice_index"] == 1
        )
        self.assertEqual(
            "designer_full_service",
            full_service["consequences"]["operation"],
        )
        self.assertEqual(
            2, full_service["consequences"]["original_button_index"]
        )

    def test_compacted_designer_without_original_index_fails_closed(self):
        screen = typed_designer_screen(["a", "b", "c", "d"])
        for option in screen.options:
            option.original_button_index = None
        screen.options[0].disabled = True
        screen.options[0].choice_index = None
        for compact_index, option in enumerate(screen.options[1:]):
            option.choice_index = compact_index
        game = LiveDecisionGame(
            screen, deck=[strike(), defend()], gold=200
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsNone(action)
        self.assertTrue(all(
            not row["selection_eligible"]
            for row in agent.last_noncombat_decision["candidates"]
        ))

    def test_golden_idol_healthy_penalty_loses_max_hp_instead_of_taking_injury(self):
        game = LiveDecisionGame(
            event_screen("Golden Idol", ["Take Injury", "Take damage", "Lose max HP"]),
            deck=[strike(), defend()],
            hp=64,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(2, action.choice_index)

    def test_back_to_basics_upgrades_all_when_three_unupgraded_basics_exist(self):
        deck = [strike(), defend(0), defend(1), make_card("Backflip")]
        game = LiveDecisionGame(
            event_screen("Back to Basics", ["Remove a card", "Upgrade all Strikes and Defends"]),
            deck=deck,
            hp=25,
            max_hp=70,
            act=2,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(1, action.choice_index)

    def test_nloth_trades_spent_one_time_relic_instead_of_live_combat_relic(self):
        game = LiveDecisionGame(
            event_screen("N'loth", ["Offer Tiny House", "Offer Ninja Scroll", "Leave"]),
            deck=[make_card("Blade Dance")],
            act=2,
        )
        game.relics = [
            Relic("Tiny House", "Tiny House", counter=-1),
            Relic("Ninja Scroll", "Ninja Scroll", counter=-1),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual(0, action.choice_index)

    def test_live_smith_grid_prioritizes_run_defining_upgrade_over_static_order(self):
        cases = [
            (
                make_card("Wraith Form v2", card_type=CardType.POWER, rarity=CardRarity.RARE),
                make_card("A Thousand Cuts", card_type=CardType.POWER, rarity=CardRarity.RARE),
            ),
            (
                make_card("Catalyst"),
                make_card("Footwork", card_type=CardType.POWER),
            ),
        ]

        for preferred, static_distractor in cases:
            with self.subTest(preferred=preferred.card_id):
                cards = [static_distractor, preferred]
                game = LiveDecisionGame(grid_screen(cards, for_upgrade=True), deck=cards)
                agent = SimpleAgent(PlayerClass.THE_SILENT)

                action = agent.get_next_action_in_game(game)

                self.assertIsInstance(action, CardSelectAction)
                self.assertEqual([preferred.card_id], [card.card_id for card in action.cards])

    def test_astrolabe_parent_context_types_native_ambiguous_grid(self):
        cards = [
            strike(0), strike(1), defend(0),
            make_card(
                "Immolate", card_type=CardType.ATTACK,
                rarity=CardRarity.RARE, upgrades=1,
                uuid="astrolabe-immolate",
            ),
        ]
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "BOSS_REWARD",
            "source_option_id": "option:astrolabe",
            "source_choice_index": 2,
            "relic_id": "Astrolabe",
            "operation": "transform",
            "select_count": 3,
        }
        # The base game exposes Astrolabe with all three native operation
        # flags false, so the accepted boss-relic choice is the authority.
        screen = grid_screen(
            cards,
            num_cards=3,
            parent_choice_context=parent,
        )
        game = LiveDecisionGame(screen, deck=cards, act=2, floor=17)
        game.current_action = None
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual(3, len(action.cards))
        decision = agent.last_noncombat_decision
        self.assertEqual(
            "grid_remove_or_transform_weakest", decision["reason"]
        )
        self.assertEqual(4, len(decision["candidates"]))
        self.assertTrue(all(
            row["consequences"]["operation"] == "grid_transform"
            for row in decision["candidates"]
        ))

    def test_non_destructive_grid_selects_best_card_instead_of_purge_target(self):
        footwork = make_card("Footwork", card_type=CardType.POWER)
        thousand_cuts = make_card("A Thousand Cuts", card_type=CardType.POWER, rarity=CardRarity.RARE)
        screen = GridSelectScreen(
            cards=[thousand_cuts, footwork], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=False,
        )
        game = LiveDecisionGame(screen, deck=[thousand_cuts, footwork])
        game.current_action = "BottledTornado"
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual(["Footwork"], [card.card_id for card in action.cards])

    def test_bottled_lightning_reward_binds_opening_skill_grid_in_any_order(self):
        shockwave = Card(
            "Shockwave", "Shockwave", CardType.SKILL,
            CardRarity.UNCOMMON, upgrades=1, cost=2,
            uuid="real-shockwave-plus", exhausts=True, magic_number=5,
        )
        shrug = Card(
            "Shrug It Off", "Shrug It Off", CardType.SKILL,
            CardRarity.COMMON, upgrades=1, cost=1,
            uuid="real-shrug-plus", block=11, base_block=11,
        )
        defends = [
            Card(
                "Defend_R", "Defend", CardType.SKILL,
                CardRarity.BASIC, cost=1, uuid=f"real-defend-{index}",
                block=5, base_block=5,
            )
            for index in range(4)
        ]
        skills = [shockwave, shrug, *defends]
        deck = [
            *[
                make_card(
                    "Strike_R",
                    card_type=CardType.ATTACK,
                    rarity=CardRarity.BASIC,
                    uuid=f"real-strike-{index}",
                )
                for index in range(4)
            ],
            *defends,
            make_card(
                "Bash", card_type=CardType.ATTACK,
                rarity=CardRarity.BASIC, upgrades=1,
                uuid="real-bash-plus",
            ),
            make_card(
                "Wild Strike", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid="real-wild-strike",
            ),
            make_card(
                "Twin Strike", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid="real-twin-strike",
            ),
            make_card(
                "Thunderclap", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid="real-thunderclap",
            ),
            shrug,
            shockwave,
            make_card(
                "Juggernaut", card_type=CardType.POWER,
                rarity=CardRarity.RARE, uuid="real-juggernaut",
            ),
        ]
        orders = [
            skills,
            list(reversed(skills)),
            [defends[1], shrug, defends[3], shockwave, defends[0], defends[2]],
        ]

        for order in orders:
            with self.subTest(order=[card.uuid for card in order]):
                bottle = Relic("Bottled Lightning", "Bottled Lightning")
                reward = CombatReward(RewardType.RELIC, relic=bottle)
                game = LiveDecisionGame(
                    CombatRewardScreen([reward]),
                    deck=deck,
                    gold=474,
                    act=2,
                    floor=20,
                    hp=55,
                    max_hp=67,
                )
                game.room_type = "TreasureRoom"
                game.key_system_unlocked = True
                game.has_sapphire_key = True
                game.relics = [
                    Relic("Burning Blood", "Burning Blood"),
                    Relic("Champion Belt", "Champion Belt"),
                    Relic("Golden Idol", "Golden Idol"),
                    Relic("Kunai", "Kunai"),
                    Relic("Coffee Dripper", "Coffee Dripper"),
                ]
                agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

                reward_action = agent.get_next_action_in_game(game)

                self.assertIsInstance(reward_action, CombatRewardAction)
                self.assertIs(reward, reward_action.combat_reward)
                self.assertEqual(
                    "bottled_lightning",
                    agent.pending_selection_context["source"],
                )
                retry_action = agent.get_next_action_in_game(game)
                self.assertIsInstance(retry_action, CombatRewardAction)
                self.assertEqual(
                    0,
                    agent.pending_selection_context["relic_count_before"],
                )

                game.relics.append(bottle)
                game.set_screen(grid_screen(order))
                game.current_action = None
                grid_action = agent.get_next_action_in_game(game)

                self.assertIsInstance(grid_action, CardSelectAction)
                self.assertEqual([shockwave], grid_action.cards)
                decision = agent.last_noncombat_decision
                self.assertEqual(
                    "grid_bottled_lightning_opening_value",
                    decision["reason"],
                )
                self.assertNotEqual(
                    "grid_unknown_conservative_weak_card",
                    decision["reason"],
                )
                scores = {
                    row["id"]: row["score"]
                    for row in decision["candidates"]
                }
                self.assertGreater(scores["Shockwave"], scores["Shrug It Off"])
                self.assertGreater(scores["Shrug It Off"], scores["Defend_R"])
                self.assertEqual(
                    "bottled_lightning",
                    decision["selection_context"]["source"],
                )

                agent.confirm_card_selection(game, shockwave.uuid)
                self.assertIsNone(agent.pending_selection_context)

    def test_owned_bottled_lightning_does_not_retype_unbound_grid(self):
        shockwave = Card(
            "Shockwave", "Shockwave", CardType.SKILL,
            CardRarity.UNCOMMON, upgrades=1, cost=2,
            uuid="unbound-shockwave", exhausts=True, magic_number=5,
        )
        weak_defend = Card(
            "Defend_R", "Defend", CardType.SKILL,
            CardRarity.BASIC, cost=1, uuid="unbound-defend",
            block=5, base_block=5,
        )
        game = LiveDecisionGame(
            grid_screen([shockwave, weak_defend]),
            deck=[shockwave, weak_defend],
            act=2,
            floor=20,
        )
        game.current_action = None
        game.relics = [Relic("Bottled Lightning", "Bottled Lightning")]
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual([weak_defend], action.cards)
        self.assertEqual(
            "grid_unknown_conservative_weak_card",
            agent.last_noncombat_decision["reason"],
        )

    def test_bottled_lightning_binding_fails_closed_on_non_skill_grid(self):
        bottle = Relic("Bottled Lightning", "Bottled Lightning")
        reward = CombatReward(RewardType.RELIC, relic=bottle)
        shockwave = make_card(
            "Shockwave", upgrades=1, uuid="mismatched-shockwave"
        )
        strike_card = make_card(
            "Strike_R",
            card_type=CardType.ATTACK,
            rarity=CardRarity.BASIC,
            uuid="mismatched-strike",
        )
        game = LiveDecisionGame(
            CombatRewardScreen([reward]),
            deck=[shockwave, strike_card],
            act=2,
            floor=20,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.get_next_action_in_game(game)
        game.relics = [bottle]
        game.set_screen(grid_screen([shockwave, strike_card]))
        game.current_action = None

        action = agent.get_next_action_in_game(game)

        self.assertEqual([strike_card], action.cards)
        self.assertEqual(
            "grid_unknown_conservative_weak_card",
            agent.last_noncombat_decision["reason"],
        )
        self.assertIsNone(agent.pending_selection_context)

    def test_bottled_lightning_context_expires_before_later_grid(self):
        for transition in ("next_floor", "same_floor_card_reward"):
            with self.subTest(transition=transition):
                bottle = Relic("Bottled Lightning", "Bottled Lightning")
                reward = CombatReward(RewardType.RELIC, relic=bottle)
                shockwave = make_card(
                    "Shockwave", upgrades=1,
                    uuid=f"lifecycle-shockwave-{transition}",
                )
                weak_defend = make_card(
                    "Defend_R", rarity=CardRarity.BASIC,
                    uuid=f"lifecycle-defend-{transition}",
                )
                game = LiveDecisionGame(
                    CombatRewardScreen([reward]),
                    deck=[shockwave, weak_defend],
                    act=2,
                    floor=20,
                )
                agent = SimpleAgent(PlayerClass.IRONCLAD)
                agent.get_next_action_in_game(game)
                game.relics = [bottle]

                if transition == "next_floor":
                    game.floor = 21
                else:
                    game.set_screen(CardRewardScreen(
                        [make_card("Pommel Strike", uuid="intervening-card")],
                        can_bowl=False,
                        can_skip=True,
                    ))
                    agent.get_next_action_in_game(game)
                    self.assertIsNone(agent.pending_selection_context)

                game.set_screen(grid_screen([shockwave, weak_defend]))
                game.current_action = None
                action = agent.get_next_action_in_game(game)

                self.assertEqual([weak_defend], action.cards)
                self.assertEqual(
                    "grid_unknown_conservative_weak_card",
                    agent.last_noncombat_decision["reason"],
                )
                self.assertIsNone(agent.pending_selection_context)

    def test_bottled_lightning_grid_requires_a_new_relic_instance(self):
        for old_bottle_count in (0, 1):
            with self.subTest(old_bottle_count=old_bottle_count):
                offered = Relic("Bottled Lightning", "Bottled Lightning")
                reward = CombatReward(RewardType.RELIC, relic=offered)
                shockwave = make_card(
                    "Shockwave", upgrades=1,
                    uuid=f"unsettled-shockwave-{old_bottle_count}",
                )
                weak_defend = make_card(
                    "Defend_R", rarity=CardRarity.BASIC,
                    uuid=f"unsettled-defend-{old_bottle_count}",
                )
                game = LiveDecisionGame(
                    CombatRewardScreen([reward]),
                    deck=[shockwave, weak_defend],
                    act=2,
                    floor=20,
                )
                game.relics = [
                    Relic("Bottled Lightning", "Old Bottled Lightning")
                    for _ in range(old_bottle_count)
                ]
                agent = SimpleAgent(PlayerClass.IRONCLAD)

                agent.get_next_action_in_game(game)
                self.assertEqual(
                    old_bottle_count,
                    agent.pending_selection_context["relic_count_before"],
                )
                # Simulate an abnormal transition where the reward click was
                # bound but no new relic was added to authoritative state.
                game.set_screen(grid_screen([shockwave, weak_defend]))
                game.current_action = None
                action = agent.get_next_action_in_game(game)

                self.assertEqual([weak_defend], action.cards)
                self.assertEqual(
                    "grid_unknown_conservative_weak_card",
                    agent.last_noncombat_decision["reason"],
                )
                self.assertIsNone(agent.pending_selection_context)

    def test_bonfire_sacrifice_grid_protects_run_defining_card(self):
        wraith = make_card("Wraith Form v2", card_type=CardType.POWER, rarity=CardRarity.RARE)
        weak_strike = strike()
        screen = GridSelectScreen(
            cards=[wraith, weak_strike], selected_cards=[], num_cards=1,
            any_number=False, confirm_up=False, for_upgrade=False,
            for_transform=False, for_purge=False,
        )
        game = LiveDecisionGame(screen, deck=[wraith, weak_strike])
        game.current_action = "BonfireChooseCardAction"
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual(["Strike_G"], [card.card_id for card in action.cards])

    def test_owned_bottle_does_not_turn_later_gift_grid_into_positive_selection(self):
        wraith = make_card("Wraith Form v2", card_type=CardType.POWER, rarity=CardRarity.RARE)
        weak_strike = strike()
        game = LiveDecisionGame(
            grid_screen([wraith, weak_strike]),
            deck=[wraith, weak_strike],
        )
        game.current_action = "WeMeetAgain"
        game.relics = [Relic("Bottled Tornado", "Bottled Tornado")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(["Strike_G"], [card.card_id for card in action.cards])

    def test_duplicator_grid_selects_high_value_card(self):
        footwork = make_card("Footwork", card_type=CardType.POWER)
        weak_strike = strike()
        game = LiveDecisionGame(
            grid_screen([weak_strike, footwork]),
            deck=[weak_strike, footwork],
        )
        game.current_action = "Duplicator"
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(["Footwork"], [card.card_id for card in action.cards])

    def test_duplicator_does_not_copy_limit_break_with_only_flex_support(self):
        flex = make_card(
            "Flex", rarity=CardRarity.COMMON, uuid="flex-source"
        )
        heavy_blade = make_card(
            "Heavy Blade", card_type=CardType.ATTACK, uuid="heavy-payoff"
        )
        sword_boomerang = make_card(
            "Sword Boomerang",
            card_type=CardType.ATTACK,
            rarity=CardRarity.COMMON,
            uuid="sword-payoff",
        )
        limit_break = make_card(
            "Limit Break",
            rarity=CardRarity.RARE,
            upgrades=1,
            uuid="owned-limit-break",
        )
        weak_defend = make_card(
            "Defend_R", rarity=CardRarity.BASIC, uuid="weak-defend"
        )
        deck = [
            flex,
            heavy_blade,
            sword_boomerang,
            limit_break,
            weak_defend,
        ]
        game = LiveDecisionGame(
            grid_screen(deck), deck=deck, act=2, floor=21
        )
        game.current_action = "Duplicator"
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(
            ["Heavy Blade"], [card.card_id for card in action.cards]
        )
        scores = {
            item["id"]: item["score"]
            for item in agent.last_noncombat_decision["candidates"]
        }
        self.assertLess(scores["Limit Break"], scores["Heavy Blade"])

    def test_library_read_binds_positive_grid_without_current_action_text(self):
        footwork = make_card("Footwork", card_type=CardType.POWER)
        weak_strike = strike()
        game = LiveDecisionGame(
            event_screen("The Library", ["Read", "Sleep"]),
            deck=[weak_strike, footwork],
            hp=65,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        event_action = agent.get_next_action_in_game(game)

        self.assertEqual(0, event_action.choice_index)
        self.assertEqual("positive_card", agent.pending_selection_context["kind"])

        game.set_screen(grid_screen([weak_strike, footwork]))
        game.current_action = ""
        grid_action = agent.get_next_action_in_game(game)

        self.assertEqual(["Footwork"], [card.card_id for card in grid_action.cards])
        self.assertEqual(
            "grid_positive_event_card_value",
            agent.last_noncombat_decision["reason"],
        )
        self.assertEqual(
            footwork.uuid, agent.pending_selection_context["card_uuid"]
        )
        agent.confirm_card_selection(game, footwork.uuid)
        self.assertIsNone(agent.pending_selection_context)

    def test_duplicator_entry_binds_positive_grid_without_current_action_text(self):
        footwork = make_card("Footwork", card_type=CardType.POWER)
        weak_strike = strike()
        game = LiveDecisionGame(
            event_screen("Duplicator", ["Pray", "Leave"]),
            deck=[weak_strike, footwork],
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        event_action = agent.get_next_action_in_game(game)

        self.assertEqual(0, event_action.choice_index)
        self.assertEqual("duplicator", agent.pending_selection_context["source"])

        game.set_screen(grid_screen([weak_strike, footwork]))
        game.current_action = ""
        grid_action = agent.get_next_action_in_game(game)

        self.assertEqual(["Footwork"], [card.card_id for card in grid_action.cards])
        self.assertEqual(
            footwork.uuid, agent.pending_selection_context["card_uuid"]
        )
        agent.confirm_card_selection(game, footwork.uuid)
        self.assertIsNone(agent.pending_selection_context)

    def test_positive_grid_stale_retry_keeps_the_same_card_uuid_until_confirmed(self):
        first_best = make_card(
            "Footwork", card_type=CardType.POWER, uuid="bound-footwork"
        )
        first_other = make_card("Strike_G", uuid="other-strike")
        game = LiveDecisionGame(
            grid_screen([first_other, first_best]),
            deck=[first_other, first_best],
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.pending_selection_context = {
            "kind": "positive_card",
            "source": "thelibrary",
            "act": game.act,
            "floor": game.floor,
        }

        with patch.object(
            agent,
            "_card_reward_score",
            side_effect=lambda card: 100 if card.uuid == "bound-footwork" else 0,
        ):
            first_action = agent.get_next_action_in_game(game)

        # A stale rejection reconstructs every Card object and the fresh
        # heuristic now prefers the distractor.  The unconfirmed transaction
        # must still retry the originally bound UUID.
        retry_best = make_card(
            "Footwork", card_type=CardType.POWER, uuid="bound-footwork"
        )
        retry_other = make_card("Strike_G", uuid="other-strike")
        game.set_screen(grid_screen([retry_other, retry_best]))
        with patch.object(
            agent,
            "_card_reward_score",
            side_effect=lambda card: 100 if card.uuid == "other-strike" else 0,
        ):
            retry_action = agent.get_next_action_in_game(game)

        self.assertEqual(["bound-footwork"], [card.uuid for card in first_action.cards])
        self.assertEqual(["bound-footwork"], [card.uuid for card in retry_action.cards])
        self.assertIsNotNone(agent.pending_selection_context)
        agent.confirm_card_selection(game, "bound-footwork")
        self.assertIsNone(agent.pending_selection_context)

    def test_masked_bandits_fights_when_gold_hp_and_deck_are_ready(self):
        predator = make_card(
            "Predator", card_type=CardType.ATTACK,
            rarity=CardRarity.UNCOMMON, upgrades=1,
        )
        predator.damage = predator.base_damage = 15
        dash = make_card(
            "Dash", card_type=CardType.ATTACK,
            rarity=CardRarity.UNCOMMON,
        )
        dash.damage = dash.base_damage = 10
        backflip = make_card("Backflip", rarity=CardRarity.COMMON)
        backflip.block = backflip.base_block = 5
        footwork = make_card("Footwork", card_type=CardType.POWER)
        game = LiveDecisionGame(
            event_screen("Masked Bandits", ["Pay", "Fight"]),
            deck=[predator, dash, backflip, footwork],
            gold=220,
            act=2,
            floor=24,
            hp=65,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        self.assertEqual(
            "masked_bandits_gold_vs_combat_readiness",
            agent.last_noncombat_decision["reason"],
        )

    def test_masked_bandits_pays_when_low_hp_even_with_large_gold(self):
        game = LiveDecisionGame(
            event_screen("Masked Bandits", ["Pay", "Fight"]),
            deck=[make_card("Predator", card_type=CardType.ATTACK)],
            gold=300,
            act=2,
            floor=24,
            hp=25,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)

    def test_masked_bandits_does_not_treat_basic_starter_deck_as_ready(self):
        game = LiveDecisionGame(
            event_screen("Masked Bandits", ["Pay", "Fight"]),
            deck=[
                *(strike(index) for index in range(5)),
                *(defend(index) for index in range(5)),
            ],
            gold=300,
            act=2,
            floor=24,
            hp=65,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)

    def test_colosseum_takes_second_fight_only_when_run_is_ready(self):
        predator = make_card(
            "Predator", card_type=CardType.ATTACK, upgrades=1
        )
        predator.damage = predator.base_damage = 20
        predator.cost = 2
        dash = make_card("Dash", card_type=CardType.ATTACK)
        dash.damage = dash.base_damage = 10
        dash.block = dash.base_block = 10
        dash.cost = 2
        backflip = make_card("Backflip")
        backflip.block = backflip.base_block = 5
        footwork = make_card("Footwork", card_type=CardType.POWER)
        compact_deck = [predator, dash, backflip, footwork]

        ready_screen = event_screen("Colosseum", ["Cower", "Fight"])
        ready_screen.options = list(reversed(ready_screen.options))
        ready_game = LiveDecisionGame(
            ready_screen,
            deck=compact_deck,
            act=2,
            floor=28,
            hp=70,
            max_hp=70,
        )
        ready_agent = SimpleAgent(PlayerClass.THE_SILENT)
        self.assertEqual(
            1, ready_agent.get_next_action_in_game(ready_game).choice_index
        )

        # Four named role cards alone do not prove elite readiness. This
        # compact deck still has limited mitigation and no damage scaling;
        # the shared conservative budget should distinguish its HP margin.
        marginal_game = LiveDecisionGame(
            event_screen("Colosseum", ["Cower", "Fight"]),
            deck=compact_deck,
            act=2,
            floor=28,
            hp=65,
            max_hp=70,
        )
        marginal_agent = SimpleAgent(PlayerClass.THE_SILENT)
        self.assertEqual(
            0, marginal_agent.get_next_action_in_game(marginal_game).choice_index
        )
        self.assertGreater(
            marginal_agent.last_noncombat_decision["death_risk"], 0
        )

        weak_game = LiveDecisionGame(
            event_screen("Colosseum", ["Cower", "Fight"]),
            deck=compact_deck,
            act=2,
            floor=28,
            hp=24,
            max_hp=70,
        )
        weak_agent = SimpleAgent(PlayerClass.THE_SILENT)
        self.assertEqual(
            0, weak_agent.get_next_action_in_game(weak_game).choice_index
        )

    def test_golden_idol_prices_the_forced_trap_without_a_current_hp_cliff(self):
        game = LiveDecisionGame(
            event_screen("Golden Idol", ["Take", "Leave"]),
            deck=[strike(), defend()],
            hp=10,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertTrue(
            decision["candidate_contract"]["strategy_quality_auditable"]
        )
        idol = next(
            row for row in decision["candidates"] if row["id"] == "0"
        )
        self.assertGreater(
            idol["consequences"]["forced_followup_min_cost"], 0.0
        )

    def test_secret_portal_is_rejected_while_any_heart_key_is_missing(self):
        game = LiveDecisionGame(
            event_screen("Secret Portal", ["Enter", "Leave"]),
            act=3,
            floor=45,
        )
        game.key_system_unlocked = True
        game.has_ruby_key = True
        game.has_sapphire_key = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_secret_portal_is_rejected_even_without_key_constraint(self):
        game = LiveDecisionGame(
            event_screen("Secret Portal", ["Enter", "Leave"]),
            act=3,
            floor=37,
            hp=70,
            max_hp=80,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_big_fish_does_not_waste_banana_at_full_hp(self):
        game = LiveDecisionGame(
            event_screen("Big Fish", ["Banana", "Donut", "Box"]),
            hp=70,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_low_hp_dead_adventurer_leaves_instead_of_searching(self):
        game = LiveDecisionGame(
            event_screen("Dead Adventurer", ["Search", "Leave"]),
            act=2,
            hp=34,
            max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_neow_scores_all_rewards_instead_of_taking_first_option(self):
        game = LiveDecisionGame(
            typed_neow_screen(
                [
                    "Choose a card",
                    "Gain 100 Gold",
                    "Lose 7 Max HP. Transform 2 cards",
                    "Lose your starting Relic. Obtain a random Boss Relic.",
                ],
                [
                    neow_reward_contract("THREE_CARDS"),
                    neow_reward_contract("HUNDRED_GOLD"),
                    neow_reward_contract(
                        "TRANSFORM_TWO_CARDS", "TEN_PERCENT_HP_LOSS"
                    ),
                    neow_reward_contract("BOSS_RELIC"),
                ],
            ),
            deck=[strike(), defend()],
            gold=99,
            floor=0,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        self.assertEqual("neow_option_context_score", agent.last_noncombat_decision["reason"])
        rows = agent.last_noncombat_decision["candidates"]
        self.assertEqual(4, len(rows))
        self.assertEqual(4, len({row["choice_id"] for row in rows}))
        self.assertEqual(
            [0, 1, 2, 3], [row["choice_index"] for row in rows]
        )
        self.assertTrue(all(row["consequences"] for row in rows))

    def test_neow_semantic_choice_ignores_option_container_order(self):
        labels = [
            "Choose a card",
            "Gain 100 Gold",
            "Lose 7 Max HP. Transform 2 cards",
            "Lose your starting Relic. Obtain a random Boss Relic.",
        ]
        contracts = [
            neow_reward_contract("THREE_CARDS"),
            neow_reward_contract("HUNDRED_GOLD"),
            neow_reward_contract(
                "TRANSFORM_TWO_CARDS", "TEN_PERCENT_HP_LOSS"
            ),
            neow_reward_contract("BOSS_RELIC"),
        ]
        first_screen = typed_neow_screen(labels, contracts)
        second_screen = typed_neow_screen(labels, contracts)
        second_screen.options.reverse()
        first_agent = SimpleAgent(PlayerClass.THE_SILENT)
        second_agent = SimpleAgent(PlayerClass.THE_SILENT)

        first = first_agent.get_next_action_in_game(LiveDecisionGame(
            first_screen, deck=[strike(), defend()], gold=99, floor=0
        ))
        second = second_agent.get_next_action_in_game(LiveDecisionGame(
            second_screen, deck=[strike(), defend()], gold=99, floor=0
        ))

        self.assertEqual(first.choice_index, second.choice_index)
        self.assertEqual(
            [row["choice_id"] for row in first_agent.last_noncombat_decision["candidates"]],
            [row["choice_id"] for row in second_agent.last_noncombat_decision["candidates"]],
        )

    def test_neow_localized_text_cannot_change_typed_choice(self):
        contracts = [
            neow_reward_contract("THREE_CARDS"),
            neow_reward_contract("HUNDRED_GOLD"),
        ]
        first = typed_neow_screen(["free gold", "terrible curse"], contracts)
        second = typed_neow_screen(["terrible curse", "free gold"], contracts)
        first_agent = SimpleAgent(PlayerClass.IRONCLAD)
        second_agent = SimpleAgent(PlayerClass.IRONCLAD)

        first_action = first_agent.get_next_action_in_game(
            LiveDecisionGame(first, deck=[strike(), defend()], floor=0)
        )
        second_action = second_agent.get_next_action_in_game(
            LiveDecisionGame(second, deck=[strike(), defend()], floor=0)
        )

        self.assertEqual(1, first_action.choice_index)
        self.assertEqual(1, second_action.choice_index)

    def test_neow_missing_typed_contract_is_explicitly_not_auditable(self):
        screen = event_screen("Neow Event", ["localized free gold"])
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(
            LiveDecisionGame(screen, deck=[strike(), defend()], floor=0)
        )

        self.assertIsNone(action)
        decision = agent.last_noncombat_decision
        self.assertTrue(decision["fail_closed"])
        self.assertFalse(
            decision["candidate_contract"]["strategy_quality_auditable"]
        )
        self.assertFalse(decision["candidates"][0]["selection_eligible"])
        self.assertEqual(
            "unclassified_neow_contract",
            decision["candidates"][0]["consequences"]["operation"],
        )

    def test_neow_dialog_advance_uses_typed_single_choice_contract(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_DIALOG_ADVANCE",
            "screen_num": 1,
            "resource_effect": "NONE",
        }
        screen = typed_neow_screen(["continue"], [contract])
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(
            LiveDecisionGame(screen, deck=[strike(), defend()], floor=0)
        )

        self.assertEqual(0, action.choice_index)
        decision = agent.last_noncombat_decision
        self.assertTrue(
            decision["candidate_contract"]["strategy_quality_auditable"]
        )
        consequence = decision["candidates"][0]["consequences"]
        self.assertEqual("neow_dialog_advance", consequence["operation"])
        self.assertEqual([], consequence["random_effects"])

    def test_red_mask_tomb_keeps_large_gold_stack(self):
        game = LiveDecisionGame(
            event_screen("Tomb of Lord Red Mask", ["Offer 236 Gold", "Leave"]),
            gold=236,
            act=3,
            floor=39,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_scored_event_semantics_ignore_visible_container_order(self):
        cases = [
            (
                "Tomb of Lord Red Mask",
                ["Take the Red Mask", "Leave"],
                [],
                0,
                70,
                70,
                0,
            ),
            (
                "Secret Portal",
                ["Enter", "Leave"],
                [],
                0,
                70,
                70,
                1,
            ),
            (
                "Purifier",
                ["Purify", "Leave"],
                [strike(), defend()],
                0,
                70,
                70,
                0,
            ),
            (
                "Duplicator",
                ["Pray", "Leave"],
                [make_card("Footwork", card_type=CardType.POWER)],
                0,
                70,
                70,
                0,
            ),
            (
                "The Library",
                ["Read", "Sleep"],
                [strike(), defend()],
                0,
                70,
                70,
                0,
            ),
        ]
        for event_id, labels, deck, gold, hp, max_hp, expected in cases:
            with self.subTest(event_id=event_id):
                screen = event_screen(event_id, labels)
                screen.options = list(reversed(screen.options))
                game = LiveDecisionGame(
                    screen,
                    deck=deck,
                    gold=gold,
                    hp=hp,
                    max_hp=max_hp,
                    act=3 if event_id == "Secret Portal" else 2,
                    floor=37 if event_id == "Secret Portal" else 20,
                )
                agent = SimpleAgent(PlayerClass.THE_SILENT)

                action = agent.get_next_action_in_game(game)

                self.assertEqual(expected, action.choice_index)

    def test_compacted_leave_option_is_not_rebound_as_shrine_operation(self):
        for event_id in ("Purifier", "Duplicator"):
            with self.subTest(event_id=event_id):
                screen = event_screen(event_id, ["Leave"])
                game = LiveDecisionGame(
                    screen,
                    deck=[strike(), defend()],
                    hp=70,
                    max_hp=70,
                )
                agent = SimpleAgent(PlayerClass.THE_SILENT)

                action = agent.get_next_action_in_game(game)

                self.assertEqual(0, action.choice_index)
                self.assertEqual(
                    "leave",
                    agent.last_noncombat_decision["chosen_kind"],
                )

    def test_woman_in_blue_buys_bundle_that_fits_empty_slots(self):
        game = LiveDecisionGame(
            event_screen(
                "The Woman in Blue",
                ["Buy 1 Potion", "Buy 2 Potions", "Buy 3 Potions", "Leave"],
            ),
            gold=57,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)

    def test_woman_in_blue_respects_ascension_eleven_two_slot_limit(self):
        game = LiveDecisionGame(
            event_screen(
                "The Woman in Blue",
                ["Buy 1 Potion", "Buy 2 Potions", "Buy 3 Potions", "Leave"],
            ),
            gold=57,
        )
        game.ascension_level = 11
        game.potions = [
            SimpleNamespace(potion_id="Potion Slot") for _ in range(2)
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        # Three rewards are still better than two slots: the third bottle is
        # a visible replacement/selection option, not an illegal purchase.
        self.assertEqual(2, action.choice_index)
        chosen = next(
            row
            for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "2"
        )
        self.assertEqual(1, chosen["consequences"]["replacement_choices"])

    def test_woman_in_blue_can_replace_weak_potions_on_a_full_belt(self):
        game = LiveDecisionGame(
            event_screen(
                "The Woman in Blue",
                ["Buy 1 Potion", "Buy 2 Potions", "Buy 3 Potions", "Leave"],
            ),
            gold=40,
        )
        owned = [
            Potion("SmokeBomb", f"Smoke Bomb {index}", True, True, False)
            for index in range(3)
        ]
        game.potions = owned
        game.get_real_potions = lambda: owned
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)
        self.assertGreater(
            agent.last_noncombat_decision["candidates"][2]["consequences"][
                "replacement_option_value"
            ],
            0.0,
        )

    def test_woman_in_blue_a15_prices_leave_hp_loss(self):
        game = LiveDecisionGame(
            event_screen(
                "The Woman in Blue",
                ["Buy 1 Potion", "Buy 2 Potions", "Buy 3 Potions", "Leave"],
            ),
            gold=20,
            hp=4,
            max_hp=70,
        )
        game.ascension_level = 15
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        leave = next(
            row
            for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "3"
        )
        self.assertEqual(-4, leave["consequences"]["hp_delta"])
        self.assertTrue(leave["consequences"]["lethal"])

    def test_ghosts_accepts_apparitions_even_when_current_hp_ratio_is_low(self):
        screen = event_screen("Ghosts", ["Accept", "Refuse"])
        screen.options[0].card = make_card(
            "Ghostly", rarity=CardRarity.SPECIAL,
        )
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            hp=35,
            max_hp=70,
            act=2,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(0, action.choice_index)
        chosen = agent.last_noncombat_decision["outcome_facts"]["0"]
        self.assertEqual("Ghostly", chosen["consequences"]["card_id"])
        self.assertEqual(
            {
                "kind": "card_package", "id": "Ghostly",
                "card_id": "Ghostly", "count": 5,
            },
            chosen["consequences"]["acquired_benefit"],
        )
        valuation = chosen["post_state"]["apparition_valuation"]
        self.assertEqual(5, len(valuation["marginal_card_values"]))
        self.assertGreater(
            valuation["marginal_card_values"][0],
            valuation["marginal_card_values"][-1],
        )
        selected = next(
            row
            for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "0"
        )
        self.assertEqual(
            "ghosts_contextual_apparition_value_v3",
            selected["score_rule_id"],
        )

    def test_ghosts_prices_ethereal_timing_bloat_and_sustain_capacity(self):
        screen = event_screen("Ghosts", ["Accept", "Refuse"])
        screen.options[0].card = make_card(
            "Ghostly", rarity=CardRarity.SPECIAL,
        )
        attacks = []
        for index in range(8):
            card = make_card(
                f"Attack {index}", card_type=CardType.ATTACK,
                rarity=CardRarity.COMMON, uuid=f"attack-{index}",
            )
            card.base_damage = 8
            card.damage = 8
            attacks.append(card)
        blocks = [
            make_card("Backflip", uuid=f"backflip-{index}")
            for index in range(5)
        ]
        for card in blocks:
            card.base_block = 5
            card.block = 5
        deck = (
            [strike(index) for index in range(3)]
            + [defend(index) for index in range(4)]
            + attacks
            + blocks
        )
        game = LiveDecisionGame(
            screen, deck=deck, hp=41, max_hp=70, act=2, floor=19,
        )
        game.act_boss = "Collector"
        game.relics = [Relic("MealTicket", "Meal Ticket")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        valuation = agent.last_noncombat_decision[
            "outcome_facts"
        ]["0"]["post_state"]["apparition_valuation"]
        self.assertEqual(1, action.choice_index)
        self.assertLess(valuation["playability_fraction"], 0.75)
        self.assertGreater(valuation["kill_clock_bloat_cost"], 0.0)
        self.assertIn("MealTicket", valuation["sustain_relic_ids"])
        self.assertGreater(valuation["max_hp_capacity_weight"], 0.52)

    def test_ghosts_declines_when_slow_deck_already_has_dense_block(self):
        screen = event_screen("Ghosts", ["Accept", "Refuse"])
        screen.options[0].card = make_card(
            "Ghostly", rarity=CardRarity.SPECIAL,
        )
        deck = []
        for index in range(28):
            card = make_card(
                "Ghostly Armor", uuid=f"ghostly-armor-{index}",
            )
            card.base_block = 10
            card.block = 10
            deck.append(card)
        game = LiveDecisionGame(
            screen,
            deck=deck,
            hp=80,
            max_hp=80,
            act=2,
        )
        game.act_boss = "Automaton"
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        accept = agent.last_noncombat_decision["outcome_facts"]["0"]
        valuation = accept["post_state"]["apparition_valuation"]
        self.assertEqual(1.0, valuation["block_coverage"])
        self.assertLess(
            valuation["expected_prevention_value"],
            valuation["max_hp_capacity_cost"]
            + valuation["immediate_hp_clamp_cost"]
            + valuation["first_cycle_delay_cost"],
        )

    def test_ghosts_toxic_egg_improves_apparition_playability(self):
        screen = event_screen("Ghosts", ["Accept", "Refuse"])
        screen.options[0].card = make_card(
            "Ghostly", rarity=CardRarity.SPECIAL,
        )
        game = LiveDecisionGame(
            screen,
            deck=[strike(index) for index in range(4)]
            + [defend(index) for index in range(4)],
            hp=35,
            max_hp=70,
            act=2,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game
        base = agent._apparition_event_valuation(5, 0, upgraded=False)
        upgraded = agent._apparition_event_valuation(5, 0, upgraded=True)

        self.assertGreater(
            upgraded["playability_fraction"], base["playability_fraction"]
        )
        self.assertGreater(upgraded["score"], base["score"])

    def test_ghosts_declines_recent_velvet_choker_package(self):
        def historical_deck():
            deck = (
                [strike(index) for index in range(4)]
                + [defend(index) for index in range(5)]
                + [
                    make_card("Survivor"),
                    make_card("Neutralize", card_type=CardType.ATTACK),
                    make_card(
                        "Backstab", card_type=CardType.ATTACK, upgrades=1
                    ),
                    make_card("Dagger Spray", card_type=CardType.ATTACK),
                    make_card("Dodge and Roll"),
                    make_card("Backflip"),
                    make_card("Well Laid Plans", card_type=CardType.POWER),
                    make_card("Adrenaline"),
                    make_card("Footwork", card_type=CardType.POWER),
                    make_card(
                        "Flying Knee", card_type=CardType.ATTACK, upgrades=1
                    ),
                    make_card("PiercingWail"),
                ]
            )
            card_values = {
                "Strike_G": ("damage", 6),
                "Defend_G": ("block", 5),
                "Survivor": ("block", 8),
                "Neutralize": ("damage", 3),
                "Backstab": ("damage", 15),
                "Dagger Spray": ("damage", 4),
                "Dodge and Roll": ("block", 4),
                "Backflip": ("block", 5),
                "Flying Knee": ("damage", 11),
            }
            for card in deck:
                field, value = card_values.get(card.card_id, (None, None))
                if field is not None:
                    setattr(card, field, value)
                    setattr(card, f"base_{field}", value)
            return deck

        screen = event_screen("Ghosts", ["Accept", "Refuse"])
        screen.options[0].card = make_card(
            "Ghostly", rarity=CardRarity.SPECIAL,
        )
        game = LiveDecisionGame(
            screen,
            deck=historical_deck(),
            hp=31,
            max_hp=82,
            act=2,
            floor=30,
        )
        game.act_boss = "Collector"
        game.max_energy = 4
        game.relics = [Relic("Velvet Choker", "Velvet Choker")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)
        valuation = agent.last_noncombat_decision[
            "outcome_facts"
        ]["0"]["post_state"]["apparition_valuation"]
        self.assertLess(valuation["choker_slot_fraction"], 0.75)
        self.assertLess(valuation["playability_fraction"], 0.50)
        accept = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["id"] == "0"
        )
        self.assertLess(accept["score"], 0.0)

    def test_ghosts_package_is_not_immediately_ranked_above_starters(self):
        ghostlies = [
            make_card(
                "Ghostly", rarity=CardRarity.SPECIAL,
                uuid=f"ghostly-{index}",
            )
            for index in range(5)
        ]
        starter = strike(index=9)
        cards = [*ghostlies, starter, defend(index=9)]
        game = LiveDecisionGame(
            grid_screen(cards, for_transform=True),
            deck=cards, act=2, floor=20, hp=35, max_hp=35,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertNotEqual("Ghostly", action.cards[0].card_id)
        rows = agent.last_noncombat_decision["candidates"]
        ghostly_row = next(row for row in rows if row["id"] == "Ghostly")
        selected_row = next(
            row for row in rows
            if row["id"] == action.cards[0].card_id
        )
        self.assertLess(ghostly_row["score"], selected_row["score"])
        removal = ghostly_row["consequences"]["score_components"][
            "removal_evaluation"
        ]
        self.assertIn(
            "intangible_retention_value",
            removal["score_parts"],
        )

    def test_winding_halls_avoids_madness_bloat_when_heal_is_not_needed(self):
        game = LiveDecisionGame(
            event_screen("Winding Halls", ["Embrace Madness", "Focus", "Retrace Steps"]),
            deck=[strike(), defend()],
            hp=70,
            max_hp=70,
            act=3,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)

    def test_cleric_with_disabled_remove_does_not_pay_to_heal_one_hp(self):
        screen = typed_cleric_screen(
            ["Heal", "Purify", "Leave"], max_hp=65
        )
        screen.options[1].disabled = True
        screen.options[1].choice_index = None
        screen.options[2].choice_index = 1
        game = LiveDecisionGame(
            screen,
            gold=100,
            hp=64,
            max_hp=65,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(1, action.choice_index)

    def test_nloth_keeps_valuable_relics_and_leaves(self):
        game = LiveDecisionGame(
            event_screen("N'loth", ["Offer Cursed Key", "Offer Ring of the Snake", "Leave"]),
            act=2,
        )
        game.relics = [
            Relic("Cursed Key", "Cursed Key"),
            Relic("Ring of the Snake", "Ring of the Snake"),
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)

    def test_falling_selects_category_with_weakest_sacrifice(self):
        weak_strike = strike()
        wraith = make_card("Wraith Form v2", card_type=CardType.POWER, rarity=CardRarity.RARE)
        backflip = make_card("Backflip", card_type=CardType.SKILL)
        screen = event_screen(
            "Falling", ["Lose Skill", "Lose Power", "Lose Attack"]
        )
        screen.options[0].card = backflip
        screen.options[1].card = wraith
        screen.options[2].card = weak_strike
        game = LiveDecisionGame(
            screen,
            deck=[weak_strike, wraith, backflip],
            act=3,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertEqual(2, action.choice_index)
        rows = agent.last_noncombat_decision["candidates"]
        self.assertTrue(all(
            row["score_rule_id"]
            == "falling_exact_offered_card_removal_value_v1"
            for row in rows
        ))
        selected = rows[2]
        self.assertEqual(
            weak_strike.card_id,
            selected["consequences"]["card_id"],
        )
        self.assertEqual(
            "falling_sacrifice_offered_card",
            selected["consequences"]["operation"],
        )

    def test_low_hp_map_rejects_branch_forced_through_elite_before_rest(self):
        current = Node(0, 0, "M")
        risky_entry = Node(0, 1, "?")
        safe_entry = Node(1, 1, "?")
        forced_elite = Node(0, 2, "E")
        safe_event = Node(1, 2, "?")
        rest = Node(0, 3, "R")

        current.children = [risky_entry, safe_entry]
        risky_entry.children = [forced_elite]
        safe_entry.children = [safe_event]
        forced_elite.children = [rest]
        safe_event.children = [rest]
        dungeon_map = make_map(current, risky_entry, safe_entry, forced_elite, safe_event, rest)
        game = LiveDecisionGame(
            MapScreen(current, [risky_entry, safe_entry], boss_available=False),
            dungeon_map=dungeon_map,
            hp=20,
            max_hp=70,
            act=2,
            floor=21,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        # Make the unsafe branch overwhelmingly attractive numerically.  A
        # correct low-HP route guard must remove it before score comparison.
        artificial_scores = {
            (0, 0): 0,
            (0, 1): 500,
            (1, 1): 0,
            (0, 2): 500,
            (1, 2): 0,
            (0, 3): 0,
        }
        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: artificial_scores[(node.x, node.y)],
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(safe_entry, action.node)

    def test_neow_lament_routes_to_reachable_one_hp_elite(self):
        lament_entry = Node(0, 0, "M")
        ordinary_entry = Node(1, 0, "M")
        lament_shop = Node(0, 1, "$")
        ordinary_combat = Node(1, 1, "M")
        lament_event = Node(0, 2, "?")
        ordinary_event = Node(1, 2, "?")
        lament_elite = Node(0, 3, "E")
        ordinary_rest = Node(1, 3, "R")

        lament_entry.children = [lament_shop]
        lament_shop.children = [lament_event]
        lament_event.children = [lament_elite]
        ordinary_entry.children = [ordinary_combat]
        ordinary_combat.children = [ordinary_event]
        ordinary_event.children = [ordinary_rest]

        dungeon_map = make_map(
            lament_entry,
            ordinary_entry,
            lament_shop,
            ordinary_combat,
            lament_event,
            ordinary_event,
            lament_elite,
            ordinary_rest,
        )
        game = LiveDecisionGame(
            MapScreen(
                None,
                [lament_entry, ordinary_entry],
                boss_available=False,
            ),
            dungeon_map=dungeon_map,
            hp=70,
            max_hp=70,
            act=1,
            floor=0,
        )
        game.relics = [Relic("NeowsBlessing", "Neow's Lament", counter=2)]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(lament_entry, action.node)
        self.assertEqual(
            2,
            agent.last_noncombat_decision["neow_lament_charges"],
        )
        chosen = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate.get("id") == "map:0,0"
        )
        paths = chosen["consequences"]["route_summary"][
            "legal_path_options"
        ]
        lament_elite_paths = [
            path for path in paths
            if path["neow_lament_one_hp_elite_depths"]
        ]
        self.assertEqual(1, len(lament_elite_paths))
        self.assertEqual(
            [3],
            lament_elite_paths[0]["neow_lament_one_hp_elite_depths"],
        )
        self.assertIn(
            lament_elite_paths[0]["selection_basis"],
            {"lowest_attrition", "earliest_neow_lament_elite"},
        )

    def test_neow_lament_preserves_third_combat_elite_beyond_summary_depth(self):
        lament_nodes = [
            Node(1, 0, "M"),
            Node(2, 1, "M"),
            Node(2, 2, "$"),
            Node(2, 3, "?"),
            Node(3, 4, "?"),
            Node(3, 5, "R"),
            Node(2, 6, "?"),
            Node(2, 7, "?"),
            Node(3, 8, "T"),
            Node(2, 9, "R"),
            Node(1, 10, "E"),
        ]
        for parent, child in zip(lament_nodes, lament_nodes[1:]):
            parent.children = [child]
        waste_nodes = [
            Node(4, 0, "M"),
            Node(5, 1, "M"),
            Node(5, 2, "M"),
            Node(4, 3, "M"),
        ]
        for parent, child in zip(waste_nodes, waste_nodes[1:]):
            parent.children = [child]
        game = LiveDecisionGame(
            MapScreen(
                None,
                [lament_nodes[0], waste_nodes[0]],
                boss_available=False,
            ),
            dungeon_map=make_map(*lament_nodes, *waste_nodes),
            hp=80,
            max_hp=80,
            act=1,
            floor=1,
        )
        game.relics = [Relic("NeowsBlessing", "Neow's Lament", counter=3)]
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None, **kwargs: (
                500.0 if node in waste_nodes else 0.0
            ),
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(lament_nodes[0], action.node)
        self.assertTrue(
            agent.last_noncombat_decision[
                "neow_lament_elite_route_forced"
            ]
        )
        chosen = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate.get("id") == "map:1,0"
        )
        elite_route = chosen["consequences"]["route_summary"][
            "earliest_neow_lament_elite"
        ]
        self.assertEqual(3, elite_route["elite_combat_number"])
        self.assertEqual(10, elite_route["rooms_before_elite"])
        wasted = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate.get("id") == "map:4,0"
        )
        self.assertFalse(wasted["selection_eligible"])
        self.assertIn(
            "neow_lament_one_hp_elite_route", wasted["veto_reason"]
        )

    def test_neow_lament_last_charge_prefers_immediate_elite_at_low_hp(self):
        current = Node(0, 5, "R")
        lament_elite = Node(0, 6, "E")
        waste_event = Node(1, 6, "?")
        wasted_normal = Node(1, 7, "M")
        current.children = [lament_elite, waste_event]
        waste_event.children = [wasted_normal]
        game = LiveDecisionGame(
            MapScreen(
                current,
                [lament_elite, waste_event],
                boss_available=False,
            ),
            dungeon_map=make_map(
                current, lament_elite, waste_event, wasted_normal
            ),
            hp=49,
            max_hp=80,
            act=1,
            floor=7,
        )
        game.relics = [
            Relic("NeowsBlessing", "Neow's Lament", counter=1)
        ]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None, **kwargs: (
                500.0 if node is waste_event else 0.0
            ),
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(lament_elite, action.node)
        self.assertTrue(
            agent.last_noncombat_decision[
                "neow_lament_elite_route_forced"
            ]
        )
        elite = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate.get("id") == "map:0,6"
        )
        self.assertTrue(elite["selection_eligible"])
        self.assertNotIn(
            "elite_immediate_survival_reserve", elite["veto_reason"] or []
        )
        wasted = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate.get("id") == "map:1,6"
        )
        self.assertFalse(wasted["selection_eligible"])
        self.assertIn(
            "neow_lament_one_hp_elite_route", wasted["veto_reason"]
        )

    def test_coffee_dripper_route_does_not_treat_campfire_as_full_recovery(self):
        current = Node(0, 0, "M")
        false_recovery_entry = Node(0, 1, "?")
        safer_entry = Node(1, 1, "?")
        unusable_rest = Node(0, 2, "R")
        safe_event = Node(1, 2, "?")
        forced_elite = Node(0, 3, "E")
        ordinary_combat = Node(1, 3, "M")

        current.children = [false_recovery_entry, safer_entry]
        false_recovery_entry.children = [unusable_rest]
        safer_entry.children = [safe_event]
        unusable_rest.children = [forced_elite]
        safe_event.children = [ordinary_combat]
        dungeon_map = make_map(
            current,
            false_recovery_entry,
            safer_entry,
            unusable_rest,
            safe_event,
            forced_elite,
            ordinary_combat,
        )
        game = LiveDecisionGame(
            MapScreen(
                current,
                [false_recovery_entry, safer_entry],
                boss_available=False,
            ),
            dungeon_map=dungeon_map,
            hp=30,
            max_hp=70,
            act=2,
            floor=21,
        )
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(safer_entry, action.node)

    def test_ordinary_rest_still_counts_forced_elite_after_it(self):
        rest = Node(0, 1, "R")
        forced_elite = Node(0, 2, "E")
        rest.children = [forced_elite]
        dungeon_map = make_map(rest, forced_elite)
        game = LiveDecisionGame(
            MapScreen(None, [rest], boss_available=False),
            dungeon_map=dungeon_map,
            hp=20,
            max_hp=70,
            act=2,
            floor=21,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertGreater(agent._path_survival_risk(rest), 0)

    def test_final_act_three_recall_is_not_counted_as_route_healing(self):
        recall = Node(0, 14, "R")
        combat = Node(0, 15, "M")
        recall.children = [combat]
        dungeon_map = make_map(recall, combat)
        game = LiveDecisionGame(
            MapScreen(None, [recall], boss_available=False),
            dungeon_map=dungeon_map,
            hp=20,
            max_hp=70,
            act=3,
            floor=48,
        )
        game.key_system_unlocked = True
        game.has_ruby_key = False
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        self.assertGreater(agent._path_survival_risk(recall), 0)

    def test_route_campfire_uses_same_smith_choice_as_open_screen(self):
        rest = Node(0, 5, "R")
        combat = Node(0, 6, "M")
        rest.children = [combat]
        dungeon_map = make_map(rest, combat)
        game = LiveDecisionGame(
            MapScreen(None, [rest], boss_available=False),
            deck=[make_card("Wraith Form")],
            dungeon_map=dungeon_map,
            hp=69,
            max_hp=70,
            act=2,
            floor=21,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        route_choice, _, _ = agent._route_campfire_choice(rest, 69)
        self.assertEqual(RestOption.SMITH, route_choice)
        self.assertFalse(agent._campfire_will_restore(rest, 69))
        self.assertGreater(agent._path_survival_risk(rest), 0)

        game.set_screen(
            RestScreen(False, [RestOption.REST, RestOption.SMITH])
        )
        action = agent.choose_rest_option()
        self.assertIsInstance(action, RestAction)
        self.assertEqual(route_choice, action.rest_option)

    def test_future_campfires_do_not_repeat_the_same_projected_upgrade(self):
        current = Node(0, 12, "?")
        smith_entry = Node(0, 13, "R")
        event_entry = Node(1, 13, "?")
        second_fire = Node(0, 14, "R")
        second_event = Node(1, 14, "?")
        current.children = [smith_entry, event_entry]
        smith_entry.children = [second_fire]
        event_entry.children = [second_event]
        premium_upgrade = make_card(
            "Wraith Form",
            card_type=CardType.POWER,
            uuid="premium-upgrade",
        )
        game = LiveDecisionGame(
            MapScreen(
                current, [smith_entry, event_entry], boss_available=False,
            ),
            deck=[premium_upgrade],
            dungeon_map=make_map(
                current,
                smith_entry,
                event_entry,
                second_fire,
                second_event,
            ),
            hp=70,
            max_hp=70,
            act=2,
            floor=29,
        )
        game.act_boss = "The Collector"
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        with patch.object(
            agent, "_upgrade_score", return_value=50.0
        ), patch.object(
            agent, "_path_survival_risk", return_value=0.0
        ), patch.object(
            agent, "_estimated_map_room_cost", return_value=0.0
        ):
            first_choice, _, first_details = agent._route_campfire_choice(
                smith_entry, 70
            )
            projection_id = first_details["best_upgrade_projection_id"]
            second_choice, _, second_details = agent._route_campfire_choice(
                second_fire,
                70,
                projected_upgrade_ids=(projection_id,),
            )
            action = agent.make_map_choice()

        self.assertEqual(RestOption.SMITH, first_choice)
        self.assertIsNotNone(projection_id)
        self.assertNotEqual(RestOption.SMITH, second_choice)
        self.assertIsNone(second_details["best_upgrade_projection_id"])
        self.assertIs(event_entry, action.node)

    def test_map_terminal_uses_projected_boss_entry_readiness(self):
        current = Node(0, 13, "?")
        attrition_entry = Node(0, 14, "?")
        safe_entry = Node(1, 14, "?")
        current.children = [attrition_entry, safe_entry]
        game = LiveDecisionGame(
            MapScreen(
                current, [attrition_entry, safe_entry], boss_available=False,
            ),
            dungeon_map=make_map(current, attrition_entry, safe_entry),
            hp=70,
            max_hp=70,
            act=2,
            floor=30,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        readiness_hp = []

        def projected_readiness(profile=None, projected_hp=None):
            hp = game.current_hp if projected_hp is None else projected_hp
            readiness_hp.append(float(hp))
            return {
                "score": 0.0,
                "progress_score": float(hp) / game.max_hp,
            }

        def room_cost(node, lament_combat=False):
            return 35.0 if node is attrition_entry else 0.0

        with patch.object(
            agent,
            "_boss_entry_readiness",
            side_effect=projected_readiness,
        ), patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: 0.0,
        ), patch.object(
            agent, "_path_survival_risk", return_value=0.0
        ), patch.object(
            agent, "_estimated_map_room_cost", side_effect=room_cost
        ):
            action = agent.make_map_choice()

        self.assertIs(safe_entry, action.node)
        self.assertIn(35.0, readiness_hp)
        self.assertIn(70.0, readiness_hp)
        self.assertEqual(
            120.0,
            agent.last_noncombat_decision["boss_terminal_score_weight"],
        )

    def test_boss_progress_score_survives_a_missing_scaling_bottleneck(self):
        game = LiveDecisionGame(
            MapScreen(None, [], boss_available=False),
            hp=70,
            max_hp=70,
            act=2,
            floor=30,
        )
        game.act_boss = "The Collector"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        missing_scaling = {
            "coverage": {
                "frontload": 1.0,
                "block": 1.0,
                "scaling": 0.0,
                "draw": 1.0,
            },
            "consistency": 1.0,
            "kill_clock": {"score": 1.0},
        }

        with patch.object(
            agent, "_deck_readiness", return_value=missing_scaling
        ):
            readiness = agent._boss_entry_readiness({"roles": {}})

        self.assertEqual(0.0, readiness["score"])
        self.assertGreater(readiness["progress_score"], 0.0)
        self.assertEqual(0.0, readiness["bottleneck_ratio"])
        self.assertFalse(readiness["ready"])

    def test_boss_entry_readiness_can_score_projected_route_hp(self):
        game = LiveDecisionGame(
            MapScreen(None, [], boss_available=False),
            hp=70,
            max_hp=70,
            act=2,
            floor=30,
        )
        game.act_boss = "The Collector"
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        complete_readiness = {
            "coverage": {
                "frontload": 1.0,
                "block": 1.0,
                "scaling": 1.0,
                "draw": 1.0,
            },
            "consistency": 1.0,
            "kill_clock": {"score": 1.0},
        }

        with patch.object(
            agent, "_deck_readiness", return_value=complete_readiness
        ):
            injured = agent._boss_entry_readiness(
                {"roles": {}}, projected_hp=14
            )
            healthy = agent._boss_entry_readiness(
                {"roles": {}}, projected_hp=70
            )

        self.assertEqual(14, injured["survival"]["hp"])
        self.assertEqual(70, healthy["survival"]["hp"])
        self.assertGreater(healthy["score"], injured["score"])
        self.assertEqual(70, game.current_hp)

    def test_mark_of_the_bloom_route_never_gets_fake_healing(self):
        rest = Node(0, 5, "R")
        combat = Node(0, 6, "M")
        rest.children = [combat]
        dungeon_map = make_map(rest, combat)
        game = LiveDecisionGame(
            MapScreen(None, [rest], boss_available=False),
            deck=[make_card("Wraith Form", upgrades=1)],
            dungeon_map=dungeon_map,
            hp=20,
            max_hp=70,
            act=2,
            floor=21,
        )
        game.relics = [Relic("Mark of the Bloom", "Mark of the Bloom")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        route_choice, _, details = agent._route_campfire_choice(rest, 20)

        self.assertEqual(RestOption.REST, route_choice)
        self.assertTrue(details["healing_blocked"])
        self.assertEqual(0, details["actual_recovery"])
        self.assertFalse(agent._campfire_will_restore(rest, 20))
        self.assertEqual(0, agent.score_map_node(rest))
        self.assertGreater(agent._path_survival_risk(rest), 0)

    def test_healthy_act1_deck_with_only_slow_or_skill_damage_avoids_optional_elite(self):
        current = Node(0, 6, "R")
        elite = Node(0, 7, "E")
        normal = Node(1, 7, "M")
        current.children = [elite, normal]
        dungeon_map = make_map(current, elite, normal)
        deck = [
            strike(), strike(1), strike(2), strike(3), strike(4),
            defend(), defend(1), defend(2), defend(3), defend(4),
            make_card("Deadly Poison"),
            make_card("Blade Dance"),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [elite, normal], boss_available=False),
            deck=deck,
            dungeon_map=dungeon_map,
            hp=59,
            max_hp=70,
            act=1,
            floor=8,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIs(normal, action.node)

    def test_near_ready_act1_deck_takes_safe_first_elite_before_rest(self):
        current = Node(0, 5, "?")
        elite = Node(0, 6, "E")
        normal = Node(1, 6, "M")
        rest = Node(0, 7, "R")
        current.children = [elite, normal]
        elite.children = [rest]
        normal.children = [rest]
        deck = [
            strike(), strike(1), strike(2), strike(3),
            defend(), defend(1), defend(2), defend(3),
            make_card("Cold Snap", card_type=CardType.ATTACK),
            make_card("Defragment", card_type=CardType.POWER, upgrades=1),
            make_card("Glacier", upgrades=1),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [elite, normal], boss_available=False),
            deck=deck,
            dungeon_map=make_map(current, elite, normal, rest),
            hp=67,
            max_hp=75,
            act=1,
            floor=6,
        )
        game.get_real_potions = lambda: [
            Potion("BlockPotion", "Block Potion", True, True, False),
            Potion("WeakPotion", "Weak Potion", True, True, True),
        ]
        agent = SimpleAgent(PlayerClass.DEFECT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(elite, action.node)
        self.assertEqual(0.5, agent._act1_elite_readiness())
        self.assertEqual(
            80.0,
            agent.last_noncombat_decision[
                "first_act1_elite_development_bonus"
            ],
        )
        candidates = {
            row["consequences"]["route"]["symbol"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertTrue(candidates["E"]["selection_eligible"])
        self.assertGreater(candidates["E"]["score"], candidates["M"]["score"])

    def test_act1_first_elite_bonus_stops_at_old_ready_endpoint(self):
        current = Node(0, 5, "?")
        elite = Node(0, 6, "E")
        normal = Node(1, 6, "M")
        current.children = [elite, normal]
        deck = [
            strike(), strike(1), strike(2), strike(3), strike(4),
            defend(), defend(1), defend(2), defend(3), defend(4),
            make_card("Glass Knife", card_type=CardType.ATTACK),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [elite, normal], boss_available=False),
            deck=deck,
            dungeon_map=make_map(current, elite, normal),
            hp=69,
            max_hp=70,
            act=1,
            floor=6,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        with patch.object(agent, "_path_survival_risk", return_value=0.0):
            agent.get_next_action_in_game(game)

        candidates = {
            row["consequences"]["route"]["symbol"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(1.0, agent._act1_elite_readiness())
        self.assertEqual(85.0, candidates["E"]["raw_local_score"])

    def test_act1_first_elite_bonus_requires_conservatively_safe_path(self):
        current = Node(0, 4, "?")
        monster = Node(0, 5, "M")
        elite = Node(0, 6, "E")
        safe_event = Node(1, 5, "?")
        rest = Node(1, 6, "R")
        current.children = [monster, safe_event]
        monster.children = [elite]
        safe_event.children = [rest]
        deck = [
            strike(), strike(1), strike(2), strike(3),
            defend(), defend(1), defend(2), defend(3),
            make_card("Cold Snap", card_type=CardType.ATTACK),
            make_card("Defragment", card_type=CardType.POWER, upgrades=1),
            make_card("Glacier", upgrades=1),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [monster, safe_event], boss_available=False),
            deck=deck,
            dungeon_map=make_map(
                current, monster, elite, safe_event, rest,
            ),
            hp=67,
            max_hp=75,
            act=1,
            floor=5,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)

        def conservative_risk(node, *args, **kwargs):
            return float(game.current_hp) if node is monster else 0.0

        with patch.object(
            agent, "_path_survival_risk", side_effect=conservative_risk,
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIs(safe_event, action.node)
        candidates = {
            row["consequences"]["route"]["symbol"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertLess(candidates["M"]["raw_local_score"], 35.0)

    def test_safe_sibling_does_not_authorize_unsafe_elite_bonus(self):
        current = Node(0, 4, "?")
        fork = Node(0, 5, "?")
        alternate = Node(1, 5, "?")
        elite = Node(0, 6, "E")
        safe_sibling = Node(1, 6, "?")
        current.children = [fork, alternate]
        fork.children = [elite, safe_sibling]
        game = LiveDecisionGame(
            MapScreen(
                current, [fork, alternate], boss_available=False
            ),
            dungeon_map=make_map(
                current, fork, alternate, elite, safe_sibling
            ),
            hp=70,
            max_hp=100,
            act=1,
            floor=5,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game

        def conservative_cost(node, lament_combat=False):
            return 60.0 if node is elite else 0.0

        with patch.object(
            agent, "_act1_elite_readiness", return_value=1.0
        ), patch.object(
            agent, "_estimated_map_room_cost", return_value=0.0
        ), patch.object(
            agent,
            "_estimated_survival_room_cost",
            side_effect=conservative_cost,
        ), patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: (
                50.0 if node is alternate else 0.0
            ),
        ):
            self.assertEqual(0.0, agent._path_survival_risk(fork))
            action = agent.get_next_action_in_game(game)

        self.assertIs(alternate, action.node)
        candidates = {
            (
                row["consequences"]["route"]["x"],
                row["consequences"]["route"]["y"],
            ): row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertLess(
            candidates[(fork.x, fork.y)]["raw_local_score"],
            candidates[(alternate.x, alternate.y)]["raw_local_score"],
        )

    def test_act1_first_elite_bonus_expires_after_floor_eleven(self):
        current = Node(0, 13, "?")
        elite = Node(0, 14, "E")
        safe_event = Node(1, 14, "?")
        current.children = [elite, safe_event]
        deck = [
            strike(), strike(1), strike(2), strike(3),
            defend(), defend(1), defend(2), defend(3),
            make_card("Cold Snap", card_type=CardType.ATTACK),
            make_card("Defragment", card_type=CardType.POWER, upgrades=1),
            make_card("Glacier", upgrades=1),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [elite, safe_event], boss_available=False),
            deck=deck,
            dungeon_map=make_map(current, elite, safe_event),
            hp=67,
            max_hp=75,
            act=1,
            floor=14,
        )
        game.get_real_potions = lambda: [
            Potion("BlockPotion", "Block Potion", True, True, False),
            Potion("WeakPotion", "Weak Potion", True, True, True),
        ]
        agent = SimpleAgent(PlayerClass.DEFECT)

        with patch.object(
            agent, "_path_survival_risk", return_value=0.0
        ), patch.object(
            agent,
            "_boss_entry_readiness",
            return_value={"score": 0.0, "progress_score": 0.0},
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIs(safe_event, action.node)
        candidates = {
            row["consequences"]["route"]["symbol"]: row
            for row in agent.last_noncombat_decision["candidates"]
        }
        self.assertEqual(-15.0, candidates["E"]["raw_local_score"])

    def test_low_hp_act1_route_vetoes_elite_despite_development_bonus(self):
        current = Node(0, 5, "?")
        elite = Node(0, 6, "E")
        elite_monster_one = Node(0, 7, "M")
        elite_monster_two = Node(0, 8, "M")
        safe_event = Node(1, 6, "?")
        rest = Node(1, 7, "R")
        current.children = [elite, safe_event]
        elite.children = [elite_monster_one]
        elite_monster_one.children = [elite_monster_two]
        safe_event.children = [rest]
        deck = [
            strike(), strike(1), strike(2), strike(3), strike(4),
            defend(), defend(1), defend(2), defend(3), defend(4),
            make_card("Glass Knife", card_type=CardType.ATTACK),
        ]
        game = LiveDecisionGame(
            MapScreen(current, [elite, safe_event], boss_available=False),
            deck=deck,
            dungeon_map=make_map(
                current,
                elite,
                elite_monster_one,
                elite_monster_two,
                safe_event,
                rest,
            ),
            hp=26,
            max_hp=70,
            act=1,
            floor=6,
        )
        agent = SimpleAgent(PlayerClass.THE_SILENT)

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(safe_event, action.node)
        elite_row = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["consequences"]["route"]["symbol"] == "E"
        )
        self.assertFalse(elite_row["selection_eligible"])
        self.assertTrue(
            {
                "elite_immediate_survival_reserve",
                "low_hp_voluntary_elite_veto",
            }
            & set((elite_row["veto_reason"] or "").split(";"))
        )

    def test_whirlwind_opens_act_one_elite_only_above_hp_gate(self):
        def scores(hp):
            current = Node(0, 5, "M")
            elite = Node(0, 6, "E")
            normal = Node(1, 6, "M")
            current.children = [elite, normal]
            whirlwind = make_card(
                "Whirlwind", card_type=CardType.ATTACK
            )
            whirlwind.cost = -1
            game = LiveDecisionGame(
                MapScreen(current, [elite, normal], boss_available=False),
                deck=[
                    strike(), strike(1), strike(2), strike(3), strike(4),
                    defend(), defend(1), defend(2), defend(3), defend(4),
                    whirlwind,
                ],
                dungeon_map=make_map(current, elite, normal),
                hp=hp,
                max_hp=80,
                act=1,
                floor=6,
            )
            agent = SimpleAgent(PlayerClass.IRONCLAD)
            agent.game = game
            return (
                agent,
                agent.score_map_node(elite),
                agent.score_map_node(normal),
            )

        healthy_agent, healthy_elite, healthy_normal = scores(70)
        injured_agent, injured_elite, injured_normal = scores(49)

        self.assertTrue(healthy_agent._act1_elite_ready())
        self.assertEqual(2.0, healthy_agent._early_offense_value())
        self.assertGreater(healthy_elite, healthy_normal)
        self.assertTrue(injured_agent._act1_elite_ready())
        self.assertEqual(2.0, injured_agent._early_offense_value())
        self.assertLess(injured_elite, injured_normal)

    def test_coffee_dripper_low_hp_campfire_is_not_scored_as_healing(self):
        current = Node(0, 0, "M")
        rest = Node(0, 1, "R")
        shop = Node(1, 1, "$")
        current.children = [rest, shop]
        dungeon_map = make_map(current, rest, shop)
        game = LiveDecisionGame(
            MapScreen(current, [rest, shop], boss_available=False),
            dungeon_map=dungeon_map,
            hp=20,
            max_hp=70,
            gold=180,
            act=2,
            floor=21,
        )
        game.relics = [Relic("Coffee Dripper", "Coffee Dripper")]
        agent = SimpleAgent(PlayerClass.THE_SILENT)
        agent.game = game

        self.assertLess(agent.score_map_node(rest), agent.score_map_node(shop))

    def test_high_gold_lookahead_converts_purse_at_first_reachable_shop(self):
        def choose(gold):
            current = Node(0, 0, "M")
            no_shop_entry = Node(0, 1, "M")
            shop_entry = Node(1, 1, "M")
            reward_event = Node(0, 2, "?")
            shop = Node(1, 2, "$")
            current.children = [no_shop_entry, shop_entry]
            no_shop_entry.children = [reward_event]
            shop_entry.children = [shop]
            game = LiveDecisionGame(
                MapScreen(
                    current, [no_shop_entry, shop_entry],
                    boss_available=False,
                ),
                dungeon_map=make_map(
                    current, no_shop_entry, shop_entry, reward_event, shop
                ),
                gold=gold,
                hp=75,
                max_hp=75,
                act=2,
                floor=18,
            )
            agent = SimpleAgent(PlayerClass.IRONCLAD)
            agent.game = game
            room_values = {
                (0, 1): 5.0,
                (1, 1): 0.0,
                (0, 2): 70.0,
                (1, 2): 20.0,
            }
            with patch.object(
                agent, "score_map_node",
                side_effect=lambda node, projected_hp=None: room_values[
                    (node.x, node.y)
                ],
            ), patch.object(
                agent, "_path_survival_risk", return_value=0.0
            ), patch.object(
                agent, "_estimated_map_room_cost", return_value=0.0
            ):
                action = agent.make_map_choice()
            return action.node, agent.last_noncombat_decision

        low_gold_node, low_gold_audit = choose(100)
        neow_gold_node, neow_gold_audit = choose(378)
        high_gold_node, high_gold_audit = choose(534)

        self.assertEqual((0, 1), (low_gold_node.x, low_gold_node.y))
        self.assertEqual((1, 1), (neow_gold_node.x, neow_gold_node.y))
        self.assertEqual((1, 1), (high_gold_node.x, high_gold_node.y))
        self.assertEqual(0.0, low_gold_audit["high_gold_shop_route_bonus"])
        self.assertEqual(120.0, neow_gold_audit["high_gold_shop_route_bonus"])
        self.assertEqual(120.0, high_gold_audit["high_gold_shop_route_bonus"])

    def test_high_gold_safe_shop_is_admission_priority_not_only_bonus(self):
        current = Node(0, 0, "M")
        no_shop_entry = Node(0, 1, "?")
        shop_entry = Node(1, 1, "?")
        oversized_reward = Node(0, 2, "?")
        shop = Node(1, 2, "$")
        current.children = [no_shop_entry, shop_entry]
        no_shop_entry.children = [oversized_reward]
        shop_entry.children = [shop]
        game = LiveDecisionGame(
            MapScreen(
                current, [no_shop_entry, shop_entry], boss_available=False,
            ),
            dungeon_map=make_map(
                current, no_shop_entry, shop_entry, oversized_reward, shop,
            ),
            gold=408,
            hp=75,
            max_hp=75,
            act=2,
            floor=18,
        )
        agent = SimpleAgent(PlayerClass.DEFECT)
        agent.game = game
        room_values = {
            (0, 1): 0.0,
            (1, 1): 0.0,
            (0, 2): 400.0,
            (1, 2): 0.0,
        }

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: room_values[
                (node.x, node.y)
            ],
        ), patch.object(
            agent, "_path_survival_risk", return_value=0.0
        ), patch.object(
            agent, "_estimated_map_room_cost", return_value=0.0
        ), patch.object(
            agent, "_estimated_survival_room_cost", return_value=0.0
        ):
            action = agent.make_map_choice()

        self.assertIs(shop_entry, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["high_gold_safe_shop_admission"]
        )
        no_shop_audit = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["label"] == "?@0,1"
        )
        self.assertFalse(no_shop_audit["selection_eligible"])
        self.assertIn(
            "high_gold_safe_shop_conversion",
            no_shop_audit["veto_reason"],
        )

    def test_high_gold_does_not_pull_route_to_unreachable_shop(self):
        current = Node(0, 0, "M")
        no_shop_entry = Node(0, 1, "M")
        shop_entry = Node(1, 1, "M")
        safe_event = Node(0, 2, "?")
        shop = Node(1, 2, "$")
        current.children = [no_shop_entry, shop_entry]
        no_shop_entry.children = [safe_event]
        shop_entry.children = [shop]
        game = LiveDecisionGame(
            MapScreen(
                current, [no_shop_entry, shop_entry], boss_available=False,
            ),
            dungeon_map=make_map(
                current, no_shop_entry, shop_entry, safe_event, shop,
            ),
            gold=534,
            hp=30,
            max_hp=75,
            act=2,
            floor=18,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)
        agent.game = game
        room_values = {
            (0, 1): 5.0,
            (1, 1): 0.0,
            (0, 2): 70.0,
            (1, 2): 20.0,
        }

        def room_cost(node, lament_combat=False):
            return 10.0 if node.symbol == "M" else 0.0

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: room_values[
                (node.x, node.y)
            ],
        ), patch.object(
            agent, "_path_survival_risk", return_value=0.0
        ), patch.object(
            agent, "_estimated_map_room_cost", side_effect=room_cost
        ):
            action = agent.make_map_choice()

        self.assertIs(no_shop_entry, action.node)
        candidates = agent.last_noncombat_decision["candidates"]
        scores = {candidate["label"]: candidate["score"] for candidate in candidates}
        self.assertLess(scores["M@1,1"], scores["M@0,1"])
        self.assertGreater(
            agent.last_noncombat_decision["shop_arrival_survival_floor"],
            20.0,
        )

    def test_healthy_act_two_missing_emerald_preserves_burning_elite_subtree(self):
        current = Node(0, 0, "M")
        key_entry = Node(0, 1, "?")
        no_key_entry = Node(1, 1, "?")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        ordinary_event = Node(1, 2, "?")
        rest = Node(0, 3, "R")

        current.children = [key_entry, no_key_entry]
        key_entry.children = [burning_elite]
        no_key_entry.children = [ordinary_event]
        burning_elite.children = [rest]
        ordinary_event.children = [rest]
        dungeon_map = make_map(current, key_entry, no_key_entry, burning_elite, ordinary_event, rest)
        ready_deck = [
            make_card(f"prepared-card-{index}", uuid=f"prepared-{index}")
            for index in range(10)
        ]
        game = LiveDecisionGame(
            MapScreen(current, [key_entry, no_key_entry], boss_available=False),
            deck=ready_deck,
            dungeon_map=dungeon_map,
            hp=75,
            max_hp=75,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")

        with patch.object(
            agent, "_path_survival_risk", return_value=20.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(key_entry, action.node)

    def test_developing_act_one_preserves_reachable_floor_thirteen_emerald(self):
        current = Node(0, 8, "?")
        key_chest = Node(0, 9, "T")
        no_key_chest = Node(1, 9, "T")
        key_event_one = Node(0, 10, "?")
        no_key_event_one = Node(1, 10, "?")
        key_event_two = Node(0, 11, "?")
        no_key_event_two = Node(1, 11, "?")
        burning_elite = Node(0, 12, "E", has_emerald_key=True)
        ordinary_event = Node(1, 12, "?")
        rest = Node(0, 13, "R")

        current.children = [key_chest, no_key_chest]
        key_chest.children = [key_event_one]
        no_key_chest.children = [no_key_event_one]
        key_event_one.children = [key_event_two]
        no_key_event_one.children = [no_key_event_two]
        key_event_two.children = [burning_elite]
        no_key_event_two.children = [ordinary_event]
        burning_elite.children = [rest]
        ordinary_event.children = [rest]
        dungeon_map = make_map(
            current,
            key_chest,
            no_key_chest,
            key_event_one,
            no_key_event_one,
            key_event_two,
            no_key_event_two,
            burning_elite,
            ordinary_event,
            rest,
        )
        game = LiveDecisionGame(
            MapScreen(
                current,
                [key_chest, no_key_chest],
                boss_available=False,
            ),
            deck=[
                make_card("Clothesline", card_type=CardType.ATTACK),
                make_card("Uppercut", card_type=CardType.ATTACK),
            ],
            dungeon_map=dungeon_map,
            hp=71,
            max_hp=80,
            act=1,
            floor=9,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        action = agent.get_next_action_in_game(game)

        self.assertFalse(agent._act1_elite_ready())
        self.assertEqual(2.5, agent._early_offense_value())
        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(key_chest, action.node)

    def test_a0_act_two_uses_smaller_reserve_for_viable_emerald_route(self):
        current = Node(0, 0, "M")
        key_entry = Node(0, 1, "?")
        no_key_rest = Node(1, 1, "R")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        rest = Node(0, 3, "R")
        current.children = [key_entry, no_key_rest]
        key_entry.children = [burning_elite]
        burning_elite.children = [rest]
        no_key_rest.children = [rest]
        game = LiveDecisionGame(
            MapScreen(
                current, [key_entry, no_key_rest], boss_available=False
            ),
            dungeon_map=make_map(
                current, key_entry, no_key_rest, burning_elite, rest
            ),
            hp=60,
            max_hp=80,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_path_survival_risk", return_value=54.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIs(key_entry, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_route_safe"]
        )
        self.assertEqual(
            6.0,
            agent.last_noncombat_decision["emerald_route_reserve"],
        )

    def test_a0_act_two_rejects_emerald_route_just_below_reserve(self):
        current = Node(0, 0, "M")
        key_entry = Node(0, 1, "?")
        safe_shop = Node(1, 1, "$")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        rest = Node(0, 3, "R")
        current.children = [key_entry, safe_shop]
        key_entry.children = [burning_elite]
        burning_elite.children = [rest]
        safe_shop.children = [rest]
        ready_deck = [
            make_card(f"prepared-{index}")
            for index in range(10)
        ]
        game = LiveDecisionGame(
            MapScreen(
                current, [key_entry, safe_shop], boss_available=False
            ),
            deck=ready_deck,
            gold=200,
            dungeon_map=make_map(
                current, key_entry, safe_shop, burning_elite, rest
            ),
            hp=60,
            max_hp=80,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        with patch.object(
            agent, "_path_survival_risk", return_value=55.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIs(safe_shop, action.node)
        self.assertFalse(
            agent.last_noncombat_decision["emerald_route_safe"]
        )
        self.assertTrue(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )
        self.assertEqual(
            6.0,
            agent.last_noncombat_decision["emerald_route_reserve"],
        )

    def test_emerald_fallback_keeps_mixed_safe_shop_entrance_eligible(self):
        current = Node(0, 0, "M")
        mixed_entry = Node(0, 1, "?")
        no_shop_entry = Node(1, 1, "?")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        safe_shop = Node(1, 2, "$")
        reward_event = Node(2, 2, "?")
        rest = Node(1, 3, "R")
        current.children = [mixed_entry, no_shop_entry]
        mixed_entry.children = [burning_elite, safe_shop]
        no_shop_entry.children = [reward_event]
        burning_elite.children = [rest]
        safe_shop.children = [rest]
        reward_event.children = [rest]
        game = LiveDecisionGame(
            MapScreen(
                current, [mixed_entry, no_shop_entry], boss_available=False,
            ),
            dungeon_map=make_map(
                current,
                mixed_entry,
                no_shop_entry,
                burning_elite,
                safe_shop,
                reward_event,
                rest,
            ),
            gold=369,
            hp=60,
            max_hp=80,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        game.ascension_level = 0
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        def route_risk(node, emerald_pending=None, **_kwargs):
            return 55.0 if emerald_pending else 0.0

        with patch.object(
            agent, "_path_survival_risk", side_effect=route_risk
        ), patch.object(
            agent, "_estimated_map_room_cost", return_value=0.0
        ), patch.object(
            agent, "_estimated_survival_room_cost", return_value=0.0
        ):
            action = agent.make_map_choice()

        self.assertIs(mixed_entry, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )
        self.assertTrue(
            agent.last_noncombat_decision["high_gold_safe_shop_admission"]
        )
        mixed_audit = next(
            candidate
            for candidate in agent.last_noncombat_decision["candidates"]
            if candidate["label"] == "?@0,1"
        )
        self.assertTrue(mixed_audit["selection_eligible"])
        self.assertEqual(
            60.0,
            mixed_audit["consequences"]["first_shop_arrival_hp"],
        )

    def test_three_hundred_gold_admits_safe_shop_bridge_to_emerald(self):
        def choose(gold):
            current = Node(0, 0, "M")
            safe_key_entry = Node(0, 1, "?")
            shop_key_entry = Node(1, 1, "?")
            safe_event = Node(0, 2, "?")
            shop = Node(1, 2, "$")
            burning_elite = Node(0, 3, "E", has_emerald_key=True)
            rest = Node(0, 4, "R")
            current.children = [safe_key_entry, shop_key_entry]
            safe_key_entry.children = [safe_event]
            shop_key_entry.children = [shop]
            safe_event.children = [burning_elite]
            shop.children = [burning_elite]
            burning_elite.children = [rest]
            game = LiveDecisionGame(
                MapScreen(
                    current,
                    [safe_key_entry, shop_key_entry],
                    boss_available=False,
                ),
                dungeon_map=make_map(
                    current,
                    safe_key_entry,
                    shop_key_entry,
                    safe_event,
                    shop,
                    burning_elite,
                    rest,
                ),
                deck=[make_card(f"ready-{index}") for index in range(10)],
                gold=gold,
                hp=60,
                max_hp=80,
                act=2,
                floor=18,
            )
            game.key_system_unlocked = True
            game.has_emerald_key = False
            game.ascension_level = 0
            agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
            agent.game = game

            def route_risk(node, emerald_pending=None, **_kwargs):
                if emerald_pending and node is shop_key_entry:
                    return 55.0
                return 50.0 if emerald_pending else 0.0

            with patch.object(
                agent, "_path_survival_risk", side_effect=route_risk
            ), patch.object(
                agent, "_estimated_map_room_cost", return_value=0.0
            ), patch.object(
                agent, "_estimated_survival_room_cost", return_value=0.0
            ), patch.object(
                agent, "score_map_node", return_value=0.0
            ):
                action = agent.make_map_choice()
            return action.node, agent.last_noncombat_decision

        below_node, below_audit = choose(299)
        threshold_node, threshold_audit = choose(300)

        self.assertEqual((0, 1), (below_node.x, below_node.y))
        self.assertFalse(below_audit["emerald_shop_bridge_admission"])
        self.assertEqual((1, 1), (threshold_node.x, threshold_node.y))
        self.assertTrue(threshold_audit["emerald_shop_bridge_admission"])
        self.assertTrue(threshold_audit["high_gold_safe_shop_admission"])

    def test_act_three_still_requires_emerald_route_at_critical_hp(self):
        current = Node(0, 4, "M")
        rest = Node(0, 5, "R")
        key_monster = Node(1, 5, "M")
        burning_elite = Node(1, 6, "E", has_emerald_key=True)
        current.children = [rest, key_monster]
        key_monster.children = [burning_elite]
        game = LiveDecisionGame(
            MapScreen(
                current, [rest, key_monster], boss_available=False
            ),
            dungeon_map=make_map(
                current, rest, key_monster, burning_elite
            ),
            hp=19,
            max_hp=80,
            gold=424,
            act=3,
            floor=40,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")

        action = agent.get_next_action_in_game(game)

        self.assertIs(key_monster, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_required"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_route_safe"]
        )
        self.assertTrue(
            agent.last_noncombat_decision["emerald_deadline_forced"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )

    def test_unreachable_emerald_key_reenables_critical_hp_survival_guard(self):
        current = Node(0, 10, "M")
        rest = Node(0, 11, "R")
        monster = Node(1, 11, "M")
        current.children = [rest, monster]
        dungeon_map = make_map(current, rest, monster)
        game = LiveDecisionGame(
            MapScreen(current, [rest, monster], boss_available=False),
            dungeon_map=dungeon_map,
            hp=25,
            max_hp=75,
            act=3,
            floor=44,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: (
                500 if node.symbol == "M" else 0
            ),
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(rest, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_route_reachable"]
        )

    def test_unsafe_act_two_emerald_route_uses_survival_fallback(self):
        current = Node(0, 0, "M")
        key_entry = Node(0, 1, "?")
        safe_entry = Node(1, 1, "R")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        rest = Node(0, 3, "R")
        current.children = [key_entry, safe_entry]
        key_entry.children = [burning_elite]
        burning_elite.children = [rest]
        safe_entry.children = [rest]
        dungeon_map = make_map(
            current, key_entry, safe_entry, burning_elite, rest
        )
        game = LiveDecisionGame(
            MapScreen(
                current, [key_entry, safe_entry], boss_available=False
            ),
            dungeon_map=dungeon_map,
            hp=18,
            max_hp=75,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")

        action = agent.get_next_action_in_game(game)

        self.assertIs(safe_entry, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )
        self.assertTrue(
            agent.last_noncombat_decision["emerald_route_reachable"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_route_safe"]
        )

    def test_unsafe_act_two_only_emerald_route_stays_key_constrained(self):
        """Do not abandon the sole legal route to the mandatory key.

        A low-HP Act 2 route may fail the reserve check.  If there is a
        non-key alternative, the survival fallback covered above is correct;
        when the key subtree is the only entrance, clearing ``emerald_pending``
        would let the recursive route scorer silently walk away from the
        Heart objective on the next map row.
        """

        current = Node(0, 0, "M")
        key_entry = Node(0, 1, "?")
        burning_elite = Node(0, 2, "E", has_emerald_key=True)
        rest = Node(0, 3, "R")
        current.children = [key_entry]
        key_entry.children = [burning_elite]
        burning_elite.children = [rest]
        game = LiveDecisionGame(
            MapScreen(current, [key_entry], boss_available=False),
            dungeon_map=make_map(
                current, key_entry, burning_elite, rest
            ),
            hp=18,
            max_hp=75,
            act=2,
            floor=18,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")

        with patch.object(
            agent, "_path_survival_risk", return_value=20.0
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(key_entry, action.node)
        self.assertTrue(
            agent.last_noncombat_decision["emerald_route_reachable"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_route_safe"]
        )
        self.assertFalse(
            agent.last_noncombat_decision["emerald_survival_fallback"]
        )

    def test_peak_elite_loss_gate_precedes_future_reward_score(self):
        current = Node(0, 5, "M")
        elite = Node(0, 6, "E")
        rest = Node(1, 6, "R")
        current.children = [elite, rest]
        dungeon_map = make_map(current, elite, rest)
        game = LiveDecisionGame(
            MapScreen(current, [elite, rest], boss_available=False),
            dungeon_map=dungeon_map,
            hp=64,
            max_hp=80,
            act=2,
            floor=22,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD)

        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: (
                500 if node.symbol == "E" else 0
            ),
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(rest, action.node)

    def test_emerald_recursion_ignores_safe_branches_that_abandon_key(self):
        current = Node(0, 0, "M")
        risky_key_entry = Node(0, 1, "?")
        safer_key_entry = Node(1, 1, "?")

        risky_key_child = Node(0, 2, "M")
        risky_no_key_decoy = Node(2, 2, "$")
        safer_key_child = Node(1, 2, "?")
        safer_no_key_decoy = Node(3, 2, "M")
        burning_elite = Node(0, 3, "E", has_emerald_key=True)
        post_key_rest = Node(0, 4, "R")
        post_key_shop = Node(1, 4, "$")

        current.children = [risky_key_entry, safer_key_entry]
        risky_key_entry.children = [
            risky_key_child,
            risky_no_key_decoy,
        ]
        safer_key_entry.children = [
            safer_key_child,
            safer_no_key_decoy,
        ]
        risky_key_child.children = [burning_elite]
        safer_key_child.children = [burning_elite]
        burning_elite.children = [post_key_rest, post_key_shop]
        dungeon_map = make_map(
            current,
            risky_key_entry,
            safer_key_entry,
            risky_key_child,
            risky_no_key_decoy,
            safer_key_child,
            safer_no_key_decoy,
            burning_elite,
            post_key_rest,
            post_key_shop,
        )
        game = LiveDecisionGame(
            MapScreen(
                current,
                [risky_key_entry, safer_key_entry],
                boss_available=False,
            ),
            deck=[make_card("Prepared Defect card")],
            dungeon_map=dungeon_map,
            hp=80,
            max_hp=100,
            act=3,
            floor=35,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        risky_key_risk = agent._path_survival_risk(risky_key_entry)
        safer_key_risk = agent._path_survival_risk(safer_key_entry)
        post_key_children, pending_after_elite = (
            agent._emerald_constrained_children(
                burning_elite, emerald_pending=True
            )
        )

        self.assertGreater(risky_key_risk, safer_key_risk)
        self.assertFalse(pending_after_elite)
        self.assertEqual(
            [(0, 4), (1, 4)],
            [(node.x, node.y) for node in post_key_children],
        )
        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: 0,
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(safer_key_entry, action.node)

    def test_emerald_pending_does_not_filter_survivable_immediate_elite(self):
        current = Node(0, 0, "M")
        immediate_burning = Node(
            0,
            1,
            "E",
            has_emerald_key=True,
        )
        detour_event = Node(1, 1, "?")
        later_burning = Node(
            1,
            2,
            "E",
            has_emerald_key=True,
        )
        immediate_rest = Node(0, 2, "R")
        later_rest = Node(1, 3, "R")

        current.children = [immediate_burning, detour_event]
        immediate_burning.children = [immediate_rest]
        detour_event.children = [later_burning]
        later_burning.children = [later_rest]
        dungeon_map = make_map(
            current,
            immediate_burning,
            detour_event,
            later_burning,
            immediate_rest,
            later_rest,
        )
        game = LiveDecisionGame(
            MapScreen(
                current,
                [immediate_burning, detour_event],
                boss_available=False,
            ),
            deck=[make_card("Prepared Defect card")],
            dungeon_map=dungeon_map,
            hp=47,
            max_hp=70,
            act=3,
            floor=35,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        self.assertLess(
            agent._path_survival_risk(immediate_burning),
            agent._path_survival_risk(detour_event),
        )

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(immediate_burning, action.node)

    def test_future_burning_elite_uses_hp_after_cumulative_path_risk(self):
        current = Node(0, -1, "M")
        key_path = [
            Node(
                0,
                y,
                "E" if y == 7 else "M" if y < 2 else "?",
                has_emerald_key=(y == 7),
            )
            for y in range(8)
        ]
        safe_path = [Node(1, y, "?") for y in range(8)]
        current.children = [key_path[0], safe_path[0]]
        for path in (key_path, safe_path):
            for parent, child in zip(path, path[1:]):
                parent.children = [child]
        dungeon_map = make_map(current, *key_path, *safe_path)
        game = LiveDecisionGame(
            MapScreen(
                current,
                [key_path[0], safe_path[0]],
                boss_available=False,
            ),
            deck=[
                make_card(
                    "Glass Knife",
                    card_type=CardType.ATTACK,
                )
            ],
            dungeon_map=dungeon_map,
            hp=70,
            max_hp=70,
            act=1,
            floor=0,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        agent = SimpleAgent(
            PlayerClass.THE_SILENT,
            goal_mode="HEART",
        )

        action = agent.get_next_action_in_game(game)

        self.assertGreater(
            agent.score_map_node(key_path[-1], projected_hp=70),
            200,
        )
        self.assertLess(
            agent.score_map_node(key_path[-1], projected_hp=43),
            100,
        )
        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(safe_path[0], action.node)

class PickingMacroAdvisor:
    def __init__(self, selector):
        self.selector = selector
        self.config = replace(
            MacroPolicyConfig(), mode="assist", cache_warmup=False
        )
        self.requests = []

    def advise(self, **request):
        self.requests.append(request)
        selected = self.selector(request["candidates"])
        remaining = [
            item for item in request["candidates"]
            if item["candidate_id"] != selected
        ]
        return AdvisorResult(
            status="recommended",
            model_choice_id=selected,
            confidence=0.99,
            rankings=[
                {"candidate_id": selected, "score": 100.0},
                *[
                    {"candidate_id": item["candidate_id"], "score": 50.0 - index}
                    for index, item in enumerate(remaining)
                ],
            ],
        )


class DeepSeekMacroRoutingTests(unittest.TestCase):
    @staticmethod
    def by_label(label):
        return lambda candidates: next(
            item["candidate_id"]
            for item in candidates
            if item.get("label") == label
        )

    def test_permanent_card_reward_can_rerank_exact_card_instance(self):
        first = make_card("First", uuid="first-instance")
        second = make_card("Second", uuid="second-instance")
        game = LiveDecisionGame(
            CardRewardScreen([first, second], can_bowl=False, can_skip=True),
            deck=[strike(), defend()],
            floor=8,
        )
        adviser = PickingMacroAdvisor(self.by_label("Second"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        with patch.object(
            agent,
            "_card_reward_score",
            side_effect=lambda card: 11 if card is first else 10,
        ):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, CardRewardAction)
        self.assertIs(second, action.card)
        self.assertEqual("applied", agent.last_noncombat_decision["model_advice"]["status"])
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "CARD_REWARD"
        )
        self.assertNotIn(
            "ACT1_FRONTLOAD_GATE_ALREADY_APPLIED",
            request["hard_constraints"],
        )

    def test_run_plan_refreshes_when_supported_primary_emerges_in_same_act(self):
        adviser = PickingMacroAdvisor(
            lambda candidates: candidates[0]["candidate_id"]
        )
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        agent.game = LiveDecisionGame(
            CardRewardScreen([], can_bowl=False, can_skip=True),
            deck=[strike(), defend()],
            act=1,
            floor=1,
        )
        primary = {"value": "balanced"}

        def plan_summary(_profile=None):
            archetypes = (
                {"frost": {
                    "sources": 1,
                    "source_reliability": 1.0,
                    "payoffs": 0,
                    "relic_support": 0.0,
                    "signal": 3.0,
                }}
                if primary["value"] == "frost" else {}
            )
            return {
                "primary": primary["value"],
                "confidence": 0.5 if archetypes else 0.0,
                "needs": ["block"],
                "demand": {},
                "roles": {},
                "archetypes": archetypes,
            }

        with patch.object(agent, "_deck_plan_summary", side_effect=plan_summary):
            agent._refresh_model_run_plan()
            primary["value"] = "frost"
            agent._refresh_model_run_plan()

        requests = [
            item for item in adviser.requests
            if item["decision_type"] == "RUN_PLAN"
        ]
        self.assertEqual(2, len(requests))

    def test_run_plan_rejects_unpaired_archetype_but_accepts_paired_support(self):
        def plan_summary(payoffs):
            return {
                "primary": "balanced",
                "confidence": 0.4,
                "needs": ["block"],
                "need_horizons": {},
                "demand": {},
                "roles": {},
                "commitment": {"phase": "explore"},
                "boss_readiness": {},
                "archetypes": {"frost": {
                    "sources": 1,
                    "source_reliability": 1.0,
                    "payoffs": payoffs,
                    "relic_support": 0.0,
                    "signal": 4.0,
                }},
            }

        for payoffs, expected in ((0, None), (1, "frost")):
            with self.subTest(payoffs=payoffs):
                adviser = PickingMacroAdvisor(self.by_label("frost"))
                agent = SimpleAgent(
                    PlayerClass.DEFECT,
                    goal_mode="HEART",
                    macro_advisor=adviser,
                )
                agent.game = LiveDecisionGame(
                    CardRewardScreen([], can_bowl=False, can_skip=True),
                    deck=[strike(), defend()],
                    act=1,
                    floor=5,
                )
                with patch.object(
                    agent,
                    "_deck_plan_summary",
                    return_value=plan_summary(payoffs),
                ):
                    agent._refresh_model_run_plan()

                actual = (
                    (agent.model_run_plan or {}).get("primary")
                    if agent.model_run_plan else None
                )
                self.assertEqual(expected, actual)
                self.assertEqual(int(payoffs > 0), agent._model_run_plan_updates)

    def test_run_plan_expires_across_act_even_after_update_budget(self):
        adviser = PickingMacroAdvisor(lambda rows: rows[0]["candidate_id"])
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        agent.game = LiveDecisionGame(
            CardRewardScreen([], can_bowl=False, can_skip=True),
            deck=[strike(), defend()],
            act=2,
            floor=18,
        )
        agent.game.act_boss = "Collector"
        agent._model_run_plan_updates = 4
        agent.model_run_plan = {
            "primary": "frost",
            "act": 1,
            "boss": "Hexaghost",
            "needs": ["scaling"],
        }

        agent._refresh_model_run_plan()

        self.assertIsNone(agent.model_run_plan)
        self.assertIsNone(agent._macro_model_run_plan())
        self.assertEqual([], adviser.requests)

    def test_run_plan_refreshes_derived_needs_at_update_budget(self):
        adviser = PickingMacroAdvisor(lambda rows: rows[0]["candidate_id"])
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        agent.game = LiveDecisionGame(
            CardRewardScreen([], can_bowl=False, can_skip=True),
            deck=[strike(), defend()],
            act=2,
            floor=18,
        )
        agent.game.act_boss = "Collector"
        agent._model_run_plan_updates = 4
        agent.model_run_plan = {
            "primary": "frost",
            "act": 2,
            "boss": "Collector",
            "needs": ["old_need"],
        }
        current = agent._deck_plan_summary()
        current["needs"] = ["block", "draw"]

        with patch.object(agent, "_deck_plan_summary", return_value=current):
            agent._refresh_model_run_plan()

        self.assertEqual(["block", "draw"], agent.model_run_plan["needs"])
        self.assertEqual([], adviser.requests)

    def test_boss_relic_can_rerank_but_runic_dome_is_not_sent(self):
        first = Relic("FirstRelic", "First Relic")
        second = Relic("SecondRelic", "Second Relic")
        dome = Relic("Runic Dome", "Runic Dome")
        game = LiveDecisionGame(
            BossRewardScreen([first, second, dome]),
            deck=[strike(), defend()],
            hp=20,
            max_hp=80,
        )
        game.ascension_level = 20
        adviser = PickingMacroAdvisor(self.by_label("Second Relic"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        def score(relic):
            return {"FirstRelic": 11, "SecondRelic": 10, "Runic Dome": -1000}[relic.relic_id]

        with patch.object(agent, "_boss_relic_score", side_effect=score):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, BossRewardAction)
        self.assertIs(second, action.relic)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "BOSS_RELIC"
        )
        sent_ids = {
            item["relic_id"] for item in request["candidates"]
        }
        self.assertNotIn("Runic Dome", sent_ids)
        transition = request["state"]["decision_context"][
            "boss_reward_transition"
        ]
        self.assertEqual(65, transition["projected_next_act_hp"])
        self.assertEqual(45, transition["automatic_heal_amount"])
        self.assertFalse(transition["healing_blocked"])

    def test_busted_crown_remote_override_requires_mature_deck(self):
        tiny = Relic("Tiny House", "Tiny House")
        crown = Relic("Busted Crown", "Busted Crown")
        game = LiveDecisionGame(
            BossRewardScreen([tiny, crown]),
            deck=[
                *(strike() for _ in range(4)),
                *(defend() for _ in range(4)),
                make_card("Pommel Strike"),
                make_card("Shrug It Off"),
            ],
            act=1,
            hp=30,
            max_hp=80,
        )
        adviser = PickingMacroAdvisor(self.by_label("Busted Crown"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        with patch.object(
            agent,
            "_boss_relic_score",
            side_effect=lambda relic: 11 if relic is tiny else 10,
        ):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BossRewardAction)
        self.assertIs(tiny, action.relic)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "BOSS_RELIC"
        )
        self.assertEqual(
            ["Tiny House"],
            [item["relic_id"] for item in request["candidates"]],
        )
        profile = agent._busted_crown_model_override_profile()
        self.assertFalse(profile["override_eligible"])
        self.assertLess(profile["deck_size"], profile["required"]["deck_size"])

    def test_boss_relic_noop_return_is_not_a_model_candidate(self):
        tiny = Relic("Tiny House", "Tiny House")
        game = LiveDecisionGame(
            BossRewardScreen([tiny]),
            deck=[strike(), defend()],
            act=1,
        )
        game.cancel_available = True
        adviser = PickingMacroAdvisor(lambda _rows: "action:return")
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        with patch.object(agent, "_boss_relic_score", return_value=-1.0):
            action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, BossRewardAction)
        self.assertIs(tiny, action.relic)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "BOSS_RELIC"
        )
        self.assertEqual(
            ["relic:Tiny House:0"],
            [item["candidate_id"] for item in request["candidates"]],
        )
        noop = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if row["choice_id"] == "action:return"
        )
        self.assertFalse(noop["selection_eligible"])
        self.assertEqual("boss_relic_return_is_noop", noop["veto_reason"])

    def test_boss_relic_prompt_exposes_marginal_energy_and_real_wound_cost(self):
        coffee = Relic("Coffee Dripper", "Coffee Dripper")
        mark = Relic("Mark of Pain", "Mark of Pain")
        tiny = Relic("Tiny House", "Tiny House")
        game = LiveDecisionGame(
            BossRewardScreen([coffee, mark, tiny]),
            deck=[
                make_card("Power Through"),
                make_card("Immolate", card_type=CardType.ATTACK),
                strike(), defend(),
            ],
            act=2,
            hp=12,
            max_hp=80,
        )
        game.relics = [Relic("Ectoplasm", "Ectoplasm")]
        adviser = PickingMacroAdvisor(lambda rows: rows[0]["candidate_id"])
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "BOSS_RELIC"
        )
        by_id = {
            item["relic_id"]: item["facts"]
            for item in request["candidates"]
        }
        self.assertEqual(
            4,
            by_id["Mark of Pain"]["energy_marginal"][
                "current_permanent_energy"
            ],
        )
        mark_facts = by_id["Mark of Pain"]["mark_of_pain_consequence"]
        self.assertEqual(2, mark_facts["wounds_added_each_combat"])
        self.assertEqual([], mark_facts["observed_status_payoffs"])
        self.assertEqual(0, by_id["Tiny House"]["tiny_house_post_state"]["effective_gold_gain"])
        self.assertTrue(
            by_id["Coffee Dripper"]["coffee_dripper_consequence"][
                "rest_disabled"
            ]
        )

    def test_shop_rerank_keeps_exact_purchase_object(self):
        first = make_card("ShopA", uuid="shop-a")
        second = make_card("ShopB", uuid="shop-b")
        first.price = second.price = 50
        game = LiveDecisionGame(
            ShopScreen([first, second], [], [], False, 0),
            deck=[strike(), defend()],
            gold=100,
            floor=6,
        )
        adviser = PickingMacroAdvisor(self.by_label("ShopB"))
        agent = SimpleAgent(
            PlayerClass.THE_SILENT, goal_mode="HEART", macro_advisor=adviser
        )
        with patch.object(
            agent,
            "_card_reward_score",
            side_effect=lambda card: 20 if card is first else 19,
        ):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, BuyCardAction)
        self.assertIs(second, action.card)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "SHOP"
        )
        expected_ids = {
            "shop:card:shop-a:0",
            "shop:card:shop-b:1",
            "action:return",
        }
        self.assertEqual(
            expected_ids,
            {item["candidate_id"] for item in request["candidates"]},
        )
        advice = agent.last_noncombat_decision["model_advice"]
        self.assertEqual(
            expected_ids,
            {item["candidate_id"] for item in advice["rankings"]},
        )
        selected = next(
            item for item in request["candidates"]
            if item.get("label") == "ShopB"
        )
        self.assertEqual(50, selected["facts"]["price"])
        self.assertEqual(50, selected["facts"]["post_purchase_gold"])
        self.assertEqual(
            "buy_card_then_rerank_shop",
            selected["facts"]["operation"],
        )
        self.assertIn("deck_context", selected["facts"])

    def test_full_belt_shop_exposes_all_non_dominated_replacements(self):
        weak = Potion("WeakOwned", "Weak Owned", True, True, False)
        medium = Potion("MediumOwned", "Medium Owned", True, True, False)
        premium = Potion(
            "PremiumNew", "Premium New", True, True, False, price=90
        )
        efficient = Potion(
            "EfficientNew", "Efficient New", True, True, False, price=45
        )
        game = LiveDecisionGame(
            ShopScreen([], [], [premium, efficient], False, 75),
            deck=[strike(), defend()],
            gold=200,
        )
        game.potion_available = True
        game.potions = [weak, medium]
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: [weak, medium]
        for index, potion in enumerate((premium, efficient)):
            potion.protocol_choice_index = index
            potion.protocol_option_id = f"option:shop-potion:{index}"
        adviser = PickingMacroAdvisor(lambda rows: rows[0]["candidate_id"])
        agent = SimpleAgent(
            PlayerClass.THE_SILENT,
            goal_mode="HEART",
            macro_advisor=adviser,
        )
        keep_values = {
            "WeakOwned": 10.0,
            "MediumOwned": 20.0,
            "PremiumNew": 40.0,
            "EfficientNew": 35.0,
        }

        with patch.object(
            agent,
            "_potion_keep_score",
            side_effect=lambda potion: keep_values[potion.potion_id],
        ), patch.object(agent, "_shop_potion_value", return_value=20.0):
            agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "SHOP"
        )
        swaps = [
            item for item in request["candidates"]
            if item["facts"].get("operation")
            == "replace_potion_then_buy_exact_listing"
        ]
        self.assertEqual(2, len(swaps))
        pairs = {
            (
                item["facts"]["old_potion"]["id"],
                item["facts"]["new_potion"]["id"],
            )
            for item in swaps
        }
        self.assertEqual({
            ("WeakOwned", "PremiumNew"),
            ("WeakOwned", "EfficientNew"),
        }, pairs)

    def test_shop_purge_sends_exact_cost_and_projected_removal(self):
        game = LiveDecisionGame(
            ShopScreen([], [], [], True, 75),
            deck=[
                *[strike(index) for index in range(5)],
                *[defend(index) for index in range(5)],
            ],
            gold=100,
            floor=6,
        )
        game.screen.protocol_purge_choice_index = 0
        adviser = PickingMacroAdvisor(
            lambda candidates: next(
                item["candidate_id"]
                for item in candidates
                if item.get("facts", {}).get("operation")
                == "purge_one_card_then_rerank_shop"
            )
        )
        agent = SimpleAgent(
            PlayerClass.THE_SILENT, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        self.assertEqual("purge", action.name)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "SHOP"
        )
        purge = next(
            item for item in request["candidates"]
            if item.get("facts", {}).get("operation")
            == "purge_one_card_then_rerank_shop"
        )
        self.assertEqual(75, purge["facts"]["price"])
        self.assertEqual(25, purge["facts"]["post_purchase_gold"])
        self.assertEqual(
            "Strike_G",
            purge["facts"]["projected_target"]["card"]["card_id"],
        )

    def test_generic_event_only_reranks_visible_option_ids(self):
        game = LiveDecisionGame(
            event_screen("Unknown Mod Event", ["Leave", "Gain a potion"]),
            deck=[strike(), defend()],
        )
        adviser = PickingMacroAdvisor(self.by_label("Leave"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        action = agent.get_next_action_in_game(game)
        self.assertEqual(0, action.choice_index)
        self.assertEqual(
            {"event:0", "event:1"},
            {
                item["candidate_id"]
                for item in next(
                    request for request in adviser.requests
                    if request["decision_type"] == "EVENT"
                )["candidates"]
            },
        )

    def test_we_meet_again_sends_same_signed_exact_costs_to_model(self):
        compile_driver = make_card(
            "Compile Driver", card_type=CardType.ATTACK
        )
        screen = EventScreen("WeMeetAgain", "We Meet Again", "")
        screen.options = [
            EventOption(
                "Lose Focus Potion. Gain a relic.",
                "Give potion",
                disabled=False,
                choice_index=0,
            ),
            EventOption(
                "Lose 84 gold. Gain a relic.",
                "Give gold",
                disabled=False,
                choice_index=1,
            ),
            EventOption(
                "Lose Compile Driver. Gain a relic.",
                "Give card",
                disabled=False,
                choice_index=2,
            ),
        ]
        game = LiveDecisionGame(
            screen,
            deck=[compile_driver, strike(), defend()],
            gold=200,
        )
        focus = Potion(
            "FocusPotion", "Focus Potion", True, True, False
        )
        game.potions = [focus]
        game.get_real_potions = lambda: [focus]

        def selector(candidates):
            return next(
                (
                    item["candidate_id"]
                    for item in candidates
                    if item.get("label") == "Give gold"
                ),
                candidates[0]["candidate_id"],
            )

        adviser = PickingMacroAdvisor(selector)
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, ChooseAction)
        # This test verifies the information contract, not whether a merely
        # advisory model is allowed to override the local regret guard.  A
        # safe local choice may remain authoritative even when the model
        # ranks another visible option first.
        self.assertIn(action.choice_index, {0, 1, 2})
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "EVENT"
        )
        by_label = {
            item["label"]: item["facts"]["consequences"]
            for item in request["candidates"]
        }
        self.assertEqual(-84, by_label["Give gold"]["gold_delta"])
        self.assertEqual(1, by_label["Give gold"]["relic_delta"])
        self.assertEqual(-1, by_label["Give card"]["card_delta"])
        self.assertEqual(
            "Compile Driver",
            by_label["Give card"]["lost_card"]["card"]["card_id"],
        )
        self.assertEqual(-1, by_label["Give potion"]["potion_delta"])
        self.assertEqual(
            "FocusPotion", by_label["Give potion"]["lost_potion"]["id"]
        )

    def test_mind_bloom_sends_awake_and_healthy_as_distinct_post_states(self):
        screen = EventScreen("MindBloom", "Mind Bloom", "")
        screen.options = [
            EventOption(
                "Fight an Act 1 boss and gain a rare relic.",
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
                "Heal to full. Become cursed - Doubt.",
                "I am Healthy",
                False,
                2,
            ),
        ]
        game = LiveDecisionGame(
            screen,
            deck=[strike(), defend()],
            act=3,
            floor=42,
            hp=30,
            max_hp=80,
        )

        def selector(candidates):
            return next(
                (
                    item["candidate_id"]
                    for item in candidates
                    if item.get("label") == "I am Healthy"
                ),
                candidates[0]["candidate_id"],
            )

        adviser = PickingMacroAdvisor(selector)
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "EVENT"
        )
        by_outcome = {
            item["facts"]["outcome_id"]: item["facts"]
            for item in request["candidates"]
        }
        self.assertTrue(
            by_outcome["awake"]["consequences"]["healing_locked"]
        )
        self.assertGreaterEqual(
            by_outcome["awake"]["post_state"]["eligible_upgrade_count"],
            1,
        )
        self.assertEqual(
            50, by_outcome["healthy"]["consequences"]["hp_delta"]
        )
        self.assertEqual(
            1, by_outcome["healthy"]["consequences"]["curse_delta"]
        )
        self.assertNotIn(
            "healing_locked", by_outcome["healthy"]["consequences"]
        )

    def test_campfire_rerank_cannot_bypass_mandatory_recall(self):
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.RECALL, RestOption.SMITH]),
            deck=[strike(), defend()],
            act=3,
            floor=49,
        )
        game.has_ruby_key = False
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )
        action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.RECALL, action.rest_option)
        self.assertEqual([], adviser.requests)

    def test_campfire_rerank_cannot_bypass_scheduled_early_recall(self):
        game = LiveDecisionGame(
            RestScreen(
                False,
                [RestOption.RECALL, RestOption.SMITH, RestOption.REST],
            ),
            deck=[strike(), defend()],
            act=2,
            floor=23,
            hp=71,
            max_hp=75,
        )
        game.has_ruby_key = False
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.RECALL, action.rest_option)
        self.assertEqual([], adviser.requests)

    def test_act_one_low_cost_recall_is_exposed_to_campfire_model(self):
        future_fire = Node(0, 10, "R")
        final_fire = Node(0, 14, "R")
        game = LiveDecisionGame(
            RestScreen(
                False,
                [RestOption.RECALL, RestOption.SMITH, RestOption.REST],
            ),
            deck=[strike(), defend()],
            act=1,
            floor=6,
            hp=71,
            max_hp=75,
            dungeon_map=make_map(future_fire, final_fire),
        )
        game.has_ruby_key = False
        adviser = PickingMacroAdvisor(self.by_label("RECALL"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertEqual(RestOption.RECALL, action.rest_option)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "CAMPFIRE"
        )
        recall = next(
            item for item in request["candidates"]
            if item["candidate_id"] == "rest:RECALL"
        )
        self.assertEqual("RUBY", recall["facts"]["key"])
        self.assertEqual(
            [11, 15],
            recall["facts"]["visible_future_campfire_rows_any_route"],
        )
        self.assertIn(
            "RUBY_URGENCY_IS_INDEPENDENT_OF_SAPPHIRE_AND_EMERALD",
            request["hard_constraints"],
        )

    def test_campfire_model_receives_slime_boss_risk_profile(self):
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST, RestOption.SMITH]),
            deck=[make_card("Defragment", card_type=CardType.POWER)],
            act=1,
            floor=15,
            hp=60,
            max_hp=75,
        )
        game.act_boss = "Slime Boss"
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertEqual(RestOption.SMITH, action.rest_option)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "CAMPFIRE"
        )
        self.assertEqual("Slime Boss", request["state"]["boss"])
        self.assertEqual(
            "slimeboss", request["state"]["decision_context"]["boss_id"]
        )
        self.assertEqual(
            {"slimeboss"},
            {item["facts"]["boss_id"] for item in request["candidates"]},
        )
        self.assertEqual(
            {"low"},
            {item["facts"]["boss_risk"] for item in request["candidates"]},
        )

    def test_final_fire_rest_does_not_double_count_pantograph_healing(self):
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST, RestOption.SMITH]),
            deck=[make_card("Defragment", card_type=CardType.POWER)],
            act=1,
            floor=15,
            hp=60,
            max_hp=80,
        )
        game.act_boss = "Slime Boss"
        game.relics = [Relic("Pantograph", "Pantograph")]
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        with patch.object(
            agent, "_upgrade_score", return_value=10.325
        ), patch.object(
            agent,
            "_boss_entry_readiness",
            return_value={"ready": False, "score": 0.0},
        ), patch.object(
            agent, "_deck_readiness", return_value={"score": 0.419}
        ):
            chosen, values, details = agent._choose_campfire_option(
                [RestOption.REST, RestOption.SMITH], 60, 15
            )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])
        self.assertEqual(20, details["actual_recovery"])
        self.assertEqual(25, details["pending_boss_healing"])
        self.assertEqual(20, details["pending_healing_overlap"])
        self.assertEqual(0, details["effective_recovery"])
        self.assertEqual(80, details["boss_entry_hp_without_rest"])
        self.assertEqual(80, details["boss_entry_hp_with_rest"])
        self.assertFalse(details["mandatory_rest"])

        game.relics = []
        control = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        control.game = game
        with patch.object(
            control, "_upgrade_score", return_value=10.325
        ), patch.object(
            control,
            "_boss_entry_readiness",
            return_value={"ready": False, "score": 0.0},
        ), patch.object(
            control, "_deck_readiness", return_value={"score": 0.419}
        ):
            control_choice, _, control_details = (
                control._choose_campfire_option(
                    [RestOption.REST, RestOption.SMITH], 60, 15
                )
            )

        # Without Pantograph the heal is real, but 60/80 is already above
        # the Slime Boss survival threshold and the same auditable Smith
        # value still wins.  Do not resurrect the old hidden mandatory-Rest
        # override merely to make the control choose a different action.
        self.assertEqual(RestOption.SMITH, control_choice)
        self.assertEqual(20, control_details["effective_recovery"])
        self.assertFalse(control_details["mandatory_rest"])

    def test_hexaghost_divider_keeps_pantograph_overlap_deduplicated(self):
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST, RestOption.SMITH]),
            deck=[make_card("Defragment", card_type=CardType.POWER)],
            act=1,
            floor=15,
            hp=60,
            max_hp=80,
        )
        game.act_boss = "Hexaghost"
        game.relics = [Relic("Pantograph", "Pantograph")]
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        agent.game = game

        with patch.object(
            agent, "_upgrade_score", return_value=10.325
        ), patch.object(
            agent,
            "_boss_entry_readiness",
            return_value={"ready": False, "score": 0.0},
        ), patch.object(
            agent, "_deck_readiness", return_value={"score": 0.419}
        ):
            chosen, values, details = agent._choose_campfire_option(
                [RestOption.REST, RestOption.SMITH], 60, 15
            )

        self.assertEqual(RestOption.SMITH, chosen)
        self.assertGreater(values[RestOption.SMITH], values[RestOption.REST])
        self.assertEqual(20, details["actual_recovery"])
        self.assertEqual(25, details["pending_boss_healing"])
        self.assertEqual(20, details["pending_healing_overlap"])
        self.assertEqual(0, details["hexaghost_divider_penalty"])
        self.assertEqual(0, details["effective_recovery"])
        self.assertEqual(80, details["boss_entry_hp_without_rest"])
        self.assertEqual(80, details["boss_entry_hp_with_rest"])

    def test_campfire_smith_exposes_top_targets_as_estimates(self):
        cards = [
            make_card("Defragment", card_type=CardType.POWER),
            make_card("Buffer", card_type=CardType.POWER),
            make_card("Zap", card_type=CardType.SKILL),
        ]
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST, RestOption.SMITH]),
            deck=cards,
            act=1,
            floor=12,
            hp=70,
            max_hp=75,
        )
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "CAMPFIRE"
        )
        smith = next(
            item for item in request["candidates"]
            if item["candidate_id"] == "rest:SMITH"
        )
        targets = smith["facts"]["top_upgrade_candidates"]
        self.assertEqual(3, len(targets))
        self.assertTrue(all(
            item["projection_available"] is False for item in targets
        ))
        self.assertIn(
            "controller_estimate", smith["facts"]["projection_contract"]
        )

    def test_low_hp_survival_rest_does_not_call_campfire_model(self):
        game = LiveDecisionGame(
            RestScreen(
                False,
                [RestOption.RECALL, RestOption.SMITH, RestOption.REST],
            ),
            deck=[strike(), defend()],
            act=1,
            floor=15,
            hp=20,
            max_hp=75,
        )
        game.has_ruby_key = False
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertEqual(RestOption.REST, action.rest_option)
        self.assertEqual([], adviser.requests)

    def test_mark_of_bloom_only_rest_option_skips_empty_model_rerank(self):
        """Replay floor 44: the only legal no-op must still leave the fire."""

        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST]),
            deck=[strike(), defend()],
            act=3,
            floor=44,
            hp=37,
            max_hp=85,
        )
        game.has_ruby_key = True
        game.relics = [
            Relic("Mark of the Bloom", "Mark of the Bloom"),
            Relic("Fusion Hammer", "Fusion Hammer"),
        ]
        adviser = PickingMacroAdvisor(self.by_label("REST"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertIsInstance(action, RestAction)
        self.assertEqual(RestOption.REST, action.rest_option)
        self.assertEqual([], adviser.requests)

    def test_unsafe_act_one_recall_is_not_described_as_legal_to_model(self):
        game = LiveDecisionGame(
            RestScreen(
                False,
                [RestOption.RECALL, RestOption.SMITH, RestOption.REST],
            ),
            deck=[strike(), defend()],
            act=1,
            floor=6,
            hp=70,
            max_hp=80,
        )
        game.has_ruby_key = False
        adviser = PickingMacroAdvisor(self.by_label("SMITH"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "CAMPFIRE"
        )
        self.assertNotIn(
            "rest:RECALL",
            {item["candidate_id"] for item in request["candidates"]},
        )
        self.assertIn(
            "RUBY_RECALL_IS_NOT_AVAILABLE_IN_THIS_CANDIDATE_SET",
            request["hard_constraints"],
        )

    def test_permanent_grid_reranks_exact_uuid(self):
        first = make_card("UpgradeA", uuid="upgrade-a")
        second = make_card("UpgradeB", uuid="upgrade-b")
        game = LiveDecisionGame(
            grid_screen([first, second], for_upgrade=True),
            deck=[first, second],
        )
        game.current_action = "Smith"
        adviser = PickingMacroAdvisor(self.by_label("UpgradeB"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        with patch.object(
            agent,
            "_upgrade_score",
            side_effect=lambda card: 11 if card is first else 10,
        ):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, CardSelectAction)
        self.assertEqual([second], action.cards)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "GRID"
        )
        self.assertTrue(all(
            candidate["facts"]["selection_effect"]["operation"]
            == "upgrade_this_exact_card"
            for candidate in request["candidates"]
        ))
        self.assertTrue(all(
            "marginal upgrade gain"
            in candidate["facts"]["selection_effect"]["value_semantics"]
            for candidate in request["candidates"]
        ))

    def test_act2_sapphire_key_can_compare_only_its_linked_relic(self):
        linked = Relic("DataDisk", "Data Disk")
        key_reward = CombatReward(RewardType.SAPPHIRE_KEY, link=linked)
        relic_reward = CombatReward(RewardType.RELIC, relic=linked)
        game = LiveDecisionGame(
            CombatRewardScreen([key_reward, relic_reward]),
            deck=[strike(), defend()],
            act=2,
            floor=25,
        )
        game.has_sapphire_key = False
        adviser = PickingMacroAdvisor(self.by_label("Data Disk"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        with patch.object(agent, "_relic_acquisition_score", return_value=25):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(relic_reward, action.combat_reward)
        advice = agent.last_noncombat_decision["model_advice"]
        self.assertEqual("reward:relic:1", advice["model_choice_id"])
        self.assertEqual(
            ["reward:relic:1"], advice["final_choice_ids"]
        )
        self.assertEqual(
            {"reward:sapphire_key:0", "reward:relic:1"},
            {row["candidate_id"] for row in advice["rankings"]},
        )

    def test_act3_sapphire_key_is_hard_and_does_not_call_model(self):
        linked = Relic("DataDisk", "Data Disk")
        key_reward = CombatReward(RewardType.SAPPHIRE_KEY, link=linked)
        relic_reward = CombatReward(RewardType.RELIC, relic=linked)
        game = LiveDecisionGame(
            CombatRewardScreen([key_reward, relic_reward]),
            deck=[strike(), defend()],
            act=3,
            floor=42,
        )
        game.has_sapphire_key = False
        adviser = PickingMacroAdvisor(self.by_label("Data Disk"))
        agent = SimpleAgent(
            PlayerClass.DEFECT, goal_mode="HEART", macro_advisor=adviser
        )
        action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, CombatRewardAction)
        self.assertIs(key_reward, action.combat_reward)
        self.assertEqual([], adviser.requests)

    def test_final_safe_map_candidates_can_be_reranked(self):
        current = Node(0, 0, "M")
        first = Node(0, 1, "?")
        second = Node(1, 1, "?")
        current.children = [first, second]
        game = LiveDecisionGame(
            MapScreen(current, [first, second], boss_available=False),
            deck=[strike(), defend()],
            dungeon_map=make_map(current, first, second),
        )
        adviser = PickingMacroAdvisor(self.by_label("?@1,1"))
        agent = SimpleAgent(
            PlayerClass.THE_SILENT, goal_mode="HEART", macro_advisor=adviser
        )
        with patch.object(
            agent,
            "score_map_node",
            side_effect=lambda node, projected_hp=None: 11 if node is first else 10,
        ), patch.object(agent, "_path_survival_risk", return_value=0):
            action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, ChooseMapNodeAction)
        self.assertIs(second, action.node)

    def test_map_prompt_contains_only_connected_route_sequences(self):
        current = Node(0, 0, "M")
        entrance = Node(0, 1, "?")
        rest = Node(0, 2, "R")
        elite = Node(1, 2, "E")
        shop = Node(0, 3, "$")
        monster = Node(1, 3, "M")
        current.children = [entrance]
        entrance.children = [rest, elite]
        rest.children = [shop]
        elite.children = [monster]
        game = LiveDecisionGame(
            MapScreen(current, [entrance], boss_available=False),
            deck=[strike(), defend()],
            dungeon_map=make_map(
                current, entrance, rest, elite, shop, monster
            ),
            hp=80,
            max_hp=80,
        )
        adviser = PickingMacroAdvisor(self.by_label("?@0,1"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        action = agent.get_next_action_in_game(game)

        self.assertIs(entrance, action.node)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "MAP"
        )
        summary = request["candidates"][0]["facts"]["route_summary"]
        self.assertNotIn("symbols_by_depth", summary)
        self.assertGreaterEqual(len(summary["legal_path_options"]), 2)
        for path in summary["legal_path_options"]:
            sequence = path["sequence"]
            self.assertFalse("R@0,2" in sequence and "E@1,2" in sequence)

    def test_map_prompt_paths_preserve_mandatory_emerald_reachability(self):
        current = Node(0, 0, "M")
        entrance = Node(0, 1, "?")
        dead_end = Node(0, 2, "R")
        burning = Node(1, 2, "E")
        dead_shop = Node(0, 3, "$")
        key_exit = Node(1, 3, "?")
        burning.has_emerald_key = True
        current.children = [entrance]
        entrance.children = [dead_end, burning]
        dead_end.children = [dead_shop]
        burning.children = [key_exit]
        game = LiveDecisionGame(
            MapScreen(current, [entrance], boss_available=False),
            deck=[strike(), defend()],
            dungeon_map=make_map(
                current, entrance, dead_end, burning, dead_shop, key_exit
            ),
            act=3,
            floor=40,
            hp=80,
            max_hp=80,
        )
        game.key_system_unlocked = True
        game.has_emerald_key = False
        adviser = PickingMacroAdvisor(self.by_label("?@0,1"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        with patch.object(agent, "_path_survival_risk", return_value=0):
            action = agent.get_next_action_in_game(game)

        self.assertIs(entrance, action.node)
        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "MAP"
        )
        paths = request["candidates"][0]["facts"]["route_summary"][
            "legal_path_options"
        ]
        self.assertTrue(paths)
        self.assertTrue(
            all("E@1,2" in path["sequence"] for path in paths), paths
        )
        self.assertTrue(all("R@0,2" not in path["sequence"] for path in paths))

    def test_map_prompt_keeps_seventh_and_eighth_route_nodes(self):
        current = Node(0, 0, "M")
        symbols = ["?", "M", "?", "M", "?", "M", "$", "R"]
        nodes = [
            Node(0, index + 1, symbol)
            for index, symbol in enumerate(symbols)
        ]
        current.children = [nodes[0]]
        for parent, child in zip(nodes, nodes[1:]):
            parent.children = [child]
        game = LiveDecisionGame(
            MapScreen(current, [nodes[0]], boss_available=False),
            deck=[strike(), defend()],
            dungeon_map=make_map(current, *nodes),
            hp=80,
            max_hp=80,
        )
        adviser = PickingMacroAdvisor(self.by_label("?@0,1"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "MAP"
        )
        summary = request["candidates"][0]["facts"]["route_summary"]
        self.assertEqual(8, summary["depth_limit"])
        self.assertTrue(any(
            len(path["sequence"]) == 8
            and path["sequence"][-1] == "R@0,8"
            for path in summary["legal_path_options"]
        ))

    def test_map_prompt_marks_a_live_ninth_node_as_depth_truncated(self):
        current = Node(0, 0, "M")
        symbols = ["?", "M", "?", "M", "?", "M", "$", "R", "E"]
        nodes = [
            Node(0, index + 1, symbol)
            for index, symbol in enumerate(symbols)
        ]
        current.children = [nodes[0]]
        for parent, child in zip(nodes, nodes[1:]):
            parent.children = [child]
        game = LiveDecisionGame(
            MapScreen(current, [nodes[0]], boss_available=False),
            deck=[strike(), defend()],
            dungeon_map=make_map(current, *nodes),
            hp=80,
            max_hp=80,
        )
        adviser = PickingMacroAdvisor(self.by_label("?@0,1"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )

        agent.get_next_action_in_game(game)

        request = next(
            item for item in adviser.requests
            if item["decision_type"] == "MAP"
        )
        summary = request["candidates"][0]["facts"]["route_summary"]
        self.assertTrue(summary["enumeration_truncated"])
        self.assertTrue(all(
            len(path["sequence"]) == 8
            for path in summary["legal_path_options"]
        ))

    def test_recursive_map_and_campfire_scoring_never_call_model(self):
        adviser = PickingMacroAdvisor(self.by_label("REST"))
        agent = SimpleAgent(
            PlayerClass.IRONCLAD, goal_mode="HEART", macro_advisor=adviser
        )
        game = LiveDecisionGame(
            RestScreen(False, [RestOption.REST, RestOption.SMITH]),
            deck=[strike(), defend()],
            hp=40,
            max_hp=80,
        )
        agent.game = game
        agent._choose_campfire_option(
            [RestOption.REST, RestOption.SMITH], 40, 10
        )
        agent.score_map_node(Node(0, 5, "R"), projected_hp=40)
        self.assertEqual([], adviser.requests)


if __name__ == "__main__":
    unittest.main()
