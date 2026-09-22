import copy
import json
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import bridge


TEST_ATTEMPT_BINDING = {
    "attempt_id": "attempt-command-binding",
    "run_id": "IRONCLAD:0:123",
    "seed": 123,
    "character": "IRONCLAD",
    "ascension_level": 0,
    "run_type": "standard",
    "decision_hash": "decision-command-binding",
    "controller_hash": "controller-command-binding",
    "policy_version": "fast-policy-v5",
    "selection_id": "selection-command-binding",
    "selection_digest": "a" * 64,
}


def with_attempt_binding(state):
    state.update(TEST_ATTEMPT_BINDING)
    return state


def with_payload_binding(payload, state):
    payload.update({field: state[field] for field in bridge.ATTEMPT_BINDING_FIELDS})
    return payload


def sample_state():
    raw = {
        "available_commands": ["choose", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "GRID",
            "screen_state": {
                "cards": [
                    {"id": "Strike_G", "name": "Strike", "uuid": "card-a", "upgrades": 0},
                    {"id": "Strike_G", "name": "Strike", "uuid": "card-b", "upgrades": 0},
                ],
                "for_upgrade": True,
            },
            "choice_list": ["strike", "strike"],
            "room_phase": "INCOMPLETE",
            "action_phase": "WAITING_ON_USER",
            "deck": [
                {"id": "Strike_G", "name": "Strike", "uuid": "card-a", "upgrades": 0},
                {"id": "Strike_G", "name": "Strike", "uuid": "card-b", "upgrades": 0},
            ],
            "potions": [],
        },
    }
    return with_attempt_binding(bridge.enrich_state(raw, 41))


def initializing_combat_raw():
    return {
        "available_commands": ["play", "end", "potion", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "NONE",
            "room_phase": "COMBAT",
            "action_phase": "WAITING_ON_USER",
            "potions": [],
            "combat_state": {
                "turn": 1,
                "hand": [],
                "monsters": [
                    {
                        "id": "SlaverRed",
                        "name": "Red Slaver",
                        "current_hp": 47,
                        "max_hp": 47,
                        "intent": "DEBUG",
                        "half_dead": False,
                        "is_gone": False,
                    }
                ],
            },
        },
    }


def payload_for(state, **changes):
    payload = {
        "id": "test-request",
        "action": "choose",
        "policy_version": "fast-policy-v5",
        "expected_seq": state["state_seq"],
        "decision_id": state["decision_id"],
        "phase": state["phase"],
        "option_id": state["options"][1]["option_id"],
        "choice_index": 1,
    }
    with_payload_binding(payload, state)
    payload.update(changes)
    return payload


class MainMenuProgressionTests(unittest.TestCase):
    def test_all_profile_wins_enable_final_act_projection_before_start(self):
        raw = {
            "available_commands": ["start", "state"],
            "ready_for_command": True,
            "in_game": False,
            "key_system_unlocked": False,
            "silent_third_act_win": True,
            "defect_third_act_win": True,
            "ironclad_third_act_win": True,
        }

        state = bridge.enrich_state(raw, 42)

        self.assertEqual("MAIN_MENU", state["phase"])
        self.assertTrue(state["key_system_unlocked"])

    def test_in_game_final_act_value_is_never_derived_from_profile_wins(self):
        raw = {
            "available_commands": ["state"],
            "ready_for_command": True,
            "in_game": True,
            "key_system_unlocked": False,
            "silent_third_act_win": True,
            "defect_third_act_win": True,
            "ironclad_third_act_win": True,
            "game_state": {
                "screen_type": "NONE",
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

        state = bridge.enrich_state(raw, 43)

        self.assertFalse(state["key_system_unlocked"])


class AuthoritativeDecisionPhaseTests(unittest.TestCase):
    def test_astrolabe_boss_reward_binds_three_card_transform_grid(self):
        state = {
            "phase": "BOSS_REWARD",
            "options": [{
                "option_id": "option:astrolabe",
                "choice_index": 2,
                "target": {
                    "kind": "relic",
                    "relic": {"id": "Astrolabe", "name": "Astrolabe"},
                },
            }],
        }

        context = bridge.accepted_grid_followup_context(
            {"action": "choose", "option_id": "option:astrolabe"},
            state,
        )

        self.assertEqual("BOSS_REWARD", context["parent_phase"])
        self.assertEqual("transform", context["operation"])
        self.assertEqual(3, context["select_count"])
        self.assertEqual("accepted_protocol_choice", context["authority"])

    def test_bottled_relic_reward_binds_typed_followup_grid(self):
        cases = (
            ("Bottled Flame", "bottle_attack", "ATTACK"),
            ("Bottled Lightning", "bottle_skill", "SKILL"),
            ("Bottled Tornado", "bottle_power", "POWER"),
        )
        for relic_id, operation, card_type in cases:
            with self.subTest(relic_id=relic_id):
                state = {
                    "phase": "COMBAT_REWARD",
                    "options": [{
                        "option_id": f"option:{operation}",
                        "choice_index": 0,
                        "target": {
                            "kind": "reward",
                            "reward": {
                                "reward_type": "RELIC",
                                "relic": {"id": relic_id, "name": relic_id},
                            },
                        },
                    }],
                }
                context = bridge.accepted_grid_followup_context(
                    {"action": "choose", "option_id": f"option:{operation}"},
                    state,
                )

                self.assertEqual("COMBAT_REWARD", context["parent_phase"])
                self.assertEqual(operation, context["operation"])
                self.assertEqual(card_type, context["card_type"])
                self.assertEqual(1, context["select_count"])

    def test_neow_choice_context_binds_followup_grid_operation(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": "REMOVE_TWO",
            "drawback_kind": "NONE",
            "parameters": {
                "hp_bonus": 8, "cursed": False,
                "drawback_def_kind": None,
            },
        }
        mechanism_id = bridge.stable_id("neow-mechanism", contract)
        state = {
            "phase": "NEOW",
            "options": [{
                "option_id": "option:remove-two",
                "choice_index": 2,
                "target": {
                    "kind": "event_option",
                    "event_id": "Neow Event",
                    "neow_contract": contract,
                    "mechanism_id": mechanism_id,
                },
            }],
        }
        context = bridge.accepted_grid_followup_context(
            {"action": "choose", "option_id": "option:remove-two"},
            state,
        )

        self.assertEqual("remove", context["operation"])
        self.assertEqual(2, context["select_count"])
        self.assertEqual("accepted_protocol_choice", context["authority"])

        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "GRID",
                "screen_state": {
                    "cards": [], "selected_cards": [], "num_cards": 2,
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False,
                },
                "choice_list": [], "room_phase": "INCOMPLETE",
                "action_phase": "WAITING_ON_USER", "deck": [],
                "potions": [],
            },
        }
        enriched = bridge.enrich_state(
            raw, 42, parent_choice_context=context
        )
        self.assertEqual(
            context,
            enriched["game_state"]["screen_state"][
                "parent_choice_context"
            ],
        )

    @staticmethod
    def event_raw(event_id):
        return {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "EVENT",
                "screen_state": {
                    "event_id": event_id,
                    "body_text": "Choose a blessing.",
                    "options": [
                        {
                            "label": "Blessing",
                            "text": "Obtain a blessing.",
                            "disabled": False,
                            "original_button_index": 0,
                            "choice_index": 0,
                        }
                    ],
                },
                "choice_list": ["Blessing"],
                "room_phase": "EVENT",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

    @staticmethod
    def sapphire_raw(rewards, choice_list=None):
        return {
            "available_commands": ["choose", "proceed", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "COMBAT_REWARD",
                "screen_state": {"rewards": rewards},
                "choice_list": (
                    list(choice_list) if choice_list is not None
                    else [reward["label"] for reward in rewards]
                ),
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

    def test_real_neow_event_id_has_distinct_auditable_phase(self):
        state = bridge.enrich_state(self.event_raw("Neow Event"), 11)

        self.assertEqual("NEOW", state["phase"])
        self.assertEqual("neowevent", bridge.normalized_game_id(
            state["game_state"]["screen_state"]["event_id"]
        ))
        self.assertEqual(1, len(state["options"]))

    def test_non_neow_event_including_match_remains_event(self):
        for event_id in ("Match and Keep!", "Neow's Lament"):
            with self.subTest(event_id=event_id):
                state = bridge.enrich_state(self.event_raw(event_id), 12)
                self.assertEqual("EVENT", state["phase"])

    def test_sapphire_reward_phase_and_semantics_ignore_container_order(self):
        relic = {
            "choice_index": 1,
            "reward_type": "RELIC",
            "label": "Anchor",
            "relic": {"id": "Anchor", "name": "Anchor"},
        }
        key = {
            "choice_index": 0,
            "reward_type": "SAPPHIRE_KEY",
            "label": "Sapphire Key",
            "link": {"id": "Anchor", "name": "Anchor"},
        }
        choices = ["Sapphire Key", "Anchor"]
        first = bridge.enrich_state(
            self.sapphire_raw([key, relic], choices), 13
        )
        reordered = bridge.enrich_state(
            self.sapphire_raw([relic, key], choices), 14
        )

        self.assertEqual("SAPPHIRE_KEY", first["phase"])
        self.assertEqual("SAPPHIRE_KEY", reordered["phase"])
        self.assertIn("proceed", first["legal_actions"])
        self.assertEqual("sapphire_key", first["options"][0][
            "target"
        ]["kind"])
        self.assertEqual("SAPPHIRE_KEY", first["options"][0][
            "target"
        ]["reward"]["reward_type"])
        self.assertEqual("Anchor", first["options"][0][
            "target"
        ]["link"]["id"])
        self.assertEqual("reward", first["options"][1]["target"]["kind"])
        self.assertEqual("RELIC", first["options"][1][
            "target"
        ]["reward"]["reward_type"])
        self.assertEqual("Anchor", first["options"][1][
            "target"
        ]["reward"]["relic"]["id"])
        self.assertEqual([0, 1], [
            option["choice_index"] for option in first["options"]
        ])
        first_ids = {
            option["target"]["reward"]["reward_type"]:
            option["option_id"]
            for option in first["options"]
        }
        reordered_ids = {
            option["target"]["reward"]["reward_type"]:
            option["option_id"]
            for option in reordered["options"]
        }
        self.assertEqual(first_ids, reordered_ids)

    def test_sapphire_surface_missing_or_duplicate_binding_is_unbound(self):
        missing_type = {
            "choice_index": 0,
            "label": "Unknown",
            "relic": {"id": "DataDisk", "name": "Data Disk"},
        }
        key = {
            "choice_index": 1,
            "reward_type": "SAPPHIRE_KEY",
            "label": "Sapphire Key",
            "link": {"id": "DataDisk", "name": "Data Disk"},
        }
        missing_state = bridge.enrich_state(
            self.sapphire_raw(
                [key, missing_type], ["Unknown", "Sapphire Key"]
            ),
            15,
        )
        self.assertEqual("reward_unbound", missing_state["options"][0][
            "target"
        ]["kind"])
        self.assertEqual("missing_reward_type", missing_state["options"][0][
            "target"
        ]["binding_status"])
        self.assertEqual("sapphire_key", missing_state["options"][1][
            "target"
        ]["kind"])

        duplicate_key = copy.deepcopy(key)
        duplicate_key["choice_index"] = 0
        duplicate_state = bridge.enrich_state(
            self.sapphire_raw(
                [duplicate_key, key], ["Sapphire Key", "Sapphire Key"]
            ),
            16,
        )
        self.assertTrue(all(
            option["target"]["kind"] == "reward_unbound"
            and option["target"]["binding_status"]
            == "duplicate_sapphire_key_reward"
            for option in duplicate_state["options"]
        ))

    def test_duplicate_reward_choice_index_never_binds_by_container_order(self):
        key = {
            "choice_index": 0,
            "reward_type": "SAPPHIRE_KEY",
            "label": "Sapphire Key",
            "link": {"id": "DataDisk", "name": "Data Disk"},
        }
        relic = {
            "choice_index": 0,
            "reward_type": "RELIC",
            "label": "Data Disk",
            "relic": {"id": "DataDisk", "name": "Data Disk"},
        }
        state = bridge.enrich_state(
            self.sapphire_raw(
                [key, relic], ["Sapphire Key", "Data Disk"]
            ),
            17,
        )

        self.assertTrue(all(
            option["target"]["kind"] == "reward_unbound"
            and option["target"]["binding_status"]
            == "missing_or_duplicate_choice_index"
            for option in state["options"]
        ))

    def test_combat_reward_without_sapphire_key_stays_combat_reward(self):
        state = bridge.enrich_state(
            self.sapphire_raw([{
                "choice_index": 0,
                "reward_type": "RELIC",
                "label": "Anchor",
                "relic": {"id": "Anchor", "name": "Anchor"},
            }]),
            15,
        )

        self.assertEqual("COMBAT_REWARD", state["phase"])


class EventOptionProtocolTests(unittest.TestCase):
    @staticmethod
    def raw(event_id, options, choices, *, relics=None):
        game_state = {
            "screen_type": "EVENT",
            "screen_state": {
                "event_id": event_id,
                "options": options,
            },
            "choice_list": choices,
        }
        if relics is not None:
            game_state["relics"] = [
                {"id": relic_id} for relic_id in relics
            ]
        return {
            "game_state": game_state,
        }

    @staticmethod
    def staged_contract(
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

    @staticmethod
    def options_from_contracts(
        contracts, *, disabled=(), cards=None,
    ):
        disabled = set(disabled)
        cards = cards or {}
        options = []
        choice_index = 0
        for contract in contracts:
            original = contract["original_button_index"]
            item = {
                "text": f"localized-{original}",
                "label": f"localized-{original}",
                "disabled": original in disabled,
                "original_button_index": original,
                "event_contract": contract,
            }
            if original in cards:
                item["card"] = dict(cards[original])
            if original not in disabled:
                item["choice_index"] = choice_index
                choice_index += 1
            options.append(item)
        return options

    @staticmethod
    def goop_contract(original_index, *, gold_loss=27):
        if original_index == 0:
            option_kind = "GATHER"
            parameters = {"gold_gain": 75, "hp_damage": 11}
        else:
            option_kind = "LEAVE"
            parameters = {"gold_loss": gold_loss}
        return {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "World of Goop",
            "event_class": bridge.WORLD_OF_GOOP_EVENT_CLASS,
            "original_button_index": original_index,
            "option_kind": option_kind,
            "parameters": parameters,
        }

    @classmethod
    def goop_options(cls):
        return [
            {
                "text": "localized gather", "label": "localized gather",
                "disabled": False, "original_button_index": 0,
                "choice_index": 0,
                "event_contract": cls.goop_contract(0),
            },
            {
                "text": "localized leave", "label": "localized leave",
                "disabled": False, "original_button_index": 1,
                "choice_index": 1,
                "event_contract": cls.goop_contract(1),
            },
        ]

    @classmethod
    def cleric_contracts(cls, stage="MAIN", *, heal=20, purify=50):
        instance = {
            "heal_amount": heal,
            "heal_gold_cost": 35,
            "purify_cost": purify,
        }
        if stage == "RESULT":
            specs = [(0, "CONTINUE", {})]
        else:
            specs = [
                (0, "HEAL", {"gold_cost": 35, "heal_amount": heal}),
                (1, "PURIFY", {
                    "gold_cost_if_purgeable": purify,
                    "purge_select_count": 1,
                    "selection_mode": "PLAYER_SELECT",
                }),
                (2, "LEAVE", {}),
            ]
        return [
            cls.staged_contract(
                "The Cleric", bridge.CLERIC_EVENT_CLASS, stage,
                index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def designer_contracts(
        cls, stage="MAIN", *, adjustment=True, clean_up=False,
        costs=(40, 60, 90, 3),
    ):
        adjust_cost, clean_cost, full_cost, hp_loss = costs
        instance = {
            "adjustment_upgrades_one": adjustment,
            "clean_up_removes_cards": clean_up,
            "adjust_cost": adjust_cost,
            "clean_up_cost": clean_cost,
            "full_service_cost": full_cost,
            "hp_loss": hp_loss,
        }
        if stage == "INTRO":
            specs = [(0, "OPEN_SERVICES", {})]
        elif stage == "DONE":
            specs = [(0, "CONTINUE", {})]
        else:
            specs = [
                (0, (
                    "ADJUSTMENT_GRID_UPGRADE" if adjustment
                    else "ADJUSTMENT_RANDOM_UPGRADE"
                ), {
                    "gold_cost": adjust_cost,
                    **({
                        "upgrade_select_count": 1,
                        "selection_mode": "PLAYER_SELECT",
                    } if adjustment else {
                        "upgrade_max_count": 2,
                        "selection_mode": "RANDOM_UP_TO_AVAILABLE",
                    }),
                }),
                (1, (
                    "CLEAN_UP_GRID_PURGE" if clean_up
                    else "CLEAN_UP_GRID_TRANSFORM"
                ), ({
                    "gold_cost": clean_cost,
                    "selection_mode": "PLAYER_SELECT",
                    "purge_select_count": 1,
                } if clean_up else {
                    "gold_cost": clean_cost,
                    "selection_mode": "PLAYER_SELECT",
                    "transform_select_count": 2,
                    "transform_result": "RANDOM",
                })),
                (2, "FULL_SERVICE", {
                    "gold_cost": full_cost,
                    "purge_select_count": 1,
                    "random_upgrade_max_count": 1,
                    "selection_mode": (
                        "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                    ),
                }),
                (3, "PUNCH_AND_LEAVE", {"hp_loss": hp_loss}),
            ]
        return [
            cls.staged_contract(
                "Designer", bridge.DESIGNER_EVENT_CLASS, stage,
                index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def cursed_tome_contracts(
        cls, stage, *, final_hp=10, damage_taken=0,
        pool=None,
    ):
        pool = list(pool or bridge.CURSED_TOME_BOOK_IDS)
        instance = {
            "final_hp_loss": final_hp,
            "damage_taken": damage_taken,
            "random_relic_pool": pool,
        }
        if stage == "INTRO":
            specs = [
                (0, "ENTER_RANDOM_BOOK_CHAIN", {
                    "future_hp_loss_to_complete": 6 + final_hp,
                    "random_relic_count": 1,
                    "reward_surface": "COMBAT_REWARD",
                    "selection_mode": "UNIFORM_MISC_RNG",
                }),
                (1, "LEAVE", {}),
            ]
        elif stage in {"PAGE_1", "PAGE_2", "PAGE_3"}:
            page = int(stage[-1])
            specs = [(0, f"READ_{stage}", {"hp_loss": page})]
        elif stage == "LAST_PAGE":
            specs = [
                (0, "COMPLETE_RANDOM_BOOK", {
                    "hp_loss": final_hp,
                    "random_relic_count": 1,
                    "reward_surface": "COMBAT_REWARD",
                    "selection_mode": "UNIFORM_MISC_RNG",
                }),
                (1, "STOP", {"hp_loss": 3}),
            ]
        else:
            specs = [(0, "PROCEED", {})]
        return [
            cls.staged_contract(
                "Cursed Tome", bridge.CURSED_TOME_EVENT_CLASS, stage,
                index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def mausoleum_contracts(cls, stage="INTRO", *, percent=50):
        instance = {"curse_probability_percent": percent}
        if stage == "RESULT":
            specs = [(0, "CONTINUE", {})]
        else:
            specs = [
                (0, "OPEN", {
                    "random_relic_count": 1,
                    "relic_selection_mode": (
                        "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                    ),
                    "curse_card_id": "Writhe",
                    "curse_probability_percent": percent,
                }),
                (1, "LEAVE", {}),
            ]
        return [
            cls.staged_contract(
                "The Mausoleum", bridge.MAUSOLEUM_EVENT_CLASS, stage,
                index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def knowing_skull_contracts(
        cls, stage="ASK", *, costs=(6, 6, 6, 6),
    ):
        potion_cost, card_cost, gold_cost, leave_cost = costs
        instance = {
            "potion_cost": potion_cost,
            "card_cost": card_cost,
            "gold_cost": gold_cost,
            "leave_cost": leave_cost,
            "gold_reward": 90,
        }
        if stage == "INTRO_1":
            specs = [(0, "OPEN_QUESTIONS", {})]
        elif stage == "COMPLETE":
            specs = [(0, "CONTINUE", {})]
        else:
            specs = [
                (0, "TAKE_POTION", {
                    "hp_loss": potion_cost,
                    "reward_count": 1,
                    "reward_kind": "RANDOM_POTION",
                }),
                (1, "TAKE_GOLD", {
                    "hp_loss": gold_cost,
                    "gold_gain": 90,
                }),
                (2, "TAKE_CARD", {
                    "hp_loss": card_cost,
                    "reward_count": 1,
                    "reward_color": "COLORLESS",
                    "reward_rarity": "UNCOMMON",
                    "selection_mode": "RANDOM",
                }),
                (3, "LEAVE", {"hp_loss": leave_cost}),
            ]
        return [
            cls.staged_contract(
                "Knowing Skull", bridge.KNOWING_SKULL_EVENT_CLASS, stage,
                index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def dead_adventurer_contracts(cls, stage="INTRO"):
        instance = {
            "num_rewards": 0,
            "encounter_chance_percent": 25,
            "remaining_rewards": ["GOLD", "RELIC", "NOTHING"],
            "enemy_index": 1,
            "encounter_id": "Gremlin Nob",
        }
        specs = (
            [
                (0, "SEARCH", {
                    "roll_min": 0, "roll_max_inclusive": 99,
                    "encounter_roll_lt": 25,
                    "encounter_id": "Gremlin Nob",
                    "encounter_reward_gold_min": 25,
                    "encounter_reward_gold_max": 35,
                    "success_reward_kind": "GOLD",
                    "success_gold_gain": 30,
                    "success_random_relic_count": 0,
                    "success_relic_selection_mode": "NONE",
                }),
                (1, "LEAVE", {}),
            ]
            if stage == "INTRO" else
            [(0, "FIGHT", {
                "encounter_id": "Gremlin Nob",
                "combat_reward_gold_min": 25,
                "combat_reward_gold_max": 35,
            })]
        )
        return [
            cls.staged_contract(
                "Dead Adventurer", bridge.DEAD_ADVENTURER_EVENT_CLASS,
                stage, index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def scrap_ooze_contracts(cls, stage="MAIN"):
        instance = {
            "relic_chance_percent_displayed": 45,
            "damage": 5,
            "total_damage_dealt": 7,
            "screen_num": 0 if stage == "MAIN" else 1,
        }
        specs = (
            [
                (0, "REACH_INSIDE", {
                    "hp_loss": 5, "roll_min": 0,
                    "roll_max_inclusive": 99,
                    "success_roll_min_inclusive": 54,
                    "success_random_relic_count": 1,
                    "success_relic_selection_mode": (
                        "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                    ),
                }),
                (1, "LEAVE", {}),
            ]
            if stage == "MAIN" else [(0, "CONTINUE", {})]
        )
        return [
            cls.staged_contract(
                "Scrap Ooze", bridge.SCRAP_OOZE_EVENT_CLASS,
                stage, index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def face_trader_contracts(cls, stage="MAIN"):
        pool = list(bridge.FACE_TRADER_RELIC_IDS)
        instance = {
            "gold_reward": 75, "damage": 8,
            "random_face_pool": pool,
        }
        specs = (
            [
                (0, "TOUCH", {"hp_loss": 8, "gold_gain": 75}),
                (1, "TRADE", {
                    "random_relic_pool": pool,
                    "random_relic_count": 1,
                    "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
                }),
                (2, "LEAVE", {}),
            ]
            if stage == "MAIN" else [(0, "OPEN", {})]
        )
        return [
            cls.staged_contract(
                "Face Trader", bridge.FACE_TRADER_EVENT_CLASS,
                stage, index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def duplicator_contracts(cls, stage="MAIN"):
        instance = {"screen_num": 0 if stage == "MAIN" else 2}
        specs = (
            [
                (0, "DUPLICATE", {
                    "duplicate_select_count": 1,
                    "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
                }),
                (1, "LEAVE", {}),
            ]
            if stage == "MAIN" else [(0, "CONTINUE", {})]
        )
        return [
            cls.staged_contract(
                "Duplicator", bridge.DUPLICATOR_EVENT_CLASS,
                stage, index, kind, instance, parameters,
            )
            for index, kind, parameters in specs
        ]

    @classmethod
    def bonfire_contracts(cls, stage):
        instance = {"card_select": False}
        kind, parameters = (
            (
                "OFFER_CARD",
                {
                    "offer_select_count": 1,
                    "selection_mode": (
                        "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
                    ),
                },
            )
            if stage == "CHOOSE" else ("CONTINUE", {})
        )
        return [cls.staged_contract(
            "Bonfire Elementals", bridge.BONFIRE_EVENT_CLASS,
            stage, 0, kind, instance, parameters,
        )]

    def test_disabled_buttons_keep_original_index_while_choose_is_compressed(self):
        options = [
            {
                "text": "disabled", "label": "disabled",
                "disabled": True, "original_button_index": 0,
            },
            {
                "text": "first", "label": "first",
                "disabled": False, "original_button_index": 1,
                "choice_index": 0,
            },
            {
                "text": "second", "label": "second",
                "disabled": False, "original_button_index": 2,
                "choice_index": 1,
            },
        ]

        built = bridge.build_options(
            self.raw("Ordinary Event", options, ["first", "second"])
        )

        self.assertEqual([0, 1], [item["choice_index"] for item in built])
        self.assertEqual(
            [1, 2],
            [item["target"]["original_button_index"] for item in built],
        )

        malformed = [dict(item) for item in options]
        malformed[0]["choice_index"] = 0
        rejected = bridge.build_options(
            self.raw("Ordinary Event", malformed, ["first", "second"])
        )
        self.assertTrue(all(
            item["target"]["kind"] == "event_option_unbound"
            for item in rejected
        ))

    def test_world_of_goop_contract_is_stable_under_container_reorder(self):
        options = self.goop_options()
        choices = ["gather", "leave"]
        ordered = bridge.build_options(
            self.raw("World of Goop", options, choices)
        )
        reordered = bridge.build_options(
            self.raw("World of Goop", list(reversed(options)), choices)
        )

        self.assertEqual(
            [item["option_id"] for item in ordered],
            [item["option_id"] for item in reordered],
        )
        self.assertEqual(
            [item["target"]["mechanism_id"] for item in ordered],
            [item["target"]["mechanism_id"] for item in reordered],
        )
        gather = reordered[0]["target"]
        self.assertEqual(0, gather["original_button_index"])
        self.assertEqual(
            {"gold_gain": 75, "hp_damage": 11},
            gather["event_contract"]["parameters"],
        )
        self.assertNotIn("localized", bridge.canonical(gather))

    def test_world_of_goop_result_continue_has_exact_no_delta_contract(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "World of Goop",
            "event_class": bridge.WORLD_OF_GOOP_EVENT_CLASS,
            "original_button_index": 0,
            "option_kind": "CONTINUE",
            "parameters": {},
        }
        options = [{
            "text": "localized continue", "label": "localized continue",
            "disabled": False, "original_button_index": 0,
            "choice_index": 0, "event_contract": contract,
        }]

        built = bridge.build_options(
            self.raw("World of Goop", options, ["continue"])
        )

        self.assertEqual("event_option", built[0]["target"]["kind"])
        self.assertEqual(
            "CONTINUE",
            built[0]["target"]["event_contract"]["option_kind"],
        )
        self.assertEqual(
            {}, built[0]["target"]["event_contract"]["parameters"]
        )

        malformed = [dict(options[0])]
        malformed[0]["event_contract"] = dict(contract)
        malformed[0]["event_contract"]["parameters"] = {"gold_loss": 0}
        rejected = bridge.build_options(
            self.raw("World of Goop", malformed, ["continue"])
        )
        self.assertEqual(
            "event_option_unbound", rejected[0]["target"]["kind"]
        )

    def test_original_button_index_shape_errors_are_unbound_and_rejected(self):
        for mutation in ("missing", "duplicate", "wrong_type"):
            with self.subTest(mutation=mutation):
                options = self.goop_options()
                if mutation == "missing":
                    del options[0]["original_button_index"]
                elif mutation == "duplicate":
                    options[1]["original_button_index"] = 0
                else:
                    options[0]["original_button_index"] = "0"
                built = bridge.build_options(
                    self.raw("World of Goop", options, ["gather", "leave"])
                )
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option_unbound"
                    for item in built
                ))
                state = {
                    "legal_actions": ["choose"], "options": built,
                    "state_seq": 9, "decision_id": "decision:event",
                    "phase": "EVENT", "in_game": False,
                }
                payload = {
                    "action": "choose",
                    "policy_version": "fast-policy-v5",
                    "option_id": built[0]["option_id"],
                    "choice_index": 0,
                    "expected_seq": 9,
                    "decision_id": "decision:event",
                    "phase": "EVENT",
                }
                with self.assertRaisesRegex(
                    bridge.CommandRejected, "authoritative target"
                ):
                    bridge.resolve_command(payload, state)

    def test_world_of_goop_missing_or_malformed_contract_is_unbound(self):
        cases = []
        missing = self.goop_options()
        del missing[0]["event_contract"]
        cases.append(missing)
        wrong_parameter_type = self.goop_options()
        wrong_parameter_type[0]["event_contract"]["parameters"][
            "hp_damage"
        ] = True
        cases.append(wrong_parameter_type)
        wrong_contract_index = self.goop_options()
        wrong_contract_index[0]["event_contract"][
            "original_button_index"
        ] = 1
        cases.append(wrong_contract_index)

        for options in cases:
            with self.subTest(options=options):
                built = bridge.build_options(
                    self.raw("World of Goop", options, ["gather", "leave"])
                )
                self.assertEqual(
                    "event_option_unbound", built[0]["target"]["kind"]
                )
                self.assertNotIn("mechanism_id", built[0]["target"])

    def test_cleric_disabled_compression_reorder_and_contract_fail_closed(self):
        contracts = self.cleric_contracts()
        options = self.options_from_contracts(contracts, disabled={0})
        raw = self.raw("The Cleric", options, ["purify", "leave"])
        ordered = bridge.build_options(raw)
        reordered = bridge.build_options(self.raw(
            "The Cleric", list(reversed(options)), ["purify", "leave"]
        ))

        self.assertEqual(
            [1, 2],
            [item["target"]["original_button_index"] for item in ordered],
        )
        self.assertEqual(
            ["PURIFY", "LEAVE"],
            [
                item["target"]["event_contract"]["option_kind"]
                for item in ordered
            ],
        )
        self.assertEqual(
            [item["option_id"] for item in ordered],
            [item["option_id"] for item in reordered],
        )
        self.assertEqual(
            [item["target"]["mechanism_id"] for item in ordered],
            [item["target"]["mechanism_id"] for item in reordered],
        )

        malformed_surfaces = []
        missing = copy.deepcopy(options)
        del missing[0]["event_contract"]
        malformed_surfaces.append(missing)
        wrong_type = copy.deepcopy(options)
        wrong_type[1]["event_contract"]["parameters"][
            "purge_select_count"
        ] = True
        malformed_surfaces.append(wrong_type)
        instance_mismatch = copy.deepcopy(options)
        instance_mismatch[2]["event_contract"]["instance_parameters"][
            "heal_amount"
        ] = 19
        malformed_surfaces.append(instance_mismatch)
        for malformed in malformed_surfaces:
            with self.subTest(malformed=malformed):
                built = bridge.build_options(self.raw(
                    "The Cleric", malformed, ["purify", "leave"]
                ))
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option_unbound"
                    for item in built
                ))

    def test_designer_realized_variants_are_bound_not_container_order(self):
        contracts = self.designer_contracts(
            adjustment=False, clean_up=True,
        )
        options = self.options_from_contracts(
            contracts, disabled={0, 2}
        )
        choices = ["clean", "punch"]
        ordered = bridge.build_options(
            self.raw("Designer", options, choices)
        )
        reordered = bridge.build_options(self.raw(
            "Designer", list(reversed(options)), choices
        ))

        self.assertEqual(
            [1, 3],
            [item["target"]["original_button_index"] for item in ordered],
        )
        self.assertEqual(
            ["CLEAN_UP_GRID_PURGE", "PUNCH_AND_LEAVE"],
            [
                item["target"]["event_contract"]["option_kind"]
                for item in ordered
            ],
        )
        self.assertEqual(
            [item["option_id"] for item in ordered],
            [item["option_id"] for item in reordered],
        )
        instance = ordered[0]["target"]["event_contract"][
            "instance_parameters"
        ]
        self.assertIs(instance["adjustment_upgrades_one"], False)
        self.assertIs(instance["clean_up_removes_cards"], True)

        wrong_kind = copy.deepcopy(options)
        wrong_kind[0]["event_contract"][
            "option_kind"
        ] = "ADJUSTMENT_GRID_UPGRADE"
        rejected = bridge.build_options(
            self.raw("Designer", wrong_kind, choices)
        )
        self.assertTrue(all(
            item["target"]["kind"] == "event_option_unbound"
            for item in rejected
        ))

    def test_staged_result_buttons_are_exact_noop_contracts(self):
        cases = [
            ("The Cleric", self.cleric_contracts("RESULT"), None),
            ("Designer", self.designer_contracts("DONE"), None),
            (
                "Cursed Tome",
                self.cursed_tome_contracts(
                    "END", damage_taken=16,
                ),
                [],
            ),
            ("The Mausoleum", self.mausoleum_contracts("RESULT"), None),
            (
                "Knowing Skull",
                self.knowing_skull_contracts("COMPLETE"), None,
            ),
        ]
        for event_id, contracts, relics in cases:
            with self.subTest(event_id=event_id):
                options = self.options_from_contracts(contracts)
                raw = self.raw(
                    event_id, options, ["continue"], relics=relics
                )
                built = bridge.build_options(raw)
                contract = built[0]["target"]["event_contract"]
                self.assertEqual({}, contract["parameters"])
                self.assertIn(
                    contract["option_kind"], {"CONTINUE", "PROCEED"}
                )
                self.assertEqual("event_option", built[0]["target"]["kind"])

    def test_cursed_tome_stage_identity_and_authoritative_random_pool(self):
        pool = ["Necronomicon", "Nilry's Codex"]
        owned_relics = ["Enchiridion"]
        intro_contracts = self.cursed_tome_contracts(
            "INTRO", pool=pool
        )
        intro_options = self.options_from_contracts(intro_contracts)
        intro = bridge.build_options(self.raw(
            "Cursed Tome", intro_options, ["read", "leave"],
            relics=owned_relics,
        ))
        last_contracts = self.cursed_tome_contracts(
            "LAST_PAGE", damage_taken=6, pool=pool
        )
        last = bridge.build_options(self.raw(
            "Cursed Tome", self.options_from_contracts(last_contracts),
            ["complete", "stop"], relics=owned_relics,
        ))

        self.assertEqual(
            "ENTER_RANDOM_BOOK_CHAIN",
            intro[0]["target"]["event_contract"]["option_kind"],
        )
        self.assertEqual(
            "COMPLETE_RANDOM_BOOK",
            last[0]["target"]["event_contract"]["option_kind"],
        )
        self.assertNotEqual(
            intro[0]["target"]["mechanism_id"],
            last[0]["target"]["mechanism_id"],
        )
        self.assertEqual(
            pool,
            intro[0]["target"]["event_contract"][
                "instance_parameters"
            ]["random_relic_pool"],
        )

        page = bridge.build_options(self.raw(
            "Cursed Tome",
            self.options_from_contracts(self.cursed_tome_contracts(
                "PAGE_1", pool=pool,
            )),
            ["read"], relics=owned_relics,
        ))
        end = bridge.build_options(self.raw(
            "Cursed Tome",
            self.options_from_contracts(self.cursed_tome_contracts(
                "END", damage_taken=16, pool=pool,
            )),
            ["continue"], relics=owned_relics,
        ))
        self.assertEqual(
            "READ_PAGE_1",
            page[0]["target"]["event_contract"]["option_kind"],
        )
        self.assertEqual(
            "PROCEED",
            end[0]["target"]["event_contract"]["option_kind"],
        )

        wrong_pool = bridge.build_options(self.raw(
            "Cursed Tome", intro_options, ["read", "leave"], relics=[]
        ))
        self.assertTrue(all(
            item["target"]["kind"] == "event_option_unbound"
            for item in wrong_pool
        ))
        wrong_damage = self.cursed_tome_contracts(
            "PAGE_2", damage_taken=0, pool=pool
        )
        rejected = bridge.build_options(self.raw(
            "Cursed Tome", self.options_from_contracts(wrong_damage),
            ["read"], relics=owned_relics,
        ))
        self.assertEqual(
            "event_option_unbound", rejected[0]["target"]["kind"]
        )
        wrong_type = copy.deepcopy(last_contracts)
        wrong_type[0]["parameters"]["hp_loss"] = True
        rejected = bridge.build_options(self.raw(
            "Cursed Tome", self.options_from_contracts(wrong_type),
            ["complete", "stop"], relics=owned_relics,
        ))
        self.assertTrue(all(
            item["target"]["kind"] == "event_option_unbound"
            for item in rejected
        ))

    def test_mausoleum_contract_requires_exact_writhe_preview(self):
        contracts = self.mausoleum_contracts()
        writhe = {"id": "Writhe", "type": "CURSE", "rarity": "CURSE"}
        options = self.options_from_contracts(
            contracts, cards={0: writhe}
        )
        ordered = bridge.build_options(self.raw(
            "The Mausoleum", options, ["open", "leave"]
        ))
        reordered = bridge.build_options(self.raw(
            "The Mausoleum", list(reversed(options)), ["open", "leave"]
        ))
        self.assertEqual(
            ["OPEN", "LEAVE"],
            [
                item["target"]["event_contract"]["option_kind"]
                for item in ordered
            ],
        )
        self.assertEqual(
            [item["option_id"] for item in ordered],
            [item["option_id"] for item in reordered],
        )
        self.assertEqual(
            50,
            ordered[0]["target"]["event_contract"]["parameters"][
                "curse_probability_percent"
            ],
        )

        missing_preview = copy.deepcopy(options)
        del missing_preview[0]["card"]
        wrong_preview = copy.deepcopy(options)
        wrong_preview[0]["card"]["id"] = "Pain"
        instance_mismatch = copy.deepcopy(options)
        instance_mismatch[1]["event_contract"]["instance_parameters"][
            "curse_probability_percent"
        ] = 100
        instance_mismatch[1]["event_contract"]["parameters"] = {}
        for malformed in (
            missing_preview, wrong_preview, instance_mismatch,
        ):
            with self.subTest(malformed=malformed):
                rejected = bridge.build_options(self.raw(
                    "The Mausoleum", malformed, ["open", "leave"]
                ))
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option_unbound"
                    for item in rejected
                ))

    def test_knowing_skull_contract_binds_private_costs_not_text(self):
        contracts = self.knowing_skull_contracts(
            costs=(7, 8, 9, 6)
        )
        options = self.options_from_contracts(contracts, disabled={0})
        ordered = bridge.build_options(self.raw(
            "Knowing Skull", options,
            ["gold-localized", "card-localized", "leave-localized"],
        ))
        reordered = bridge.build_options(self.raw(
            "Knowing Skull", list(reversed(options)),
            ["gold-localized", "card-localized", "leave-localized"],
        ))

        self.assertEqual(
            [1, 2, 3],
            [item["target"]["original_button_index"] for item in ordered],
        )
        self.assertEqual(
            ["TAKE_GOLD", "TAKE_CARD", "LEAVE"],
            [
                item["target"]["event_contract"]["option_kind"]
                for item in ordered
            ],
        )
        self.assertEqual(
            [item["option_id"] for item in ordered],
            [item["option_id"] for item in reordered],
        )
        self.assertEqual(
            9,
            ordered[0]["target"]["event_contract"]["parameters"][
                "hp_loss"
            ],
        )
        self.assertNotIn("localized", bridge.canonical(ordered[0]["target"]))

    def test_knowing_skull_contract_tampering_fails_closed(self):
        contracts = self.knowing_skull_contracts(
            costs=(6, 8, 9, 6)
        )
        options = self.options_from_contracts(contracts)
        malformed_surfaces = []

        wrong_parameter = copy.deepcopy(options)
        wrong_parameter[1]["event_contract"]["parameters"]["hp_loss"] = 8
        malformed_surfaces.append(wrong_parameter)
        mixed_instance = copy.deepcopy(options)
        mixed_instance[2]["event_contract"]["instance_parameters"][
            "leave_cost"
        ] = 7
        malformed_surfaces.append(mixed_instance)
        wrong_reward = copy.deepcopy(options)
        wrong_reward[0]["event_contract"]["parameters"][
            "reward_kind"
        ] = "NAMED_POTION"
        malformed_surfaces.append(wrong_reward)
        bool_cost = copy.deepcopy(options)
        bool_cost[3]["event_contract"]["instance_parameters"][
            "leave_cost"
        ] = True
        malformed_surfaces.append(bool_cost)

        for malformed in malformed_surfaces:
            with self.subTest(malformed=malformed):
                rejected = bridge.build_options(self.raw(
                    "Knowing Skull", malformed,
                    ["potion", "gold", "card", "leave"],
                ))
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option_unbound"
                    for item in rejected
                ))

    def test_repeatable_event_contracts_bind_dynamic_state_and_fail_closed(self):
        cases = [
            ("Dead Adventurer", self.dead_adventurer_contracts(), {}),
            ("Scrap Ooze", self.scrap_ooze_contracts(), {
                "ascension_level": 0,
            }),
            ("Face Trader", self.face_trader_contracts(), {
                "ascension_level": 0, "max_hp": 80, "relics": [],
            }),
        ]
        for event_id, contracts, game_fields in cases:
            with self.subTest(event_id=event_id):
                raw = self.raw(
                    event_id, self.options_from_contracts(contracts),
                    [f"choice-{index}" for index in range(len(contracts))],
                )
                raw["game_state"].update(game_fields)
                built = bridge.build_options(raw)
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option"
                    for item in built
                ))
                malformed = copy.deepcopy(contracts)
                malformed[0]["parameters"]["forged"] = 1
                raw["game_state"]["screen_state"]["options"] = (
                    self.options_from_contracts(malformed)
                )
                rejected = bridge.build_options(raw)
                self.assertTrue(all(
                    item["target"]["kind"] == "event_option_unbound"
                    for item in rejected
                ))

        guaranteed = self.scrap_ooze_contracts()
        for event_contract in guaranteed:
            event_contract["instance_parameters"].update({
                "relic_chance_percent_displayed": 105,
                "damage": 11,
                "total_damage_dealt": 52,
            })
        guaranteed[0]["parameters"].update({
            "hp_loss": 11,
            "success_roll_min_inclusive": -6,
        })
        raw = self.raw(
            "Scrap Ooze", self.options_from_contracts(guaranteed),
            ["reach", "leave"],
        )
        raw["game_state"]["ascension_level"] = 0
        built = bridge.build_options(raw)
        self.assertTrue(all(
            item["target"]["kind"] == "event_option" for item in built
        ))

        for total_damage in (7, 12):
            with self.subTest(
                event_id="Scrap Ooze", stage="RESULT",
                total_damage=total_damage,
            ):
                result_contracts = self.scrap_ooze_contracts("RESULT")
                result_contracts[0]["instance_parameters"][
                    "total_damage_dealt"
                ] = total_damage
                result_raw = self.raw(
                    "Scrap Ooze",
                    self.options_from_contracts(result_contracts),
                    ["continue"],
                )
                result_raw["game_state"]["ascension_level"] = 0
                result = bridge.build_options(result_raw)
                self.assertEqual(
                    "event_option", result[0]["target"]["kind"]
                )

        invalid_result = self.scrap_ooze_contracts("RESULT")
        invalid_result[0]["instance_parameters"]["total_damage_dealt"] = 11
        invalid_raw = self.raw(
            "Scrap Ooze", self.options_from_contracts(invalid_result),
            ["continue"],
        )
        invalid_raw["game_state"]["ascension_level"] = 0
        invalid = bridge.build_options(invalid_raw)
        self.assertEqual(
            "event_option_unbound", invalid[0]["target"]["kind"]
        )

    def test_duplicator_choice_binds_authoritative_grid_parent_context(self):
        contracts = self.duplicator_contracts()
        raw = self.raw(
            "Duplicator", self.options_from_contracts(contracts),
            ["duplicate", "leave"],
        )
        raw["game_state"].update({"ascension_level": 0, "relics": []})
        options = bridge.build_options(raw)
        state = {
            "phase": "EVENT", "options": options,
            "game_state": raw["game_state"],
        }
        context = bridge.accepted_grid_followup_context({
            "action": "choose", "option_id": options[0]["option_id"],
        }, state)

        self.assertEqual("EVENT", context["parent_phase"])
        self.assertEqual("duplicate", context["operation"])
        self.assertEqual(1, context["select_count"])
        self.assertEqual(
            "DUPLICATE", context["event_contract"]["option_kind"]
        )

    def test_bonfire_stages_bind_and_offer_card_opens_remove_grid(self):
        for stage in ("INTRO", "CHOOSE", "COMPLETE"):
            with self.subTest(stage=stage):
                contracts = self.bonfire_contracts(stage)
                raw = self.raw(
                    "Bonfire Elementals",
                    self.options_from_contracts(contracts),
                    ["untrusted"],
                )
                raw["game_state"].update({
                    "ascension_level": 0,
                    "event_class": bridge.BONFIRE_EVENT_CLASS,
                })
                options = bridge.build_options(raw)
                self.assertEqual(
                    "event_option", options[0]["target"]["kind"]
                )
                self.assertEqual(
                    stage,
                    options[0]["target"]["event_contract"]["event_stage"],
                )
                if stage == "CHOOSE":
                    context = bridge.accepted_grid_followup_context({
                        "action": "choose",
                        "option_id": options[0]["option_id"],
                    }, {
                        "phase": "EVENT",
                        "options": options,
                        "game_state": raw["game_state"],
                    })
                    self.assertEqual("remove", context["operation"])
                    self.assertEqual(1, context["select_count"])


class AttemptStateBindingTests(unittest.TestCase):
    @staticmethod
    def raw(seed=123, screen_type="NONE"):
        return {
            "available_commands": ["state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "IRONCLAD",
                "ascension_level": 0,
                "seed": seed,
                "is_standard_run": True,
                "screen_type": screen_type,
                "room_phase": "COMPLETE",
                "potions": [],
            },
        }

    @staticmethod
    def context(seed=123):
        context = {
            "schema_version": 2,
            "policy_version": "fast-policy-v5",
            "goal_mode": "HEART",
            "attempt_id": "attempt-bound",
            "run_id": f"IRONCLAD:0:{seed}",
            "seed": seed,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "decision-bound",
            "controller_hash": "controller-bound",
            "selection_id": "selection-bound",
            "terminal_state_seq": None,
            "selection": {
                "selection_id": "selection-bound",
                "decision_hash": "decision-bound",
                "controller_hash": "controller-bound",
                "policy_version": "fast-policy-v5",
                "character": "IRONCLAD",
                "ascension_level": 0,
                "run_type": "standard",
                "goal_mode": "HEART",
                "algorithm": "beta-thompson-v1",
            },
        }
        context["selection_digest"] = bridge.pre_run_binding.selection_digest(
            context["selection"]
        )
        return context

    def test_terminal_state_embeds_exact_attempt_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            context_path.write_text(
                json.dumps(self.context()), encoding="utf-8"
            )
            with mock.patch.object(
                bridge, "RUN_CONTEXT_PATH", context_path
            ):
                state = bridge.enrich_state(
                    self.raw(screen_type="GAME_OVER"), 91
                )

        for field in (
            "attempt_id", "run_id", "seed", "character",
            "ascension_level", "run_type", "decision_hash",
            "controller_hash", "policy_version", "selection_id",
            "selection_digest",
        ):
            self.assertEqual(self.context()[field], state[field])
        self.assertEqual(91, state["terminal_state_seq"])

    def test_stale_context_is_not_projected_into_another_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            context_path.write_text(
                json.dumps(self.context(seed=122)), encoding="utf-8"
            )
            with mock.patch.object(
                bridge, "RUN_CONTEXT_PATH", context_path
            ):
                state = bridge.enrich_state(self.raw(seed=123), 92)

        self.assertNotIn("attempt_id", state)
        self.assertNotIn("decision_hash", state)

    def test_context_with_mismatched_selection_hash_is_not_projected(self):
        context = self.context()
        context["selection"]["decision_hash"] = "stale-decision"
        with tempfile.TemporaryDirectory() as directory:
            context_path = Path(directory) / "run-context.json"
            context_path.write_text(json.dumps(context), encoding="utf-8")
            with mock.patch.object(
                bridge, "RUN_CONTEXT_PATH", context_path
            ):
                state = bridge.enrich_state(self.raw(), 93)

        self.assertNotIn("attempt_id", state)
        self.assertNotIn("selection_id", state)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()

    def assert_rejected(self, payload, message):
        with self.assertRaisesRegex(bridge.CommandRejected, message):
            bridge.resolve_command(payload, self.state)

    def test_valid_choice_binds_duplicate_card_by_uuid(self):
        command, receipt = bridge.resolve_command(payload_for(self.state), self.state)
        self.assertEqual("CHOOSE 1", command)
        self.assertEqual(receipt["requested_target_id"], receipt["resolved_target_id"])
        self.assertEqual("card-b", self.state["options"][1]["target"]["card_instance_id"])
        self.assertEqual(
            TEST_ATTEMPT_BINDING,
            {
                field: receipt[field]
                for field in bridge.ATTEMPT_BINDING_FIELDS
            },
        )

    def test_every_in_game_attempt_binding_field_is_required(self):
        for field in bridge.ATTEMPT_BINDING_FIELDS:
            with self.subTest(field=field):
                payload = payload_for(self.state)
                del payload[field]
                with self.assertRaises(bridge.CommandRejected):
                    bridge.resolve_command(payload, self.state)

    def test_every_in_game_attempt_binding_field_is_type_and_value_exact(self):
        for field in bridge.ATTEMPT_BINDING_FIELDS:
            with self.subTest(field=field):
                expected = self.state[field]
                wrong = (
                    str(expected)
                    if type(expected) is int
                    else f"{expected}-wrong"
                )
                payload = payload_for(self.state, **{field: wrong})
                with self.assertRaises(bridge.CommandRejected):
                    bridge.resolve_command(payload, self.state)

    def test_wrong_state_version_is_rejected(self):
        self.assert_rejected(payload_for(self.state, expected_seq=40), "stale state")

    def test_wrong_decision_id_is_rejected(self):
        self.assert_rejected(payload_for(self.state, decision_id="decision:wrong"), "decision_id mismatch")

    def test_wrong_index_is_rejected(self):
        self.assert_rejected(payload_for(self.state, choice_index=0), "choice index")

    def test_wrong_target_id_is_rejected(self):
        self.assert_rejected(payload_for(self.state, option_id="option:wrong"), "does not resolve uniquely")

    def test_wrong_policy_version_is_rejected(self):
        self.assert_rejected(payload_for(self.state, policy_version="shared-combat-v4"), "policy_version")

    def test_option_ids_do_not_collapse_duplicate_names(self):
        self.assertNotEqual(self.state["options"][0]["option_id"], self.state["options"][1]["option_id"])

    def test_event_option_binding_carries_card_preview_semantics(self):
        preview = {
            "id": "Apotheosis",
            "name": "Apotheosis",
            "uuid": "note-apotheosis",
            "type": "SKILL",
            "rarity": "RARE",
            "upgrades": 0,
            "cost": 2,
        }
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "EVENT",
                "screen_state": {
                    "event_id": "A Note For Yourself",
                    "options": [
                        {
                            "text": "[Take] Trade a card.",
                            "label": "Take",
                            "disabled": False,
                            "original_button_index": 0,
                            "choice_index": 0,
                            "card": preview,
                        }
                    ],
                },
                "choice_list": ["Take"],
                "room_phase": "INCOMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

        option = bridge.enrich_state(raw, 42)["options"][0]

        self.assertEqual("Apotheosis", option["target"]["card"]["id"])
        self.assertEqual("note-apotheosis", option["target"]["card"]["card_instance_id"])

    def test_known_event_progress_is_bound_into_every_option_target(self):
        event_class = (
            "com.megacrit.cardcrawl.events.beyond.WindingHalls"
        )
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "EVENT",
                "screen_state": {
                    "event_id": "Winding Halls",
                    "event_class": event_class,
                    "screen_num": 1,
                    "options": [{
                        "text": f"choice {index}",
                        "label": f"choice {index}",
                        "disabled": False,
                        "original_button_index": index,
                        "choice_index": index,
                    } for index in range(3)],
                },
                "choice_list": [f"choice {index}" for index in range(3)],
                "room_phase": "INCOMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

        options = bridge.enrich_state(raw, 42)["options"]

        self.assertEqual(3, len(options))
        self.assertTrue(all(
            option["target"]["event_class"] == event_class
            and option["target"]["screen_num"] == 1
            for option in options
        ))

        forged = copy.deepcopy(raw)
        forged["game_state"]["screen_state"]["event_class"] = (
            "modded.events.WindingHalls"
        )
        rejected = bridge.enrich_state(forged, 43)["options"]
        self.assertTrue(all(
            "event_class" not in option["target"]
            and "screen_num" not in option["target"]
            for option in rejected
        ))

    def test_new_exact_screen_number_events_bind_only_matching_classes(self):
        cases = (
            (
                "Liars Game",
                "com.megacrit.cardcrawl.events.exordium.Sssserpent",
            ),
            (
                "Vampires",
                "com.megacrit.cardcrawl.events.city.Vampires",
            ),
            (
                "Golden Shrine",
                "com.megacrit.cardcrawl.events.shrines.GoldShrine",
            ),
        )
        for event_id, event_class in cases:
            with self.subTest(event_id=event_id):
                progress_field = (
                    "screen_num" if event_id == "Vampires"
                    else "event_stage"
                )
                progress = 1 if progress_field == "screen_num" else "COMPLETE"
                screen = {
                    "event_id": event_id,
                    "event_class": event_class,
                    progress_field: progress,
                }
                self.assertEqual(
                    {"event_class": event_class, progress_field: progress},
                    bridge._validated_event_progress(screen),
                )
                screen["event_class"] = "modded.events.Lookalike"
                self.assertEqual({}, bridge._validated_event_progress(screen))

    @staticmethod
    def _neow_reward_contract(reward_kind="HUNDRED_GOLD", drawback_kind="NONE"):
        return {
            "contract_version": 1,
            "contract_kind": "NEOW_REWARD",
            "reward_kind": reward_kind,
            "drawback_kind": drawback_kind,
            "parameters": {
                "hp_bonus": 0,
                "cursed": drawback_kind == "CURSE",
                "drawback_def_kind": (
                    None if drawback_kind == "NONE" else drawback_kind
                ),
            },
        }

    @staticmethod
    def _neow_raw(screen_options, choice_list):
        return {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "EVENT",
                "screen_state": {
                    "event_id": "Neow Event",
                    "options": screen_options,
                },
                "choice_list": choice_list,
                "room_phase": "INCOMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }

    def test_neow_contract_binding_uses_choice_index_not_container_order(self):
        gold = self._neow_reward_contract("HUNDRED_GOLD")
        transform = self._neow_reward_contract("TRANSFORM_CARD")
        options = [
            {
                "text": "localized transform", "label": "transform",
                "disabled": False, "original_button_index": 0,
                "choice_index": 0,
                "neow_contract": transform,
            },
            {
                "text": "localized gold", "label": "gold",
                "disabled": False, "original_button_index": 1,
                "choice_index": 1,
                "neow_contract": gold,
            },
        ]
        ordered = bridge.build_options(self._neow_raw(options, ["transform", "gold"]))
        reordered = bridge.build_options(
            self._neow_raw(list(reversed(options)), ["transform", "gold"])
        )
        self.assertEqual(
            [item["target"]["mechanism_id"] for item in ordered],
            [item["target"]["mechanism_id"] for item in reordered],
        )
        self.assertEqual(
            "TRANSFORM_CARD",
            reordered[0]["target"]["neow_contract"]["reward_kind"],
        )
        self.assertEqual(
            "HUNDRED_GOLD",
            reordered[1]["target"]["neow_contract"]["reward_kind"],
        )
        self.assertNotIn("localized", bridge.canonical(reordered[0]["target"]))

    def test_neow_free_choices_restore_gson_omitted_null_contract_field(self):
        free_kinds = (
            "ONE_RANDOM_RARE_CARD", "RANDOM_COMMON_RELIC", "BOSS_RELIC",
        )
        contracts = []
        for reward_kind in free_kinds:
            contract = self._neow_reward_contract(reward_kind)
            contract["parameters"].pop("drawback_def_kind")
            contracts.append(contract)
        costly = self._neow_reward_contract(
            "RANDOM_COLORLESS_2", "TEN_PERCENT_HP_LOSS"
        )
        contracts.insert(2, costly)
        options = [
            {
                "text": f"localized {index}",
                "label": f"option {index}",
                "disabled": False,
                "original_button_index": index,
                "choice_index": index,
                "neow_contract": contract,
            }
            for index, contract in enumerate(contracts)
        ]

        built = bridge.build_options(
            self._neow_raw(options, [f"option {index}" for index in range(4)])
        )

        self.assertEqual(
            ["event_option"] * 4,
            [item["target"]["kind"] for item in built],
        )
        self.assertEqual(
            ["ONE_RANDOM_RARE_CARD", "RANDOM_COMMON_RELIC",
             "RANDOM_COLORLESS_2", "BOSS_RELIC"],
            [
                item["target"]["neow_contract"]["reward_kind"]
                for item in built
            ],
        )
        self.assertIsNone(
            built[0]["target"]["neow_contract"]["parameters"][
                "drawback_def_kind"
            ]
        )

    def test_neow_real_drawback_cannot_omit_definition(self):
        malformed = self._neow_reward_contract(
            "RANDOM_COLORLESS_2", "TEN_PERCENT_HP_LOSS"
        )
        malformed["parameters"].pop("drawback_def_kind")
        built = bridge.build_options(self._neow_raw([{
            "text": "costly", "label": "costly", "disabled": False,
            "original_button_index": 0, "choice_index": 0,
            "neow_contract": malformed,
        }], ["costly"]))
        self.assertEqual("event_option_unbound", built[0]["target"]["kind"])

    def test_neow_missing_or_duplicate_choice_index_is_unbound(self):
        contract = self._neow_reward_contract()
        for screen_options in (
            [{
                "text": "gold", "label": "gold", "disabled": False,
                "original_button_index": 0,
                "neow_contract": contract,
            }],
            [
                {
                    "text": "gold a", "label": "gold", "disabled": False,
                    "original_button_index": 0, "choice_index": 0,
                    "neow_contract": contract,
                },
                {
                    "text": "gold b", "label": "gold", "disabled": False,
                    "original_button_index": 1, "choice_index": 0,
                    "neow_contract": contract,
                },
            ],
        ):
            with self.subTest(option_count=len(screen_options)):
                built = bridge.build_options(self._neow_raw(screen_options, ["gold"]))
                self.assertEqual("event_option_unbound", built[0]["target"]["kind"])
                self.assertNotIn("mechanism_id", built[0]["target"])

    def test_neow_malformed_contract_does_not_gain_mechanism_id(self):
        malformed = self._neow_reward_contract("HUNDRED_GOLD")
        malformed["reward_kind"] = "LOCALIZED_FREE_GOLD"
        built = bridge.build_options(self._neow_raw([{
            "text": "localized", "label": "localized", "disabled": False,
            "original_button_index": 0, "choice_index": 0,
            "neow_contract": malformed,
        }], ["localized"]))
        self.assertEqual("event_option_unbound", built[0]["target"]["kind"])
        self.assertEqual(
            "missing_or_invalid_neow_contract",
            built[0]["target"]["binding_status"],
        )
        self.assertNotIn("mechanism_id", built[0]["target"])

        missing = bridge.build_options(self._neow_raw([{
            "text": "localized", "label": "localized", "disabled": False,
            "original_button_index": 0, "choice_index": 0,
        }], ["localized"]))
        self.assertEqual("event_option_unbound", missing[0]["target"]["kind"])

    def test_neow_dialog_advance_is_typed_no_resource_effect(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "NEOW_DIALOG_ADVANCE",
            "screen_num": 1,
            "resource_effect": "NONE",
        }
        built = bridge.build_options(self._neow_raw([{
            "text": "continue", "label": "continue", "disabled": False,
            "original_button_index": 0, "choice_index": 0,
            "neow_contract": contract,
        }], ["continue"]))
        target = built[0]["target"]
        self.assertEqual(contract, target["neow_contract"])
        self.assertTrue(target["mechanism_id"].startswith("neow-mechanism:"))

    def test_event_body_page_changes_decision_with_same_continue_option(self):
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "EVENT",
                "screen_state": {
                    "event_id": "Masked Bandits",
                    "body_text": "First dialogue page",
                    "options": [{
                        "text": "[Continue]",
                        "label": "Continue",
                        "disabled": False,
                        "original_button_index": 0,
                        "choice_index": 0,
                    }],
                },
                "choice_list": ["Continue"],
                "room_phase": "EVENT",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }
        first = bridge.enrich_state(raw, 42)
        second_raw = copy.deepcopy(raw)
        second_raw["game_state"]["screen_state"]["body_text"] = (
            "Second dialogue page"
        )
        second = bridge.enrich_state(second_raw, 43)

        self.assertEqual(
            first["options"][0]["option_id"],
            second["options"][0]["option_id"],
        )
        self.assertNotEqual(first["decision_id"], second["decision_id"])

    def test_state_command_recovers_from_error_frame_without_binding(self):
        command, receipt = bridge.resolve_command(
            {"id": "state-request", "action": "state"},
            {"protocol_version": 2, "state_seq": 99, "phase": "ERROR", "error": "bad command"},
        )
        self.assertEqual("STATE", command)
        self.assertEqual("action:state", receipt["requested_target_id"])


class PotionTargetBindingTests(unittest.TestCase):
    @staticmethod
    def state(*, requires_target, half_dead=False, is_gone=False):
        raw = {
            "available_commands": ["potion", "end", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "IRONCLAD",
                "ascension_level": 0,
                "seed": 123,
                "is_standard_run": True,
                "screen_type": "NONE",
                "room_phase": "COMBAT",
                "action_phase": "WAITING_ON_USER",
                "floor": 4,
                "potions": [{
                    "id": "Fire Potion" if requires_target else "Strength Potion",
                    "name": "Fire Potion" if requires_target else "Strength Potion",
                    "requires_target": requires_target,
                    "can_use": True,
                    "can_discard": True,
                }],
                "combat_state": {
                    "turn": 1,
                    "hand": [],
                    "monsters": [{
                        "id": "JawWorm",
                        "name": "Jaw Worm",
                        "current_hp": 40,
                        "max_hp": 40,
                        "intent": "ATTACK",
                        "half_dead": half_dead,
                        "is_gone": is_gone,
                    }],
                },
            },
        }
        return with_attempt_binding(bridge.enrich_state(raw, 70))

    @staticmethod
    def payload(state, operation, *, include_enemy=True):
        potion = state["game_state"]["potions"][0]
        payload = {
            "id": "potion-request",
            "action": "potion",
            "policy_version": "fast-policy-v5",
            "expected_seq": state["state_seq"],
            "decision_id": state["decision_id"],
            "phase": state["phase"],
            "operation": operation,
            "potion_instance_id": potion["potion_instance_id"],
        }
        if include_enemy:
            payload["enemy_instance_id"] = (
                state["game_state"]["combat_state"]["monsters"][0]
                ["enemy_instance_id"]
            )
        return with_payload_binding(payload, state)

    def test_non_targeted_use_rejects_enemy_binding(self):
        state = self.state(requires_target=False)
        with self.assertRaisesRegex(
            bridge.CommandRejected, "non-targeted potion operation"
        ):
            bridge.resolve_command(self.payload(state, "use"), state)

    def test_discard_rejects_enemy_binding_even_for_targeted_potion(self):
        state = self.state(requires_target=True)
        with self.assertRaisesRegex(
            bridge.CommandRejected, "non-targeted potion operation"
        ):
            bridge.resolve_command(self.payload(state, "discard"), state)

    def test_targeted_use_rejects_gone_or_half_dead_enemy(self):
        for field in ("is_gone", "half_dead"):
            with self.subTest(field=field):
                state = self.state(
                    requires_target=True,
                    is_gone=field == "is_gone",
                    half_dead=field == "half_dead",
                )
                with self.assertRaisesRegex(
                    bridge.CommandRejected, "not a legal target"
                ):
                    bridge.resolve_command(self.payload(state, "use"), state)

    def test_targeted_use_binds_the_exact_live_enemy(self):
        state = self.state(requires_target=True)
        command, receipt = bridge.resolve_command(
            self.payload(state, "use"), state
        )

        enemy_id = (
            state["game_state"]["combat_state"]["monsters"][0]
            ["enemy_instance_id"]
        )
        self.assertEqual("POTION use 0 0", command)
        self.assertEqual(enemy_id, receipt["requested_enemy_id"])
        self.assertEqual(enemy_id, receipt["resolved_enemy_id"])


class ReturnCommandBindingTests(unittest.TestCase):
    def resolve_return(self, available_commands):
        raw = {
            "available_commands": available_commands,
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "CARD_REWARD",
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "screen_state": {"cards": [], "skip_available": True},
                "choice_list": [],
                "potions": [],
            },
        }
        state = with_attempt_binding(bridge.enrich_state(raw, 52))
        payload = {
            "id": "return-request",
            "action": "return",
            "policy_version": "fast-policy-v5",
            "expected_seq": state["state_seq"],
            "decision_id": state["decision_id"],
            "phase": state["phase"],
            "target_id": "action:return",
        }
        with_payload_binding(payload, state)
        return bridge.resolve_command(payload, state)

    def test_card_reward_return_uses_advertised_skip_command(self):
        command, receipt = self.resolve_return(["skip", "state"])
        self.assertEqual("SKIP", command)
        self.assertEqual("action:return", receipt["requested_target_id"])
        self.assertEqual(receipt["requested_target_id"], receipt["resolved_target_id"])

    def test_return_remains_return_when_advertised(self):
        command, _ = self.resolve_return(["return", "state"])
        self.assertEqual("RETURN", command)


class OverlayRecoveryBindingTests(unittest.TestCase):
    def setUp(self):
        raw = {
            "available_commands": ["key", "click", "wait", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "NONE",
                "screen_name": "SETTINGS",
                "is_screen_up": True,
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }
        self.state = with_attempt_binding(bridge.enrich_state(raw, 60))

    def payload(self, **changes):
        payload = {
            "id": "overlay-recovery",
            "action": "key",
            "policy_version": "fast-policy-v5",
            "expected_seq": self.state["state_seq"],
            "decision_id": self.state["decision_id"],
            "phase": self.state["phase"],
            "key": "CANCEL",
            "target_id": "key:CANCEL",
        }
        with_payload_binding(payload, self.state)
        payload.update(changes)
        return payload

    def test_settings_is_a_distinct_bound_phase(self):
        self.assertEqual("OVERLAY_SETTINGS", self.state["phase"])

    def test_bound_cancel_key_closes_recognized_overlay(self):
        command, receipt = bridge.resolve_command(self.payload(), self.state)
        self.assertEqual("KEY CANCEL", command)
        self.assertEqual("key:CANCEL", receipt["requested_target_id"])
        self.assertEqual(
            receipt["requested_target_id"], receipt["resolved_target_id"]
        )

    def test_arbitrary_key_is_rejected(self):
        with self.assertRaisesRegex(
            bridge.CommandRejected, "only the CANCEL recovery key"
        ):
            bridge.resolve_command(
                self.payload(key="MAP", target_id="key:MAP"),
                self.state,
            )

    def test_overlay_identity_changes_decision_binding(self):
        raw = {
            "available_commands": ["key", "click", "wait", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "NONE",
                "screen_name": "MASTER_DECK_VIEW",
                "is_screen_up": True,
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }
        other = bridge.enrich_state(raw, 61)
        self.assertNotEqual(self.state["decision_id"], other["decision_id"])


class ReceiptCompletionTests(unittest.TestCase):
    def pending(self, action="choose"):
        return {
            "payload": {"action": action},
            "accepted_decision_id": "decision:old",
        }

    def combat_reward_state(self, card_count, decision_id="decision:new"):
        return {
            "phase": "COMBAT_REWARD",
            "decision_id": decision_id,
            "game_state": {
                "screen_state": {
                    "rewards": [
                        {"reward_type": "CARD"}
                        for _ in range(card_count)
                    ],
                },
            },
        }

    def event_state(
        self, deck_ids, decision_id="decision:new", event_id="Ghosts",
        ascension_level=0,
    ):
        return {
            "phase": "EVENT",
            "decision_id": decision_id,
            "game_state": {
                "ascension_level": ascension_level,
                "deck": [
                    {"id": card_id, "uuid": f"card-{index}"}
                    for index, card_id in enumerate(deck_ids)
                ],
                "screen_state": {"event_id": event_id},
            },
        }

    def test_non_ready_frame_does_not_complete_receipt(self):
        state = {"decision_id": "decision:new"}
        raw = {"ready_for_command": False}
        self.assertEqual("wait", bridge.pending_frame_outcome(self.pending(), state, raw))

    def test_map_choose_completes_on_non_ready_room_transition(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "MAP",
            "accepted_decision_id": "decision:map",
        })
        state = {
            "phase": "COMBAT_INITIALIZING",
            "decision_id": "decision:combat",
            "ready_for_command": False,
            "game_state": {"combat_state": {"monsters": []}},
        }
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(
                pending,
                state,
                {"ready_for_command": False},
            ),
        )

    def test_hand_select_choose_dispatch_waits_for_two_stable_frames(self):
        pending = {
            "payload": {"action": "choose", "option_id": "option:defend"},
            "accepted_decision_id": "decision:gambler",
            "accepted_phase": "HAND_SELECT",
            "dispatch_pending": True,
            "dispatch_frames": 0,
            "dispatch_stable_frames": 0,
            "dispatch_signature": None,
        }
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:gambler",
            "ready_for_command": True,
            "available_commands": ["choose", "confirm", "wait", "state"],
            "options": [
                {
                    "option_id": "option:neutralize",
                    "choice_index": 0,
                    "target": {"kind": "card", "card_instance_id": "card-neutralize"},
                },
                {
                    "option_id": "option:defend",
                    "choice_index": 1,
                    "target": {"kind": "card", "card_instance_id": "card-defend"},
                },
            ],
            "game_state": {
                "screen_state": {
                    "hand": [
                        {"card_instance_id": "card-neutralize"},
                        {"card_instance_id": "card-defend"},
                    ],
                    "selected": [{"card_instance_id": "card-dazed"}],
                },
            },
        }

        self.assertEqual("settle", bridge.hand_select_dispatch_frame(pending, state))
        self.assertEqual(1, pending["dispatch_stable_frames"])
        self.assertEqual("dispatch", bridge.hand_select_dispatch_frame(pending, state))
        self.assertFalse(pending["dispatch_pending"])
        self.assertEqual("CHOOSE 1", pending["raw_command"])
        self.assertEqual("card-defend", pending["accepted_card_instance_id"])

    def test_hand_select_choose_rebinds_after_decision_surface_refresh(self):
        pending = {
            "payload": {"action": "choose", "option_id": "option:defend"},
            "accepted_decision_id": "decision:before-refresh",
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": "card-defend",
            "dispatch_pending": True,
            "dispatch_frames": 0,
            "dispatch_stable_frames": 0,
            "dispatch_signature": None,
        }
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:after-refresh",
            "ready_for_command": True,
            "available_commands": ["choose", "confirm", "wait", "state"],
            "options": [
                {
                    "option_id": "option:defend",
                    "choice_index": 1,
                    "target": {"kind": "card", "card_instance_id": "card-defend"},
                },
            ],
            "game_state": {
                "screen_state": {
                    "hand": [
                        {"card_instance_id": "card-neutralize"},
                        {"card_instance_id": "card-defend"},
                    ],
                    "selected": [],
                },
            },
        }

        self.assertEqual("settle", bridge.hand_select_dispatch_frame(pending, state))
        self.assertEqual("decision:after-refresh", pending["accepted_decision_id"])
        self.assertEqual(1, pending["dispatch_stable_frames"])
        self.assertEqual("dispatch", bridge.hand_select_dispatch_frame(pending, state))
        self.assertFalse(pending["dispatch_pending"])
        self.assertEqual("CHOOSE 1", pending["raw_command"])

    def test_hand_select_choose_waits_for_refreshed_options(self):
        pending = {
            "payload": {"action": "choose", "option_id": "option:defend"},
            "accepted_decision_id": "decision:before-refresh",
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": "card-defend",
            "dispatch_pending": True,
            "dispatch_frames": 0,
            "dispatch_stable_frames": 0,
            "dispatch_signature": None,
        }
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:after-refresh",
            "ready_for_command": True,
            "available_commands": ["choose", "confirm", "wait", "state"],
            "options": [],
            "game_state": {
                "screen_state": {
                    "hand": [{"card_instance_id": "card-defend"}],
                    "selected": [],
                },
            },
        }

        self.assertEqual("wait", bridge.hand_select_dispatch_frame(pending, state))
        self.assertEqual("decision:after-refresh", pending["accepted_decision_id"])
        self.assertTrue(pending["dispatch_pending"])

    def test_unchanged_decision_does_not_complete_state_changing_action(self):
        state = {"decision_id": "decision:old"}
        raw = {"ready_for_command": True}
        self.assertEqual("unchanged", bridge.pending_frame_outcome(self.pending(), state, raw))

    def test_rest_choice_waits_for_async_campfire_settlement(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "REST",
            "accepted_room_phase": "INCOMPLETE",
            "settle_frames": 0,
        })
        state = {
            "phase": "REST",
            "decision_id": "decision:old",
            "game_state": {"room_phase": "INCOMPLETE"},
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.REST_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "rest_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_rest_choice_completes_on_room_or_phase_advance(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "REST",
            "accepted_room_phase": "INCOMPLETE",
            "settle_frames": 1,
        })
        raw = {"ready_for_command": True}
        room_advanced = {
            "phase": "REST",
            "decision_id": "decision:old",
            "game_state": {"room_phase": "COMPLETE"},
        }
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, room_advanced, raw),
        )
        phase_advanced = {
            "phase": "MAP",
            "decision_id": "decision:old",
            "game_state": {"room_phase": "INCOMPLETE"},
        }
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, phase_advanced, raw),
        )

    def test_changed_ready_decision_completes_action(self):
        state = {"decision_id": "decision:new"}
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending(), state, raw))

    def test_same_turn_play_waits_one_frame_after_decision_change(self):
        pending = self.pending(action="play")
        pending.update({
            "accepted_phase": "COMBAT_TURN_3",
            "settle_frames": 0,
        })
        state = {
            "phase": "COMBAT_TURN_3",
            "decision_id": "decision:new",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 3},
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.COMBAT_PLAY_SETTLE_FRAMES
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_play_phase_transition_bypasses_same_turn_barrier(self):
        pending = self.pending(action="play")
        pending.update({
            "accepted_phase": "COMBAT_TURN_3",
            "accepted_turn": 3,
            "settle_frames": 0,
        })
        state = {
            "phase": "GRID",
            "decision_id": "decision:new",
            "game_state": {"room_phase": "COMBAT"},
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_play_waits_for_backwards_transient_turn_frame(self):
        pending = self.pending(action="play")
        pending.update({
            "accepted_phase": "COMBAT_TURN_3",
            "accepted_turn": 3,
            "settle_frames": 0,
        })
        state = {
            "phase": "COMBAT_TURN_1",
            "decision_id": "decision:new",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 1},
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.COMBAT_PLAY_SETTLE_FRAMES
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_hand_select_proceed_waits_for_exhaust_settlement(self):
        pending = self.pending(action="proceed")
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "settle_frames": 0,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": "ExhaustAction",
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = (
            bridge.HAND_SELECT_PROCEED_SETTLE_TIMEOUT_FRAMES
        )
        self.assertEqual(
            "hand_select_proceed_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_hand_select_proceed_completes_on_new_selection_surface(self):
        pending = self.pending(action="proceed")
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "accepted_decision_id": "decision:old",
            "settle_frames": 0,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:new",
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": "DiscardAction",
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_delayed_error_for_previous_command_does_not_fail_new_start(self):
        pending = self.pending(action="start")
        pending["raw_command"] = "START IRONCLAD 0"
        state = {"decision_id": "decision:error"}
        raw = {
            "error": (
                "Invalid command: proceed. Possible commands: [start, state]"
            )
        }

        self.assertEqual(
            "stale_error",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_error_for_pending_command_still_fails_that_command(self):
        pending = self.pending(action="start")
        pending["raw_command"] = "START IRONCLAD 0"
        state = {"decision_id": "decision:error"}
        raw = {
            "error": (
                "Invalid command: start. Possible commands: [state]"
            )
        }

        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_end_completes_when_well_laid_plans_opens_hand_select(self):
        pending = self.pending(action="end")
        pending.update({
            "accepted_phase": "COMBAT_TURN_1",
            "accepted_room_phase": "COMBAT",
            "accepted_turn": 1,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 1},
                "screen_state": {"cards": []},
            },
        }
        raw = {"ready_for_command": True}

        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_exact_grid_selection_completes_without_decision_change(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "GRID",
            "accepted_card_instance_id": "card-b",
        })
        state = {
            "decision_id": "decision:old",
            "game_state": {
                "screen_state": {
                    "selected_cards": [{"card_instance_id": "card-b"}],
                },
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_exact_hand_selection_completes_independently_of_next_command_readiness(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": "card-b",
            "settle_frames": 0,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
            "game_state": {
                "screen_state": {
                    "selected": [{"card_instance_id": "card-b"}],
                    "max_cards": 1,
                    "can_pick_zero": False,
                },
            },
        }
        raw = {
            "ready_for_command": False,
            "available_commands": ["confirm", "state"],
        }

        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = 8
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.HAND_SELECT_CHOOSE_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, state, raw),
        )
        raw["ready_for_command"] = True
        pending["settle_frames"] = 8
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_hand_select_choose_waits_when_target_is_still_unselected(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": "card-b",
            "settle_frames": 0,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
            "game_state": {
                "screen_state": {
                    "hand": [{"card_instance_id": "card-b"}],
                    "selected": [{"card_instance_id": "card-a"}],
                },
            },
        }
        raw = {"ready_for_command": True}

        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.HAND_SELECT_CHOOSE_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "hand_select_choose_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_state_resync_completes_on_non_ready_hand_selection_frame(self):
        pending = self.pending(action="state")
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": None,
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:changed-by-selection",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_state": {
                    "selected": [{"card_instance_id": "dodge-and-roll"}],
                    "max_cards": 1,
                    "can_pick_zero": False,
                },
            },
        }
        raw = {
            "in_game": True,
            "ready_for_command": False,
            "available_commands": ["confirm", "state"],
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "HAND_SELECT",
                "combat_state": {
                    "monsters": [{
                        "current_hp": 294,
                        "intent": "ATTACK_DEBUFF",
                        "half_dead": False,
                        "is_gone": False,
                    }],
                },
            },
        }

        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, state, raw)
        )

    def test_non_ready_hand_selection_requires_exact_selected_instance(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "HAND_SELECT",
            "accepted_card_instance_id": "card-b",
        })
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
            "game_state": {
                "screen_state": {
                    "selected": [{"card_instance_id": "card-a"}],
                },
            },
        }

        self.assertEqual(
            "wait",
            bridge.pending_frame_outcome(
                pending, state, {"ready_for_command": False}
            ),
        )

    def test_different_grid_selection_does_not_complete(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "GRID",
            "accepted_card_instance_id": "card-b",
        })
        state = {
            "decision_id": "decision:old",
            "game_state": {
                "screen_state": {
                    "selected_cards": [{"card_instance_id": "card-a"}],
                },
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual("unchanged", bridge.pending_frame_outcome(pending, state, raw))

    def test_known_match_game_card_completes_without_decision_change(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Match and Keep!",
            "match_game_context": {
                "second_flip": False,
                "chosen_card_id": "PanicButton",
                "known_match": False,
                "deck_size_before": 1,
            },
        })
        state = {
            "phase": "EVENT",
            "decision_id": "decision:old",
            "options": [
                {"option_id": "option:a"},
                {"option_id": "option:b"},
            ],
            "game_state": {
                "deck": [{"id": "Strike_R"}],
                "screen_state": {"event_id": "Match and Keep!"},
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_match_board_parity_and_known_labels_identify_second_matching_flip(self):
        state = self.event_state(["Strike_R"], decision_id="decision:old", event_id="Match and Keep!")
        state["options"] = [
            {
                "option_id": "option:panic",
                "choice_index": 0,
                "label": "panicbutton",
                "target": {"kind": "event_option", "label": "PanicButton"},
            },
            {
                "option_id": "option:demon",
                "choice_index": 1,
                "label": "demon form",
                "target": {"kind": "event_option", "label": "Demon Form"},
            },
            {
                "option_id": "option:hidden",
                "choice_index": 2,
                "label": "card8",
                "target": {"kind": "event_option", "label": "card8"},
            },
        ]
        context = bridge.match_game_choice_context(
            {"action": "choose", "option_id": "option:panic"},
            state,
            first_flip_card_id="PanicButton",
        )
        self.assertTrue(context["second_flip"])
        self.assertTrue(context["known_match"])
        self.assertEqual("PanicButton", context["chosen_card_id"])
        self.assertEqual(1, context["deck_size_before"])
        self.assertEqual(
            {
                "kind": "deck_card_gain",
                "event_id": "Match and Keep!",
                "card_id": "PanicButton",
                "expected_delta": 1,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
            bridge.match_game_reward_postcondition(context, state),
        )

    def test_match_even_board_click_is_first_flip_and_hidden_label_is_not_card_id(self):
        state = self.event_state(["Strike_R"], decision_id="decision:old", event_id="Match and Keep!")
        state["options"] = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": f"card{index}",
                "target": {"kind": "event_option", "label": f"card{index}"},
            }
            for index in range(4)
        ]
        context = bridge.match_game_choice_context(
            {"action": "choose", "option_id": "option:0"}, state
        )
        self.assertFalse(context["second_flip"])
        self.assertIsNone(context["chosen_card_id"])
        self.assertFalse(context["known_match"])

    def test_known_match_waits_for_exact_card_gain_even_on_leave_surface(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Match and Keep!",
            "settle_frames": 10,
            "match_game_context": {
                "second_flip": True,
                "chosen_card_id": "PanicButton",
                "known_match": True,
                "deck_size_before": 1,
            },
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Match and Keep!",
                "card_id": "PanicButton",
                "expected_delta": 1,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
        })
        state = self.event_state(["Strike_R"], event_id="Match and Keep!")
        state["options"] = [{"option_id": "option:leave"}]
        raw = {"ready_for_command": True}
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        state["game_state"]["deck"].append({"id": "PanicButton"})
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_known_match_missing_reward_times_out_instead_of_leaving(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Match and Keep!",
            "settle_frames": bridge.EVENT_SETTLE_TIMEOUT_FRAMES,
            "match_game_context": {
                "second_flip": True,
                "chosen_card_id": "PanicButton",
                "known_match": True,
                "deck_size_before": 1,
            },
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Match and Keep!",
                "card_id": "PanicButton",
                "expected_delta": 1,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
        })
        state = self.event_state(["Strike_R"], event_id="Match and Keep!")
        state["options"] = [{"option_id": "option:leave"}]
        self.assertEqual(
            "event_settlement_timeout",
            bridge.pending_frame_outcome(
                pending, state, {"ready_for_command": True}
            ),
        )

    def test_unknown_second_flip_waits_full_pair_window_before_completion(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Match and Keep!",
            "settle_frames": 0,
            "match_game_context": {
                "second_flip": True,
                "chosen_card_id": None,
                "known_match": False,
                "deck_size_before": 1,
            },
        })
        state = self.event_state(["Strike_R"], event_id="Match and Keep!")
        # The first ready frame may still expose the board rather than Leave.
        state["options"] = [
            {"option_id": f"option:{index}"}
            for index in range(8)
        ]
        raw = {"ready_for_command": True}
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        state["options"] = [{"option_id": "option:leave"}]
        pending["settle_frames"] = bridge.MATCH_UNKNOWN_PAIR_SETTLE_FRAMES - 1
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = bridge.MATCH_UNKNOWN_PAIR_SETTLE_FRAMES
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_unknown_final_match_completes_when_deck_gain_arrives(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Match and Keep!",
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
            "match_game_context": {
                "second_flip": True,
                "chosen_card_id": None,
                "known_match": False,
                "deck_size_before": 1,
            },
        })
        state = self.event_state(
            ["Strike_R", "PanicButton"], event_id="Match and Keep!"
        )
        state["options"] = [{"option_id": "option:leave"}]
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(
                pending, state, {"ready_for_command": True}
            ),
        )

    def test_opening_card_reward_captures_parent_card_count(self):
        state = self.combat_reward_state(2)
        state["options"] = [
            {
                "option_id": "option:first-card",
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "CARD"},
                },
            },
            {
                "option_id": "option:second-card",
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "CARD"},
                },
            },
        ]
        payload = {"action": "choose", "option_id": "option:first-card"}
        self.assertEqual(2, bridge.opened_card_reward_parent_count(payload, state))

    def test_non_card_combat_reward_does_not_create_card_parent_context(self):
        state = self.combat_reward_state(1)
        state["options"] = [
            {
                "option_id": "option:gold",
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "GOLD"},
                },
            },
        ]
        payload = {"action": "choose", "option_id": "option:gold"}
        self.assertIsNone(bridge.opened_card_reward_parent_count(payload, state))

    def test_ghosts_accept_captures_exact_five_apparition_gain(self):
        state = self.event_state(["Strike_B", "Apparition"], decision_id="decision:old")
        state["options"] = [
            {
                "option_id": "option:accept",
                "choice_index": 0,
                "target": {
                    "kind": "event_option",
                    "event_id": "Ghosts",
                    "card": {"id": "Ghostly"},
                },
            },
        ]
        postcondition = bridge.event_choice_postcondition(
            {"action": "choose", "option_id": "option:accept"}, state
        )
        self.assertEqual(
            {
                "kind": "deck_card_gain",
                "event_id": "Ghosts",
                "card_id": ("Ghostly", "Apparition"),
                "expected_delta": 5,
                "deck_size_before": 2,
                "card_count_before": 1,
            },
            postcondition,
        )

    def test_ghosts_decline_has_no_reward_postcondition(self):
        state = self.event_state(["Strike_B"], decision_id="decision:old")
        state["options"] = [
            {
                "option_id": "option:decline",
                "choice_index": 1,
                "target": {"kind": "event_option", "event_id": "Ghosts"},
            },
        ]
        self.assertIsNone(bridge.event_choice_postcondition(
            {"action": "choose", "option_id": "option:decline"}, state
        ))

    def test_ghosts_high_ascension_requires_three_apparitions(self):
        state = self.event_state(["Strike_B"], ascension_level=15)
        state["options"] = [
            {
                "option_id": "option:accept",
                "choice_index": 0,
                "target": {
                    "kind": "event_option",
                    "event_id": "Ghosts",
                    "card": {"id": "Ghostly"},
                },
            },
        ]
        postcondition = bridge.event_choice_postcondition(
            {"action": "choose", "option_id": "option:accept"}, state
        )
        self.assertEqual(3, postcondition["expected_delta"])

    def test_ghosts_transient_missing_deck_uses_bounded_fallback(self):
        accepted = self.event_state([], decision_id="decision:old")
        accepted["game_state"].pop("deck")
        accepted["options"] = [{
            "option_id": "option:accept",
            "choice_index": 0,
            "target": {
                "kind": "event_option",
                "event_id": "Ghosts",
                "card": {"id": "Ghostly"},
            },
        }]
        payload = {"action": "choose", "option_id": "option:accept"}

        self.assertIsNone(
            bridge.event_choice_postcondition(payload, accepted)
        )

        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Ghosts",
            "accepted_event_reward_card_id": "ghostly",
            "accepted_event_effect_signature": bridge.event_effect_signature(
                accepted
            ),
            "settle_frames": 0,
        })
        settled = self.event_state(
            ["Ghostly"] * 5, decision_id="decision:new"
        )
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, settled, raw)
        )
        pending["settle_frames"] = bridge.EVENT_REWARD_FALLBACK_FRAMES
        self.assertEqual(
            "complete", bridge.pending_frame_outcome(pending, settled, raw)
        )

    def test_vampires_captures_five_bites_and_strike_replacement_delta(self):
        state = self.event_state(
            ["Strike_R", "Strike_R", "Strike_R", "Strike_R", "Defend_R"],
            event_id="Vampires",
        )
        state["options"] = [{
            "option_id": "option:accept",
            "choice_index": 0,
            "target": {
                "kind": "event_option",
                "event_id": "Vampires",
                "card": {"id": "Bite"},
            },
        }]

        postcondition = bridge.event_choice_postcondition(
            {"action": "choose", "option_id": "option:accept"}, state
        )

        self.assertEqual("Bite", postcondition["card_id"])
        self.assertEqual(5, postcondition["expected_delta"])
        self.assertEqual(1, postcondition["expected_deck_delta"])

    def test_multicard_event_leave_does_not_reinstall_reward_postcondition(self):
        for event_id, deck in (
            ("Ghosts", ["Ghostly"] * 5),
            ("Vampires", ["Bite"] * 5),
        ):
            with self.subTest(event_id=event_id):
                state = self.event_state(deck, event_id=event_id)
                state["options"] = [{
                    "option_id": "option:leave",
                    "choice_index": 0,
                    "target": {
                        "kind": "event_option",
                        "event_id": event_id,
                        "label": "leave",
                        "card": None,
                    },
                }]

                payload = {"action": "choose", "option_id": "option:leave"}
                self.assertEqual("", bridge.event_choice_reward_card_id(payload, state))
                self.assertIsNone(
                    bridge.event_choice_postcondition(payload, state)
                )

    def test_vampires_replacement_waits_for_all_bites_and_removed_strikes(self):
        postcondition = {
            "kind": "deck_card_gain",
            "event_id": "Vampires",
            "card_id": "Bite",
            "expected_delta": 5,
            "expected_deck_delta": 1,
            "deck_size_before": 8,
            "card_count_before": 0,
        }

        self.assertEqual(
            "pending",
            bridge.event_postcondition_status(
                postcondition,
                self.event_state(
                    ["Defend_R"] * 4 + ["Bite"] * 3,
                    event_id="Vampires",
                ),
            ),
        )
        self.assertEqual(
            "satisfied",
            bridge.event_postcondition_status(
                postcondition,
                self.event_state(
                    ["Defend_R"] * 4 + ["Bite"] * 5,
                    event_id="Vampires",
                ),
            ),
        )

    def test_ghosts_waits_until_all_five_apparitions_are_in_master_deck(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Ghosts",
            "settle_frames": 10,
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Ghosts",
                "card_id": "Apparition",
                "expected_delta": 5,
                "deck_size_before": 2,
                "card_count_before": 1,
            },
        })
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle",
            bridge.pending_frame_outcome(
                pending,
                self.event_state(["Strike_B", "Apparition"]),
                raw,
            ),
        )
        self.assertEqual(
            "settle",
            bridge.pending_frame_outcome(
                pending,
                self.event_state(
                    ["Strike_B", "Apparition", *(["Apparition"] * 4)]
                ),
                raw,
            ),
        )
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(
                pending,
                self.event_state(
                    ["Strike_B", "Apparition", *(["Apparition"] * 5)]
                ),
                raw,
            ),
        )

    def test_ghosts_exact_reward_still_gets_short_generic_settle_window(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Ghosts",
            "settle_frames": 0,
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Ghosts",
                "card_id": "Apparition",
                "expected_delta": 5,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
        })
        state = self.event_state(["Strike_B", *(["Apparition"] * 5)])
        raw = {"ready_for_command": True}
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = 1
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = 2
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_ghosts_inconsistent_deck_delta_fails_closed(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Ghosts",
            "settle_frames": 10,
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Ghosts",
                "card_id": "Apparition",
                "expected_delta": 5,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
        })
        state = self.event_state(
            ["Strike_B", *(["Apparition"] * 4), "Doubt"]
        )
        self.assertEqual(
            "event_postcondition_mismatch",
            bridge.pending_frame_outcome(
                pending, state, {"ready_for_command": True}
            ),
        )

    def test_ghosts_missing_reward_has_bounded_failure(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Ghosts",
            "settle_frames": bridge.EVENT_SETTLE_TIMEOUT_FRAMES,
            "event_postcondition": {
                "kind": "deck_card_gain",
                "event_id": "Ghosts",
                "card_id": "Apparition",
                "expected_delta": 5,
                "deck_size_before": 1,
                "card_count_before": 0,
            },
        })
        self.assertEqual(
            "event_settlement_timeout",
            bridge.pending_frame_outcome(
                pending,
                self.event_state(["Strike_B"]),
                {"ready_for_command": True},
            ),
        )

    def test_card_reward_return_waits_until_parent_count_decreases(self):
        pending = self.pending("return")
        pending.update({
            "accepted_phase": "CARD_REWARD",
            "parent_card_reward_count_before": 1,
            "settle_frames": 0,
        })
        raw = {"ready_for_command": True}
        stale_parent = self.combat_reward_state(1)
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, stale_parent, raw))
        pending["settle_frames"] = 3
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, stale_parent, raw))
        cleaned_parent = self.combat_reward_state(0)
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, cleaned_parent, raw))

    def test_card_reward_choose_also_waits_for_parent_cleanup(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "CARD_REWARD",
            "parent_card_reward_count_before": 1,
            "settle_frames": 0,
        })
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(1), raw),
        )
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(0), raw),
        )

    def test_card_reward_cleanup_has_bounded_failure(self):
        pending = self.pending("return")
        pending.update({
            "accepted_phase": "CARD_REWARD",
            "parent_card_reward_count_before": 1,
            "settle_frames": 120,
        })
        raw = {"ready_for_command": True}
        self.assertEqual(
            "reward_cleanup_timeout",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(1), raw),
        )

    def test_prayer_wheel_first_reward_completes_on_two_to_one(self):
        pending = self.pending("return")
        pending.update({
            "accepted_phase": "CARD_REWARD",
            "parent_card_reward_count_before": 2,
            "settle_frames": 0,
        })
        raw = {"ready_for_command": True}
        self.assertEqual(
            "settle",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(2), raw),
        )
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(1), raw),
        )

    def test_parent_card_count_must_decrease_by_exactly_one(self):
        pending = self.pending("choose")
        pending.update({
            "accepted_phase": "CARD_REWARD",
            "parent_card_reward_count_before": 2,
            "settle_frames": 0,
        })
        raw = {"ready_for_command": True}
        self.assertEqual(
            "reward_count_mismatch",
            bridge.pending_frame_outcome(pending, self.combat_reward_state(0), raw),
        )

    def test_noncombat_card_reward_still_completes_on_changed_decision(self):
        pending = self.pending("choose")
        pending["accepted_phase"] = "CARD_REWARD"
        state = {
            "phase": "EVENT",
            "decision_id": "decision:new",
            "game_state": {"screen_state": {"event_id": "Library"}},
        }
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_ordinary_event_gets_short_settle_window_before_completion(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Golden Idol",
            "settle_frames": 0,
        })
        state = {"decision_id": "decision:new"}
        raw = {"ready_for_command": True}
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = 1
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = 2
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_ordinary_event_same_decision_gets_bounded_animation_window(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Golden Idol",
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
        })
        state = {"phase": "EVENT", "decision_id": "decision:old"}
        raw = {"ready_for_command": True}
        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = bridge.EVENT_UNCHANGED_SETTLE_FRAMES
        self.assertEqual(
            "event_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_wheel_same_decision_completes_after_authoritative_reward_change(self):
        accepted = self.event_state(
            ["Strike_R"], decision_id="decision:old", event_id="Wheel of Change"
        )
        accepted["game_state"]["gold"] = 10
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Wheel of Change",
            "accepted_event_effect_signature": bridge.event_effect_signature(
                accepted
            ),
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
        })
        rewarded = self.event_state(
            ["Strike_R"], decision_id="decision:old", event_id="Wheel of Change"
        )
        rewarded["game_state"]["gold"] = 110

        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(
                pending, rewarded, {"ready_for_command": True}
            ),
        )

    def test_wheel_unchanged_surface_waits_for_settlement_instead_of_completing(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Wheel of Change",
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
        })
        state = {"phase": "EVENT", "decision_id": "decision:old"}
        raw = {"ready_for_command": True}

        self.assertEqual("settle", bridge.pending_frame_outcome(pending, state, raw))
        pending["settle_frames"] = bridge.EVENT_UNCHANGED_SETTLE_FRAMES
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.WHEEL_SETTLE_TIMEOUT_FRAMES - 1
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.WHEEL_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "event_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_wheel_changed_decision_with_spin_surface_stays_pending(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Wheel of Change",
            "accepted_event_option_label": "鐜╁皬娓告垙",
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
        })
        state = self.event_state(
            ["Strike_R"], decision_id="decision:new", event_id="Wheel of Change"
        )
        state["options"] = [{"label": "spin"}]
        raw = {"ready_for_command": True}

        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.WHEEL_SETTLE_TIMEOUT_FRAMES - 1
        self.assertEqual(
            "settle", bridge.pending_frame_outcome(pending, state, raw)
        )
        pending["settle_frames"] = bridge.WHEEL_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "event_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, raw),
        )

    def test_wheel_result_surface_completes_after_decision_change(self):
        pending = self.pending()
        pending.update({
            "accepted_phase": "EVENT",
            "accepted_event_id": "Wheel of Change",
            "accepted_event_option_label": "鐜╁皬娓告垙",
            "settle_frames": bridge.EVENT_SETTLE_MIN_FRAMES,
        })
        state = self.event_state(
            ["Strike_R"], decision_id="decision:new", event_id="Wheel of Change"
        )
        state["options"] = [{"label": "Leave"}]
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(
                pending, state, {"ready_for_command": True}
            ),
        )

    def test_state_and_wait_may_complete_without_decision_change(self):
        state = {"decision_id": "decision:old"}
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending("state"), state, raw))
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending("wait"), state, raw))

    def test_error_frame_completes_as_failure(self):
        state = {"decision_id": "decision:old"}
        raw = {"ready_for_command": False, "error": "bad command"}
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending(), state, raw))

    def test_end_requires_authoritative_turn_advance(self):
        pending = self.pending("end")
        pending.update({"accepted_room_phase": "COMBAT", "accepted_turn": 4})
        state = {
            "decision_id": "decision:new",
            "game_state": {"room_phase": "COMBAT", "combat_state": {"turn": 4}},
        }
        raw = {"ready_for_command": True}
        self.assertEqual("unchanged", bridge.pending_frame_outcome(pending, state, raw))
        state["game_state"]["combat_state"]["turn"] = 5
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_end_completes_on_game_over_without_turn_advance(self):
        pending = self.pending("end")
        pending.update({"accepted_room_phase": "COMBAT", "accepted_turn": 4})
        state = {
            "decision_id": "decision:new",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "GAME_OVER",
                "combat_state": {"turn": 4},
            },
        }
        raw = {"ready_for_command": True}
        self.assertEqual("complete", bridge.pending_frame_outcome(pending, state, raw))

    def test_debug_intent_blocks_gameplay_but_allows_state_receipt(self):
        raw = initializing_combat_raw()
        state = bridge.enrich_state(raw, 42)
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending("state"), state, raw))
        self.assertEqual("unstable", bridge.pending_frame_outcome(self.pending("play"), state, raw))

    def test_game_over_debug_intent_completes_receipt(self):
        raw = initializing_combat_raw()
        raw["ready_for_command"] = False
        raw["game_state"]["screen_type"] = "GAME_OVER"
        state = bridge.enrich_state(raw, 42)
        self.assertEqual("GAME_OVER", state["phase"])
        self.assertNotIn("unstable_reason", state)
        self.assertEqual("complete", bridge.pending_frame_outcome(self.pending("end"), state, raw))

    def test_proceed_waits_for_stale_game_over_to_reach_main_menu(self):
        pending = self.pending("proceed")
        pending["accepted_phase"] = "GAME_OVER"
        state = {
            "phase": "GAME_OVER",
            "decision_id": "decision:old",
        }
        stale = {
            "in_game": True,
            "ready_for_command": True,
            "game_state": {"screen_type": "GAME_OVER"},
        }
        self.assertEqual(
            "proceed_settle",
            bridge.pending_frame_outcome(pending, state, stale),
        )
        pending["settle_frames"] = bridge.PROCEED_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "proceed_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, stale),
        )
        menu = {
            "in_game": False,
            "ready_for_command": True,
            "game_state": {"screen_type": "GAME_OVER"},
        }
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, state, menu),
        )

    def test_victory_proceed_gets_extended_settle_budget(self):
        pending = self.pending("proceed")
        pending["accepted_phase"] = "GAME_OVER"
        pending["settle_frames"] = bridge.PROCEED_SETTLE_TIMEOUT_FRAMES
        victory = {
            "in_game": True,
            "ready_for_command": True,
            "game_state": {
                "screen_type": "GAME_OVER",
                "room_type": "TrueVictoryRoom",
                "run_victory": True,
                "screen_state": {"victory": True},
            },
        }
        state = {"phase": "GAME_OVER", "decision_id": "decision:old"}
        self.assertEqual(
            "proceed_settle",
            bridge.pending_frame_outcome(pending, state, victory),
        )
        pending["settle_frames"] = bridge.PROCEED_VICTORY_SETTLE_TIMEOUT_FRAMES
        self.assertEqual(
            "proceed_settlement_timeout",
            bridge.pending_frame_outcome(pending, state, victory),
        )

    def test_combat_hand_select_proceed_completes_on_game_over(self):
        pending = self.pending("proceed")
        pending["accepted_phase"] = "HAND_SELECT"
        state = {
            "phase": "HAND_SELECT",
            "decision_id": "decision:old",
        }
        game_over = {
            "in_game": True,
            "ready_for_command": True,
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "GAME_OVER",
                "screen_state": {"victory": False},
            },
        }
        self.assertEqual(
            "complete",
            bridge.pending_frame_outcome(pending, state, game_over),
        )


class ResumeBindingTests(unittest.TestCase):
    def setUp(self):
        raw = {
            "available_commands": ["start", "resume", "state"],
            "ready_for_command": True,
            "in_game": False,
            "ironclad_autosave_exists": True,
            "silent_autosave_exists": True,
            "defect_autosave_exists": False,
        }
        self.state = bridge.enrich_state(raw, 77)

    def payload(self, **changes):
        payload = {
            "id": "resume-request",
            "action": "resume",
            "policy_version": "fast-policy-v5",
            "expected_seq": self.state["state_seq"],
            "decision_id": self.state["decision_id"],
            "phase": self.state["phase"],
            "player_class": "THE_SILENT",
            "pre_run_binding_version": 1,
            "attempt_id": "attempt-a",
            "run_id": "THE_SILENT:0:123",
            "seed": 123,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "selection_id": "selection-a",
            "goal_mode": "HEART",
            "character": "THE_SILENT",
            "ascension_level": 0,
            "run_type": "standard",
            "target_id": "resume:THE_SILENT:autosave",
        }
        payload.update(changes)
        return payload

    def test_resume_binds_exact_character_autosave(self):
        payload = self.payload()
        with mock.patch.object(
            bridge.pre_run_binding,
            "load_and_validate_resume_payload",
            return_value=payload,
        ) as validate:
            command, receipt = bridge.resolve_command(payload, self.state)
        self.assertEqual("RESUME THE_SILENT", command)
        self.assertEqual(receipt["requested_target_id"], receipt["resolved_target_id"])
        validate.assert_called_once()

    def test_main_menu_start_requires_selector_binding(self):
        payload = {
            "id": "start-request",
            "action": "start",
            "policy_version": "fast-policy-v5",
            "expected_seq": self.state["state_seq"],
            "decision_id": self.state["decision_id"],
            "phase": self.state["phase"],
            "player_class": "IRONCLAD",
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "goal_mode": "HEART",
            "pre_run_binding_version": 1,
            "selection_id": "selection-a",
            "selection_digest": "digest-a",
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "eligible_attempt_ids": [],
            "target_id": "run:IRONCLAD:a0:standard",
        }

        with mock.patch.object(
            bridge.pre_run_binding,
            "load_and_validate_start_payload",
            return_value=payload,
        ) as validate:
            command, receipt = bridge.resolve_command(payload, self.state)

        self.assertEqual("START IRONCLAD 0", command)
        self.assertEqual("run:IRONCLAD:a0:standard", receipt["resolved_target_id"])
        validate.assert_called_once()
        self.assertEqual(payload["selection_digest"], receipt["selection_digest"])
        self.assertTrue(all(
            field not in receipt
            for field in bridge.ATTEMPT_BINDING_FIELDS
            if field != "selection_digest"
        ))

    def test_resume_rejects_wrong_target(self):
        payload = self.payload(target_id="resume:IRONCLAD:autosave")
        with mock.patch.object(
            bridge.pre_run_binding,
            "load_and_validate_resume_payload",
            return_value=payload,
        ):
            with self.assertRaisesRegex(bridge.CommandRejected, "target_id mismatch"):
                bridge.resolve_command(payload, self.state)

    def test_resume_rejects_missing_autosave(self):
        payload = self.payload(
            player_class="DEFECT", character="DEFECT",
            target_id="resume:DEFECT:autosave",
        )
        with mock.patch.object(
            bridge.pre_run_binding,
            "load_and_validate_resume_payload",
            return_value=payload,
        ):
            with self.assertRaisesRegex(bridge.CommandRejected, "no authoritative autosave marker"):
                bridge.resolve_command(payload, self.state)

    def test_unbound_main_menu_start_and_resume_are_rejected(self):
        start = {
            "id": "start", "action": "start",
            "policy_version": "fast-policy-v5",
            "expected_seq": self.state["state_seq"],
            "decision_id": self.state["decision_id"],
            "phase": self.state["phase"],
            "player_class": "IRONCLAD", "ascension_level": 0,
            "target_id": "run:IRONCLAD:a0:standard",
        }
        for payload in (start, {
            "id": "resume", "action": "resume",
            "policy_version": "fast-policy-v5",
            "expected_seq": self.state["state_seq"],
            "decision_id": self.state["decision_id"],
            "phase": self.state["phase"],
            "player_class": "THE_SILENT",
            "target_id": "resume:THE_SILENT:autosave",
        }):
            with self.subTest(action=payload["action"]):
                with self.assertRaisesRegex(
                    bridge.CommandRejected, "binding rejected"
                ):
                    bridge.resolve_command(payload, self.state)


class CombatInitializationTests(unittest.TestCase):
    def test_debug_intent_exposes_only_transport_actions(self):
        state = bridge.enrich_state(initializing_combat_raw(), 42)
        self.assertFalse(state["ready_for_command"])
        self.assertEqual("COMBAT_INITIALIZING", state["phase"])
        self.assertEqual(["wait", "state"], state["legal_actions"])
        self.assertEqual("uninitialized_monster_intent", state["unstable_reason"])


class BridgeLifecycleTests(unittest.TestCase):
    @staticmethod
    def _write_claim_owner(lock_path, *, pid, token="owner-token", created_at=1.0):
        owner = bridge._claim_owner_payload(
            token=token,
            pid=pid,
            created_at=created_at,
            launch_id="launch-test",
        )
        (lock_path / bridge.BRIDGE_CLAIM_OWNER_FILE).write_text(
            json.dumps(owner, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return owner

    def test_claim_requires_launcher_environment_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path), \
                    mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "launch_id"):
                    bridge.claim_bridge_instance()

            self.assertFalse(instance_path.exists())

    def test_restart_sequence_uses_newer_state_after_meta_write_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meta_path = root / "state-meta.json"
            state_path = root / "state.json"
            meta_path.write_text(
                json.dumps({"sequence": 41}), encoding="utf-8"
            )
            state_path.write_text(
                json.dumps({"state_seq": 42}), encoding="utf-8"
            )
            with mock.patch.object(bridge, "META_PATH", meta_path), mock.patch.object(
                bridge, "STATE_PATH", state_path
            ):
                sequence = bridge.read_initial_sequence()

        self.assertEqual(42, sequence)

    def test_restart_sequence_uses_newer_meta_and_ignores_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meta_path = root / "state-meta.json"
            state_path = root / "state.json"
            meta_path.write_text(
                json.dumps({"sequence": 45}), encoding="utf-8"
            )
            state_path.write_text(
                json.dumps({"state_seq": True}), encoding="utf-8"
            )
            with mock.patch.object(bridge, "META_PATH", meta_path), mock.patch.object(
                bridge, "STATE_PATH", state_path
            ):
                sequence = bridge.read_initial_sequence()

        self.assertEqual(45, sequence)

    def test_live_instance_cannot_be_superseded(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                old_token = bridge.claim_bridge_instance(
                    launch_id="launch-test"
                )
                old_record = json.loads(
                    instance_path.read_text(encoding="utf-8")
                )
                self.assertEqual(2, old_record["schema_version"])
                self.assertEqual(2, old_record["protocol_version"])
                self.assertEqual(old_token, old_record["instance_token"])
                self.assertEqual(os.getpid(), old_record["bridge_pid"])
                self.assertEqual(os.getppid(), old_record["parent_java_pid"])
                self.assertEqual("launch-test", old_record["launch_id"])
                self.assertEqual(
                    bridge._sha256_file(Path(bridge.__file__).resolve()),
                    old_record["bridge_sha256"],
                )
                runtime_sources = bridge.freeze_manifest.source_snapshot(
                    bridge.ROOT
                )
                self.assertEqual(
                    bridge.freeze_manifest.snapshot_digest(runtime_sources),
                    old_record["runtime_source_digest"],
                )
                self.assertEqual(
                    len(runtime_sources),
                    old_record["runtime_source_file_count"],
                )
                with self.assertRaisesRegex(RuntimeError, "still alive"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: True,
                        launch_id="launch-test",
                    )

                self.assertTrue(bridge.bridge_instance_is_current(old_token))
                bridge.release_bridge_instance(old_token)
                self.assertFalse(instance_path.exists())

    def test_stale_instance_can_be_replaced_without_old_cleanup_deleting_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                old_token = bridge.claim_bridge_instance(
                    launch_id="launch-old"
                )
                new_token = bridge.claim_bridge_instance(
                    process_alive_fn=lambda pid: False,
                    launch_id="launch-new",
                )

                self.assertNotEqual(old_token, new_token)
                self.assertFalse(bridge.bridge_instance_is_current(old_token))
                self.assertTrue(bridge.bridge_instance_is_current(new_token))
                bridge.release_bridge_instance(old_token)
                self.assertTrue(instance_path.exists())
                bridge.release_bridge_instance(new_token)
                self.assertFalse(instance_path.exists())

    def test_dead_instance_pid_never_replaces_new_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            instance_path.write_text(json.dumps({
                "schema_version": 2,
                "protocol_version": bridge.PROTOCOL_VERSION,
                "instance_token": "801:dead-instance",
                "bridge_pid": 801,
                "parent_java_pid": 800,
                "launch_id": "launch-old",
                "bridge_sha256": "a" * 64,
                "started_at": 1.0,
            }), encoding="utf-8")
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                token = bridge.claim_bridge_instance(
                    process_alive_fn=lambda pid: False,
                    current_pid_fn=lambda: 802,
                    parent_pid_fn=lambda: 803,
                    launch_id="launch-new",
                )

            record = json.loads(instance_path.read_text(encoding="utf-8"))
            self.assertTrue(token.startswith("802:"))
            self.assertEqual(802, record["bridge_pid"])
            self.assertEqual(803, record["parent_java_pid"])
            self.assertEqual("launch-new", record["launch_id"])

    def test_concurrent_bridge_claim_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(
                instance_path.name + ".claim-lock"
            )
            claim_lock.mkdir()
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with self.assertRaisesRegex(RuntimeError, "claim is in progress"):
                    bridge.claim_bridge_instance(launch_id="launch-test")

            self.assertFalse(instance_path.exists())

    def test_live_claim_owner_is_never_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            self._write_claim_owner(claim_lock, pid=701)
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with self.assertRaisesRegex(RuntimeError, "claim is in progress"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: pid == 701,
                        current_pid_fn=lambda: 702,
                        parent_pid_fn=lambda: 703,
                        launch_id="launch-test",
                    )
            self.assertTrue((claim_lock / bridge.BRIDGE_CLAIM_OWNER_FILE).exists())

    def test_dead_claim_owner_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            self._write_claim_owner(claim_lock, pid=711)
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                token = bridge.claim_bridge_instance(
                    process_alive_fn=lambda pid: False,
                    current_pid_fn=lambda: 712,
                    parent_pid_fn=lambda: 713,
                    launch_id="launch-test",
                )
                self.assertTrue(bridge.bridge_instance_is_current(token) is False)
            self.assertFalse(claim_lock.exists())
            record = json.loads(instance_path.read_text(encoding="utf-8"))
            self.assertEqual(712, record["bridge_pid"])

    def test_fresh_ownerless_claim_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with self.assertRaisesRegex(RuntimeError, "claim is in progress"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: False,
                        current_pid_fn=lambda: 722,
                        parent_pid_fn=lambda: 723,
                        clock=lambda: claim_lock.stat().st_mtime + 1,
                        claim_lock_stale_seconds=30,
                        launch_id="launch-test",
                    )
            self.assertTrue(claim_lock.exists())

    def test_stale_ownerless_claim_lock_is_recovered_without_live_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            os.utime(claim_lock, (100.0, 100.0))
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                token = bridge.claim_bridge_instance(
                    process_alive_fn=lambda pid: False,
                    current_pid_fn=lambda: 732,
                    parent_pid_fn=lambda: 733,
                    clock=lambda: 200.0,
                    claim_lock_stale_seconds=30,
                    launch_id="launch-test",
                )
            self.assertTrue(token.startswith("732:"))
            self.assertFalse(claim_lock.exists())

    def test_stale_ownerless_claim_lock_refuses_live_bridge_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            os.utime(claim_lock, (100.0, 100.0))
            instance_path.write_text(json.dumps({
                "schema_version": 2,
                "protocol_version": bridge.PROTOCOL_VERSION,
                "instance_token": "live-token",
                "bridge_pid": 741,
                "parent_java_pid": 742,
                "launch_id": "launch-live",
                "bridge_sha256": "a" * 64,
                "started_at": 99.0,
            }), encoding="utf-8")
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with self.assertRaisesRegex(RuntimeError, "live bridge instance"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: pid == 741,
                        current_pid_fn=lambda: 743,
                        parent_pid_fn=lambda: 744,
                        clock=lambda: 200.0,
                        claim_lock_stale_seconds=30,
                        launch_id="launch-test",
                    )
            self.assertTrue(claim_lock.exists())

    def test_malformed_claim_owner_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            (claim_lock / bridge.BRIDGE_CLAIM_OWNER_FILE).write_text(
                '{"owner_pid":751,"unexpected":true}', encoding="utf-8"
            )
            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: False,
                        current_pid_fn=lambda: 752,
                        parent_pid_fn=lambda: 753,
                        launch_id="launch-test",
                    )
            self.assertTrue((claim_lock / bridge.BRIDGE_CLAIM_OWNER_FILE).exists())

    def test_concurrent_claims_have_exactly_one_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            barrier = threading.Barrier(2)

            def claim():
                barrier.wait()
                try:
                    return bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: True,
                        current_pid_fn=lambda: 761,
                        parent_pid_fn=lambda: 762,
                        launch_id="launch-test",
                    )
                except RuntimeError:
                    return None

            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _index: claim(), range(2)))
            self.assertEqual(1, sum(result is not None for result in results))
            self.assertTrue(instance_path.exists())

    def test_failed_recovery_does_not_delete_late_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "bridge-instance.json"
            claim_lock = instance_path.with_name(instance_path.name + ".claim-lock")
            claim_lock.mkdir()
            os.utime(claim_lock, (100.0, 100.0))
            calls = []

            def instance_check(_process_alive_fn):
                calls.append(True)
                if len(calls) == 2:
                    self._write_claim_owner(
                        claim_lock, pid=771, token="late-owner", created_at=200.0
                    )
                return False

            with mock.patch.object(bridge, "INSTANCE_PATH", instance_path), \
                    mock.patch.object(
                        bridge, "_bridge_instance_has_live_owner",
                        side_effect=instance_check,
                    ):
                with self.assertRaisesRegex(RuntimeError, "changed during recovery"):
                    bridge.claim_bridge_instance(
                        process_alive_fn=lambda pid: pid == 771,
                        current_pid_fn=lambda: 772,
                        parent_pid_fn=lambda: 773,
                        clock=lambda: 200.0,
                        claim_lock_stale_seconds=30,
                        launch_id="launch-test",
                    )
            owner = json.loads(
                (claim_lock / bridge.BRIDGE_CLAIM_OWNER_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual("late-owner", owner["owner_token"])
            self.assertFalse((claim_lock / bridge.BRIDGE_CLAIM_RECOVERY_FILE).exists())

    def test_nonpositive_parent_pid_is_not_alive(self):
        self.assertFalse(bridge.parent_process_is_alive(0))


class CommunicationModInputIsolationTests(unittest.TestCase):
    def test_boss_relic_choice_does_not_click_unrelated_ui_globally(self):
        source_path = (
            Path(__file__).resolve().parent
            / "src"
            / "CommunicationMod-1.2.1"
            / "src"
            / "main"
            / "java"
            / "communicationmod"
            / "ChoiceScreenUtils.java"
        )
        source = source_path.read_text(encoding="utf-8")
        method = source.split(
            "public static void makeBossRewardChoice(int choice)", 1
        )[1].split("\n    }", 1)[0]

        self.assertIn("AbstractRelicUpdatePatch.doHover = true", method)
        self.assertIn("AbstractRelicUpdatePatch.hoverRelic = chosenRelic", method)
        self.assertNotIn("InputHelper.justClickedLeft", method)

    def test_shipped_jar_has_consistent_choice_screen_switch_class(self):
        jar_path = Path(__file__).resolve().parent / "CommunicationMod.jar"
        main_entry = "communicationmod/ChoiceScreenUtils.class"
        switch_entry = "communicationmod/ChoiceScreenUtils$1.class"
        switch_field = b"$SwitchMap$communicationmod$ChoiceScreenUtils$ChoiceType"

        with zipfile.ZipFile(jar_path) as archive:
            main_class = archive.read(main_entry)
            switch_class = archive.read(switch_entry)

        # javac stores enum-switch tables in the synthetic $1 class.  Updating
        # only ChoiceScreenUtils.class leaves a loadable JAR that crashes on
        # the first post-START state conversion with NoSuchFieldError.
        self.assertIn(switch_field, main_class)
        self.assertIn(switch_field, switch_class)


class EventGridFollowupRegressionTests(unittest.TestCase):
    def test_note_exchange_binds_offered_card_to_grid_parent(self):
        offered = {
            "id": "Apotheosis", "name": "Apotheosis", "type": "SKILL",
            "upgrades": 0, "card_instance_id": "offered:apotheosis",
        }
        option = {
            "option_id": "option:take-note", "choice_index": 0,
            "target": {
                "kind": "event_option", "event_id": "NoteForYourself",
                "original_button_index": 0, "card": offered,
            },
        }
        state = {
            "phase": "EVENT", "options": [option],
            "game_state": {"screen_state": {
                "event_class": (
                    "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
                ),
                "options": [
                    {"original_button_index": 0},
                    {"original_button_index": 1},
                ],
            }},
        }

        context = bridge.accepted_grid_followup_context({
            "action": "choose", "option_id": "option:take-note",
        }, state)

        self.assertEqual("note_exchange", context["operation"])
        self.assertEqual(offered, context["offered_card"])
        self.assertEqual(1, context["select_count"])

    def test_drug_dealer_transform_two_binds_grid_parent(self):
        target = {
            "kind": "event_option", "event_id": "Drug Dealer",
            "original_button_index": 1,
        }
        option = {
            "option_id": "option:transform-two", "choice_index": 1,
            "target": target,
        }
        state = {
            "phase": "EVENT", "options": [option],
            "game_state": {"screen_state": {
                "event_class": "com.megacrit.cardcrawl.events.city.DrugDealer",
                "options": [
                    {"original_button_index": 0},
                    {"original_button_index": 1},
                    {"original_button_index": 2},
                ],
            }},
        }

        context = bridge.accepted_grid_followup_context({
            "action": "choose", "option_id": "option:transform-two",
        }, state)

        self.assertEqual("transform", context["operation"])
        self.assertEqual(2, context["select_count"])
        self.assertEqual("Drug Dealer", context["event_id"])

    def test_library_read_binds_visible_card_gain_grid(self):
        option = {
            "option_id": "option:read", "choice_index": 0,
            "target": {
                "kind": "event_option", "event_id": "The Library",
                "original_button_index": 0,
            },
        }
        state = {
            "phase": "EVENT", "options": [option],
            "game_state": {"screen_state": {
                "event_class": "com.megacrit.cardcrawl.events.city.TheLibrary",
                "options": [
                    {"original_button_index": 0},
                    {"original_button_index": 1},
                ],
            }},
        }

        context = bridge.accepted_grid_followup_context({
            "action": "choose", "option_id": "option:read",
        }, state)

        self.assertEqual("gain", context["operation"])
        self.assertEqual("library_card_offering", context["selection_domain"])
        self.assertEqual(1, context["select_count"])


if __name__ == "__main__":
    unittest.main()
