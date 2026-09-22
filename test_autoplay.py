import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autoplay
import independent_oracle
import strategy_audit
import bridge


def command_context(character="IRONCLAD", seed=123):
    return {
        "attempt_id": "attempt-command-binding",
        "run_id": f"{character}:0:{seed}",
        "seed": seed,
        "character": character,
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": "decision-command-binding",
        "controller_hash": "controller-command-binding",
        "policy_version": "fast-policy-v5",
        "selection_id": "selection-command-binding",
        "selection_digest": "b" * 64,
    }


def bind_test_command(state, payload, *, character="IRONCLAD", seed=123):
    context = command_context(character, seed)
    state.update(context)
    autoplay.bind_attempt_payload(payload, state, context)
    return context


def canonical_test_candidate(
    choice_id, choice_index, score, *, action="choose", operation=None,
    consequences=None,
):
    candidate = {
        "choice_id": choice_id,
        "choice_index": choice_index,
        "action": action,
        "score": score,
        "local_reason": "test typed candidate",
        "reason_codes": ["TEST_TYPED_CANDIDATE"],
        "score_rule_id": "test_sum_components",
        "score_formula": {"kind": "sum_components_v1"},
        "score_inputs": {"base": score},
        "score_components": [{
            "name": "base", "input": "base", "coefficient": 1,
            "value": score,
        }],
        "consequences": dict(consequences or {}),
    }
    if operation is not None:
        candidate["operation"] = operation
    return candidate


def typed_neow_option(
    choice_index, reward_kind="HUNDRED_GOLD", drawback_kind="NONE",
    *, max_hp=80, label="Neow option",
):
    contract = {
        "contract_version": 1,
        "contract_kind": "NEOW_REWARD",
        "reward_kind": reward_kind,
        "drawback_kind": drawback_kind,
        "parameters": {
            "hp_bonus": max_hp // 10,
            "cursed": drawback_kind == "CURSE",
            "drawback_def_kind": (
                None if drawback_kind == "NONE" else drawback_kind
            ),
        },
    }
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "option_id": f"neow:{choice_index}",
        "choice_index": choice_index,
        "label": label,
        "target": {
            "kind": "event_option",
            "event_id": "Neow Event",
            "neow_contract": contract,
            "mechanism_id": (
                "neow-mechanism:"
                + hashlib.sha256(encoded).hexdigest()[:20]
            ),
        },
    }


def goop_event_contract(original_index, *, gold_loss=27, continue_only=False):
    if continue_only:
        option_kind = "CONTINUE"
        parameters = {}
    elif original_index == 0:
        option_kind = "GATHER"
        parameters = {"gold_gain": 75, "hp_damage": 11}
    else:
        option_kind = "LEAVE"
        parameters = {"gold_loss": gold_loss}
    return {
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


def goop_event_target(choice_index, original_index, **contract_kwargs):
    contract = goop_event_contract(original_index, **contract_kwargs)
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "kind": "event_option",
        "event_id": "World of Goop",
        "choice_index": choice_index,
        "original_button_index": original_index,
        "event_contract": contract,
        "mechanism_id": (
            "event-mechanism:"
            + hashlib.sha256(encoded).hexdigest()[:20]
        ),
    }


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


def staged_event_target(
    event_id, event_class, stage, choice_index, original_index,
    option_kind, instance_parameters, parameters,
):
    contract = staged_event_contract(
        event_id, event_class, stage, original_index, option_kind,
        instance_parameters, parameters,
    )
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "kind": "event_option",
        "event_id": event_id,
        "choice_index": choice_index,
        "original_button_index": original_index,
        "event_contract": contract,
        "mechanism_id": (
            "event-mechanism:" + hashlib.sha256(encoded).hexdigest()[:20]
        ),
    }


def knowing_skull_surface(*, costs=(7, 10, 8, 6)):
    event_id = "Knowing Skull"
    event_class = "com.megacrit.cardcrawl.events.city.KnowingSkull"
    instance = {
        "potion_cost": costs[0], "gold_cost": costs[1],
        "card_cost": costs[2], "leave_cost": costs[3],
        "gold_reward": 90,
    }
    rows = (
        ("TAKE_POTION", {"hp_loss": costs[0], "reward_count": 1,
                         "reward_kind": "RANDOM_POTION"}),
        ("TAKE_GOLD", {"hp_loss": costs[1], "gold_gain": 90}),
        ("TAKE_CARD", {"hp_loss": costs[2], "reward_count": 1,
                       "reward_color": "COLORLESS",
                       "reward_rarity": "UNCOMMON",
                       "selection_mode": "RANDOM"}),
        ("LEAVE", {"hp_loss": costs[3]}),
    )
    targets = [
        staged_event_target(
            event_id, event_class, "ASK", index, index, kind,
            instance, parameters,
        )
        for index, (kind, parameters) in enumerate(rows)
    ]
    options = [{
        "choice_index": index, "original_button_index": index,
        "disabled": False,
        "event_contract": copy.deepcopy(target["event_contract"]),
    } for index, target in enumerate(targets)]
    return targets, options


def terminal_attempt_fixture(attempt_id="attempt-terminal-transaction"):
    initial = {
        "in_game": True,
        "protocol_version": 2,
        "ready_for_command": True,
        "state_seq": 40,
        "key_system_unlocked": True,
        "silent_third_act_win": True,
        "defect_third_act_win": True,
        "ironclad_third_act_win": True,
        "game_state": {
            "class": "IRONCLAD",
            "ascension_level": 0,
            "seed": 424242,
            "is_standard_run": True,
            "screen_type": "NONE",
        },
    }
    selection = {
        "selection_id": "selection-terminal-transaction",
        "algorithm": "beta-thompson-v1",
        "decision_hash": autoplay.DECISION_HASH,
        "controller_hash": autoplay.CONTROLLER_HASH,
        "goal_mode": "HEART",
        "policy_version": autoplay.POLICY_VERSION,
        "ascension_level": 0,
        "run_type": "standard",
        "character": "IRONCLAD",
    }
    context = autoplay.run_context_lib.create_context(
        initial,
        autoplay.DECISION_HASH,
        autoplay.CONTROLLER_HASH,
        selection,
    )
    context["attempt_id"] = attempt_id
    terminal_seq = 41
    terminal = {
        **initial,
        "state_seq": terminal_seq,
        **{
            field: context[field]
            for field in (
                "attempt_id", "run_id", "seed", "character",
                "ascension_level", "run_type", "decision_hash",
                "controller_hash", "policy_version", "selection_id",
                "selection_digest",
            )
        },
        "terminal_state_seq": terminal_seq,
        "game_state": {
            **initial["game_state"],
            "screen_type": "GAME_OVER",
            "run_victory": True,
            "heart_defeated": True,
            "act": 4,
            "floor": 55,
            "current_hp": 3,
            "max_hp": 80,
            "has_ruby_key": True,
            "has_emerald_key": True,
            "has_sapphire_key": True,
            "deck": [],
            "relics": [],
            "potions": [],
        },
    }
    audit_report = {
        "schema_version": 2,
        "audit_engine_sha256": strategy_audit.audit_engine_sha256(),
        "policy_version": autoplay.POLICY_VERSION,
        "attempt_id": context["attempt_id"],
        "run_id": context["run_id"],
        "seed": context["seed"],
        "character": context["character"],
        "ascension_level": context["ascension_level"],
        "run_type": context["run_type"],
        "decision_hash": context["decision_hash"],
        "controller_hash": context["controller_hash"],
        "selection_id": context["selection_id"],
        "selection_digest": context["selection_digest"],
        "terminal_state_seq": terminal_seq,
        "termination_kind": "game_over",
        "audit_status": "inconclusive",
        "issue_count": 1,
        "review_finding_count": 0,
        "eligible_unknown_count": 1,
        "oracle_disagreement_count": 0,
        "protocol_correctness": {"status": "inconclusive"},
        "mechanics_coverage": {"status": "inconclusive"},
        "strategy_quality": {"status": "inconclusive"},
        "independent_oracle": {
            "status": "inconclusive",
            "issue_count": 1,
            "eligible_unknown_count": 1,
            "disagreement_count": 0,
        },
        "death_observed": False,
        "death_replay": {
            "status": "not_applicable",
            "issue_count": 0,
            "eligible_unknown_count": 0,
            "death_observed": False,
        },
        "model_advice": {"conflicts": 0},
        "release_gate_passed": False,
    }
    return initial, context, terminal, audit_report


class IsolatedAutoplayTestCase(unittest.TestCase):
    """Keep every replay-corpus write away from production artifacts."""

    def setUp(self):
        super().setUp()
        autoplay.reset_authoritative_error_fallback()
        self.addCleanup(autoplay.reset_authoritative_error_fallback)
        autoplay._COMBAT_TRACE_ENCOUNTERS.clear()
        self.addCleanup(autoplay._COMBAT_TRACE_ENCOUNTERS.clear)
        self._artifact_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._artifact_directory.cleanup)
        decision_cases_path = (
            Path(self._artifact_directory.name) / "decision-cases.jsonl"
        )
        decision_cases_patch = patch.object(
            autoplay, "DECISION_CASES_PATH", decision_cases_path
        )
        decision_cases_patch.start()
        self.addCleanup(decision_cases_patch.stop)
        # Never let a user's pending command affect offline protocol tests.
        command_patch = patch.object(
            autoplay.stsctl, "COMMAND_PATH",
            Path(self._artifact_directory.name) / "command.json",
        )
        command_patch.start()
        self.addCleanup(command_patch.stop)



class AuthoritativeTelemetryTests(IsolatedAutoplayTestCase):
    def test_combat_trace_encounter_serial_separates_same_room_stages(self):
        autoplay._COMBAT_TRACE_ENCOUNTERS.clear()
        self.addCleanup(autoplay._COMBAT_TRACE_ENCOUNTERS.clear)
        game = {
            "class": "IRONCLAD", "ascension_level": 0, "seed": 123,
            "act": 2, "floor": 27, "room_type": "EventRoom",
            "room_phase": "COMBAT", "combat_state": {},
        }
        first = autoplay.combat_trace_encounter_serial(
            game, "attempt-colosseum", 100
        )
        same = autoplay.combat_trace_encounter_serial(
            game, "attempt-colosseum", 120
        )
        interlude = dict(game, room_phase="EVENT")
        self.assertIsNone(autoplay.combat_trace_encounter_serial(
            interlude, "attempt-colosseum", 130
        ))
        second = autoplay.combat_trace_encounter_serial(
            game, "attempt-colosseum", 140
        )

        self.assertEqual(100, first)
        self.assertEqual(first, same)
        self.assertEqual(140, second)
        self.assertNotEqual(
            autoplay.combat_trace_context(
                game, attempt_id="attempt-colosseum",
                encounter_serial=first,
            )["combat_id"],
            autoplay.combat_trace_context(
                game, attempt_id="attempt-colosseum",
                encounter_serial=second,
            )["combat_id"],
        )

    def test_transition_and_navigation_commands_are_not_strategy_candidates(self):
        transition = {
            "phase": "REST", "options": [],
            "available_commands": ["proceed", "state"],
        }
        map_state = {
            "phase": "MAP", "available_commands": ["choose", "return"],
            "options": [
                {"option_id": "map:a", "choice_index": 0,
                 "target": {"kind": "map_node", "x": 1, "y": 2}},
                {"option_id": "map:b", "choice_index": 1,
                 "target": {"kind": "map_node", "x": 2, "y": 2}},
            ],
        }

        self.assertFalse(autoplay.has_strategic_noncombat_surface(transition))
        animated_event = {
            "phase": "EVENT", "available_commands": ["choose", "wait"],
            "options": [{
                "option_id": "match:remaining", "choice_index": 0,
                "target": {"kind": "event_option", "event_id": "Match and Keep!"},
            }],
        }
        self.assertFalse(autoplay.has_strategic_noncombat_surface(
            animated_event, {"action": "wait"},
        ))
        self.assertTrue(autoplay.has_strategic_noncombat_surface(
            animated_event, {"action": "choose"},
        ))
        choices = autoplay.canonical_legal_choices(
            map_state, {"action": "choose", "option_id": "map:a"},
            {"candidates": []},
        )
        self.assertNotIn("action:return", {
            row["choice_id"] for row in choices
        })

    def test_neow_parent_context_recovers_ambiguous_grid_operation(self):
        contract = typed_neow_option(
            0, "REMOVE_TWO", "NONE", max_hp=80
        )["target"]["neow_contract"]
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "NEOW",
            "source_option_id": "option:remove-two",
            "source_choice_index": 0,
            "mechanism_id": autoplay._neow_mechanism_id(contract),
            "neow_contract": contract,
            "operation": "remove",
            "select_count": 2,
        }
        card = {
            "id": "Defend_R", "name": "Defend", "upgrades": 0,
            "card_instance_id": "card:defend",
        }
        state = {
            "phase": "GRID",
            "game_state": {"screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": False, "cards": [card],
                "parent_choice_context": parent,
            }},
        }
        target = {
            "kind": "card", "card_instance_id": "card:defend",
            "card": card,
        }

        operation, selected, error = autoplay._validated_grid_target(
            target, state
        )

        self.assertIsNone(error)
        self.assertEqual("grid_remove", operation)
        self.assertEqual("card:defend", selected["card_instance_id"])

    def test_shop_surface_binds_only_protocol_visible_items_by_identity(self):
        card = SimpleNamespace(
            uuid="card-visible", card_id="Metallicize", price=75,
        )
        hidden_relic = SimpleNamespace(
            relic_id="Girya", name="Girya", price=170,
        )
        visible_relic = SimpleNamespace(
            relic_id="OrangePellets", name="Orange Pellets", price=145,
        )
        potion_a = SimpleNamespace(
            potion_id="SpeedPotion", name="Speed Potion", price=51,
        )
        potion_b = SimpleNamespace(
            potion_id="SpeedPotion", name="Speed Potion", price=50,
        )
        screen = SimpleNamespace(
            cards=[card], relics=[hidden_relic, visible_relic],
            potions=[potion_a, potion_b], purge_available=True,
        )
        game = SimpleNamespace(screen=screen)
        state = {
            "phase": "SHOP_SCREEN",
            # Deliberately reorder the container while preserving the
            # authoritative choice indexes and semantic identities.
            "options": [
                {
                    "option_id": "option:potion-b", "choice_index": 11,
                    "target": {"kind": "potion", "item": {
                        "id": "SpeedPotion", "name": "Speed Potion",
                        "price": 50,
                    }},
                },
                {
                    "option_id": "option:relic", "choice_index": 8,
                    "target": {"kind": "relic", "item": {
                        "id": "OrangePellets", "price": 145,
                    }},
                },
                {
                    "option_id": "option:card", "choice_index": 5,
                    "target": {"kind": "card", "item": {
                        "id": "Metallicize",
                        "card_instance_id": "card-visible", "price": 75,
                    }},
                },
                {
                    "option_id": "option:potion-a", "choice_index": 9,
                    "target": {"kind": "potion", "item": {
                        "id": "SpeedPotion", "name": "Speed Potion",
                        "price": 51,
                    }},
                },
                {
                    "option_id": "option:purge", "choice_index": 0,
                    "target": {"kind": "purge", "price": 75},
                },
            ],
        }

        autoplay.bind_shop_protocol_surface(game, state)

        self.assertEqual([card], screen.cards)
        self.assertEqual([visible_relic], screen.relics)
        self.assertEqual([potion_a, potion_b], screen.potions)
        self.assertEqual(5, card.protocol_choice_index)
        self.assertEqual(8, visible_relic.protocol_choice_index)
        self.assertEqual([9, 11], [
            potion.protocol_choice_index for potion in screen.potions
        ])
        self.assertEqual(0, screen.protocol_purge_choice_index)

    def test_shop_surface_rejects_ambiguous_duplicate_listing(self):
        duplicate = lambda: SimpleNamespace(
            potion_id="SpeedPotion", name="Speed Potion", price=50,
        )
        game = SimpleNamespace(screen=SimpleNamespace(
            cards=[], relics=[], potions=[duplicate(), duplicate()],
            purge_available=False,
        ))
        state = {
            "phase": "SHOP_SCREEN",
            "options": [{
                "option_id": "option:potion", "choice_index": 0,
                "target": {"kind": "potion", "item": {
                    "id": "SpeedPotion", "name": "Speed Potion", "price": 50,
                }},
            }],
        }

        with self.assertRaisesRegex(
            autoplay.SafetyError, "did not bind uniquely"
        ):
            autoplay.bind_shop_protocol_surface(game, state)

    def test_shop_surface_binds_counted_identical_protocol_listings(self):
        first = SimpleNamespace(
            potion_id="Dexterity Potion", name="Dexterity Potion", price=50,
        )
        second = SimpleNamespace(
            potion_id="Dexterity Potion", name="Dexterity Potion", price=50,
        )
        screen = SimpleNamespace(cards=[], relics=[], potions=[first, second])
        game = SimpleNamespace(screen=screen)
        state = {
            "phase": "SHOP_SCREEN",
            # CommunicationMod uses an occurrence suffix for identical
            # descriptors.  A complete counted group is safe; one option for
            # two objects remains covered by the negative test above.
            "options": [
                {
                    "option_id": "option:dexterity", "choice_index": 11,
                    "target": {"kind": "potion", "item": {
                        "id": "Dexterity Potion", "name": "Dexterity Potion",
                        "price": 50,
                    }},
                },
                {
                    "option_id": "option:dexterity:1", "choice_index": 12,
                    "target": {"kind": "potion", "item": {
                        "id": "Dexterity Potion", "name": "Dexterity Potion",
                        "price": 50,
                    }},
                },
            ],
        }

        autoplay.bind_shop_protocol_surface(game, state)

        self.assertEqual([first, second], screen.potions)
        self.assertEqual([11, 12], [
            potion.protocol_choice_index for potion in screen.potions
        ])
        self.assertEqual(
            ["option:dexterity", "option:dexterity:1"],
            [potion.protocol_option_id for potion in screen.potions],
        )

    def test_unaffordable_purge_does_not_occupy_first_protocol_index(self):
        card = SimpleNamespace(
            uuid="card-visible", card_id="Metallicize", price=5,
        )
        screen = SimpleNamespace(
            cards=[card], relics=[], potions=[], purge_available=True,
            purge_cost=75,
        )
        game = SimpleNamespace(screen=screen)
        state = {
            "phase": "SHOP_SCREEN",
            "options": [{
                "option_id": "option:card", "choice_index": 0,
                "target": {"kind": "card", "item": {
                    "id": "Metallicize",
                    "card_instance_id": "card-visible",
                    "price": 5,
                }},
            }],
        }

        autoplay.bind_shop_protocol_surface(game, state)

        self.assertEqual(0, card.protocol_choice_index)
        self.assertFalse(hasattr(screen, "protocol_purge_choice_index"))

    def _noncombat_state(
        self, *, seq, phase="EVENT", gold=100, deck=None, option=None,
        room_phase="EVENT",
    ):
        return {
            "state_seq": seq,
            "decision_id": f"decision-{seq}",
            "phase": phase,
            "ready_for_command": True,
            "available_commands": ["choose", "state"],
            "options": [option] if option else [],
            "game_state": {
                "class": "IRONCLAD",
                "ascension_level": 0,
                "seed": 123,
                "act": 1,
                "floor": 5,
                "current_hp": 60,
                "max_hp": 80,
                "gold": gold,
                "room_phase": room_phase,
                "room_type": "EventRoom",
                "screen_type": phase,
                "deck": list(deck or []),
                "relics": [],
                "potions": [],
                "has_ruby_key": False,
                "has_emerald_key": False,
                "has_sapphire_key": False,
            },
        }

    def test_observable_inventory_records_noncombat_and_combat_block_delta(self):
        before = self._noncombat_state(seq=10)["game_state"]
        before["relics"] = [{
            "id": "Burning Blood", "name": "Burning Blood",
            "counter": -1, "tier": "STARTER",
        }]
        after = copy.deepcopy(before)
        after["room_phase"] = "COMBAT"
        after["combat_state"] = {"player": {"block": 7}}

        before_snapshot = autoplay.observable_inventory_snapshot(before)
        after_snapshot = autoplay.observable_inventory_snapshot(after)
        delta = autoplay.observable_inventory_delta(
            before_snapshot, after_snapshot
        )

        self.assertEqual(0, before_snapshot["block"])
        self.assertEqual(7, after_snapshot["block"])
        self.assertEqual(7, delta["block_delta"])
        self.assertEqual("STARTER", before_snapshot["relics"][0]["tier"])

    def test_authoritative_state_snapshot_is_exact_bounded_and_combat_complete(self):
        state = {
            "state_seq": 91,
            "decision_id": "decision-91",
            "phase": "COMBAT_TURN_3",
            "ready_for_command": True,
            "available_commands": ["play", "end", "state"],
            "game_state": {
                "class": "THE_SILENT",
                "ascension_level": 0,
                "seed": 456,
                "act": 4,
                "floor": 55,
                "current_hp": 12,
                "max_hp": 70,
                "gold": 77,
                "room_phase": "COMBAT",
                "room_type": "MonsterRoomBoss",
                "screen_type": "NONE",
                "screen_name": "combat",
                "map": [{"large": "presentation-only"}],
                "deck": [{
                    "id": "Strike_G", "card_instance_id": "deck:strike",
                    "description": "omitted",
                }],
                "relics": [{"id": "Burning Blood", "counter": -1}],
                "potions": [{
                    "id": "Block Potion", "potion_instance_id": "potion:1",
                    "can_use": True, "requires_target": False,
                }],
                "combat_state": {
                    "turn": 3,
                    "player": {
                        "current_hp": 12, "max_hp": 70, "block": 4,
                        "energy": 2,
                        "powers": [{"id": "Strength", "amount": 1}],
                        "orbs": [],
                    },
                    "monsters": [{
                        "enemy_instance_id": "enemy:heart",
                        "id": "CorruptHeart",
                        "current_hp": 650,
                        "max_hp": 750,
                        "block": 0,
                        "intent": "ATTACK_BUFF",
                        "move_adjusted_damage": 2,
                        "move_hits": 15,
                        "is_gone": False,
                        "half_dead": False,
                        "powers": [
                            {"id": "BeatOfDeath", "amount": 2},
                            {"id": "Invincible", "amount": 200},
                        ],
                    }],
                    "hand": [{
                        "id": "Neutralize", "card_instance_id": "hand:1",
                        "cost": 0, "is_playable": True,
                    }],
                    "draw_pile": [],
                    "discard_pile": [],
                    "exhaust_pile": [],
                },
            },
        }

        snapshot = autoplay.authoritative_state_snapshot(state)

        self.assertEqual(91, snapshot["state_seq"])
        self.assertEqual("decision-91", snapshot["decision_id"])
        game = snapshot["game_state"]
        self.assertNotIn("map", game)
        self.assertNotIn("description", game["deck"][0])
        self.assertEqual(4, game["block"])
        self.assertEqual("hand:1", game["combat_state"]["hand"][0]["card_instance_id"])
        self.assertEqual(
            ["BeatOfDeath", "Invincible"],
            [
                power["id"]
                for power in game["combat_state"]["monsters"][0]["powers"]
            ],
        )

    def test_authoritative_event_snapshot_keeps_only_typed_option_evidence(self):
        contract = goop_event_contract(1, gold_loss=27)
        state = self._noncombat_state(seq=92, phase="EVENT")
        state["game_state"].update({
            "screen_type": "EVENT",
            "screen_state": {
                "event_id": "World of Goop",
                "body_text": "presentation must be omitted",
                "options": [{
                    "label": "claims free gold",
                    "text": "Lose 999 Gold",
                    "disabled": False,
                    "choice_index": 0,
                    "original_button_index": 1,
                    "mechanism_id": "event-mechanism:fixture",
                    "event_contract": contract,
                    "card": {
                        "id": "Writhe",
                        "card_instance_id": "preview:writhe",
                        "description": "presentation must be omitted",
                    },
                }],
            },
        })

        screen = autoplay.authoritative_state_snapshot(state)[
            "game_state"
        ]["screen_state"]

        self.assertEqual("World of Goop", screen["event_id"])
        option = screen["options"][0]
        self.assertNotIn("label", option)
        self.assertNotIn("text", option)
        self.assertEqual(1, option["original_button_index"])
        self.assertEqual(
            {"gold_loss": 27},
            option["event_contract"]["parameters"],
        )
        self.assertNotIn("description", option["card"])

    def test_authoritative_event_snapshot_keeps_progress_binding(self):
        state = self._noncombat_state(seq=94, phase="EVENT")
        state["game_state"]["screen_state"] = {
            "event_id": "Masked Bandits",
            "event_class": "com.megacrit.cardcrawl.events.city.MaskedBandits",
            "event_stage": "INTRO",
            "screen_num": None,
            "options": [
                {"disabled": False, "choice_index": 0,
                 "original_button_index": 0},
                {"disabled": False, "choice_index": 1,
                 "original_button_index": 1},
            ],
        }

        screen = autoplay.authoritative_state_snapshot(state)[
            "game_state"
        ]["screen_state"]

        self.assertEqual(
            "com.megacrit.cardcrawl.events.city.MaskedBandits",
            screen["event_class"],
        )
        self.assertEqual("INTRO", screen["event_stage"])
        self.assertIsNone(screen["screen_num"])

    def test_authoritative_event_snapshot_does_not_invent_missing_binding(self):
        state = self._noncombat_state(seq=93, phase="EVENT")
        state["game_state"]["screen_state"] = {
            "event_id": "World of Goop",
            "options": [{"disabled": False, "choice_index": 0}],
        }

        option = autoplay.authoritative_state_snapshot(state)[
            "game_state"
        ]["screen_state"]["options"][0]

        self.assertNotIn("original_button_index", option)
        self.assertNotIn("event_contract", option)
        self.assertNotIn("mechanism_id", option)

    def test_compact_action_binds_attempt_and_observes_deterministic_choice(self):
        option = {
            "option_id": "reward:card",
            "choice_index": 2,
            "label": "Pommel Strike",
            "target": {
                "kind": "card",
                "card": {
                    "id": "Pommel Strike",
                    "card_instance_id": "card:pommel",
                    "upgrades": 0,
                },
            },
        }
        before = self._noncombat_state(
            seq=10, phase="CARD_REWARD", option=option,
            room_phase="COMPLETE",
        )
        after = self._noncombat_state(
            seq=11,
            phase="COMBAT_REWARD",
            deck=[{
                "id": "Pommel Strike",
                "card_instance_id": "card:pommel",
                "upgrades": 0,
            }],
            room_phase="COMPLETE",
        )
        context = command_context()
        receipt = {
            "requested_target_id": "reward:card",
            "resolved_target_id": "reward:card",
        }

        record = autoplay.compact_action(
            before,
            {"action": "choose", "option_id": "reward:card"},
            receipt,
            after,
            decision={"candidates": [{
                "candidate_id": "reward:card", "score": 9,
            }]},
            run_context=context,
        )

        for field, expected in context.items():
            self.assertEqual(expected, record[field], field)
        self.assertEqual(10, record["authoritative_state_before"]["state_seq"])
        self.assertEqual(11, record["authoritative_state_after"]["state_seq"])
        self.assertEqual(0, record["observable_state_before"]["block"])
        self.assertEqual(0, record["decision_outcome"]["block_delta"])
        settlement = record["authoritative_choice_settlement"]
        self.assertEqual("observed", settlement["status"])
        self.assertTrue(settlement["fully_observable"])
        self.assertEqual("reward:card", settlement["choice_id"])
        self.assertTrue(settlement["observed_outcome"]["deck"]["added"])

        delayed = copy.deepcopy(record)
        delayed["legal_choices_before"][0]["consequences"]["future_costs"] = [
            {"kind": "next_event_hp_loss", "amount": 5}
        ]
        delayed_settlement = autoplay.authoritative_choice_settlement(delayed)
        self.assertEqual("unresolved", delayed_settlement["status"])
        self.assertEqual(
            "delayed_consequence_not_settled", delayed_settlement["reason"]
        )

    def test_shop_purchase_binds_typed_benefit_to_authoritative_potion_gain(self):
        item = {
            "id": "SpeedPotion", "name": "Speed Potion", "price": 75,
        }
        option = {
            "option_id": "shop:potion:speed:0",
            "choice_index": 0,
            "label": "Speed Potion",
            "target": {"kind": "potion", "item": item},
        }
        before = self._noncombat_state(
            seq=30,
            phase="SHOP_SCREEN",
            gold=100,
            option=option,
            room_phase="COMPLETE",
        )
        before["game_state"]["screen_state"] = {
            "purge_available": False, "purge_cost": 75,
        }
        after = self._noncombat_state(
            seq=31,
            phase="SHOP_SCREEN",
            gold=25,
            room_phase="COMPLETE",
        )
        after["game_state"]["potions"] = [{
            "id": "SpeedPotion",
            "name": "Speed Potion",
            "potion_instance_id": "held-speed-0",
            "slot": 0,
        }]
        benefit = {
            "kind": "potion", "id": "SpeedPotion",
            "potion_id": "SpeedPotion",
        }
        candidate = canonical_test_candidate(
            "shop:potion:SpeedPotion:0",
            0,
            12,
            consequences={
                "gold_delta": -75,
                "potion_changes": {
                    "gain": [item], "remove": [], "replace": [],
                },
                "acquired_benefit": benefit,
            },
        )

        record = autoplay.compact_action(
            before,
            {"action": "choose", "option_id": option["option_id"]},
            {
                "requested_target_id": option["option_id"],
                "resolved_target_id": option["option_id"],
            },
            after,
            decision={"candidates": [candidate]},
            run_context=command_context(),
        )

        self.assertEqual(
            benefit,
            record["legal_choices_before"][0]["consequences"][
                "acquired_benefit"
            ],
        )
        self.assertEqual(
            {
                **benefit,
                "potion_instance_id": "held-speed-0",
            },
            record["decision_outcome"]["observable_delta"],
        )

        missing_gain = copy.deepcopy(record["decision_outcome"])
        missing_gain["potions"]["added"] = []
        self.assertIsNone(
            autoplay.selected_shop_observable_acquisition(
                before,
                {"action": "choose", "option_id": option["option_id"]},
                missing_gain,
            )
        )

    def test_ghosts_binds_card_package_to_authoritative_deck_gain(self):
        preview = {
            "id": "Ghostly", "name": "Apparition", "type": "SKILL",
            "rarity": "SPECIAL", "upgrades": 0,
            "card_instance_id": "event-preview-ghostly",
        }
        option = {
            "option_id": "event:ghosts:accept", "choice_index": 0,
            "label": "Accept",
            "target": {
                "kind": "event_option", "event_id": "Ghosts",
                "original_button_index": 0, "card": preview,
            },
        }
        before = self._noncombat_state(
            seq=40, phase="EVENT", gold=0, option=option,
            room_phase="EVENT",
        )
        before["game_state"].update({
            "ascension_level": 0, "current_hp": 77, "max_hp": 77,
        })
        after = self._noncombat_state(
            seq=41, phase="EVENT", gold=0, room_phase="EVENT",
        )
        after["game_state"].update({
            "ascension_level": 0, "current_hp": 38, "max_hp": 38,
            "deck": [
                {
                    **preview,
                    "card_instance_id": f"ghostly-{index}",
                }
                for index in range(5)
            ],
        })
        benefit = {
            "kind": "card_package", "id": "Ghostly",
            "card_id": "Ghostly", "count": 5,
        }
        candidate = canonical_test_candidate(
            "event:0", 0, 10,
            consequences={
                "hp_delta": -39, "max_hp_delta": -39,
                "card_delta": 5, "card_id": "Ghostly",
                "acquired_benefit": benefit,
            },
        )

        record = autoplay.compact_action(
            before,
            {"action": "choose", "option_id": option["option_id"]},
            {
                "requested_target_id": option["option_id"],
                "resolved_target_id": option["option_id"],
            },
            after,
            decision={"candidates": [candidate]},
            run_context=command_context(),
        )

        self.assertEqual(
            benefit, record["decision_outcome"]["observable_delta"]
        )

    def test_combat_choice_surface_persists_observable_transition_claim(self):
        card = {
            "id": "Survivor", "name": "Survivor", "type": "SKILL",
            "rarity": "BASIC", "upgrades": 0, "has_target": False,
            "cost": 1, "uuid": "combat-card-a",
            "card_instance_id": "combat-card-a",
        }
        option = {
            "option_id": "hand:survivor",
            "choice_index": 0,
            "label": "Survivor",
            "target": {
                "kind": "card",
                "card_instance_id": "combat-card-a",
                "card": card,
            },
        }
        before = self._noncombat_state(
            seq=40,
            phase="HAND_SELECT",
            option=option,
            room_phase="COMBAT",
        )
        before["available_commands"] = ["choose", "state"]
        before_game = before["game_state"]
        before_game.update({
            "screen_type": "HAND_SELECT",
            "current_action": "DiscardAction",
            "screen_state": {
                "hand": [card], "selected": [], "max_cards": 1,
                "can_pick_zero": False,
            },
            "combat_state": {
                "turn": 3,
                "player": {
                    "current_hp": 60, "max_hp": 80, "block": 0,
                    "energy": 2, "powers": [], "orbs": [],
                },
                "monsters": [],
                "hand": [card],
                "draw_pile": [{**card, "uuid": "draw-a",
                               "card_instance_id": "draw-a"}],
                "discard_pile": [], "exhaust_pile": [], "limbo": [],
            },
        })
        after = copy.deepcopy(before)
        after.update({
            "state_seq": 41,
            "decision_id": "decision-41",
            "phase": "COMBAT_TURN_3",
            "options": [],
        })
        after_game = after["game_state"]
        after_game.update({
            "screen_type": "NONE",
            "current_action": None,
            "screen_state": {},
        })
        after_game["combat_state"]["hand"] = []
        after_game["combat_state"]["discard_pile"] = [card]
        outcome = SimpleNamespace(
            attack_hp_loss=0,
            end_turn_hp_loss=0,
            next_turn_start_hp_loss=0,
            total_hp_loss=0,
        )
        parsed = SimpleNamespace(monsters=[])
        candidate = canonical_test_candidate(
            "hand:survivor", 0, 1,
            consequences={"operation": "hand_select_card"},
        )

        with patch.object(
            autoplay.Game, "from_json", return_value=parsed
        ), patch.object(
            autoplay.combat_predictor, "living_monsters", return_value=[]
        ), patch.object(
            autoplay.combat_predictor,
            "projected_turn_outcome",
            return_value=outcome,
        ), patch.object(
            autoplay.combat_predictor,
            "projected_end_turn_healing",
            return_value=0,
        ), patch.object(
            autoplay.combat_predictor, "incoming_damage", return_value=0,
        ), patch.object(
            autoplay.combat_predictor,
            "projected_player_block",
            return_value=0,
        ), patch.object(
            autoplay.combat_predictor,
            "projected_doomed_monsters",
            return_value=[],
        ):
            record = autoplay.compact_action(
                before,
                {"action": "choose", "option_id": option["option_id"]},
                {
                    "requested_target_id": option["option_id"],
                    "resolved_target_id": option["option_id"],
                },
                after,
                decision={"candidates": [candidate]},
                run_context=command_context(),
            )

        self.assertIn("observable_state_before", record)
        self.assertIn("observable_state_after", record)
        transition = record["decision_outcome"][
            "combat_choice_transition"
        ]
        self.assertEqual(
            "DiscardAction", transition["before"]["current_action"]
        )
        self.assertEqual(
            ["combat-card-a"],
            transition["before"]["piles"]["hand"][
                "card_instance_ids"
            ],
        )
        self.assertEqual(
            ["combat-card-a"],
            transition["after"]["piles"]["discard_pile"][
                "card_instance_ids"
            ],
        )
        self.assertEqual(
            [],
            transition["before"]["screen_selection"][
                "card_instance_ids"
            ],
        )

    def test_combat_choice_transition_claim_covers_all_overlay_phases(self):
        for phase in ("GRID", "CARD_REWARD", "HAND_SELECT"):
            with self.subTest(phase=phase):
                before = {
                    "state_seq": 50,
                    "phase": phase,
                    "game_state": {
                        "room_phase": "COMBAT",
                        "screen_type": phase,
                        "current_action": "QueuedAction",
                        "screen_state": {},
                        "combat_state": {
                            "hand": [], "draw_pile": [],
                            "discard_pile": [], "exhaust_pile": [],
                            "limbo": [],
                        },
                    },
                }
                after = copy.deepcopy(before)
                after["state_seq"] = 51

                claim = autoplay.combat_choice_transition_claim(
                    before, after
                )

                self.assertIsNotNone(claim)
                self.assertEqual(phase, claim["before"]["phase"])
                self.assertEqual(
                    "authoritative_protocol_before_after",
                    claim["authority"],
                )

    def test_combat_choice_snapshot_does_not_claim_missing_pile_is_empty(self):
        snapshot = autoplay.combat_choice_observable_snapshot({
            "state_seq": 60,
            "phase": "GRID",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "GRID",
                "screen_state": {"cards": [], "selected_cards": []},
                "combat_state": {"hand": []},
            },
        })

        self.assertTrue(snapshot["piles"]["hand"]["source_present"])
        self.assertFalse(
            snapshot["piles"]["limbo"]["source_present"]
        )
        self.assertEqual(0, snapshot["piles"]["limbo"]["count"])

    def test_no_option_combat_choice_confirm_still_persists_transition(self):
        for phase, screen_state in (
            (
                "GRID",
                {
                    "cards": [], "selected_cards": [], "num_cards": 0,
                    "confirm_up": True,
                },
            ),
            (
                "HAND_SELECT",
                {
                    "hand": [], "selected": [], "max_cards": 0,
                    "can_pick_zero": True,
                },
            ),
        ):
            with self.subTest(phase=phase):
                before = self._noncombat_state(
                    seq=70, phase=phase, room_phase="COMBAT"
                )
                before.update({
                    "available_commands": ["proceed", "state"],
                    "options": [],
                })
                before["game_state"].update({
                    "screen_type": phase,
                    "current_action": "ZeroCardConfirmAction",
                    "screen_state": screen_state,
                    "combat_state": {
                        "hand": [], "draw_pile": [],
                        "discard_pile": [], "exhaust_pile": [],
                        "limbo": [],
                    },
                })
                after = copy.deepcopy(before)
                after.update({
                    "state_seq": 71,
                    "phase": "COMBAT_TURN_1",
                    "decision_id": "decision-71",
                })
                after["game_state"].update({
                    "screen_type": "NONE",
                    "current_action": None,
                    "screen_state": {},
                })

                outcome = SimpleNamespace(
                    attack_hp_loss=0,
                    end_turn_hp_loss=0,
                    next_turn_start_hp_loss=0,
                    total_hp_loss=0,
                )
                parsed = SimpleNamespace(monsters=[])
                with patch.object(
                    autoplay.Game, "from_json", return_value=parsed
                ), patch.object(
                    autoplay.combat_predictor,
                    "living_monsters",
                    return_value=[],
                ), patch.object(
                    autoplay.combat_predictor,
                    "safe_to_wait_for_passive_kills",
                    return_value=False,
                ), patch.object(
                    autoplay.combat_predictor,
                    "projected_turn_outcome",
                    return_value=outcome,
                ), patch.object(
                    autoplay.combat_predictor,
                    "projected_end_turn_healing",
                    return_value=0,
                ), patch.object(
                    autoplay.combat_predictor,
                    "incoming_damage",
                    return_value=0,
                ), patch.object(
                    autoplay.combat_predictor,
                    "projected_player_block",
                    return_value=0,
                ), patch.object(
                    autoplay.combat_predictor,
                    "projected_doomed_monsters",
                    return_value=[],
                ):
                    record = autoplay.compact_action(
                        before,
                        {
                            "action": "proceed",
                            "target_id": "action:proceed",
                        },
                        {
                            "requested_target_id": "action:proceed",
                            "resolved_target_id": "action:proceed",
                        },
                        after,
                        decision={},
                        run_context=command_context(),
                    )

                self.assertNotIn("legal_choices_before", record)
                claim = record["decision_outcome"][
                    "combat_choice_transition"
                ]
                self.assertEqual(phase, claim["before"]["phase"])
                self.assertEqual(
                    "ZeroCardConfirmAction",
                    claim["before"]["current_action"],
                )

    def test_unstructured_event_uncertainty_is_not_claimed_fully_observable(self):
        option = {
            "option_id": "event:gamble",
            "choice_index": 0,
            "label": "Gamble",
            "target": {"kind": "event_option", "event_id": "Gamble"},
        }
        before = self._noncombat_state(seq=20, option=option)
        after = self._noncombat_state(seq=21, gold=110)

        record = autoplay.compact_action(
            before,
            {"action": "choose", "option_id": "event:gamble"},
            {
                "requested_target_id": "event:gamble",
                "resolved_target_id": "event:gamble",
            },
            after,
            decision={"candidates": [{
                "candidate_id": "event:gamble", "score": 1,
            }]},
            run_context=command_context(),
        )

        settlement = record["authoritative_choice_settlement"]
        self.assertEqual("unresolved", settlement["status"])
        self.assertFalse(settlement["fully_observable"])
        self.assertEqual(
            "unstructured_or_multistage_uncertainty", settlement["reason"]
        )

    def test_route_future_uncertainty_is_classified_after_exact_receipt(self):
        record = {
            "phase": "MAP",
            "before_seq": 20,
            "after_seq": 21,
            "requested_target_id": "map:5,10",
            "resolved_target_id": "map:5,10",
            "decision_outcome": {
                "current_hp_delta": 0,
                "max_hp_delta": 0,
                "gold_delta": 0,
                "block_delta": 0,
                "deck": {"added": [], "removed": [], "changed": []},
                "relics": {"added": [], "removed": [], "changed": []},
                "potions": {"added": [], "removed": [], "changed": []},
                "keys_before": {"ruby": False},
                "keys_after": {"ruby": False},
            },
            "legal_choices_before": [{
                "choice_id": "map:5,10",
                "selected": True,
                "uncertainty": "future room outcomes remain probabilistic",
                "consequences": {
                    "future_costs": [],
                    "route": {"symbol": "?", "x": 5, "y": 10},
                },
            }],
        }
        settlement = autoplay.authoritative_choice_settlement(record)
        self.assertEqual("observed", settlement["status"])
        self.assertTrue(settlement["fully_observable"])
        self.assertEqual(
            "immediate_protocol_transition",
            settlement["observation_scope"],
        )

    def test_designer_a0_consequences_type_full_service_downstream_effects(self):
        designer_class = "com.megacrit.cardcrawl.events.shrines.Designer"
        designer_instance = {
            "adjustment_upgrades_one": True,
            "clean_up_removes_cards": True,
            "adjust_cost": 40,
            "clean_up_cost": 60,
            "full_service_cost": 90,
            "hp_loss": 3,
        }
        designer_kinds = (
            ("ADJUSTMENT_GRID_UPGRADE", {
                "gold_cost": 40, "upgrade_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("CLEAN_UP_GRID_PURGE", {
                "gold_cost": 60, "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("FULL_SERVICE", {
                "gold_cost": 90, "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                ),
            }),
            ("PUNCH_AND_LEAVE", {"hp_loss": 3}),
        )
        state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0,
                "relics": [],
                "screen_state": {
                    "event_id": "Designer",
                    "options": [{
                        "choice_index": index,
                        "original_button_index": index,
                        "disabled": False,
                        "event_contract": staged_event_contract(
                            "Designer", designer_class, "MAIN", index,
                            kind, designer_instance, parameters,
                        ),
                    } for index, (kind, parameters) in enumerate(
                        designer_kinds
                    )],
                },
            },
        }
        by_index = {}
        for index in range(4):
            by_index[index] = autoplay._structured_option_consequence(
                "EVENT",
                staged_event_target(
                    "Designer", designer_class, "MAIN", index, index,
                    designer_kinds[index][0], designer_instance,
                    designer_kinds[index][1],
                ),
                "localized",
                state=state,
            )

        self.assertEqual(-40, by_index[0]["gold_delta"])
        self.assertEqual(
            "designer_adjustment_grid_upgrade", by_index[0]["operation"]
        )
        self.assertEqual(
            "classified_future",
            by_index[0]["uncertainty_classification"]["status"],
        )
        self.assertEqual(-60, by_index[1]["gold_delta"])
        self.assertEqual(
            "designer_clean_up_grid_purge", by_index[1]["operation"]
        )
        self.assertEqual(-90, by_index[2]["gold_delta"])
        self.assertEqual(
            ["grid_purge", "random_upgrade"],
            [row["operation"] for row in by_index[2]["future_costs"]],
        )
        self.assertEqual(
            "known_domain",
            by_index[2]["field_knowledge"]["probabilistic_outcomes"][
                "status"
            ],
        )
        self.assertEqual(
            "classified_future",
            by_index[2]["uncertainty_classification"]["status"],
        )
        self.assertEqual(-3, by_index[3]["hp_delta"])
        self.assertTrue(by_index[3]["leave"])
        self.assertEqual([], by_index[3]["future_costs"])

    def test_cleric_a0_uses_exact_index_state_and_ignores_numeric_text(self):
        cleric_class = "com.megacrit.cardcrawl.events.exordium.Cleric"
        cleric_instance = {
            "heal_amount": 17,
            "heal_gold_cost": 35,
            "purify_cost": 50,
        }
        cleric_kinds = (
            ("HEAL", {"gold_cost": 35, "heal_amount": 17}),
            ("PURIFY", {
                "gold_cost_if_purgeable": 50,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("LEAVE", {}),
        )
        state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0,
                "current_hp": 60,
                "max_hp": 71,
                "gold": 100,
                "screen_state": {
                    "event_id": "The Cleric",
                    "options": [{
                        "choice_index": index,
                        "original_button_index": index,
                        "disabled": False,
                        "event_contract": staged_event_contract(
                            "The Cleric", cleric_class, "MAIN", index,
                            kind, cleric_instance, parameters,
                        ),
                    } for index, (kind, parameters) in enumerate(
                        cleric_kinds
                    )],
                },
            },
        }
        heal = autoplay._structured_option_consequence(
            "EVENT",
            staged_event_target(
                "The Cleric", cleric_class, "MAIN", 0, 0,
                cleric_kinds[0][0], cleric_instance, cleric_kinds[0][1],
            ),
            "Lose 999 HP and gain 1 Gold",
            state=state,
        )
        purge = autoplay._structured_option_consequence(
            "EVENT",
            staged_event_target(
                "The Cleric", cleric_class, "MAIN", 1, 1,
                cleric_kinds[1][0], cleric_instance, cleric_kinds[1][1],
            ),
            "Gain 999 Gold",
            state=state,
        )

        self.assertEqual(11, heal["hp_delta"])
        self.assertEqual(-35, heal["gold_delta"])
        self.assertEqual("cleric_heal", heal["operation"])
        self.assertEqual(-50, purge["gold_delta"])
        self.assertEqual(
            "cleric_grid_selection", purge["future_costs"][0]["kind"]
        )
        self.assertEqual(
            "classified_future", purge["uncertainty_classification"]["status"]
        )

    def test_goop_contract_projects_private_loss_and_rejects_wrong_binding(self):
        gather_contract = goop_event_contract(0)
        leave_contract = goop_event_contract(1, gold_loss=27)
        state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0,
                "current_hp": 70,
                "max_hp": 70,
                "gold": 36,
                "relics": [],
                "screen_state": {
                    "event_id": "World of Goop",
                    "options": [
                        {
                            "choice_index": 1, "disabled": False,
                            "original_button_index": 1,
                            "event_contract": leave_contract,
                        },
                        {
                            "choice_index": 0, "disabled": False,
                            "original_button_index": 0,
                            "event_contract": gather_contract,
                        },
                    ],
                },
            },
        }
        gather = autoplay._structured_option_consequence(
            "EVENT",
            goop_event_target(0, 0),
            "claims lose 1 HP and gain 999 Gold",
            state=state,
        )
        exact_leave = autoplay._structured_option_consequence(
            "EVENT",
            goop_event_target(1, 1, gold_loss=27),
            "Lose 1 Gold",
            state=state,
        )
        wrong_event = autoplay._structured_option_consequence(
            "EVENT",
            {
                "kind": "event_option", "event_id": "Other Event",
                "choice_index": 0,
            },
            "Gain 999 Gold",
            state=state,
        )

        self.assertEqual((75, -11), (
            gather["gold_delta"], gather["hp_delta"]
        ))
        self.assertEqual(-27, exact_leave["gold_delta"])
        self.assertEqual("world_of_goop_leave", exact_leave["operation"])
        self.assertEqual(
            "unknown", wrong_event["field_knowledge"]["gold_delta"]["status"]
        )
        self.assertIsNone(wrong_event["gold_delta"])

        malformed_target = goop_event_target(1, 1, gold_loss=27)
        malformed_target["mechanism_id"] = "event-mechanism:tampered"
        malformed = autoplay._structured_option_consequence(
            "EVENT",
            malformed_target,
            "Lose 27 Gold",
            state=state,
        )
        self.assertIsNone(malformed["gold_delta"])
        self.assertIn(
            "typed_event_contract_target_binding_mismatch",
            malformed["uncertainty"],
        )

    def test_knowing_skull_typed_costs_tungsten_reserve_and_settlement(self):
        targets, options = knowing_skull_surface(costs=(7, 10, 8, 6))
        game = {
            "ascension_level": 0, "current_hp": 19, "max_hp": 68,
            "gold": 510,
            "relics": [{"id": "Tungsten Rod"}],
            "potions": [{"id": "Potion Slot", "slot": 0}],
            "screen_state": {
                "event_id": "Knowing Skull", "options": options,
            },
        }
        state = {"phase": "EVENT", "game_state": game}
        projected = [
            autoplay._structured_option_consequence(
                "EVENT", target, "tampered localized text", state=state,
            )
            for target in targets
        ]
        self.assertEqual([-6, -9, -7, -5], [row["hp_delta"] for row in projected])
        self.assertEqual(90, projected[1]["gold_delta"])
        self.assertEqual(5, projected[1]["future_costs"][0]["reserved_hp_loss"])
        self.assertEqual(
            "random_potion_gain",
            projected[0]["potion_changes"]["gain"][0]["kind"],
        )
        self.assertEqual(
            "random_card_gain",
            projected[2]["card_changes"]["gain"][0]["kind"],
        )
        self.assertEqual("knowing_skull_leave", projected[3]["operation"])

        selected = projected[1]
        record = {
            "phase": "EVENT", "before_seq": 226520, "after_seq": 226523,
            "requested_target_id": "skull:gold",
            "resolved_target_id": "skull:gold",
            "decision_outcome": {
                "current_hp_delta": -9, "max_hp_delta": 0,
                "gold_delta": 90, "block_delta": 0,
                "deck": {"added": [], "removed": [], "changed": []},
                "relics": {"added": [], "removed": [], "changed": []},
                "potions": {"added": [], "removed": [], "changed": []},
                "keys_before": {}, "keys_after": {},
            },
            "legal_choices_before": [{
                "choice_id": "skull:gold", "selected": True,
                "target": targets[1], "consequences": selected,
            }],
        }
        settlement = autoplay.authoritative_choice_settlement(record)
        self.assertEqual("observed", settlement["status"])
        self.assertEqual(
            "knowing_skull_exit_reserve",
            settlement["classified_future_costs"][0]["kind"],
        )

        forged_targets, forged_options = knowing_skull_surface()
        forged_options[1]["event_contract"]["parameters"]["hp_loss"] = 1
        forged_game = {**game, "screen_state": {
            "event_id": "Knowing Skull", "options": forged_options,
        }}
        forged = autoplay._structured_option_consequence(
            "EVENT", forged_targets[1], "Lose 1 HP",
            state={"phase": "EVENT", "game_state": forged_game},
        )
        self.assertIsNone(forged["hp_delta"])
        self.assertIn(
            "typed_event_contract_missing_invalid_or_mismatched",
            forged["uncertainty"],
        )

    def test_event_single_dialog_noops_and_cursed_tome_stay_typed(self):
        expected = {
            "The Cleric": "cleric_dialog_advance_noop",
            "Designer": "designer_dialog_advance_noop",
            "World of Goop": "world_of_goop_dialog_advance_noop",
            "The Mausoleum": "mausoleum_dialog_advance_noop",
        }
        for event_id, operation in expected.items():
            with self.subTest(event_id=event_id):
                raw_option = {
                    "choice_index": 0, "disabled": False,
                    "original_button_index": 0,
                }
                target = {
                    "kind": "event_option", "event_id": event_id,
                    "choice_index": 0, "original_button_index": 0,
                }
                if event_id == "World of Goop":
                    contract = goop_event_contract(
                        0, continue_only=True
                    )
                    raw_option["event_contract"] = contract
                    target = goop_event_target(
                        0, 0, continue_only=True
                    )
                elif event_id == "The Cleric":
                    contract = staged_event_contract(
                        "The Cleric",
                        "com.megacrit.cardcrawl.events.exordium.Cleric",
                        "RESULT", 0, "CONTINUE",
                        {
                            "heal_amount": 17,
                            "heal_gold_cost": 35,
                            "purify_cost": 50,
                        },
                        {},
                    )
                    raw_option["event_contract"] = contract
                    target = staged_event_target(
                        "The Cleric",
                        "com.megacrit.cardcrawl.events.exordium.Cleric",
                        "RESULT", 0, 0, "CONTINUE",
                        {
                            "heal_amount": 17,
                            "heal_gold_cost": 35,
                            "purify_cost": 50,
                        },
                        {},
                    )
                elif event_id == "Designer":
                    contract = staged_event_contract(
                        "Designer",
                        "com.megacrit.cardcrawl.events.shrines.Designer",
                        "DONE", 0, "CONTINUE",
                        {
                            "adjustment_upgrades_one": True,
                            "clean_up_removes_cards": True,
                            "adjust_cost": 40,
                            "clean_up_cost": 60,
                            "full_service_cost": 90,
                            "hp_loss": 3,
                        },
                        {},
                    )
                    raw_option["event_contract"] = contract
                    target = staged_event_target(
                        "Designer",
                        "com.megacrit.cardcrawl.events.shrines.Designer",
                        "DONE", 0, 0, "CONTINUE",
                        {
                            "adjustment_upgrades_one": True,
                            "clean_up_removes_cards": True,
                            "adjust_cost": 40,
                            "clean_up_cost": 60,
                            "full_service_cost": 90,
                            "hp_loss": 3,
                        },
                        {},
                    )
                elif event_id == "Cursed Tome":
                    contract = staged_event_contract(
                        "Cursed Tome",
                        "com.megacrit.cardcrawl.events.city.CursedTome",
                        "INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN",
                        {
                            "final_hp_loss": 10,
                            "damage_taken": 0,
                            "random_relic_pool": [
                                "Necronomicon", "Enchiridion", "Nilry's Codex",
                            ],
                        },
                        {
                            "future_hp_loss_to_complete": 16,
                            "random_relic_count": 1,
                            "reward_surface": "COMBAT_REWARD",
                            "selection_mode": "UNIFORM_MISC_RNG",
                        },
                    )
                    raw_option["event_contract"] = contract
                    target = staged_event_target(
                        "Cursed Tome",
                        "com.megacrit.cardcrawl.events.city.CursedTome",
                        "INTRO", 0, 0, "ENTER_RANDOM_BOOK_CHAIN",
                        {
                            "final_hp_loss": 10,
                            "damage_taken": 0,
                            "random_relic_pool": [
                                "Necronomicon", "Enchiridion", "Nilry's Codex",
                            ],
                        },
                        {
                            "future_hp_loss_to_complete": 16,
                            "random_relic_count": 1,
                            "reward_surface": "COMBAT_REWARD",
                            "selection_mode": "UNIFORM_MISC_RNG",
                        },
                    )
                state = {
                    "phase": "EVENT",
                    "game_state": {
                        "ascension_level": 0,
                        "max_hp": 70 if event_id == "The Cleric" else 70,
                        "relics": [],
                        "screen_state": {
                            "event_id": event_id,
                            "options": [raw_option],
                        },
                    },
                }
                consequence = autoplay._structured_option_consequence(
                    "EVENT",
                    target,
                    "Gain 999 Gold and lose 999 HP",
                    state=state,
                )
                self.assertEqual(operation, consequence["operation"])
                self.assertEqual((0, 0, 0), (
                    consequence["hp_delta"],
                    consequence["max_hp_delta"],
                    consequence["gold_delta"],
                ))

        cursed_state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0,
                "relics": [],
                "screen_state": {
                    "event_id": "Cursed Tome",
                    "options": [{
                        "choice_index": index,
                        "original_button_index": index,
                        "disabled": False,
                        "event_contract": staged_event_contract(
                            "Cursed Tome",
                            "com.megacrit.cardcrawl.events.city.CursedTome",
                            "INTRO", index, kind,
                            {
                                "final_hp_loss": 10,
                                "damage_taken": 0,
                                "random_relic_pool": [
                                    "Necronomicon", "Enchiridion", "Nilry's Codex",
                                ],
                            },
                            parameters,
                        ),
                    } for index, (kind, parameters) in enumerate((
                        (
                            "ENTER_RANDOM_BOOK_CHAIN", {
                                "future_hp_loss_to_complete": 16,
                                "random_relic_count": 1,
                                "reward_surface": "COMBAT_REWARD",
                                "selection_mode": "UNIFORM_MISC_RNG",
                            },
                        ),
                        ("LEAVE", {}),
                    ))],
                },
            },
        }
        cursed = autoplay._structured_option_consequence(
            "EVENT",
            staged_event_target(
                "Cursed Tome",
                "com.megacrit.cardcrawl.events.city.CursedTome",
                "INTRO", 0, 0, "ENTER_RANDOM_BOOK_CHAIN",
                {
                    "final_hp_loss": 10,
                    "damage_taken": 0,
                    "random_relic_pool": [
                        "Necronomicon", "Enchiridion", "Nilry's Codex",
                    ],
                },
                {
                    "future_hp_loss_to_complete": 16,
                    "random_relic_count": 1,
                    "reward_surface": "COMBAT_REWARD",
                    "selection_mode": "UNIFORM_MISC_RNG",
                },
            ),
            "Gain Necronomicon for free",
            state=cursed_state,
        )
        self.assertEqual(
            "cursed_tome_enter_random_book_chain", cursed["operation"]
        )
        self.assertEqual(
            "known", cursed["field_knowledge"]["hp_delta"]["status"]
        )
        self.assertIn(
            "unsupported_necronomicon_activation_state",
            cursed["reason_codes"],
        )

    def test_designer_and_cleric_future_kinds_settle_as_classified(self):
        cases = (
            (
                "Designer",
                2,
                {
                    "ascension_level": 0,
                    "screen_state": {
                        "event_id": "Designer",
                        "options": [
                            {"choice_index": index, "disabled": False}
                            for index in range(4)
                        ],
                    },
                },
                -90,
            ),
            (
                "The Cleric",
                1,
                {
                    "ascension_level": 0,
                    "current_hp": 70,
                    "max_hp": 70,
                    "screen_state": {
                        "event_id": "The Cleric",
                        "options": [
                            {"choice_index": index, "disabled": False}
                            for index in range(3)
                        ],
                    },
                },
                -50,
            ),
        )
        for event_id, index, game, gold_delta in cases:
            with self.subTest(event_id=event_id):
                if event_id == "Designer":
                    event_class = (
                        "com.megacrit.cardcrawl.events.shrines.Designer"
                    )
                    instance = {
                        "adjustment_upgrades_one": True,
                        "clean_up_removes_cards": True,
                        "adjust_cost": 40,
                        "clean_up_cost": 60,
                        "full_service_cost": 90,
                        "hp_loss": 3,
                    }
                    kinds = (
                        ("ADJUSTMENT_GRID_UPGRADE", {
                            "gold_cost": 40, "upgrade_select_count": 1,
                            "selection_mode": "PLAYER_SELECT",
                        }),
                        ("CLEAN_UP_GRID_PURGE", {
                            "gold_cost": 60, "purge_select_count": 1,
                            "selection_mode": "PLAYER_SELECT",
                        }),
                        ("FULL_SERVICE", {
                            "gold_cost": 90, "purge_select_count": 1,
                            "random_upgrade_max_count": 1,
                            "selection_mode": (
                                "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                            ),
                        }),
                        ("PUNCH_AND_LEAVE", {"hp_loss": 3}),
                    )
                else:
                    event_class = (
                        "com.megacrit.cardcrawl.events.exordium.Cleric"
                    )
                    instance = {
                        "heal_amount": 17,
                        "heal_gold_cost": 35,
                        "purify_cost": 50,
                    }
                    kinds = (
                        ("HEAL", {"gold_cost": 35, "heal_amount": 17}),
                        ("PURIFY", {
                            "gold_cost_if_purgeable": 50,
                            "purge_select_count": 1,
                            "selection_mode": "PLAYER_SELECT",
                        }),
                        ("LEAVE", {}),
                    )
                game["screen_state"]["options"] = [{
                    "choice_index": option_index,
                    "original_button_index": option_index,
                    "disabled": False,
                    "event_contract": staged_event_contract(
                        event_id, event_class, "MAIN", option_index,
                        option_kind, instance, parameters,
                    ),
                } for option_index, (option_kind, parameters) in enumerate(
                    kinds
                )]
                option_kind, parameters = kinds[index]
                target = staged_event_target(
                    event_id, event_class, "MAIN", index, index,
                    option_kind, instance, parameters,
                )
                consequence = autoplay._structured_option_consequence(
                    "EVENT",
                    target,
                    "ignored",
                    state={"phase": "EVENT", "game_state": game},
                )
                record = {
                    "phase": "EVENT", "before_seq": 10, "after_seq": 11,
                    "requested_target_id": "event", "resolved_target_id": "event",
                    "decision_outcome": {
                        "current_hp_delta": 0, "max_hp_delta": 0,
                        "gold_delta": gold_delta, "block_delta": 0,
                        "deck": {"added": [], "removed": [], "changed": []},
                        "relics": {"added": [], "removed": [], "changed": []},
                        "potions": {"added": [], "removed": [], "changed": []},
                        "keys_before": {}, "keys_after": {},
                    },
                    "legal_choices_before": [{
                        "choice_id": "event", "selected": True,
                        "target": target,
                        "uncertainty": "; ".join(consequence["uncertainty"]),
                        "consequences": consequence,
                    }],
                }
                settlement = autoplay.authoritative_choice_settlement(record)
                self.assertEqual("observed", settlement["status"])
                self.assertEqual(
                    consequence["future_costs"],
                    settlement["classified_future_costs"],
                )

    def test_mausoleum_a0_a15_and_leave_use_typed_exhaustive_contract(self):
        writhe = {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
        }
        for ascension, probability, outcome_count in (
            (0, 0.5, 2), (15, 1.0, 1),
        ):
            with self.subTest(ascension=ascension):
                state = {
                    "phase": "EVENT",
                    "game_state": {
                        "ascension_level": ascension,
                        "relics": [],
                        "screen_state": {
                            "event_id": "The Mausoleum",
                            "options": [
                                {
                                    "choice_index": 0, "disabled": False,
                                    "card": writhe,
                                },
                                {"choice_index": 1, "disabled": False},
                            ],
                        },
                    },
                }
                opened = autoplay._structured_option_consequence(
                    "EVENT",
                    {
                        "kind": "event_option",
                        "event_id": "The Mausoleum",
                        "choice_index": 0,
                    },
                    "adversarial display text without mechanics",
                    state=state,
                )

                self.assertEqual(
                    "base_game_the_mausoleum_v1",
                    opened["mechanism_id"],
                )
                random_relic = opened["relic_changes"]["random_gain"][0]
                self.assertEqual(
                    "base_game_non_boss_relic_pool", random_relic["domain"]
                )
                self.assertEqual(["BOSS"], random_relic["excluded_rarities"])
                self.assertEqual(1, random_relic["count"])
                self.assertEqual(
                    probability, opened["curse"]["probability"]
                )
                self.assertEqual(
                    outcome_count, len(opened["probabilistic_outcomes"])
                )
                self.assertAlmostEqual(
                    1.0,
                    sum(
                        row["probability"]
                        for row in opened["probabilistic_outcomes"]
                    ),
                )
                self.assertEqual(
                    {"gold": 0, "hp": 0, "max_hp": 0},
                    opened["current_cost"],
                )
                self.assertEqual([], opened["future_costs"])
                self.assertEqual(
                    "exhaustive_probability",
                    opened["uncertainty_classification"]["status"],
                )

                leave = autoplay._structured_option_consequence(
                    "EVENT",
                    {
                        "kind": "event_option",
                        "event_id": "The Mausoleum",
                        "choice_index": 1,
                    },
                    "fake reward text",
                    state=state,
                )
                self.assertEqual("mausoleum_leave", leave["operation"])
                self.assertEqual(
                    (0, 0, 0),
                    (
                        leave["hp_delta"], leave["max_hp_delta"],
                        leave["gold_delta"],
                    ),
                )
                self.assertEqual([], leave["random_effects"])
                self.assertEqual([], leave["probabilistic_outcomes"])

    def test_mausoleum_omamori_branch_types_counter_without_card_gain(self):
        writhe = {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
        }
        state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0,
                "relics": [{"id": "Omamori", "counter": 2}],
                "screen_state": {
                    "event_id": "The Mausoleum",
                    "options": [
                        {
                            "choice_index": 0, "disabled": False,
                            "card": writhe,
                        },
                        {"choice_index": 1, "disabled": False},
                    ],
                },
            },
        }

        consequence = autoplay._structured_option_consequence(
            "EVENT",
            {
                "kind": "event_option", "event_id": "The Mausoleum",
                "choice_index": 0, "card": writhe,
            },
            "ignored",
            state=state,
        )

        self.assertEqual([], consequence["card_changes"]["conditional_gain"])
        self.assertTrue(consequence["curse"]["blocked"])
        self.assertEqual(
            0.0, consequence["curse"]["effective_gain_probability"]
        )
        blocked = next(
            row for row in consequence["probabilistic_outcomes"]
            if row["id"] == "writhe_blocked_by_omamori"
        )
        self.assertEqual(
            [{"id": "Omamori", "before": 2, "after": 1, "delta": -1}],
            blocked["relic_changes"]["counter"],
        )
        self.assertEqual([], blocked["card_changes"]["gain"])

    def test_mausoleum_projection_rejects_event_index_and_card_mismatch(self):
        writhe = {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
        }
        state = {
            "phase": "EVENT",
            "game_state": {
                "ascension_level": 0, "relics": [],
                "screen_state": {
                    "event_id": "The Mausoleum",
                    "options": [
                        {
                            "choice_index": 0, "disabled": False,
                            "card": writhe,
                        },
                        {"choice_index": 1, "disabled": False},
                    ],
                },
            },
        }
        cases = (
            (
                {
                    "kind": "event_option", "event_id": "Other Event",
                    "choice_index": 0, "card": writhe,
                },
                "mausoleum_event_id_mismatch",
            ),
            (
                {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 2, "card": writhe,
                },
                "mausoleum_choice_index_binding_mismatch",
            ),
            (
                {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 0,
                    "card": {**writhe, "id": "Doubt"},
                },
                "mausoleum_target_card_binding_mismatch",
            ),
        )
        for target, expected in cases:
            with self.subTest(expected=expected):
                consequence = autoplay._structured_option_consequence(
                    "EVENT", target, "claims a free relic", state=state
                )
                self.assertIn(expected, consequence["uncertainty"])
                self.assertNotIn("mechanism_id", consequence)
                self.assertEqual(
                    "unknown",
                    consequence["field_knowledge"]["relic_changes"][
                        "status"
                    ],
                )

    def test_match_hidden_identity_is_classified_not_silently_known(self):
        record = {
            "phase": "EVENT",
            "before_seq": 30,
            "after_seq": 31,
            "requested_target_id": "match:2",
            "resolved_target_id": "match:2",
            "decision_outcome": {
                "current_hp_delta": 0,
                "max_hp_delta": 0,
                "gold_delta": 0,
                "block_delta": 0,
                "deck": {"added": [], "removed": [], "changed": []},
                "relics": {"added": [], "removed": [], "changed": []},
                "potions": {"added": [], "removed": [], "changed": []},
                "keys_before": {},
                "keys_after": {},
            },
            "legal_choices_before": [{
                "choice_id": "match:2",
                "selected": True,
                "uncertainty": "hidden card identity is not protocol-visible",
                "consequences": {
                    "operation": "flip_match_position",
                    "future_costs": [],
                },
            }],
        }
        settlement = autoplay.authoritative_choice_settlement(record)
        self.assertEqual("observed", settlement["status"])
        self.assertIn(
            "hidden card identity",
            settlement["classified_future_uncertainty"],
        )

    def test_operational_error_falls_back_to_last_verified_live_frame(self):
        state, context, _terminal, _audit = terminal_attempt_fixture(
            "attempt-bridge-loss"
        )
        autoplay.remember_authoritative_state(state, context)

        with patch.object(
            autoplay.stsctl, "load_state", side_effect=OSError("bridge down")
        ), patch.object(
            autoplay.run_context_lib,
            "load_context",
            side_effect=OSError("context temporarily unreadable"),
        ):
            recovered = autoplay.active_error_context()
        with patch.object(
            autoplay.stsctl,
            "current_sequence",
            side_effect=PermissionError("state file locked"),
        ):
            recovered_seq = autoplay.operational_error_state_sequence(
                recovered
            )

        self.assertEqual(context["attempt_id"], recovered["attempt_id"])
        self.assertEqual(state["state_seq"], recovered_seq)
        snapshot = autoplay.operational_error_state_snapshot(recovered)
        self.assertEqual(state["state_seq"], snapshot["state_seq"])
        self.assertEqual(
            state["game_state"]["seed"],
            snapshot["game_state"]["seed"],
        )
        self.assertEqual(2, snapshot["protocol_version"])
        self.assertTrue(snapshot["in_game"])
        self.assertEqual(state["state_seq"], snapshot["terminal_state_seq"])
        for field in autoplay.ATTEMPT_BINDING_FIELDS:
            self.assertEqual(context[field], snapshot[field])

    def test_operational_error_never_uses_unobserved_newer_meta_sequence(self):
        state, context, _terminal, _audit = terminal_attempt_fixture(
            "attempt-meta-ahead"
        )
        autoplay.remember_authoritative_state(state, context)
        with patch.object(
            autoplay.stsctl,
            "current_sequence",
            return_value=state["state_seq"] + 50,
        ):
            self.assertEqual(
                state["state_seq"],
                autoplay.operational_error_state_sequence(context),
            )

    def test_cached_game_over_never_becomes_a_second_operational_terminal(self):
        _state, context, terminal, _audit = terminal_attempt_fixture(
            "attempt-terminal-audit-failure"
        )
        autoplay.remember_authoritative_state(terminal, context)

        with patch.object(
            autoplay.stsctl, "load_state", side_effect=OSError("bridge down")
        ):
            self.assertEqual({}, autoplay.active_error_context())
        self.assertIsNone(
            autoplay.operational_error_state_snapshot(context)
        )

    def test_exhaustive_probability_branch_requires_unique_protocol_delta(self):
        option = {
            "option_id": "event:coin",
            "choice_index": 0,
            "label": "Flip",
            "target": {
                "kind": "event_option", "event_id": "CoinFlip",
                "mechanism_id": "coin_flip_gold_10_or_zero",
                "probabilistic_outcomes": [
                    {"probability": 0.5, "gold_delta": 10},
                    {"probability": 0.5, "gold_delta": 0},
                ],
            },
        }
        before = self._noncombat_state(seq=30, option=option)
        after = self._noncombat_state(seq=31, gold=110)
        outcomes = [
            {"probability": 0.5, "gold_delta": 10},
            {"probability": 0.5, "gold_delta": 0},
        ]

        record = autoplay.compact_action(
            before,
            {"action": "choose", "option_id": "event:coin"},
            {
                "requested_target_id": "event:coin",
                "resolved_target_id": "event:coin",
            },
            after,
            decision={"candidates": [{
                "candidate_id": "event:coin",
                "choice_index": 0,
                "action": "choose",
                "score": 1,
                "consequences": {"probabilistic_outcomes": outcomes},
            }]},
            run_context=command_context(),
        )

        settlement = record["authoritative_choice_settlement"]
        self.assertEqual("observed", settlement["status"])
        self.assertTrue(settlement["fully_observable"])
        self.assertEqual(0, settlement["matched_probability_outcome_index"])

        ambiguous = copy.deepcopy(record)
        ambiguous["legal_choices_before"][0]["probability_outcomes"] = [
            {"probability": 0.5, "gold_delta": 10},
            {"probability": 0.5, "gold_delta": 10},
        ]
        ambiguous_settlement = autoplay.authoritative_choice_settlement(
            ambiguous
        )
        self.assertEqual("unresolved", ambiguous_settlement["status"])
        self.assertFalse(ambiguous_settlement["fully_observable"])

        unexplained_inventory_change = copy.deepcopy(record)
        unexplained_inventory_change["decision_outcome"]["deck"]["added"] = [
            {"id": "Doubt", "card_instance_id": "card:curse"}
        ]
        inventory_settlement = autoplay.authoritative_choice_settlement(
            unexplained_inventory_change
        )
        self.assertEqual("unresolved", inventory_settlement["status"])
        self.assertFalse(inventory_settlement["fully_observable"])

    def test_grid_consequences_bind_exact_uuid_operation_and_ignore_order(self):
        cards = [
            {
                "id": "Strike_R", "name": "Strike", "upgrades": 0,
                "card_instance_id": "uuid-strike",
            },
            {
                "id": "Bash", "name": "Bash", "upgrades": 0,
                "card_instance_id": "uuid-bash",
            },
        ]
        target = {
            "kind": "card", "card_instance_id": "uuid-bash",
            "card": copy.deepcopy(cards[1]),
        }
        expected_slots = {
            "for_upgrade": ("grid_upgrade", "upgrade", "known"),
            "for_transform": (
                "grid_transform", "transform", "known_domain",
            ),
            "for_purge": ("grid_purge", "remove", "known"),
        }
        for enabled, (operation, slot, knowledge) in expected_slots.items():
            with self.subTest(operation=operation):
                flags = {
                    name: name == enabled for name in expected_slots
                }
                state = {
                    "phase": "GRID",
                    "game_state": {"screen_state": {
                        **flags, "cards": copy.deepcopy(cards),
                    }},
                }
                consequence = autoplay._structured_option_consequence(
                    "GRID", target, "localized label", state=state
                )
                reordered = copy.deepcopy(state)
                reordered["game_state"]["screen_state"]["cards"].reverse()
                reordered_consequence = autoplay._structured_option_consequence(
                    "GRID", target, "localized label", state=reordered
                )

                self.assertEqual(operation, consequence["operation"])
                self.assertEqual([], consequence["card_changes"][slot])
                self.assertEqual(
                    "known",
                    consequence["field_knowledge"]["card_changes"]["status"],
                )
                followup = consequence["future_costs"][0]
                self.assertEqual(operation, followup["operation"])
                self.assertEqual(cards[1], followup["selected_card"])
                self.assertEqual(
                    "after_grid_confirmation", followup["commit_timing"]
                )
                self.assertEqual(consequence, reordered_consequence)

        tampered = copy.deepcopy(target)
        tampered["card"]["id"] = "Feed"
        invalid = autoplay._structured_option_consequence(
            "GRID", tampered, "localized label", state={
                "phase": "GRID", "game_state": {"screen_state": {
                    "for_upgrade": True, "for_transform": False,
                    "for_purge": False, "cards": cards,
                }},
            },
        )
        self.assertEqual(
            "unknown", invalid["field_knowledge"]["card_changes"]["status"]
        )
        self.assertIn("grid_target_card_facts_mismatch", invalid["uncertainty"])

        snapshot_state = {
            "phase": "GRID", "state_seq": 17,
            "game_state": {"screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": True, "cards": copy.deepcopy(cards),
                "selected_cards": [copy.deepcopy(cards[0])],
                "num_cards": 2, "any_number": False,
                "confirm_up": False,
            }},
        }
        screen = autoplay.authoritative_state_snapshot(snapshot_state)[
            "game_state"
        ]["screen_state"]
        self.assertEqual(2, screen["num_cards"])
        self.assertFalse(screen["any_number"])
        self.assertFalse(screen["confirm_up"])
        self.assertEqual(
            "uuid-strike", screen["selected_cards"][0]["card_instance_id"]
        )

    def test_hand_select_consequence_is_transient_and_uuid_bound(self):
        cards = [
            {
                "id": "Defend_R", "name": "Defend", "upgrades": 0,
                "card_instance_id": "hand:defend", "type": "SKILL",
                "cost": 1,
            },
            {
                "id": "Strike_R", "name": "Strike", "upgrades": 0,
                "card_instance_id": "hand:strike", "type": "ATTACK",
                "cost": 1,
            },
        ]
        state = {
            "phase": "HAND_SELECT",
            "game_state": {
                "current_action": "ArmamentsAction",
                "screen_state": {
                    "hand": copy.deepcopy(cards), "selected": [],
                    "max_cards": 1, "can_pick_zero": False,
                },
            },
        }
        target = {
            "kind": "card", "card_instance_id": "hand:strike",
            "card": copy.deepcopy(cards[1]),
        }

        consequence = autoplay._structured_option_consequence(
            "HAND_SELECT", target, "localized label", state=state
        )
        reordered = copy.deepcopy(state)
        reordered["game_state"]["screen_state"]["hand"].reverse()

        self.assertEqual("hand_select_card", consequence["operation"])
        self.assertEqual(cards[1], consequence["selected_card"])
        self.assertEqual([], consequence["card_changes"]["gain"])
        self.assertEqual(
            "ArmamentsAction",
            consequence["future_costs"][0]["current_action"],
        )
        self.assertEqual(
            consequence,
            autoplay._structured_option_consequence(
                "HAND_SELECT", target, "localized label", state=reordered
            ),
        )

        missing_action = copy.deepcopy(state)
        missing_action["game_state"].pop("current_action")
        unknown = autoplay._structured_option_consequence(
            "HAND_SELECT", target, "localized label", state=missing_action
        )
        self.assertIn(
            "hand_select_current_action_missing", unknown["uncertainty"]
        )

        snapshot = autoplay.authoritative_state_snapshot({
            "phase": "HAND_SELECT", "state_seq": 9,
            "game_state": copy.deepcopy(state["game_state"]),
        })
        self.assertEqual(
            "ArmamentsAction", snapshot["game_state"]["current_action"]
        )
        self.assertEqual(
            "hand:strike",
            snapshot["game_state"]["screen_state"]["hand"][1][
                "card_instance_id"
            ],
        )

    def test_map_entry_relic_healing_is_part_of_immediate_consequence(self):
        # A combat-only reward adds the selected card to the transient hand,
        # not to the permanent master deck audited by ``card_changes``.
        card = {
            "id": "Discovery Result",
            "name": "Discovery Result",
            "card_instance_id": "temporary-card",
            "type": "SKILL",
            "rarity": "UNCOMMON",
            "upgrades": 0,
        }
        target = {
            "kind": "card",
            "card_instance_id": "temporary-card",
            "card": copy.deepcopy(card),
        }
        transient = autoplay._structured_option_consequence(
            "CARD_REWARD", target, "localized", state={
                "phase": "CARD_REWARD",
                "game_state": {
                    "room_phase": "COMBAT",
                    "combat_state": {"hand": [], "monsters": []},
                },
            },
        )
        permanent = autoplay._structured_option_consequence(
            "CARD_REWARD", target, "localized", state={
                "phase": "CARD_REWARD",
                "game_state": {"room_phase": "COMPLETE"},
            },
        )
        self.assertEqual(
            "add_temporary_combat_card", transient["operation"]
        )
        self.assertEqual([], transient["card_changes"]["gain"])
        self.assertEqual("gain_card_reward", permanent["operation"])
        self.assertEqual([card], permanent["card_changes"]["gain"])

        state = {"phase": "MAP", "game_state": {
            "current_hp": 50, "max_hp": 80,
            "deck": [{"id": f"card-{index}"} for index in range(12)],
            "relics": [
                {"id": "Blood Vial"}, {"id": "Meal Ticket"},
                {"id": "Eternal Feather"}, {"id": "Pantograph"},
                {"id": "MawBank", "counter": -1},
            ],
        }}
        targets = (
            ({"kind": "map_node", "symbol": "M", "x": 1, "y": 2}, 2),
            ({"kind": "map_node", "symbol": "$", "x": 1, "y": 2}, 15),
            ({"kind": "map_node", "symbol": "R", "x": 1, "y": 2}, 6),
            ({"kind": "map_boss", "act": 1}, 27),
            ({"kind": "map_node", "symbol": "?", "x": 1, "y": 2}, 0),
        )
        for target, expected_heal in targets:
            with self.subTest(target=target):
                consequence = autoplay._structured_option_consequence(
                    "MAP", target, "localized", state=state
                )
                self.assertEqual(expected_heal, consequence["hp_delta"])
                self.assertEqual(12, consequence["gold_delta"])

        spent = copy.deepcopy(state)
        spent["game_state"]["relics"][-1]["counter"] = -2
        consequence = autoplay._structured_option_consequence(
            "MAP", targets[0][0], "localized", state=spent
        )
        self.assertEqual(0, consequence["gold_delta"])

        bloom = copy.deepcopy(state)
        bloom["game_state"]["relics"].append({"id": "Mark of the Bloom"})
        for target, _ in targets:
            with self.subTest(mark_of_the_bloom_target=target):
                consequence = autoplay._structured_option_consequence(
                    "MAP", target, "localized", state=bloom
                )
                self.assertEqual(0, consequence["hp_delta"])

        bloom = copy.deepcopy(state)
        bloom["game_state"]["relics"].append({"id": "Mark of the Bloom"})
        for target, _ in targets:
            with self.subTest(mark_of_the_bloom_target=target):
                consequence = autoplay._structured_option_consequence(
                    "MAP", target, "localized", state=bloom
                )
                self.assertEqual(0, consequence["hp_delta"])

        missing_counter = copy.deepcopy(state)
        del missing_counter["game_state"]["relics"][-1]["counter"]
        consequence = autoplay._structured_option_consequence(
            "MAP", targets[0][0], "localized", state=missing_counter
        )
        self.assertEqual("unknown", consequence["field_knowledge"]["gold_delta"]["status"])

    def test_shop_purge_binds_cost_and_defers_exact_removal_to_grid(self):
        target = {"kind": "purge", "price": 75}
        state = {
            "phase": "SHOP_SCREEN",
            "game_state": {
                "gold": 100,
                "screen_state": {
                    "purge_available": True, "purge_cost": 75,
                },
            },
        }
        consequence = autoplay._structured_option_consequence(
            "SHOP_SCREEN", target, "purge", state=state
        )

        self.assertEqual(0, consequence["gold_delta"])
        self.assertEqual(
            {"gold": 0, "hp": 0, "max_hp": 0},
            consequence["current_cost"],
        )
        self.assertEqual([], consequence["card_changes"]["remove"])
        self.assertEqual(
            "shop_purge_grid_selection",
            consequence["future_costs"][0]["kind"],
        )
        self.assertEqual(75, consequence["future_costs"][0]["gold_cost"])
        self.assertEqual("open_card_purge_grid", consequence["operation"])

        forged = autoplay._structured_option_consequence(
            "SHOP_SCREEN", {"kind": "purge", "price": 74}, "purge",
            state=state,
        )
        self.assertEqual(
            "unknown", forged["field_knowledge"]["gold_delta"]["status"]
        )
        self.assertIn("shop_purge_cost_binding_invalid", forged["uncertainty"])

        record = {
            "phase": "SHOP_SCREEN", "before_seq": 10, "after_seq": 11,
            "requested_target_id": "purge", "resolved_target_id": "purge",
            "decision_outcome": {
                "current_hp_delta": 0, "max_hp_delta": 0,
                "gold_delta": -75, "block_delta": 0,
                "deck": {"added": [], "removed": [], "changed": []},
                "relics": {"added": [], "removed": [], "changed": []},
                "potions": {"added": [], "removed": [], "changed": []},
                "keys_before": {}, "keys_after": {},
            },
            "legal_choices_before": [{
                "choice_id": "purge", "selected": True,
                "uncertainty": "; ".join(consequence["uncertainty"]),
                "consequences": consequence,
            }],
        }
        settlement = autoplay.authoritative_choice_settlement(record)
        self.assertEqual("observed", settlement["status"])
        self.assertEqual(
            consequence["future_costs"], settlement["classified_future_costs"]
        )

    def test_current_card_damage_projection_defers_repeats_to_ordered_search(self):
        card = SimpleNamespace(
            uuid="card:heavy", card_id="Heavy Blade", damage=10, cost=2,
            type=SimpleNamespace(name="ATTACK"),
        )
        monster = SimpleNamespace(
            current_hp=30, block=2, powers=[], half_dead=False,
            is_gone=False, monster_id="GiantHead", monster_index=0,
        )
        parsed = SimpleNamespace(
            hand=[card], monsters=[monster], relics=[],
            player=SimpleNamespace(powers=[SimpleNamespace(
                power_id="DoubleTapPower", power_name="Double Tap",
                amount=1,
            )]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "monsters": [{"enemy_instance_id": "enemy:one"}],
            },
        }
        with patch.object(autoplay.Game, "from_json", return_value=parsed), patch.object(
            autoplay.combat_predictor, "living_monsters", return_value=[monster]
        ), patch.object(
            autoplay.combat_predictor, "card_attack_profile", return_value=(10, 1)
        ):
            self.assertIs(
                autoplay._CURRENT_ATTACK_UNRESOLVED,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id="card:heavy",
                    target_id="enemy:one",
                ),
            )
            monster.current_hp = 15
            self.assertIs(
                autoplay._CURRENT_ATTACK_UNRESOLVED,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id="card:heavy",
                    target_id="enemy:one",
                ),
            )
            parsed.player.powers = [SimpleNamespace(
                power_id="DuplicationPower", power_name="Duplication",
                amount=2,
            )]
            monster.current_hp = 30
            self.assertIs(
                autoplay._CURRENT_ATTACK_UNRESOLVED,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id="card:heavy",
                    target_id="enemy:one",
                ),
            )

    def test_duplication_sword_boomerang_uses_ordered_search_and_hp_cap(self):
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "player": {
                    "powers": [{"id": "DuplicationPower", "amount": 1}],
                },
                "monsters": [{
                    "id": "Orb Walker", "monster_index": 0,
                    "enemy_instance_id": "enemy:orb-walker",
                    "current_hp": 69, "block": 0,
                }],
            },
        }
        decision = {
            "reason": "duplication_potion_bound_next_card",
            "card_id": "Sword Boomerang",
            "card_uuid": "card:sword-boomerang",
            "planned_sequence": [{
                "card_id": "Sword Boomerang",
                "card_uuid": "card:sword-boomerang",
                "target_key": None,
            }],
            "search": {
                "first_action_enemy_hp_loss": 69,
                "first_action_resolution_count": 2,
                "true_combat_end": True,
            },
        }

        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_ATTACK_UNRESOLVED,
        ) as static_projection:
            model = autoplay.damage_model_context(
                "play", decision,
                {"phase_after": "COMPLETE", "enemy_hp_loss": 69},
                before_game=before,
                available_commands=["play"],
                card_id="Sword Boomerang",
                card_instance_id="card:sword-boomerang",
            )

        static_projection.assert_not_called()
        self.assertEqual(69, model["hero_to_monsters_predicted"])
        self.assertEqual(69, model["hero_to_monsters_actual"])
        self.assertEqual(
            "turn_search_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )
        static_projection.assert_not_called()

    def test_current_card_damage_projection_applies_flight(self):
        card = SimpleNamespace(
            uuid="card:strike", card_id="Strike_R", damage=8, cost=1,
            type=SimpleNamespace(name="ATTACK"),
        )
        monster = SimpleNamespace(
            current_hp=30, block=0, half_dead=False, is_gone=False,
            monster_id="Byrd", monster_index=0,
            powers=[SimpleNamespace(
                power_id="Flight", power_name="Flight", amount=3,
            )],
        )
        parsed = SimpleNamespace(
            hand=[card], monsters=[monster], relics=[],
            player=SimpleNamespace(powers=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "monsters": [{"enemy_instance_id": "enemy:byrd"}],
            },
        }
        with patch.object(
            autoplay.Game, "from_json", return_value=parsed,
        ), patch.object(
            autoplay.combat_predictor, "living_monsters",
            return_value=[monster],
        ), patch.object(
            autoplay.combat_predictor, "card_attack_profile",
            return_value=(8, 1),
        ):
            self.assertEqual(
                4,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id="card:strike",
                    target_id="enemy:byrd",
                ),
            )

    def test_current_whirlwind_projection_keeps_flight_for_whole_card(self):
        card = SimpleNamespace(
            uuid="card:whirlwind", card_id="Whirlwind", damage=5, cost=-1,
            type=SimpleNamespace(name="ATTACK"),
        )
        monsters = [
            SimpleNamespace(
                current_hp=9, block=0, half_dead=False, is_gone=False,
                monster_id="Byrd", monster_index=0,
                powers=[
                    SimpleNamespace(
                        power_id="Flight", power_name="Flight", amount=2,
                    ),
                    SimpleNamespace(
                        power_id="Vulnerable", power_name="Vulnerable",
                        amount=1,
                    ),
                ],
            ),
        ] + [
            SimpleNamespace(
                current_hp=17, block=0, half_dead=False, is_gone=False,
                monster_id="Byrd", monster_index=index,
                powers=[SimpleNamespace(
                    power_id="Flight", power_name="Flight", amount=2,
                )],
            )
            for index in (1, 2)
        ]
        parsed = SimpleNamespace(
            hand=[card], monsters=monsters, relics=[],
            player=SimpleNamespace(powers=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "monsters": [
                    {"enemy_instance_id": "enemy:byrd:0"},
                    {"enemy_instance_id": "enemy:byrd:1"},
                    {"enemy_instance_id": "enemy:byrd:2"},
                ],
            },
        }
        with patch.object(
            autoplay.Game, "from_json", return_value=parsed,
        ), patch.object(
            autoplay.combat_predictor, "living_monsters",
            return_value=monsters,
        ), patch.object(
            autoplay.combat_predictor, "card_attack_profile",
            return_value=(20, 4),
        ):
            # The live trace dealt 9 + 8 + 8.  Flight loses stacks only after
            # all four damage packets from this Whirlwind have resolved.
            self.assertEqual(
                25,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id=card.uuid,
                ),
            )

    def test_fiend_fire_damage_defers_to_bound_turn_search(self):
        card = SimpleNamespace(
            uuid="card:fiend-fire", card_id="Fiend Fire", damage=7, cost=2,
            type=SimpleNamespace(name="ATTACK"),
        )
        monster = SimpleNamespace(
            current_hp=40, block=0, powers=[], half_dead=False,
            is_gone=False, monster_id="Darkling", monster_index=0,
        )
        parsed = SimpleNamespace(
            hand=[card], monsters=[monster], relics=[],
            player=SimpleNamespace(powers=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "monsters": [{"enemy_instance_id": "enemy:darkling"}],
            },
        }
        with patch.object(
            autoplay.Game, "from_json", return_value=parsed,
        ):
            self.assertIs(
                autoplay._CURRENT_ATTACK_UNRESOLVED,
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id=card.uuid,
                    target_id="enemy:darkling",
                ),
            )

        model = autoplay.damage_model_context(
            "play",
            {"search": {"first_action_enemy_hp_loss": 25}},
            {"enemy_hp_loss": 25},
            before_game=before,
            available_commands=["play"],
            card_instance_id=card.uuid,
            target_id="enemy:darkling",
        )
        self.assertEqual(25, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "turn_search_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_juggernaut_block_gain_publishes_narrow_verified_bounds(self):
        before = {
            "room_phase": "COMBAT",
            "relics": [{"id": "Ornamental Fan", "counter": 2}],
            "combat_state": {
                "player": {"powers": [{"id": "Juggernaut", "amount": 7}]},
                "hand": [{
                    "id": "Strike_R", "card_instance_id": "strike",
                    "type": "ATTACK", "base_block": -1,
                    "exhausts": False,
                }],
                "monsters": [{
                    "current_hp": 30, "is_gone": False, "half_dead": False,
                }],
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=10,
        ):
            model = autoplay.damage_model_context(
                "play",
                {"search": {"first_action_resolution_count": 1}},
                {"enemy_hp_loss": 17},
                before_game=before,
                available_commands=["play"],
                card_id="Strike_R",
                card_instance_id="strike",
            )

        self.assertEqual(
            "current_card_with_juggernaut_block_gain_bounds",
            model["hero_to_monsters_prediction_basis"],
        )
        self.assertEqual(10, model["hero_to_monsters_predicted_min"])
        self.assertEqual(17, model["hero_to_monsters_predicted_max"])
        self.assertEqual(
            [{"source": "ornamental_fan", "count": 1}],
            model["juggernaut_block_gain_contract"]["sources"],
        )

    def test_juggernaut_non_attack_block_no_longer_claims_zero_damage(self):
        before = {
            "room_phase": "COMBAT",
            "relics": [],
            "combat_state": {
                "player": {"powers": [{"id": "Juggernaut", "amount": 7}]},
                "hand": [{
                    "id": "Defend_R", "card_instance_id": "defend",
                    "type": "SKILL", "base_block": 5, "exhausts": False,
                }],
                "monsters": [{
                    "current_hp": 20, "is_gone": False, "half_dead": False,
                }],
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            model = autoplay.damage_model_context(
                "play", {"search": {"first_action_resolution_count": 1}},
                {"enemy_hp_loss": 7}, before_game=before,
                available_commands=["play"], card_id="Defend_R",
                card_instance_id="defend",
            )

        self.assertEqual(0, model["hero_to_monsters_predicted_min"])
        self.assertEqual(7, model["hero_to_monsters_predicted_max"])

    def test_current_random_attack_and_non_attack_never_reuse_stale_search_damage(self):
        attack = SimpleNamespace(
            uuid="card:sword-boomerang", card_id="Sword Boomerang",
            damage=3, cost=1, type=SimpleNamespace(name="ATTACK"),
        )
        monsters = [
            SimpleNamespace(
                current_hp=20, block=0, powers=[], half_dead=False,
                is_gone=False, monster_id=f"Slime{index}",
                monster_index=index,
            )
            for index in range(2)
        ]
        parsed = SimpleNamespace(
            hand=[attack], monsters=monsters, relics=[],
            player=SimpleNamespace(powers=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"monsters": [
                {"enemy_instance_id": "enemy:zero"},
                {"enemy_instance_id": "enemy:one"},
            ]},
        }
        with patch.object(autoplay.Game, "from_json", return_value=parsed), patch.object(
            autoplay.combat_predictor, "living_monsters", return_value=monsters
        ), patch.object(
            autoplay.combat_predictor, "card_attack_profile", return_value=(0, 3)
        ):
            self.assertEqual(
                {"minimum": 9, "maximum": 9},
                autoplay._current_card_attack_prediction(
                    before, ["play"], card_instance_id=attack.uuid
                ),
            )

        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value={"minimum": 7, "maximum": 12},
        ):
            bounded = autoplay.damage_model_context(
                "play", {"reason": "terminal_plan_continuation"},
                {"enemy_hp_loss": 10},
            )
        self.assertEqual(
            "current_card_random_target_hp_loss_bounds",
            bounded["hero_to_monsters_prediction_basis"],
        )
        self.assertEqual(7, bounded["hero_to_monsters_predicted_min"])
        self.assertEqual(12, bounded["hero_to_monsters_predicted_max"])
        self.assertNotIn("hero_to_monsters_predicted", bounded)

        stale_decision = {
            "reason": "terminal_plan_continuation",
            "search": {"first_action_enemy_hp_loss": 99},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_ATTACK_UNRESOLVED,
        ):
            unresolved = autoplay.damage_model_context(
                "play", stale_decision, {"enemy_hp_loss": 5}
            )
        self.assertEqual(
            "current_card_attack_unresolved",
            unresolved["hero_to_monsters_prediction_basis"],
        )
        self.assertIsNone(unresolved["hero_to_monsters_predicted"])
        self.assertEqual(5, unresolved["hero_to_monsters_actual"])

        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            harmless = autoplay.damage_model_context(
                "play", stale_decision, {"enemy_hp_loss": 0}
            )
            unexpected_damage = autoplay.damage_model_context(
                "play", stale_decision, {"enemy_hp_loss": 3}
            )
        self.assertEqual(
            "current_card_non_attack",
            harmless["hero_to_monsters_prediction_basis"],
        )
        self.assertNotIn("hero_to_monsters_predicted", harmless)
        self.assertIsNone(unexpected_damage["hero_to_monsters_predicted"])
        self.assertEqual(3, unexpected_damage["hero_to_monsters_actual"])

    def test_damage_telemetry_prefers_exact_current_search_for_reactive_copies(self):
        decision = {
            "reason": "ordered_turn_search",
            "search": {
                "first_action_enemy_hp_loss": 9,
                "first_action_resolution_count": 2,
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=12,
        ) as static_projection:
            model = autoplay.damage_model_context(
                "play",
                decision,
                {"phase_after": "COMBAT_TURN_2", "enemy_hp_loss": 9},
            )

        self.assertEqual(9, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "turn_search_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_guaranteed_combo_uses_explicit_first_action_binding(self):
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "player": {"powers": [], "orbs": []},
                "hand": [{
                    "id": "Cold Snap", "card_instance_id": "cold-snap",
                    "type": "ATTACK", "damage": 6, "base_damage": 6,
                    "cost": 1, "has_target": True,
                }],
                "monsters": [{
                    "id": "SlaverRed", "enemy_instance_id": "enemy:red",
                    "current_hp": 12, "block": 0,
                    "is_gone": False, "half_dead": False,
                }],
            },
        }
        decision = {
            "reason": "guaranteed_attack_combo_lethal",
            "card_id": "Cold Snap",
            "target_key": ["slaverred", 0],
            "first_action_enemy_hp_loss": 12,
            "search": {"true_combat_end": True},
        }

        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=6
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 12},
                before_game=before, available_commands=["play"],
                card_id="Cold Snap", card_instance_id="cold-snap",
                target_id="enemy:red",
            )

        self.assertEqual(12, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "turn_search_bound_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )


    def test_damage_telemetry_rebinds_safe_prefix_attack_override(self):
        decision = {
            "reason": "zero_loss_attack_progress",
            "card_damage": 22,
            "search": {
                "first_action_enemy_hp_loss": 0,
                "first_action_resolution_count": 1,
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=22,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 22}
            )

        self.assertEqual(22, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_final_target_hp_projection",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_does_not_bind_combo_attack_to_current_setup_skill(self):
        decision = {
            "reason": "guaranteed_combo_setup_before_attacks",
            "first_action_enemy_hp_loss": 11,
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 0}
            )

        self.assertEqual(
            "current_card_non_attack",
            model["hero_to_monsters_prediction_basis"],
        )
        self.assertNotIn("hero_to_monsters_predicted", model)
        self.assertNotIn("hero_to_monsters_actual", model)

    def test_damage_model_binds_ordered_search_to_current_non_attack(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Shrug It Off", "card_uuid": "card:shrug",
            }],
            "search": {"first_action_enemy_hp_loss": 10},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 10},
                card_id="Shrug It Off", card_instance_id="card:shrug",
            )

        self.assertEqual(10, model["hero_to_monsters_predicted"])
        self.assertEqual(10, model["hero_to_monsters_actual"])
        self.assertEqual(
            "turn_search_bound_non_attack_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_includes_bound_attack_orb_overflow(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Ball Lightning",
                "card_uuid": "card:ball",
                "target_key": ["cultist", 0],
            }],
            "search": {
                "first_action_enemy_hp_loss": 36,
                "first_action_expected_enemy_hp_loss": 36,
                "first_action_enemy_hp_loss_is_expected": False,
                "first_action_resolution_count": 1,
            },
        }
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"monsters": [{
                "id": "Cultist", "monster_index": 0,
                "enemy_instance_id": "enemy:cultist", "current_hp": 50,
            }]},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=10,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 36},
                before_game=before, card_id="Ball Lightning",
                card_instance_id="card:ball", target_id="enemy:cultist",
            )

        self.assertEqual(36, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "turn_search_bound_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_bounds_random_lightning_from_non_attack(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Dualcast", "card_uuid": "card:dualcast",
                "target_key": None,
            }],
            "search": {
                "first_action_enemy_hp_loss": 0,
                "first_action_expected_enemy_hp_loss": 16,
                "first_action_enemy_hp_loss_is_expected": True,
                "first_action_resolution_count": 1,
            },
        }
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"monsters": [
                {"id": "Byrd", "enemy_instance_id": "enemy:one",
                 "current_hp": 12},
                {"id": "Byrd", "enemy_instance_id": "enemy:two",
                 "current_hp": 20},
            ]},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 20},
                before_game=before, card_id="Dualcast",
                card_instance_id="card:dualcast",
            )

        self.assertNotIn("hero_to_monsters_predicted", model)
        self.assertEqual(0, model["hero_to_monsters_predicted_min"])
        self.assertEqual(32, model["hero_to_monsters_predicted_max"])
        self.assertEqual(16, model["hero_to_monsters_predicted_expected"])
        self.assertEqual(
            "turn_search_bound_non_attack_first_action_hp_loss_bounds",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_does_not_reuse_search_for_another_target(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Ball Lightning",
                "card_uuid": "card:ball",
                "target_key": ["cultist", 0],
            }],
            "search": {"first_action_enemy_hp_loss": 36},
        }
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"monsters": [
                {"id": "Cultist", "monster_index": 0,
                 "enemy_instance_id": "enemy:cultist", "current_hp": 50},
                {"id": "JawWorm", "monster_index": 1,
                 "enemy_instance_id": "enemy:jaw", "current_hp": 40},
            ]},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=10,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 10},
                before_game=before, card_id="Ball Lightning",
                card_instance_id="card:ball", target_id="enemy:jaw",
            )

        self.assertEqual(10, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_final_target_hp_projection",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_keeps_precise_static_aoe_over_lower_search_value(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Dagger Spray",
                "card_uuid": "card:spray",
                "target_key": None,
            }],
            "search": {
                "first_action_enemy_hp_loss": 8,
                "first_action_resolution_count": 1,
            },
        }
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"player": {"powers": []}, "monsters": [
                {"id": "FungiBeast", "enemy_instance_id": "enemy:one",
                 "current_hp": 3},
                {"id": "FungiBeast", "enemy_instance_id": "enemy:two",
                 "current_hp": 20},
            ]},
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=11,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 11},
                before_game=before, card_id="Dagger Spray",
                card_instance_id="card:spray",
            )

        self.assertEqual(11, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_final_target_hp_projection",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_uses_search_after_serialized_copy_power_is_spent(self):
        decision = {
            "reason": "ordered_turn_search",
            "planned_sequence": [{
                "card_id": "Sweeping Beam",
                "card_uuid": "card:beam",
                "target_key": None,
            }],
            "search": {
                "first_action_enemy_hp_loss": 6,
                "first_action_resolution_count": 1,
            },
        }
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "player": {"powers": [{"id": "EchoFormPower", "amount": 1}]},
                "monsters": [{
                    "id": "Cultist", "enemy_instance_id": "enemy:one",
                    "current_hp": 30,
                }],
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction", return_value=12,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 6},
                before_game=before, card_id="Sweeping Beam",
                card_instance_id="card:beam",
            )

        self.assertEqual(6, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "turn_search_bound_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_emergency_offering_models_ready_letter_opener_packet(self):
        offering = SimpleNamespace(
            uuid="card:offering", card_id="Offering",
            type=SimpleNamespace(name="SKILL"),
        )
        monster = SimpleNamespace(current_hp=5)
        parsed = SimpleNamespace(
            hand=[offering], monsters=[monster], relics=[],
        )
        before = {"room_phase": "COMBAT"}
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ), patch.object(
            autoplay.Game, "from_json", return_value=parsed,
        ), patch.object(
            autoplay.combat_predictor, "relic_ids", return_value=set(),
        ), patch.object(
            autoplay.combat_predictor,
            "letter_opener_damage", return_value=5,
        ), patch.object(
            autoplay.combat_predictor,
            "living_monsters", return_value=[monster],
        ), patch.object(
            autoplay.combat_predictor, "attack_hp_loss", return_value=5,
        ):
            model = autoplay.damage_model_context(
                "play", {"reason": "emergency_resource_rescue"},
                {"enemy_hp_loss": 5}, before_game=before,
                available_commands=["play"], card_id="Offering",
                card_instance_id="card:offering",
            )

        self.assertEqual(5, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_offering_letter_opener_exact_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_telemetry_defers_copy_split_by_grid_selection(self):
        model = autoplay.damage_model_context(
            "play",
            {
                "reason": "ordered_turn_search",
                "search": {
                    "first_action_enemy_hp_loss": 58,
                    "first_action_resolution_count": 2,
                },
            },
            {"phase_after": "GRID", "enemy_hp_loss": 28},
        )

        self.assertEqual(
            "deferred_card_resolution",
            model["hero_to_monsters_prediction_basis"],
        )
        self.assertEqual(
            "deferred_card_resolution",
            model["hero_to_monsters_settlement"],
        )
        self.assertNotIn("hero_to_monsters_predicted", model)
        self.assertNotIn("hero_to_monsters_actual", model)

    def test_action_combat_end_requires_current_action_lethal_damage(self):
        decision = {
            "planned_sequence": [{"card_id": "Cleave"}],
            "search": {"true_combat_end": True},
        }

        self.assertFalse(autoplay.action_true_combat_end_prediction(
            decision,
            current_action_enemy_hp_loss=8,
            living_enemy_hp_total=13,
        ))
        self.assertTrue(autoplay.action_true_combat_end_prediction(
            decision,
            current_action_enemy_hp_loss=13,
            living_enemy_hp_total=13,
        ))
        self.assertFalse(autoplay.action_true_combat_end_prediction(decision))

    def test_postcombat_heal_reconstructs_hidden_combust_loss(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 59, "max_hp": 72,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {
                "player": {"powers": [{"id": "Combust", "amount": 5}]},
            },
        }
        outcome = {
            "phase_after": "COMPLETE",
            "screen_type_after": "COMBAT_REWARD",
            "player_hp_delta": 5,
            "player_hp_loss": 0,
        }

        model = autoplay.damage_model_context(
            "end",
            {
                "projected_attack_hp_loss_before": 0,
                "projected_end_turn_hp_loss_before": 1,
            },
            outcome,
            before_game=before,
        )

        self.assertEqual(1, model["monsters_to_hero_predicted"])
        self.assertEqual(1, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_total_hp_loss_before_postcombat_healing",
            model["monsters_to_hero_basis"],
        )

    def test_postcombat_heal_includes_exact_regeneration(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 30, "max_hp": 80,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {
                "player": {
                    "powers": [{"id": "Regeneration", "amount": 1}],
                },
            },
        }
        outcome = {
            "phase_after": "COMBAT_REWARD",
            "room_phase_after": "COMPLETE",
            "screen_type_after": "COMBAT_REWARD",
            "player_hp_delta": 5,
            "player_hp_loss": 0,
        }

        model = autoplay.damage_model_context(
            "end",
            {
                "projected_attack_hp_loss_before": 2,
                "projected_end_turn_hp_loss_before": 0,
            },
            outcome,
            before_game=before,
        )

        self.assertEqual(2, model["monsters_to_hero_predicted"])
        self.assertEqual(2, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_total_hp_loss_before_postcombat_healing",
            model["monsters_to_hero_basis"],
        )

    def test_postcombat_heal_with_inactive_meat_on_the_bone_is_exact(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 58, "max_hp": 72,
            "relics": [
                {"id": "Burning Blood"}, {"id": "Meat on the Bone"},
            ],
            "combat_state": {"player": {"powers": []}},
        }
        outcome = {
            "phase_after": "COMPLETE",
            "screen_type_after": "COMBAT_REWARD",
            "player_hp_delta": -1,
        }

        self.assertEqual(7, autoplay._exact_postcombat_hp_loss(before, outcome))

        at_half = copy.deepcopy(before)
        at_half["current_hp"] = 43
        self.assertIsNone(
            autoplay._exact_postcombat_hp_loss(at_half, outcome)
        )

    def test_combat_reward_phase_is_postcombat_for_burning_blood(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 34, "max_hp": 83,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {"player": {"powers": []}},
        }
        model = autoplay.damage_model_context(
            "end",
            {
                "projected_attack_hp_loss_before": 0,
                "projected_end_turn_hp_loss_before": 0,
            },
            {
                "phase_after": "COMBAT_REWARD",
                "room_phase_after": "COMPLETE",
                "screen_type_after": "COMBAT_REWARD",
                "player_hp_delta": 6,
                "player_hp_loss": 0,
            },
            before_game=before,
        )

        self.assertEqual(0, model["monsters_to_hero_predicted"])
        self.assertEqual(0, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_total_hp_loss_before_postcombat_healing",
            model["monsters_to_hero_basis"],
        )

    def test_fairy_revival_reconstructs_hidden_gross_damage(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 11, "max_hp": 85,
            "relics": [],
            "potions": [{
                "id": "FairyPotion",
                "potion_instance_id": "potion:fairy",
            }],
            "combat_state": {"player": {"powers": []}},
        }
        after = {
            "room_phase": "COMBAT", "current_hp": 20, "max_hp": 85,
            "relics": [], "potions": [],
            "combat_state": {"player": {"powers": []}},
        }
        outcome = {
            "phase_after": "COMBAT_TURN_11",
            "room_phase_after": "COMBAT",
            "screen_type_after": "NONE",
            "player_hp_delta": 9,
            "player_hp_loss": 0,
        }

        with patch.object(
            autoplay.Game, "from_json", return_value=SimpleNamespace()
        ), patch.object(
            autoplay.combat_predictor,
            "fairy_in_a_bottle_healing",
            return_value=25,
        ):
            model = autoplay.damage_model_context(
                "end",
                {
                    "projected_attack_hp_loss_before": 16,
                    "projected_end_turn_hp_loss_before": 0,
                },
                outcome,
                before_game=before,
                after_game=after,
            )

        self.assertEqual(16, model["monsters_to_hero_predicted"])
        self.assertEqual(16, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_total_hp_loss_before_fairy_revival",
            model["monsters_to_hero_basis"],
        )

    def test_burning_blood_is_not_applied_during_ordinary_combat_end(self):
        before = {
            "room_phase": "COMBAT", "current_hp": 72, "max_hp": 72,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {"player": {"powers": []}},
        }
        model = autoplay.damage_model_context(
            "end",
            {
                "projected_attack_hp_loss_before": 8,
                "projected_end_turn_hp_loss_before": 0,
            },
            {
                "phase_after": "COMBAT_TURN_2",
                "room_phase_after": "COMBAT",
                "screen_type_after": "NONE",
                "player_hp_delta": -8,
                "player_hp_loss": 8,
            },
            before_game=before,
        )

        self.assertEqual(8, model["monsters_to_hero_predicted"])
        self.assertEqual(8, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_player_hp_delta", model["monsters_to_hero_basis"]
        )

    def test_end_damage_includes_brutality_next_turn_start_loss(self):
        model = autoplay.damage_model_context(
            "end",
            {
                "projected_attack_hp_loss_before": 6,
                "projected_end_turn_hp_loss_before": 0,
                "projected_next_turn_start_hp_loss_before": 1,
                "projected_hp_loss_before": 7,
            },
            {
                "phase_after": "COMBAT_TURN_3",
                "room_phase_after": "COMBAT",
                "screen_type_after": "NONE",
                "player_hp_delta": -7,
                "player_hp_loss": 7,
            },
        )

        self.assertEqual(7, model["monsters_to_hero_predicted"])
        self.assertEqual(7, model["monsters_to_hero_actual"])
        self.assertEqual(
            "end_turn_player_hp_delta", model["monsters_to_hero_basis"]
        )

    def test_non_damaging_potion_omits_unasserted_damage_pair(self):
        model = autoplay.damage_model_context(
            "potion", {}, {"enemy_hp_loss": 0}
        )

        self.assertNotIn("hero_to_monsters_predicted", model)
        self.assertNotIn("hero_to_monsters_actual", model)

    def test_direct_damage_potion_uses_exact_decision_projection(self):
        model = autoplay.damage_model_context(
            "potion",
            {"potion_immediate_enemy_hp_loss": 20},
            {"enemy_hp_loss": 20},
        )

        self.assertEqual(20, model["hero_to_monsters_predicted"])
        self.assertEqual(20, model["hero_to_monsters_actual"])
        self.assertEqual(
            "direct_potion_exact_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_fallback_potion_prediction_is_capped_by_bound_target_hp(self):
        before = {
            "combat_state": {"monsters": [{
                "enemy_instance_id": "enemy:slaver",
                "current_hp": 6,
                "is_gone": False,
                "half_dead": False,
            }]},
        }
        model = autoplay.damage_model_context(
            "potion",
            {"first_action_enemy_hp_loss": 10},
            {"enemy_hp_loss": 6},
            before_game=before,
            target_id="enemy:slaver",
        )

        self.assertEqual(6, model["hero_to_monsters_predicted"])
        self.assertEqual(6, model["hero_to_monsters_actual"])

    def test_exact_aoe_potion_prediction_keeps_sum_across_living_targets(self):
        before = {
            "combat_state": {"monsters": [
                {
                    "enemy_instance_id": "enemy:louse-a",
                    "current_hp": 15,
                    "is_gone": False,
                    "half_dead": False,
                },
                {
                    "enemy_instance_id": "enemy:louse-b",
                    "current_hp": 15,
                    "is_gone": False,
                    "half_dead": False,
                },
            ]},
        }
        model = autoplay.damage_model_context(
            "potion",
            {
                "potion_id": "Explosive Potion",
                "potion_immediate_enemy_hp_loss": 30,
            },
            {"enemy_hp_loss": 30},
            before_game=before,
            target_id="enemy:louse-a",
        )

        self.assertEqual(30, model["hero_to_monsters_predicted"])
        self.assertEqual(30, model["hero_to_monsters_actual"])
        self.assertEqual(
            "direct_potion_exact_aoe_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_invincible_explosive_potion_publishes_truthful_damage_bounds(self):
        before = {
            "combat_state": {"monsters": [{
                "enemy_instance_id": "enemy:heart",
                "id": "CorruptHeart",
                "current_hp": 300,
                "is_gone": False,
                "half_dead": False,
                "powers": [{
                    "id": "InvinciblePower", "name": "Invincible",
                    "amount": 200,
                }],
            }]},
        }
        model = autoplay.damage_model_context(
            "potion",
            {
                "potion_id": "Explosive Potion",
                "potion_immediate_enemy_hp_loss_bounds": {
                    "minimum": 0, "maximum": 10,
                },
            },
            {"enemy_hp_loss": 10},
            before_game=before,
            target_id="enemy:heart",
        )

        self.assertEqual(0, model["hero_to_monsters_predicted_min"])
        self.assertEqual(10, model["hero_to_monsters_predicted_max"])
        self.assertEqual(10, model["hero_to_monsters_actual"])
        self.assertEqual(
            "direct_potion_aoe_hp_loss_bounds",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_exact_target_potion_prediction_caps_overkill(self):
        before = {
            "combat_state": {"monsters": [{
                "enemy_instance_id": "enemy:darkling",
                "current_hp": 11,
                "is_gone": False,
                "half_dead": False,
            }]},
        }
        model = autoplay.damage_model_context(
            "potion",
            {
                "potion_id": "Fire Potion",
                "potion_immediate_enemy_hp_loss": 20,
            },
            {"enemy_hp_loss": 11},
            before_game=before,
            target_id="enemy:darkling",
        )

        self.assertEqual(11, model["hero_to_monsters_predicted"])
        self.assertEqual(11, model["hero_to_monsters_actual"])
        self.assertEqual(
            "direct_potion_exact_target_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )


class MacroFingerprintTests(IsolatedAutoplayTestCase):
    def test_audit_supervisor_and_bridge_artifacts_change_fingerprints(self):
        decision_sources = (
            "autoplay_runner.py",
            "campaign_attempt.py",
            "strategy_audit.py",
            "independent_oracle.py",
            "death_replay.py",
            "cohort_review.py",
            "freeze_manifest.py",
            "decision_case_corpus.py",
            "decision_case_replay.py",
            "test_fixtures/decision-cases-v2.jsonl",
            "CommunicationMod.jar",
        )
        controller_sources = (
            "autoplay_runner.py",
            "campaign_attempt.py",
            "pre_run_binding.py",
            "decision_case_corpus.py",
            "bridge.py",
            "CommunicationMod.jar",
        )

        baseline_decision = autoplay.decision_fingerprint()
        baseline_performance = autoplay.performance_fingerprint()
        baseline_controller = autoplay.controller_fingerprint()
        original_read_bytes = Path.read_bytes
        for relative in decision_sources:
            target = (autoplay.ROOT / relative).resolve()

            def changed_bytes(path, *, _target=target):
                value = original_read_bytes(path)
                return value + b"audit-freeze-change" if path.resolve() == _target else value

            with patch.object(Path, "read_bytes", changed_bytes):
                self.assertNotEqual(
                    baseline_decision,
                    autoplay.decision_fingerprint(),
                    relative,
                )
                self.assertEqual(
                    baseline_performance,
                    autoplay.performance_fingerprint(),
                    relative,
                )
        for relative in controller_sources:
            target = (autoplay.ROOT / relative).resolve()

            def changed_bytes(path, *, _target=target):
                value = original_read_bytes(path)
                return value + b"controller-freeze-change" if path.resolve() == _target else value

            with patch.object(Path, "read_bytes", changed_bytes):
                self.assertNotEqual(
                    baseline_controller,
                    autoplay.controller_fingerprint(),
                    relative,
                )

    def test_live_policy_source_changes_performance_fingerprint(self):
        baseline = autoplay.performance_fingerprint()
        target = (
            autoplay.ROOT
            / "src/spirecomm-master/spirecomm/ai/agent.py"
        ).resolve()
        original_read_bytes = Path.read_bytes

        def changed_bytes(path):
            value = original_read_bytes(path)
            return value + b"policy-change" if path.resolve() == target else value

        with patch.object(Path, "read_bytes", changed_bytes):
            self.assertNotEqual(
                baseline, autoplay.performance_fingerprint()
            )

    def test_generated_replay_evidence_does_not_create_policy_hash_cycle(self):
        baseline_decision = autoplay.decision_fingerprint()
        baseline_controller = autoplay.controller_fingerprint()
        original_read_bytes = Path.read_bytes
        for relative in (
            "decision-case-trace-evidence.json",
            "decision-case-resolutions.json",
        ):
            target = (autoplay.ROOT / relative).resolve()

            def changed_bytes(path, *, _target=target):
                value = original_read_bytes(path)
                return value + b"generated-evidence-change" if (
                    path.resolve() == _target
                ) else value

            with patch.object(Path, "read_bytes", changed_bytes):
                self.assertEqual(
                    baseline_decision, autoplay.decision_fingerprint(), relative
                )
                self.assertEqual(
                    baseline_controller, autoplay.controller_fingerprint(), relative
                )

    def test_macro_policy_profile_changes_decision_hash_without_secret(self):
        baseline = autoplay.decision_fingerprint()
        performance_baseline = autoplay.performance_fingerprint()
        with patch.dict(
            autoplay.MACRO_POLICY_PROFILE,
            {"mode": "assist", "synthetic_threshold": 0.123},
            clear=False,
        ):
            changed = autoplay.decision_fingerprint()
            performance_changed = autoplay.performance_fingerprint()
        self.assertNotEqual(baseline, changed)
        self.assertNotEqual(performance_baseline, performance_changed)
        self.assertNotIn("DEEPSEEK_API_KEY", json.dumps(
            autoplay.MACRO_POLICY_PROFILE, sort_keys=True
        ))


class ProtocolRecoveryTests(IsolatedAutoplayTestCase):
    def _patch_terminal_environment(
        self, stack, directory, context, terminal, audit_report
    ):
        directory = Path(directory)
        paths = {
            "context": directory / "run-context.json",
            "result": directory / "run-result.json",
            "history": directory / "run-history.jsonl",
            "trace": directory / "autoplay.log",
            "audit": directory / "run-audit.json",
            "cohort": directory / "cohort-report.json",
            "death": directory / "death-replay.json",
        }
        paths["context"].write_text(
            json.dumps(context), encoding="utf-8"
        )
        paths["trace"].write_text("", encoding="utf-8")
        for attribute, key in (
            ("RUN_CONTEXT_PATH", "context"),
            ("RESULT_PATH", "result"),
            ("RESULT_HISTORY_PATH", "history"),
            ("TRACE_PATH", "trace"),
            ("RUN_AUDIT_PATH", "audit"),
            ("COHORT_REPORT_PATH", "cohort"),
            ("DEATH_REPLAY_PATH", "death"),
        ):
            stack.enter_context(patch.object(autoplay, attribute, paths[key]))
        stack.enter_context(
            patch.object(autoplay.stsctl, "load_state", return_value=terminal)
        )
        stack.enter_context(
            patch(
                "strategy_audit.audit_attempt_trace",
                return_value=audit_report,
            )
        )
        return paths

    def _assert_single_terminal_and_audit(self, paths, attempt_id):
        history = [
            json.loads(line)
            for line in paths["history"].read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        terminals = [
            record for record in history
            if record.get("record_type") == "terminal_result"
            and record.get("attempt_id") == attempt_id
        ]
        audits = [
            record for record in history
            if record.get("record_type") == "run_audit"
            and record.get("attempt_id") == attempt_id
        ]
        self.assertEqual(1, len(terminals))
        self.assertEqual(1, len(audits))

        global_trace = [
            json.loads(line)
            for line in paths["trace"].read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        isolated_path = autoplay.attempt_trace_path(attempt_id)
        isolated_trace = [
            json.loads(line)
            for line in isolated_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual([], global_trace)
        matching = [
            record for record in isolated_trace
            if record.get("record_type") == "terminal_result"
            and record.get("attempt_id") == attempt_id
        ]
        self.assertEqual(1, len(matching))

        attempt_directory = isolated_path.parent
        self.assertTrue((attempt_directory / "run-audit.json").exists())
        self.assertTrue((attempt_directory / "run-result.json").exists())
        self.assertTrue((attempt_directory / "terminal-state.json").exists())
        self.assertTrue((attempt_directory / "selection.json").exists())

    def test_controller_lease_rejects_a_second_live_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "controller.lock"
            with autoplay.ControllerLease(lock_path):
                with self.assertRaisesRegex(
                    autoplay.SafetyError, "another autoplay controller"
                ):
                    with autoplay.ControllerLease(lock_path):
                        self.fail("second controller unexpectedly acquired lock")
            with autoplay.ControllerLease(lock_path):
                pass

    def test_canonical_choices_normalize_return_and_preserve_veto(self):
        state = {
            "phase": "EVENT",
            "available_commands": [
                "choose", "skip", "potion", "key", "click", "wait", "state",
            ],
            "options": [
                {
                    "option_id": "event:fight",
                    "choice_index": 0,
                    "label": "Fight",
                    "target": {"kind": "event_option", "event_id": "evt"},
                },
                {
                    "option_id": "event:pay",
                    "choice_index": 1,
                    "label": "Pay",
                    "target": {"kind": "event_option", "event_id": "evt"},
                },
            ],
        }
        decision = {
            "candidates": [
                {
                    "id": "event:fight",
                    "choice_index": 0,
                    "action": "choose",
                    "score": 9.0,
                    "consequences": {"hp_delta": -5},
                },
                {
                    "id": "event:pay",
                    "choice_index": 1,
                    "action": "choose",
                    "score": -1000000.0,
                    "selection_eligible": False,
                    "veto_reason": "insufficient_survival_value",
                    "consequences": {"gold_delta": -50},
                },
                {
                    "id": "action:return",
                    "choice_index": None,
                    "action": "return",
                    "score": 1.0,
                    "consequences": {"leave": True},
                },
            ],
        }

        choices = autoplay.canonical_legal_choices(
            state,
            {"action": "return", "target_id": "action:return"},
            decision,
        )

        self.assertEqual(
            {"event:fight", "event:pay", "action:return"},
            {row["choice_id"] for row in choices},
        )
        required = set(__import__("independent_oracle").CANONICAL_CHOICE_FIELDS)
        self.assertTrue(all(required <= set(row) for row in choices))
        pay = next(row for row in choices if row["choice_id"] == "event:pay")
        returned = next(
            row for row in choices if row["choice_id"] == "action:return"
        )
        self.assertFalse(pay["selection_eligible"])
        self.assertEqual("insufficient_survival_value", pay["veto_reason"])
        self.assertTrue(returned["selected"])
        self.assertEqual("skip", returned["raw_text"])

    def test_unknown_localized_event_remains_null_and_partitioned(self):
        state = {
            "phase": "EVENT",
            "available_commands": ["choose"],
            "options": [{
                "option_id": "event:unknown:0", "choice_index": 0,
                "label": "接受未知的命运",
                "target": {"kind": "event_option", "event_id": "Unknown"},
            }],
        }
        decision = {"candidates": [canonical_test_candidate(
            "event:unknown:0", 0, 1,
            consequences={"mystery_effect": "不可见"},
        )]}

        row = autoplay.canonical_legal_choices(
            state,
            {"action": "choose", "option_id": "event:unknown:0"},
            decision,
        )[0]

        self.assertIsNone(row["consequences"]["hp_delta"])
        self.assertIsNone(row["consequences"]["max_hp_delta"])
        self.assertIsNone(row["consequences"]["gold_delta"])
        self.assertEqual(
            "unknown",
            row["consequences"]["field_knowledge"]["gold_delta"]["status"],
        )
        self.assertEqual({}, row["producer_consequence_claim"])
        self.assertEqual(
            ["mystery_effect"], row["unclassified_producer_fields"]
        )
        self.assertEqual(
            {"mystery_effect": "不可见"},
            row["producer_consequence_raw"]["value"],
        )

    def test_typed_neow_surface_is_text_independent_and_reorder_stable(self):
        options = [
            typed_neow_option(0, "HUNDRED_GOLD", label="获得100金币"),
            typed_neow_option(1, "UPGRADE_CARD", label="升级一张牌"),
            typed_neow_option(
                2, "ONE_RANDOM_RARE_CARD", label="随机稀有牌"
            ),
            typed_neow_option(
                3, "TWENTY_PERCENT_HP_BONUS", "PERCENT_DAMAGE",
                label="更强韧的命运",
            ),
        ]
        state = {
            "phase": "NEOW", "available_commands": ["choose"],
            "game_state": {
                "current_hp": 80, "max_hp": 80, "gold": 99,
                "relics": [{"id": "Burning Blood"}],
            },
            "options": options,
        }
        candidates = []
        for option in options:
            consequence = autoplay._structured_option_consequence(
                "NEOW", option["target"], option["label"], state=state
            )
            claim = {
                key: copy.deepcopy(value)
                for key, value in consequence.items()
                if key in autoplay._PRODUCER_EFFECT_FIELDS
            }
            candidates.append(canonical_test_candidate(
                option["option_id"], option["choice_index"],
                10 - option["choice_index"], consequences=claim,
            ))

        rows = autoplay.canonical_legal_choices(
            state,
            {"action": "choose", "option_id": "neow:0"},
            {"candidates": candidates},
        )
        reordered = autoplay.canonical_legal_choices(
            {**state, "options": list(reversed(options))},
            {"action": "choose", "option_id": "neow:0"},
            {"candidates": list(reversed(candidates))},
        )
        by_id = {row["choice_id"]: row for row in rows}
        reordered_by_id = {row["choice_id"]: row for row in reordered}

        self.assertEqual(
            {f"neow:{index}" for index in range(4)},
            {row["semantic_id"] for row in rows},
        )
        self.assertEqual(100, by_id["neow:0"]["consequences"]["gold_delta"])
        self.assertEqual(
            "neow_grid_selection",
            by_id["neow:1"]["consequences"]["future_costs"][0]["kind"],
        )
        self.assertEqual(
            "character_cards",
            by_id["neow:2"]["consequences"]["random_effects"][0]["domain"],
        )
        self.assertEqual(-8, by_id["neow:3"]["consequences"]["hp_delta"])
        self.assertEqual(16, by_id["neow:3"]["consequences"]["max_hp_delta"])
        self.assertEqual(
            {
                key: (
                    value["semantic_id"], value["selected"],
                    value["consequences"],
                )
                for key, value in by_id.items()
            },
            {
                key: (
                    value["semantic_id"], value["selected"],
                    value["consequences"],
                )
                for key, value in reordered_by_id.items()
            },
        )

    def test_neow_without_typed_contract_remains_unknown(self):
        consequence = autoplay._structured_option_consequence(
            "NEOW", {"kind": "event_option", "event_id": "Neow Event"},
            "获得100金币",
            state={
                "phase": "NEOW",
                "game_state": {"current_hp": 80, "max_hp": 80, "gold": 99},
            },
        )

        self.assertIsNone(consequence["gold_delta"])
        self.assertEqual(
            "unknown", consequence["field_knowledge"]["gold_delta"]["status"]
        )
        self.assertIn("typed_neow_contract_missing", consequence["uncertainty"])

    def test_known_card_and_relic_pickups_use_mechanism_specific_values(self):
        card = {"id": "Shrug It Off", "name": "Shrug It Off", "upgrades": 0}
        card_state = {
            "phase": "CARD_REWARD", "available_commands": ["choose"],
            "game_state": {"relics": []},
            "options": [{
                "option_id": "card:shrug", "choice_index": 0,
                "label": "Shrug It Off",
                "target": {"kind": "card", "card": card},
            }],
        }
        card_row = autoplay.canonical_legal_choices(
            card_state,
            {"action": "choose", "option_id": "card:shrug"},
            {"candidates": [canonical_test_candidate(
                "card:shrug", 0, 3,
                consequences={
                    "hp_delta": 0, "max_hp_delta": 0, "gold_delta": 0,
                    "card_changes": {
                        "gain": [card], "remove": [], "upgrade": [],
                        "transform": [],
                    },
                },
            )]},
        )[0]
        self.assertEqual(0, card_row["consequences"]["gold_delta"])
        self.assertEqual(
            [card], card_row["consequences"]["card_changes"]["gain"]
        )

        relic_state = {
            "phase": "BOSS_REWARD", "available_commands": ["choose"],
            "game_state": {"relics": []},
            "options": [
                {
                    "option_id": "relic:mango", "choice_index": 0,
                    "label": "Mango",
                    "target": {"kind": "relic", "relic": {"id": "Mango"}},
                },
                {
                    "option_id": "relic:data", "choice_index": 1,
                    "label": "Data Disk",
                    "target": {"kind": "relic", "relic": {"id": "DataDisk"}},
                },
            ],
        }
        candidates = [
            canonical_test_candidate(
                "relic:mango", 0, 4,
                consequences={"hp_delta": 14, "max_hp_delta": 14, "gold_delta": 0},
            ),
            canonical_test_candidate(
                "relic:data", 1, 5,
                consequences={"hp_delta": 0, "max_hp_delta": 0, "gold_delta": 0},
            ),
        ]
        rows = autoplay.canonical_legal_choices(
            relic_state,
            {"action": "choose", "option_id": "relic:data"},
            {"candidates": candidates},
        )
        by_id = {row["choice_id"]: row for row in rows}
        self.assertEqual(14, by_id["relic:mango"]["consequences"]["hp_delta"])
        self.assertEqual(14, by_id["relic:mango"]["consequences"]["max_hp_delta"])
        self.assertEqual(0, by_id["relic:data"]["consequences"]["gold_delta"])

    def test_sapphire_linked_tiny_chest_pickup_is_fully_known(self):
        relic = {
            "counter": -1, "id": "Tiny Chest", "name": "小宝箱",
            "tier": "COMMON",
        }
        state = {
            "phase": "SAPPHIRE_KEY",
            "available_commands": ["choose"],
            "game_state": {"relics": []},
            "options": [{
                "option_id": "reward:relic:0",
                "choice_index": 0,
                "label": "小宝箱",
                "target": {
                    "kind": "reward",
                    "audit_projection_version": 3,
                    "reward": {
                        "reward_type": "RELIC",
                        "relic": relic,
                    },
                },
            }],
        }

        consequence = autoplay.canonical_legal_choices(
            state,
            {"action": "choose", "option_id": "reward:relic:0"},
            {"candidates": [
                canonical_test_candidate("reward:relic:0", 0, 1)
            ]},
        )[0]["consequences"]

        self.assertEqual("gain_linked_relic", consequence["operation"])
        self.assertEqual(0, consequence["hp_delta"])
        self.assertEqual(0, consequence["max_hp_delta"])
        self.assertEqual(0, consequence["gold_delta"])
        self.assertEqual([relic], consequence["relic_changes"]["gain"])
        for field in (
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
        ):
            self.assertEqual(
                "known", consequence["field_knowledge"][field]["status"]
            )

    def test_gold_reward_projects_exact_bloody_idol_heal(self):
        target = {
            "kind": "reward",
            "reward": {"reward_type": "GOLD", "gold": 19},
        }
        state = {"game_state": {
            "current_hp": 37,
            "max_hp": 51,
            "relics": [{"id": "Bloody Idol"}],
        }}

        consequence = autoplay._structured_option_consequence(
            "COMBAT_REWARD", target, "19 Gold", state=state
        )
        self.assertEqual(5, consequence["hp_delta"])
        self.assertEqual(19, consequence["gold_delta"])

        state["game_state"]["relics"].append({"id": "Mark of the Bloom"})
        blocked = autoplay._structured_option_consequence(
            "COMBAT_REWARD", target, "19 Gold", state=state
        )
        self.assertEqual(0, blocked["hp_delta"])

    def test_payload_does_not_invent_unadvertised_return_choice(self):
        state = {
            "phase": "EVENT", "available_commands": ["choose"],
            "options": [{
                "option_id": "event:0", "choice_index": 0, "label": "Accept",
                "target": {"kind": "event_option", "event_id": "Known"},
            }],
        }
        rows = autoplay.canonical_legal_choices(
            state,
            {"action": "return", "target_id": "action:return"},
            {"candidates": [canonical_test_candidate(
                "event:0", 0, 1, consequences={"operation": "accept"},
            )]},
        )
        self.assertEqual(["event:0"], [row["choice_id"] for row in rows])
        self.assertFalse(any(row["selected"] for row in rows))

    def test_duplicate_items_bind_model_by_exact_typed_candidate(self):
        state = {
            "phase": "SHOP_SCREEN", "available_commands": ["choose"],
            "options": [
                {
                    "option_id": "shop:card:uuid-a", "choice_index": 0,
                    "label": "Twin",
                    "target": {"kind": "card", "item": {
                        "id": "ShopTwin", "card_instance_id": "uuid-a", "price": 10,
                    }},
                },
                {
                    "option_id": "shop:card:uuid-b", "choice_index": 1,
                    "label": "Twin",
                    "target": {"kind": "card", "item": {
                        "id": "ShopTwin", "card_instance_id": "uuid-b", "price": 10,
                    }},
                },
            ],
            "game_state": {"relics": []},
        }
        candidates = [
            canonical_test_candidate(
                "candidate:a", 0, 1,
                consequences={"gold_delta": -10, "price": 10},
            ),
            canonical_test_candidate(
                "candidate:b", 1, 2,
                consequences={"gold_delta": -10, "price": 10},
            ),
        ]
        advice = {
            "status": "applied", "applied": True, "confidence": 0.9,
            "model_choice_id": "candidate:b", "final_choice_ids": ["candidate:b"],
            "rankings": [
                {"candidate_id": "candidate:a", "score": 11},
                {"candidate_id": "candidate:b", "score": 22},
            ],
        }
        rows = autoplay.canonical_legal_choices(
            state,
            {"action": "choose", "option_id": "shop:card:uuid-b"},
            {"candidates": candidates, "model_advice": advice},
        )
        by_id = {row["choice_id"]: row for row in rows}
        self.assertEqual(11, by_id["shop:card:uuid-a"]["model_score"])
        self.assertEqual(22, by_id["shop:card:uuid-b"]["model_score"])
        self.assertFalse(by_id["shop:card:uuid-a"]["selected"])
        self.assertTrue(by_id["shop:card:uuid-b"]["selected"])
        self.assertEqual("model", by_id["shop:card:uuid-b"]["final_source"])

    def test_model_skip_alias_binds_to_canonical_return_choice(self):
        state = {
            "phase": "CARD_REWARD",
            "available_commands": ["choose", "return"],
            "options": [{
                "option_id": "card:uuid-a", "choice_index": 0,
                "label": "Strike", "target": {"kind": "card", "card": {
                    "id": "Strike_R", "card_instance_id": "uuid-a",
                }},
            }],
        }
        card = canonical_test_candidate("card:uuid-a", 0, 2)
        skip = canonical_test_candidate(
            "action:return", None, 3, action="return",
            consequences={"deck_size_delta": 0},
        )
        skip["id"] = "skip"
        advice = {
            "status": "applied", "applied": True, "confidence": 0.9,
            "model_choice_id": "skip", "final_choice_ids": ["skip"],
            "rankings": [
                {"candidate_id": "card:uuid-a", "score": 10},
                {"candidate_id": "skip", "score": 20},
            ],
        }

        rows = autoplay.canonical_legal_choices(
            state,
            {"action": "return", "target_id": "action:return"},
            {"candidates": [card, skip], "model_advice": advice},
        )
        selected = next(row for row in rows if row.get("selected") is True)
        self.assertEqual("action:return", selected["choice_id"])
        self.assertEqual("model", selected["final_source"])
        self.assertTrue(selected["override"]["applied"])
        self.assertEqual(20, selected["model_score"])

    def test_boss_relic_return_remains_visible_but_selection_ineligible(self):
        state = {
            "phase": "BOSS_REWARD",
            "available_commands": ["choose", "return"],
            "options": [{
                "option_id": "relic:tiny", "choice_index": 0,
                "label": "Tiny House",
                "target": {"kind": "relic", "relic": {
                    "id": "Tiny House", "name": "Tiny House",
                }},
            }],
            "game_state": {"relics": []},
        }
        relic = canonical_test_candidate(
            "relic:Tiny House:0", 0, -1,
            consequences={
                "operation": "gain_boss_relic", "relic_id": "Tiny House",
            },
        )
        noop = canonical_test_candidate(
            "action:return", None, 0, action="return",
            consequences={"operation": "return"},
        )
        noop["selection_eligible"] = False
        noop["veto_reason"] = "boss_relic_return_is_noop"

        rows = autoplay.canonical_legal_choices(
            state,
            {"action": "choose", "option_id": "relic:tiny"},
            {"candidates": [relic, noop]},
        )

        by_id = {row["choice_id"]: row for row in rows}
        self.assertEqual({"relic:tiny", "action:return"}, set(by_id))
        self.assertTrue(by_id["relic:tiny"]["selected"])
        self.assertFalse(by_id["action:return"]["selection_eligible"])
        self.assertEqual(
            "boss_relic_return_is_noop",
            by_id["action:return"]["veto_reason"],
        )

    def test_tiny_house_reward_projects_bounded_pickup_package(self):
        relic = {"id": "Tiny House", "name": "Tiny House", "counter": -1}
        target = {
            "kind": "relic", "relic": relic,
            "audit_projection_version": 3,
        }
        state = {
            "phase": "BOSS_REWARD",
            "game_state": {
                "current_hp": 31, "max_hp": 80, "gold": 200,
                "deck": [{
                    "id": "Strike_R", "type": "ATTACK", "upgrades": 0,
                    "card_instance_id": "strike:1",
                }],
                "relics": [{"id": "Ectoplasm"}, {"id": "Sozu"}],
                "potions": [{"id": "Potion Slot"}],
            },
        }

        consequence = autoplay._structured_option_consequence(
            "BOSS_REWARD", target, "Tiny House", state=state,
        )

        self.assertEqual(5, consequence["max_hp_delta"])
        self.assertEqual(0, consequence["gold_delta"])
        self.assertEqual(
            {"kind": "tiny_house_effective_heal",
             "minimum": 0, "maximum": 5},
            consequence["hp_delta"],
        )
        self.assertEqual(
            1, consequence["card_changes"]["random_upgrade"][0]["count"]
        )
        self.assertNotIn("random_gain", consequence["potion_changes"])
        self.assertEqual(
            "tiny_house_card_reward_surface",
            consequence["future_costs"][0]["kind"],
        )
        self.assertEqual(
            "classified_random_domain",
            consequence["uncertainty_classification"]["status"],
        )
        self.assertEqual(
            "known_domain",
            consequence["field_knowledge"]["probabilistic_outcomes"]["status"],
        )

    def test_tiny_house_defers_gold_and_potion_to_reward_surfaces(self):
        relic = {"id": "Tiny House", "name": "Tiny House", "counter": -1}
        target = {
            "kind": "relic", "relic": relic,
            "audit_projection_version": 3,
        }
        state = {
            "phase": "BOSS_REWARD",
            "game_state": {
                "current_hp": 62, "max_hp": 80, "gold": 280,
                "deck": [{
                    "id": "Bash", "type": "ATTACK", "upgrades": 0,
                    "card_instance_id": "bash:1",
                }],
                "relics": [],
                "potions": [{"id": "Potion Slot"}],
            },
        }

        consequence = autoplay._structured_option_consequence(
            "BOSS_REWARD", target, "Tiny House", state=state,
        )
        expected = independent_oracle._expected_visible_consequence(
            "BOSS_REWARD",
            {
                "raw_text": "Tiny House",
                "target": copy.deepcopy(target),
            },
            {"authoritative_state_before": copy.deepcopy(state)},
        )
        matched, mismatches = independent_oracle._consequence_matches_visible(
            consequence, expected,
        )

        self.assertTrue(matched, mismatches)
        self.assertEqual(0, consequence["gold_delta"])
        self.assertEqual([], consequence["potion_changes"]["gain"])
        self.assertEqual(
            [
                "tiny_house_gold_reward_surface",
                "tiny_house_card_reward_surface",
                "tiny_house_potion_reward_surface",
            ],
            [item["kind"] for item in consequence["future_costs"]],
        )

    def test_post_boss_proceed_projects_automatic_act_heal(self):
        state = {
            "phase": "COMBAT_REWARD",
            "options": [],
            "game_state": {
                "act": 1,
                "room_type": "TreasureRoomBoss",
                "current_hp": 67,
                "max_hp": 85,
                "relics": [],
            },
        }
        target = {"kind": "protocol_action", "action": "proceed"}

        consequence = autoplay._structured_option_consequence(
            "COMBAT_REWARD", target, "Proceed", state=state,
        )
        expected = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD",
            {"raw_text": "Proceed", "target": copy.deepcopy(target)},
            {
                "authoritative_state_before": copy.deepcopy(state),
                "available_options_before": [],
            },
        )
        matched, mismatches = independent_oracle._consequence_matches_visible(
            consequence, expected,
        )

        self.assertTrue(matched, mismatches)
        self.assertEqual(18, consequence["hp_delta"])
        self.assertEqual(
            "automatic_post_boss_act_transition_heal",
            consequence["field_knowledge"]["hp_delta"]["reason"],
        )

    def test_resource_preparation_expands_same_potion_use_and_discard(self):
        potion = {
            "id": "Fruit Juice", "name": "Fruit Juice",
            "potion_instance_id": "potion:opaque", "slot": 0,
            "can_use": True, "can_discard": True, "requires_target": False,
        }
        state = {
            "phase": "COMBAT_REWARD",
            "available_commands": ["choose", "potion"],
            "options": [],
            "game_state": {"potions": [potion], "relics": []},
        }
        candidates = [
            canonical_test_candidate(
                "candidate:discard", 0, 1, action="potion", operation="discard",
                consequences={
                    "operation": "discard_held_potion_for_reward_slot",
                    "potion_id": "Fruit Juice", "potion_slot": 0,
                },
            ),
            canonical_test_candidate(
                "candidate:use", 0, 2, action="potion", operation="use",
                consequences={
                    "operation": "use_held_potion_for_reward_slot",
                    "potion_id": "Fruit Juice", "potion_slot": 0,
                },
            ),
        ]
        rows = autoplay.canonical_legal_choices(
            state,
            {
                "action": "potion", "operation": "use",
                "potion_instance_id": "potion:opaque",
            },
            {"candidates": candidates},
        )

        self.assertEqual(2, len(rows))
        self.assertEqual({"use", "discard"}, {row["operation"] for row in rows})
        selected = [row for row in rows if row["selected"]]
        self.assertEqual(1, len(selected))
        self.assertEqual("potion-use:potion:opaque:0", selected[0]["semantic_id"])
        self.assertEqual(5, selected[0]["consequences"]["hp_delta"])
        discard = next(row for row in rows if row["operation"] == "discard")
        self.assertEqual(0, discard["consequences"]["hp_delta"])

    def test_duplication_preparation_canonical_surface_keeps_vetoed_use(self):
        potion = {
            "id": "DuplicationPotion", "name": "Duplication Potion",
            "potion_instance_id": "potion:duplication", "slot": 0,
            "can_use": True, "can_discard": True, "requires_target": False,
        }
        state = {
            "phase": "COMBAT_REWARD",
            "available_commands": ["choose", "potion"],
            "options": [],
            "game_state": {"potions": [potion], "relics": []},
        }
        discard = canonical_test_candidate(
            "potion:discard:0:DuplicationPotion", 0, 1,
            action="potion", operation="discard",
            consequences={
                "operation": "discard_held_potion_for_reward_slot",
                "potion_id": "DuplicationPotion", "potion_slot": 0,
            },
        )
        use = canonical_test_candidate(
            "potion:use:0:DuplicationPotion", 0, 0,
            action="potion", operation="use",
            consequences={
                "operation": "use_held_potion_for_reward_slot",
                "potion_id": "DuplicationPotion", "potion_slot": 0,
            },
        )
        use["selection_eligible"] = False
        use["veto_reason"] = (
            "unsupported_duplication_power_exact_model"
        )

        rows = autoplay.canonical_legal_choices(
            state,
            {
                "action": "potion", "operation": "discard",
                "potion_instance_id": "potion:duplication",
            },
            {"candidates": [discard, use]},
        )

        self.assertEqual({"use", "discard"}, {row["operation"] for row in rows})
        use_row = next(row for row in rows if row["operation"] == "use")
        discard_row = next(row for row in rows if row["operation"] == "discard")
        self.assertFalse(use_row["selection_eligible"])
        self.assertEqual(
            "unsupported_duplication_power_exact_model",
            use_row["veto_reason"],
        )
        self.assertTrue(discard_row["selection_eligible"])
        self.assertTrue(discard_row["selected"])

    def test_transient_combat_resolution_requires_turn_regression_without_refill(self):
        before = {
            "game_state": {
                "room_phase": "COMBAT",
                "current_hp": 25,
                "combat_state": {
                    "turn": 5,
                    "player": {"energy": 2},
                },
            },
        }
        stale = {
            "game_state": {
                "room_phase": "COMBAT",
                "current_hp": 31,
                "combat_state": {
                    "turn": 1,
                    "player": {"energy": 2},
                },
            },
        }
        real_next_turn = copy.deepcopy(stale)
        real_next_turn["game_state"]["combat_state"]["player"][
            "energy"
        ] = 4

        self.assertTrue(
            autoplay.transient_combat_resolution_frame(before, stale)
        )
        self.assertFalse(
            autoplay.transient_combat_resolution_frame(
                before, real_next_turn
            )
        )

    def test_transient_combat_resolution_waits_instead_of_playing_stale_hand(self):
        before = {
            "state_seq": 10,
            "phase": "COMBAT_TURN_5",
            "game_state": {
                "room_phase": "COMBAT",
                "current_hp": 25,
                "combat_state": {
                    "turn": 5,
                    "player": {"energy": 2},
                },
            },
        }
        stale = {
            "state_seq": 11,
            "decision_id": "decision-stale",
            "phase": "COMBAT_TURN_1",
            "game_state": {
                "room_phase": "COMBAT",
                "current_hp": 31,
                "combat_state": {
                    "turn": 1,
                    "player": {"energy": 2},
                },
            },
        }
        settled = {
            "state_seq": 12,
            "decision_id": "decision-reward",
            "phase": "COMBAT_REWARD",
            "game_state": {
                "room_phase": "COMPLETE",
                "current_hp": 31,
            },
        }
        wait_payload = {
            "id": "settle-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 11,
            "result_state_seq": 12,
        }
        context = {
            "attempt_id": "attempt-settle",
            "run_id": "IRONCLAD:0:1",
        }
        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ) as send, patch.object(
            autoplay.stsctl, "load_state", return_value=settled
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ) as append_trace:
            result = autoplay.settle_transient_combat_resolution(
                before, stale, context
            )

        self.assertIs(settled, result)
        send.assert_called_once_with(wait_payload, stale, context)
        settle_records = [
            item.args[0]
            for item in append_trace.call_args_list
            if item.args[0].get("record_type") == "transition_settle"
        ]
        self.assertEqual(1, len(settle_records))
        self.assertEqual("COMBAT_REWARD", settle_records[0]["settled_phase"])

    def test_late_receipt_is_recovered_by_same_request_id_without_resend(self):
        state = {
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "COMBAT_TURN_1",
        }
        payload = {"id": "request-action", "action": "end"}
        receipt = {
            "request_id": payload["id"],
            "success": True,
            "status": "succeeded",
        }
        with patch.object(
            autoplay.stsctl, "send_payload", side_effect=TimeoutError("late")
        ) as send_payload, patch.object(
            autoplay.stsctl, "wait_for_receipt", return_value=receipt
        ) as wait_for_receipt, patch.object(
            autoplay, "append_trace"
        ) as append_trace:
            recovered = autoplay.send_payload_exactly_once(payload, state, {})

        self.assertIs(receipt, recovered)
        send_payload.assert_called_once_with(
            payload, timeout=autoplay.ACTION_RECEIPT_TIMEOUT_SECONDS
        )
        wait_for_receipt.assert_called_once_with(
            payload["id"], timeout=autoplay.LATE_RECEIPT_RECHECK_SECONDS
        )
        self.assertEqual(
            ["receipt_timeout", "late_receipt_recovered"],
            [item.args[0]["event"] for item in append_trace.call_args_list],
        )

    def test_hand_select_choose_waits_for_bridge_settlement_window(self):
        state = {
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "HAND_SELECT",
        }
        payload = {
            "id": "request-hand-select",
            "action": "choose",
            "option_id": "hand:card",
        }
        receipt = {
            "request_id": payload["id"],
            "success": True,
            "status": "succeeded",
        }
        with patch.object(
            autoplay.stsctl, "send_payload", side_effect=TimeoutError("late")
        ) as send_payload, patch.object(
            autoplay.stsctl, "wait_for_receipt", return_value=receipt
        ) as wait_for_receipt, patch.object(
            autoplay, "append_trace"
        ) as append_trace:
            recovered = autoplay.send_payload_exactly_once(payload, state, {})

        self.assertIs(receipt, recovered)
        send_payload.assert_called_once_with(
            payload, timeout=autoplay.HAND_SELECT_ACTION_RECEIPT_TIMEOUT_SECONDS
        )
        wait_for_receipt.assert_called_once_with(
            payload["id"], timeout=autoplay.LATE_RECEIPT_RECHECK_SECONDS
        )
        self.assertEqual(
            ["receipt_timeout", "late_receipt_recovered"],
            [item.args[0]["event"] for item in append_trace.call_args_list],
        )

    def test_in_game_payload_injects_and_verifies_full_frozen_attempt_binding(self):
        self.assertEqual(
            bridge.ATTEMPT_BINDING_FIELDS,
            autoplay.ATTEMPT_BINDING_FIELDS,
        )
        state = {
            "in_game": True,
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "COMBAT_TURN_1",
        }
        context = command_context()
        payload = {"id": "request-bound", "action": "end"}
        receipt = {
            "request_id": payload["id"],
            "success": True,
            "status": "succeeded",
            **context,
        }
        with patch.object(
            autoplay.stsctl, "send_payload", return_value=receipt
        ) as send_payload:
            observed = autoplay.send_payload_exactly_once(
                payload, state, context
            )

        self.assertIs(receipt, observed)
        self.assertEqual(
            context,
            {
                field: payload[field]
                for field in autoplay.ATTEMPT_BINDING_FIELDS
            },
        )
        send_payload.assert_called_once_with(
            payload, timeout=autoplay.ACTION_RECEIPT_TIMEOUT_SECONDS
        )

    def test_in_game_payload_rejects_a_preexisting_wrong_attempt_binding(self):
        state = {"in_game": True}
        context = command_context()
        payload = {
            "id": "request-wrong-binding",
            "action": "wait",
            "attempt_id": "another-attempt",
        }
        with patch.object(autoplay.stsctl, "send_payload") as send_payload:
            with self.assertRaisesRegex(
                autoplay.SafetyError, "command attempt binding mismatch"
            ):
                autoplay.send_payload_exactly_once(payload, state, context)
        send_payload.assert_not_called()

    def test_in_game_receipt_missing_any_attempt_binding_fails_closed(self):
        state = {"in_game": True}
        context = command_context()
        for missing in autoplay.ATTEMPT_BINDING_FIELDS:
            with self.subTest(field=missing):
                payload = {"id": f"request-{missing}", "action": "wait"}
                receipt = {
                    "request_id": payload["id"],
                    "success": True,
                    "status": "succeeded",
                    **context,
                }
                del receipt[missing]
                with patch.object(
                    autoplay.stsctl, "send_payload", return_value=receipt
                ):
                    with self.assertRaisesRegex(
                        autoplay.SafetyError,
                        "action receipt attempt binding mismatch",
                    ):
                        autoplay.send_payload_exactly_once(
                            payload, state, context
                        )

    def test_state_transport_request_remains_exempt_from_attempt_binding(self):
        state = {"in_game": True}
        payload = {"id": "state-request", "action": "state"}
        receipt = {"request_id": payload["id"], "success": True}
        with patch.object(
            autoplay.stsctl, "send_payload", return_value=receipt
        ):
            observed = autoplay.send_payload_exactly_once(
                payload, state, None
            )
        self.assertIs(receipt, observed)
        self.assertEqual({"id": "state-request", "action": "state"}, payload)

    def test_ambiguous_timeout_never_replays_state_changing_request(self):
        state = {
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "COMBAT_TURN_1",
        }
        # A STATE frame can advance state_seq without proving that the old
        # write settled.  The semantic binding remains ambiguous.
        refreshed = {**state, "state_seq": 11}
        payload = {"id": "request-action", "action": "end"}
        state_receipt = {"success": True, "status": "succeeded"}
        with patch.object(
            autoplay.stsctl,
            "send_payload",
            side_effect=[TimeoutError("late"), state_receipt],
        ) as send_payload, patch.object(
            autoplay.stsctl, "wait_for_receipt", side_effect=TimeoutError("late")
        ), patch.object(
            autoplay.stsctl, "load_state", return_value=refreshed
        ), patch.object(autoplay, "append_trace") as append_trace:
            with self.assertRaisesRegex(
                autoplay.SafetyError, "refusing to replay"
            ):
                autoplay.send_payload_exactly_once(payload, state, {})

        writes = [
            item.args[0]
            for item in send_payload.call_args_list
            if item.args[0].get("action") != "state"
        ]
        self.assertEqual([payload], writes)
        self.assertIn(
            "ambiguous_timeout",
            [item.args[0]["event"] for item in append_trace.call_args_list],
        )

    def test_timeout_with_changed_decision_returns_recovered_state_without_replay(self):
        state = {
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "COMBAT_TURN_1",
        }
        refreshed = {
            **state,
            "state_seq": 11,
            "decision_id": "decision-11",
        }
        payload = {"id": "request-action", "action": "end"}
        with patch.object(
            autoplay.stsctl,
            "send_payload",
            side_effect=[
                TimeoutError("late"),
                {"success": True, "status": "succeeded"},
            ],
        ) as send_payload, patch.object(
            autoplay.stsctl, "wait_for_receipt", side_effect=TimeoutError("late")
        ), patch.object(
            autoplay.stsctl, "load_state", return_value=refreshed
        ), patch.object(autoplay, "append_trace"):
            outcome = autoplay.send_payload_exactly_once(payload, state, {})

        self.assertIsInstance(outcome, autoplay.MissingReceiptOutcome)
        self.assertIs(refreshed, outcome.state)

        writes = [
            item.args[0]
            for item in send_payload.call_args_list
            if item.args[0].get("action") != "state"
        ]
        self.assertEqual([payload], writes)

    def test_timeout_uses_already_published_state_before_state_resync(self):
        state = {
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "MAP",
        }
        refreshed = {
            **state,
            "state_seq": 11,
            "decision_id": "decision-11",
            "phase": "COMBAT_INITIALIZING",
        }
        payload = {
            "id": "request-map",
            "action": "choose",
            "option_id": "map:1:2",
        }
        with patch.object(
            autoplay.stsctl, "send_payload",
            side_effect=TimeoutError("receipt blocked by transition"),
        ) as send_payload, patch.object(
            autoplay.stsctl, "wait_for_receipt",
            side_effect=TimeoutError("late"),
        ), patch.object(
            autoplay.stsctl, "load_state", return_value=refreshed,
        ), patch.object(autoplay, "append_trace") as append_trace:
            outcome = autoplay.send_payload_exactly_once(payload, state, {})

        self.assertIsInstance(outcome, autoplay.MissingReceiptOutcome)
        self.assertIs(refreshed, outcome.state)
        send_payload.assert_called_once_with(
            payload, timeout=autoplay.ACTION_RECEIPT_TIMEOUT_SECONDS
        )
        self.assertEqual(
            ["receipt_timeout", "late_receipt_unresolved", "timeout_state_advanced"],
            [item.args[0]["event"] for item in append_trace.call_args_list],
        )
        self.assertEqual(
            "direct_authoritative_state_probe",
            append_trace.call_args_list[-1].args[0]["recovery"],
        )

    def test_run_verifies_and_records_effect_when_final_receipt_is_missing(self):
        before = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 10,
            "decision_id": "decision-10",
            "phase": "WAITING",
            "available_commands": ["wait"],
            "game_state": {
                "class": "DEFECT",
                "screen_type": "NONE",
                "room_phase": "COMPLETE",
                "seed": 123,
            },
        }
        after = {
            **before,
            "state_seq": 11,
            "decision_id": "decision-11",
        }
        context = {"attempt_id": "attempt-missing-receipt", "goal_mode": "HEART"}
        agent = SimpleNamespace(
            get_next_action_in_game=lambda game: None,
            last_noncombat_decision={"reason": "wait"},
            combat_planner=SimpleNamespace(last_decision={}),
        )
        payload = {"id": "request-wait", "action": "wait", "frames": 1}
        with patch.object(
            autoplay.stsctl, "load_state", side_effect=[before, before, after]
        ), patch.object(
            autoplay.run_context_lib, "load_or_create_context", return_value=context
        ), patch.object(
            autoplay, "validate_bound_context", return_value=context
        ), patch.object(
            autoplay, "SimpleAgent", return_value=agent
        ), patch.object(
            autoplay.Game, "from_json", return_value=SimpleNamespace()
        ), patch.object(
            autoplay.stsctl, "bound_payload", return_value=payload
        ), patch.object(
            autoplay,
            "send_payload_exactly_once",
            return_value=autoplay.MissingReceiptOutcome(after),
        ), patch.object(
            autoplay,
            "verify_missing_receipt_effect",
            wraps=autoplay.verify_missing_receipt_effect,
        ) as verify_effect, patch.object(
            autoplay, "record_confirmed_effect"
        ) as record_effect, patch.object(
            autoplay, "append_trace"
        ) as append_trace, patch("builtins.print") as output:
            self.assertEqual(0, autoplay.run(1, validation=True))

        verify_effect.assert_called_once_with(before, payload, after, [])
        record_effect.assert_called_once()
        decision_records = [
            item.args[0]
            for item in append_trace.call_args_list
            if item.args[0].get("record_type") == "decision"
        ]
        self.assertEqual(1, len(decision_records))
        self.assertTrue(decision_records[0]["receipt_missing"])
        self.assertTrue(decision_records[0]["state_effect_verified"])
        self.assertEqual("action:wait", decision_records[0]["requested_target_id"])
        self.assertIsNone(decision_records[0]["resolved_target_id"])
        validation = json.loads(output.call_args.args[0])
        self.assertEqual(1, validation["actions"])

    def test_run_fails_closed_when_missing_receipt_effect_cannot_be_verified(self):
        before = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 20,
            "decision_id": "decision-20",
            "phase": "WAITING",
            "available_commands": ["wait"],
            "game_state": {
                "class": "DEFECT",
                "screen_type": "NONE",
                "room_phase": "COMPLETE",
            },
        }
        after = {**before, "state_seq": 21, "decision_id": "decision-21"}
        context = {"attempt_id": "attempt-unverified", "goal_mode": "HEART"}
        agent = SimpleNamespace(
            get_next_action_in_game=lambda game: None,
            last_noncombat_decision={},
            combat_planner=SimpleNamespace(last_decision={}),
        )
        payload = {"id": "request-wait", "action": "wait", "frames": 1}
        with patch.object(
            autoplay.stsctl, "load_state", side_effect=[before, before]
        ), patch.object(
            autoplay.run_context_lib, "load_or_create_context", return_value=context
        ), patch.object(
            autoplay, "validate_bound_context", return_value=context
        ), patch.object(
            autoplay, "SimpleAgent", return_value=agent
        ), patch.object(
            autoplay.Game, "from_json", return_value=SimpleNamespace()
        ), patch.object(
            autoplay.stsctl, "bound_payload", return_value=payload
        ), patch.object(
            autoplay,
            "send_payload_exactly_once",
            return_value=autoplay.MissingReceiptOutcome(after),
        ), patch.object(
            autoplay,
            "verify_missing_receipt_effect",
            side_effect=autoplay.SafetyError("unverified"),
        ), patch.object(
            autoplay, "record_confirmed_effect"
        ) as record_effect, patch.object(
            autoplay, "append_trace"
        ) as append_trace:
            with self.assertRaisesRegex(autoplay.SafetyError, "unverified"):
                autoplay.run(1, validation=True)

        record_effect.assert_not_called()
        self.assertFalse(any(
            item.args[0].get("record_type") == "decision"
            for item in append_trace.call_args_list
        ))
        self.assertIn(
            "missing_receipt_effect_verification_failed",
            [
                item.args[0].get("event")
                for item in append_trace.call_args_list
                if item.args[0].get("record_type") == "protocol_event"
            ],
        )

    def test_missing_end_receipt_requires_authoritative_turn_advance(self):
        before = {
            "state_seq": 10,
            "phase": "COMBAT_TURN_1",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "NONE",
                "combat_state": {"turn": 1},
            },
        }
        delayed_frame = {
            **before,
            "state_seq": 11,
            "decision_id": "changed-by-delayed-card-frame",
        }

        with self.assertRaisesRegex(
            autoplay.SafetyError, "no authoritative turn advance"
        ):
            autoplay.verify_missing_receipt_effect(
                before, {"action": "end"}, delayed_frame
            )

        next_turn = {
            **delayed_frame,
            "phase": "COMBAT_TURN_2",
            "game_state": {
                **before["game_state"],
                "combat_state": {"turn": 2},
            },
        }
        autoplay.verify_missing_receipt_effect(
            before, {"action": "end"}, next_turn
        )

    def test_missing_event_choice_receipt_fails_without_exact_postcondition(self):
        before = {
            "state_seq": 20,
            "phase": "EVENT",
            "options": [{
                "option_id": "event:choice",
                "target": {"kind": "event_option", "event_id": "Golden Idol"},
            }],
            "game_state": {"room_phase": "EVENT"},
        }
        after = {
            **before,
            "state_seq": 21,
            "phase": "COMPLETE",
            "game_state": {"room_phase": "COMPLETE"},
        }

        with self.assertRaisesRegex(
            autoplay.SafetyError, "no exact target postcondition"
        ):
            autoplay.verify_missing_receipt_effect(
                before,
                {"action": "choose", "option_id": "event:choice"},
                after,
            )

    def test_missing_map_choice_receipt_uses_exact_floor_advance(self):
        before = {
            "state_seq": 30,
            "phase": "MAP",
            "options": [{
                "option_id": "map:1:2",
                "target": {"kind": "map_node", "x": 1, "y": 2},
            }],
            "game_state": {"floor": 3},
        }
        after = {
            **before,
            "state_seq": 31,
            "phase": "COMBAT_INITIALIZING",
            "game_state": {"floor": 4},
        }

        autoplay.verify_missing_receipt_effect(
            before,
            {"action": "choose", "option_id": "map:1:2"},
            after,
        )

    def test_missing_generic_proceed_receipt_fails_closed(self):
        before = {
            "state_seq": 40,
            "phase": "COMBAT_REWARD",
            "game_state": {"room_phase": "COMPLETE"},
        }
        after = {**before, "state_seq": 41, "phase": "MAP"}

        with self.assertRaisesRegex(
            autoplay.SafetyError, "no supported action-specific postcondition"
        ):
            autoplay.verify_missing_receipt_effect(
                before, {"action": "proceed"}, after
            )

    def test_state_resync_retries_only_read_only_requests_with_bound(self):
        receipts = [
            TimeoutError("one"),
            TimeoutError("two"),
            {"success": True, "status": "succeeded"},
        ]
        with patch.object(
            autoplay.stsctl, "send_payload", side_effect=receipts
        ) as send_payload, patch.object(autoplay, "append_trace"):
            receipt = autoplay.resynchronize_protocol()

        self.assertTrue(receipt["success"])
        self.assertEqual(3, send_payload.call_count)
        self.assertTrue(all(
            item.args[0].get("action") == "state"
            for item in send_payload.call_args_list
        ))

    def test_trace_action_count_fails_closed_when_trace_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                with self.assertRaises(autoplay.SafetyError):
                    autoplay.trace_action_count("attempt-a")

    def test_bound_trace_is_written_only_to_attempt_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                autoplay.append_trace({
                    "record_type": "decision",
                    "attempt_id": "attempt-a",
                })
                isolated = autoplay.attempt_trace_path("attempt-a")
            self.assertFalse(trace_path.exists())
            self.assertEqual(
                "attempt-a",
                json.loads(isolated.read_text(encoding="utf-8"))[
                    "attempt_id"
                ],
            )

    def test_unbound_trace_uses_bounded_global_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                autoplay.append_trace({"record_type": "cache_warmup"})
            self.assertEqual(
                "cache_warmup",
                json.loads(trace_path.read_text(encoding="utf-8"))[
                    "record_type"
                ],
            )

    def test_attempt_trace_limit_fails_closed_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path), patch.object(
                autoplay, "ATTEMPT_TRACE_MAX_BYTES", 16
            ):
                with self.assertRaisesRegex(
                    autoplay.SafetyError, "size limit"
                ):
                    autoplay.append_trace({
                        "record_type": "decision",
                        "attempt_id": "attempt-a",
                    })
                isolated = autoplay.attempt_trace_path("attempt-a")
            self.assertFalse(isolated.exists())
            overflow = isolated.parent / "trace-overflow.json"
            payload = json.loads(overflow.read_text(encoding="utf-8"))
            self.assertEqual("attempt", payload["scope"])
            self.assertEqual(16, payload["max_bytes"])

    def test_global_trace_limit_fails_closed_without_large_root_file(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path), patch.object(
                autoplay, "GLOBAL_TRACE_MAX_BYTES", 16
            ):
                with self.assertRaisesRegex(
                    autoplay.SafetyError, "size limit"
                ):
                    autoplay.append_trace({
                        "record_type": "cache_warmup",
                        "payload": "this record is larger than the cap",
                    })
            self.assertFalse(trace_path.exists())
            overflow = trace_path.parent / "trace-overflow.json"
            payload = json.loads(overflow.read_text(encoding="utf-8"))
            self.assertEqual("global", payload["scope"])
            self.assertEqual(16, payload["max_bytes"])

    def test_trace_action_count_requires_bound_attempt_id(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text("{}\n", encoding="utf-8")
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                with self.assertRaises(autoplay.SafetyError):
                    autoplay.trace_action_count(None)

    def test_trace_action_count_fails_closed_when_trace_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text("{}\n", encoding="utf-8")
            with patch.object(
                autoplay, "TRACE_PATH", trace_path
            ), patch(
                "pathlib.Path.open", side_effect=OSError("denied")
            ):
                with self.assertRaises(autoplay.SafetyError):
                    autoplay.trace_action_count("attempt-a")

    def test_trace_action_count_fails_closed_on_malformed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text('{"record_type":"decision"}\n{broken\n', encoding="utf-8")
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                with self.assertRaises(autoplay.SafetyError):
                    autoplay.trace_action_count("attempt-a")

    def test_trace_action_count_requires_matching_controller_start(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text(
                json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "another-attempt",
                }) + "\n" + json.dumps({
                    "record_type": "decision",
                    "attempt_id": "attempt-a",
                }) + "\n",
                encoding="utf-8",
            )
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                with self.assertRaises(autoplay.SafetyError):
                    autoplay.trace_action_count("attempt-a")

    def test_trace_action_count_stops_at_previous_attempt_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text(
                "{malformed historical record\n"
                + json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "previous-attempt",
                }) + "\n"
                + json.dumps({
                    "record_type": "decision",
                    "attempt_id": "previous-attempt",
                }) + "\n"
                + json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "attempt-a",
                }) + "\n"
                + json.dumps({
                    "record_type": "decision",
                    "attempt_id": "attempt-a",
                }) + "\n"
                + json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "attempt-a",
                }) + "\n"
                + json.dumps({
                    "record_type": "decision",
                    "attempt_id": "attempt-a",
                }) + "\n",
                encoding="utf-8",
            )
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                self.assertEqual(2, autoplay.trace_action_count("attempt-a"))

    def test_trace_action_count_rejects_a_stale_attempt_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            trace_path.write_text(
                json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "attempt-a",
                }) + "\n"
                + json.dumps({
                    "record_type": "controller_start",
                    "attempt_id": "newer-attempt",
                }) + "\n",
                encoding="utf-8",
            )
            with patch.object(autoplay, "TRACE_PATH", trace_path):
                with self.assertRaisesRegex(
                    autoplay.SafetyError, "not the current trace segment"
                ):
                    autoplay.trace_action_count("attempt-a")

    def test_doomed_enemy_ids_bind_the_projected_before_state_indexes(self):
        first = SimpleNamespace(monster_index=0)
        second = SimpleNamespace(monster_index=1)
        raw = [
            {"enemy_instance_id": "enemy:first"},
            {"enemy_instance_id": "enemy:second"},
        ]
        with patch.object(
            autoplay.combat_predictor,
            "projected_doomed_monsters",
            return_value=[second],
        ):
            result = autoplay.projected_doomed_enemy_ids(
                SimpleNamespace(monsters=[first, second]), raw
            )

        self.assertEqual(["enemy:second"], result)

    def test_run_rejects_attaching_after_game_over_before_context_lookup(self):
        terminal = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 99,
            "game_state": {"screen_type": "GAME_OVER"},
        }
        with patch.object(autoplay.stsctl, "load_state", return_value=terminal), patch.object(
            autoplay.run_context_lib, "load_or_create_context"
        ) as create_context, patch.object(
            autoplay.run_context_lib,
            "load_matching_context",
            side_effect=autoplay.run_context_lib.RunContextError("no match"),
        ) as load_matching:
            with self.assertRaisesRegex(
                autoplay.SafetyError, "cannot attach after GAME_OVER"
            ):
                autoplay.run(1, cache_warmup=False)
        create_context.assert_not_called()
        load_matching.assert_not_called()

    def test_run_rejects_attaching_after_game_over_even_with_matching_context(self):
        terminal = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 99,
            "game_state": {"screen_type": "GAME_OVER"},
        }
        with patch.object(
            autoplay.stsctl, "load_state", return_value=terminal
        ), patch.object(
            autoplay.run_context_lib,
            "load_matching_context",
            return_value={"attempt_id": "attempt-terminal"},
        ) as load_matching, patch.object(
            autoplay.run_context_lib, "load_or_create_context"
        ) as create_context, patch.object(
            autoplay, "trace_action_count", return_value=17
        ) as action_count, patch.object(
            autoplay, "final_result"
        ) as finalize, patch.object(
            autoplay.stsctl, "send_payload"
        ) as send_payload:
            with self.assertRaisesRegex(
                autoplay.SafetyError, "cannot attach after GAME_OVER"
            ):
                autoplay.run(1, cache_warmup=False)

        load_matching.assert_not_called()
        create_context.assert_not_called()
        action_count.assert_not_called()
        send_payload.assert_not_called()
        finalize.assert_not_called()

    def test_normal_terminal_path_counts_actions_from_prior_controller_processes(self):
        initial = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 10,
            "game_state": {
                "class": "DEFECT",
                "screen_type": "NONE",
            },
        }
        terminal = {
            **initial,
            "state_seq": 11,
            "game_state": {
                **initial["game_state"],
                "screen_type": "GAME_OVER",
            },
        }
        context = {"attempt_id": "attempt-restarted", "goal_mode": "HEART"}
        recovered = {"heart_defeated": False}
        with patch.object(
            autoplay.stsctl, "load_state", side_effect=[initial, terminal]
        ), patch.object(
            autoplay.run_context_lib,
            "load_or_create_context",
            return_value=context,
        ), patch.object(
            autoplay, "validate_bound_context", return_value=context
        ), patch.object(
            autoplay, "append_trace"
        ), patch.object(
            autoplay, "trace_action_count", return_value=23
        ), patch.object(
            autoplay, "final_result", return_value=recovered
        ) as finalize, patch("builtins.print"):
            self.assertEqual(0, autoplay.run(100, cache_warmup=False))

        finalize.assert_called_once_with(terminal, 23, context)

    def test_run_drains_latched_advisor_callback_before_return(self):
        initial, context, terminal, _ = terminal_attempt_fixture(
            "attempt-advisor-drain"
        )
        callback_started = threading.Event()
        close_entered = threading.Event()
        release_callback = threading.Event()
        callback_finished = threading.Event()
        advisor_holder = {}

        class LatchedAdvisor:
            def __init__(self, callback):
                self.callback = callback
                self.worker = None

            def prewarm(self, _character):
                def emit():
                    callback_started.set()
                    if not release_callback.wait(2):
                        return
                    self.callback({
                        "record_type": "macro_cache",
                        "status": "prewarm_finished",
                    })
                    callback_finished.set()

                self.worker = threading.Thread(target=emit, daemon=True)
                self.worker.start()

            def close(self, wait=True):
                close_entered.set()
                if wait and self.worker is not None:
                    self.worker.join(timeout=2)
                    if self.worker.is_alive():
                        raise RuntimeError("advisor callback did not drain")

        def advisor_factory(_config, _assembler, callback):
            advisor = LatchedAdvisor(callback)
            advisor_holder["advisor"] = advisor
            return advisor

        def signature(path):
            path = Path(path)
            if not path.exists():
                return False, 0, None
            stat = path.stat()
            return True, stat.st_size, stat.st_mtime_ns

        real_trace = autoplay.ROOT / "autoplay.log"
        real_decision_cases = autoplay.ROOT / "decision-cases.jsonl"
        real_trace_before = signature(real_trace)
        real_cases_before = signature(real_decision_cases)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            trace_path = directory / "autoplay.log"
            attempt_trace = autoplay.attempt_trace_path(
                context["attempt_id"], trace_path=trace_path
            )
            command_path = directory / "command.json"
            outcome = {}

            def execute_run():
                try:
                    outcome["result"] = autoplay.run(10, cache_warmup=True)
                except BaseException as exc:  # captured for the parent thread
                    outcome["error"] = exc

            with patch.object(
                autoplay, "TRACE_PATH", trace_path
            ), patch.object(
                autoplay.stsctl, "COMMAND_PATH", command_path
            ), patch.object(
                autoplay.stsctl,
                "load_state",
                side_effect=[initial, terminal],
            ), patch.object(
                autoplay.run_context_lib,
                "load_or_create_context",
                return_value=context,
            ), patch.object(
                autoplay, "validate_bound_context", return_value=context
            ), patch.object(
                autoplay, "DeepSeekMacroAdvisor", side_effect=advisor_factory
            ), patch.object(
                autoplay, "SimpleAgent", return_value=object()
            ), patch.object(
                autoplay, "trace_action_count", return_value=0
            ), patch.object(
                autoplay, "final_result", return_value={"victory": True}
            ), patch.object(
                autoplay, "_ACTIVE_MACRO_ADVISOR", None
            ), patch("builtins.print"):
                runner = threading.Thread(target=execute_run)
                runner.start()
                self.assertTrue(callback_started.wait(1))
                self.assertTrue(close_entered.wait(1))
                self.assertTrue(runner.is_alive())
                self.assertFalse(callback_finished.is_set())
                release_callback.set()
                runner.join(timeout=2)
                self.assertFalse(runner.is_alive())
                self.assertNotIn("error", outcome)
                self.assertEqual(0, outcome.get("result"))
                self.assertTrue(callback_finished.is_set())
                self.assertIsNone(autoplay._ACTIVE_MACRO_ADVISOR)
                self.assertFalse(advisor_holder["advisor"].worker.is_alive())

                stable_signature = signature(attempt_trace)
                self.assertTrue(stable_signature[0])
                time.sleep(0.05)
                self.assertEqual(stable_signature, signature(attempt_trace))

            records = [
                json.loads(line)
                for line in attempt_trace.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(
                record.get("record_type") == "macro_cache"
                and record.get("status") == "prewarm_finished"
                for record in records
            ))

        self.assertEqual(real_trace_before, signature(real_trace))
        self.assertEqual(real_cases_before, signature(real_decision_cases))

    def test_final_result_persists_authoritative_terminal_end_to_end(self):
        initial = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 98,
            "key_system_unlocked": True,
            "silent_third_act_win": True,
            "defect_third_act_win": True,
            "ironclad_third_act_win": True,
            "game_state": {
                "class": "DEFECT",
                "ascension_level": 0,
                "seed": 777,
                "is_standard_run": True,
            },
        }
        selection = {
            "selection_id": "selection-terminal",
            "algorithm": "beta-thompson-v1",
            "decision_hash": autoplay.DECISION_HASH,
            "controller_hash": autoplay.CONTROLLER_HASH,
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
            "character": "DEFECT",
        }
        context = autoplay.run_context_lib.create_context(
            initial, autoplay.DECISION_HASH, autoplay.CONTROLLER_HASH, selection
        )
        terminal = {
            **initial,
            "state_seq": 99,
            **{
                field: context[field]
                for field in (
                    "attempt_id", "run_id", "seed", "character",
                    "ascension_level", "run_type", "decision_hash",
                    "controller_hash", "policy_version", "selection_id",
                    "selection_digest",
                )
            },
            "terminal_state_seq": 99,
            "game_state": {
                **initial["game_state"],
                "screen_type": "GAME_OVER",
                "heart_defeated": True,
                "run_victory": True,
                "act": 4,
                "floor": 55,
                "current_hp": 1,
                "max_hp": 75,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            context_path = directory / "run-context.json"
            result_path = directory / "run-result.json"
            history_path = directory / "run-history.jsonl"
            trace_path = directory / "autoplay.log"
            audit_path = directory / "run-audit.json"
            cohort_path = directory / "cohort-report.json"
            death_replay_path = directory / "death-replay.json"
            context_path.write_text(json.dumps(context), encoding="utf-8")
            trace_path.write_text(
                "\n".join([
                    json.dumps({
                        "record_type": "controller_start",
                        "attempt_id": context["attempt_id"],
                    }),
                    *(
                        json.dumps({
                        "record_type": "decision",
                        "attempt_id": context["attempt_id"],
                    })
                        for _ in range(2)
                    ),
                ]) + "\n",
                encoding="utf-8",
            )
            with patch.object(
                autoplay, "RUN_CONTEXT_PATH", context_path
            ), patch.object(
                autoplay, "RESULT_PATH", result_path
            ), patch.object(
                autoplay, "RESULT_HISTORY_PATH", history_path
            ), patch.object(
                autoplay, "TRACE_PATH", trace_path
            ), patch.object(
                autoplay, "RUN_AUDIT_PATH", audit_path
            ), patch.object(
                autoplay, "COHORT_REPORT_PATH", cohort_path
            ), patch.object(
                autoplay, "DEATH_REPLAY_PATH", death_replay_path
            ), patch.object(
                autoplay.stsctl, "load_state", return_value=terminal
            ), patch.object(
                autoplay.stsctl, "send_payload"
            ) as send_payload:
                persisted = autoplay.final_result(terminal, 2, context)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result, persisted)
            self.assertTrue(result["heart_defeated"])
            self.assertEqual(context["attempt_id"], result["attempt_id"])
            self.assertEqual(2, result["actions"])
            self.assertEqual(autoplay.MACRO_POLICY_PROFILE, result["macro_policy"])
            self.assertEqual(2, len(history_path.read_text(encoding="utf-8").splitlines()))
            trace_records = [
                json.loads(line)
                for line in autoplay.attempt_trace_path(
                    context["attempt_id"], trace_path=trace_path
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual("terminal_result", trace_records[-1]["record_type"])
            self.assertEqual("game_over", trace_records[-1]["termination_kind"])
            self.assertEqual(context["attempt_id"], trace_records[-1]["attempt_id"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("inconclusive", audit["audit_status"])
            self.assertFalse(audit["release_gate_passed"])
            send_payload.assert_not_called()

    def test_final_result_retry_is_idempotent_with_one_terminal_and_audit(self):
        _, context, terminal, audit_report = terminal_attempt_fixture(
            "attempt-terminal-idempotent"
        )
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            paths = self._patch_terminal_environment(
                stack, directory, context, terminal, audit_report
            )

            first = autoplay.final_result(terminal, 7, context)
            second = autoplay.final_result(terminal, 7, context)

            self.assertEqual(first, second)
            self.assertEqual(
                first,
                json.loads(paths["result"].read_text(encoding="utf-8")),
            )
            self._assert_single_terminal_and_audit(
                paths, context["attempt_id"]
            )

    def test_terminal_transaction_recovers_after_each_durable_stage(self):
        import cohort_report

        stages = (
            ("context_bind", autoplay.run_context_lib, "bind_terminal_state", None),
            ("result_write", autoplay.stsctl, "atomic_write_json", "result"),
            ("terminal_history", autoplay, "append_result_history", None),
            ("terminal_trace", autoplay, "ensure_single_terminal_trace", None),
            ("snapshots", autoplay, "persist_attempt_snapshots", None),
            ("audit_history", autoplay, "append_audit_history", None),
            ("cohort_write", cohort_report, "write_report", None),
            ("automatic_audit", autoplay, "persist_automatic_run_audit", None),
        )

        class InjectedStageFailure(RuntimeError):
            pass

        for stage, owner, attribute, path_key in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _, context, terminal, audit_report = terminal_attempt_fixture(
                    f"attempt-terminal-retry-{stage}"
                )
                paths = self._patch_terminal_environment(
                    stack, directory, context, terminal, audit_report
                )
                original = getattr(owner, attribute)
                injected = {"done": False}

                def after_durable_write(*args, **kwargs):
                    result = original(*args, **kwargs)
                    matches_path = (
                        path_key is None
                        or (args and Path(args[0]) == paths[path_key])
                    )
                    if matches_path and not injected["done"]:
                        injected["done"] = True
                        raise InjectedStageFailure(stage)
                    return result

                stack.enter_context(
                    patch.object(owner, attribute, side_effect=after_durable_write)
                )
                with self.assertRaisesRegex(InjectedStageFailure, stage):
                    autoplay.final_result(terminal, 7, context)

                recovered = autoplay.final_result(terminal, 7, context)

                self.assertTrue(injected["done"])
                self.assertEqual(context["attempt_id"], recovered["attempt_id"])
                self._assert_single_terminal_and_audit(
                    paths, context["attempt_id"]
                )

    def test_automatic_run_audit_persists_clear_report_and_gate(self):
        result = {
            "attempt_id": "attempt-audit",
            "decision_hash": "hash-audit",
        }
        report = {
            "schema_version": 2,
            "audit_engine_sha256": strategy_audit.audit_engine_sha256(),
            "attempt_id": "attempt-audit",
            "decision_hash": "hash-audit",
            "audit_status": "clear",
            "issue_count": 0,
            "review_finding_count": 0,
            "eligible_unknown_count": 0,
            "oracle_disagreement_count": 0,
            "protocol_events": 0,
            "operational_error_attempts": 0,
            "operational_error_events": 0,
            "protocol_correctness": {"status": "clear"},
            "mechanics_coverage": {"status": "clear"},
            "strategy_quality": {"status": "clear"},
            "independent_oracle": {
                "status": "clear",
                "disagreement_count": 0,
            },
            "death_replay": {
                "status": "not_applicable",
                "issue_count": 0,
                "eligible_unknown_count": 0,
            },
            "model_advice": {"conflicts": 0},
            "release_gate_passed": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            audit_path = directory / "run-audit.json"
            trace_path = directory / "trace.jsonl"
            history_path = directory / "run-history.jsonl"
            cohort_path = directory / "cohort-report.json"
            death_path = directory / "death-replay.json"
            trace_path.write_text("", encoding="utf-8")
            with patch.object(
                autoplay, "RUN_AUDIT_PATH", audit_path
            ), patch.object(
                autoplay, "TRACE_PATH", trace_path
            ), patch.object(
                autoplay, "RESULT_HISTORY_PATH", history_path
            ), patch.object(
                autoplay, "COHORT_REPORT_PATH", cohort_path
            ), patch.object(
                autoplay, "DEATH_REPLAY_PATH", death_path
            ), patch(
                "strategy_audit.audit_attempt_trace",
                return_value=report,
            ) as audit_attempt:
                with patch(
                    "strategy_audit.load_attempt_trace_suffix",
                    return_value=[],
                ), patch(
                    "death_replay.write_death_replay",
                    return_value=report["death_replay"],
                ):
                    actual = autoplay.persist_automatic_run_audit(result)
            self.assertTrue(actual["automatic"])
            self.assertTrue(actual["release_gate_passed"])
            self.assertEqual(
                actual, json.loads(audit_path.read_text(encoding="utf-8"))
            )
            audit_history = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
                and json.loads(line).get("record_type") == "run_audit"
            ]
            self.assertEqual(1, len(audit_history))
            self.assertEqual(0, audit_history[0]["protocol_events"])
            self.assertEqual(
                0, audit_history[0]["operational_error_attempts"]
            )
            self.assertEqual(0, audit_history[0]["operational_error_events"])
            audit_attempt.assert_called_once_with(
                trace_path, "hash-audit", "attempt-audit"
            )

    def test_automatic_run_audit_failure_is_inconclusive_not_clear(self):
        result = {
            "attempt_id": "attempt-audit",
            "decision_hash": "hash-audit",
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            audit_path = directory / "run-audit.json"
            history_path = directory / "run-history.jsonl"
            cohort_path = directory / "cohort-report.json"
            trace_path = directory / "autoplay.log"
            death_path = directory / "death-replay.json"
            trace_path.write_text("", encoding="utf-8")
            with patch.object(
                autoplay, "RUN_AUDIT_PATH", audit_path
            ), patch.object(
                autoplay, "RESULT_HISTORY_PATH", history_path
            ), patch.object(
                autoplay, "COHORT_REPORT_PATH", cohort_path
            ), patch.object(
                autoplay, "TRACE_PATH", trace_path
            ), patch.object(
                autoplay, "DEATH_REPLAY_PATH", death_path
            ), patch(
                "strategy_audit.audit_attempt_trace",
                side_effect=RuntimeError("broken audit"),
            ):
                actual = autoplay.persist_automatic_run_audit(result)
            self.assertEqual("inconclusive", actual["audit_status"])
            self.assertEqual("RuntimeError", actual["audit_error_class"])
            self.assertFalse(actual["release_gate_passed"])

    def test_decision_case_write_failure_is_audited_but_not_raised(self):
        record = {"record_type": "decision", "phase": "EVENT"}
        state = {"state_seq": 41, "phase": "EVENT"}
        payload = {"action": "choose", "option_id": "event:open"}
        context = {"attempt_id": "attempt-case"}
        with patch.object(
            autoplay.decision_cases,
            "append_decision_case",
            side_effect=OSError("disk full"),
        ), patch.object(autoplay, "append_protocol_event") as protocol_event:
            self.assertFalse(
                autoplay.persist_decision_case(
                    record,
                    state=state,
                    payload=payload,
                    run_context=context,
                )
            )
        protocol_event.assert_called_once_with(
            "decision_case_persist_failed",
            state,
            payload,
            run_context=context,
            error_class="OSError",
        )

    def test_live_decision_cases_use_bounded_attempt_shard(self):
        record = {
            "record_type": "decision",
            "phase": "EVENT",
            "attempt_id": "attempt-shard",
        }
        context = {"attempt_id": "attempt-shard"}
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path), patch.object(
                autoplay.decision_cases,
                "append_decision_case",
                return_value=True,
            ) as append_case:
                self.assertTrue(
                    autoplay.persist_decision_case(
                        record, run_context=context
                    )
                )
            path, observed_record = append_case.call_args.args
            self.assertEqual(
                trace_path.parent
                / "logs"
                / "attempts"
                / "attempt-shard"
                / "decision-cases.jsonl",
                path,
            )
            self.assertIs(observed_record, record)
            self.assertEqual(
                autoplay.decision_cases.ATTEMPT_DECISION_CASES_MAX_BYTES,
                append_case.call_args.kwargs["max_bytes"],
            )

    def test_live_decision_case_limit_is_a_warning_not_controller_failure(self):
        record = {
            "record_type": "decision",
            "phase": "EVENT",
            "attempt_id": "attempt-shard-limit",
        }
        state = {"state_seq": 41, "phase": "EVENT"}
        payload = {"action": "choose", "option_id": "event:open"}
        context = {"attempt_id": "attempt-shard-limit"}
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "autoplay.log"
            with patch.object(autoplay, "TRACE_PATH", trace_path), patch.object(
                autoplay.decision_cases,
                "append_decision_case",
                side_effect=autoplay.decision_cases.DecisionCaseCorpusLimitError(
                    "full"
                ),
            ), patch.object(autoplay, "append_protocol_event") as protocol_event:
                self.assertFalse(
                    autoplay.persist_decision_case(
                        record,
                        state=state,
                        payload=payload,
                        run_context=context,
                    )
                )
            protocol_event.assert_called_once_with(
                "decision_case_shard_limited",
                state,
                payload,
                run_context=context,
                error_class="DecisionCaseCorpusLimitError",
            )
            marker = (
                trace_path.parent
                / "logs"
                / "attempts"
                / "attempt-shard-limit"
                / "decision-cases-limit.json"
            )
            self.assertTrue(marker.exists())

    def test_final_result_rejects_string_terminal_booleans(self):
        state = {
            "in_game": True,
            "state_seq": 9,
            "game_state": {
                "class": "DEFECT",
                "ascension_level": 0,
                "seed": 77,
                "is_standard_run": True,
                "screen_type": "GAME_OVER",
                "run_victory": "false",
                "heart_defeated": False,
            },
        }
        context = {
            "schema_version": 2,
            "policy_version": "fast-policy-v5",
            "attempt_id": "attempt-strict-bool",
            "run_id": "DEFECT:0:77",
            "seed": 77,
            "goal_mode": "UNLOCK",
            "character": "DEFECT",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": autoplay.DECISION_HASH,
            "controller_hash": autoplay.CONTROLLER_HASH,
            "selection_id": "active-run:attempt-strict-bool",
            "selection": {
                "selection_id": "active-run:attempt-strict-bool",
                "algorithm": "active-run-fallback-v1",
            },
            "terminal_state_seq": None,
        }
        context["selection_digest"] = bridge.pre_run_binding.selection_digest(
            context["selection"]
        )

        with self.assertRaises(autoplay.SafetyError):
            autoplay.final_result(state, 1, context)

    def test_operational_error_does_not_attach_stale_context(self):
        stale = {
            "run_id": "THE_SILENT:0:old",
            "decision_hash": autoplay.DECISION_HASH,
            "controller_hash": autoplay.CONTROLLER_HASH,
            "attempt_id": "stale-attempt",
        }
        current = {
            "in_game": True,
            "game_state": {
                "class": "DEFECT",
                "ascension_level": 0,
                "seed": "new",
            },
        }
        with patch.object(
            autoplay.run_context_lib, "load_context", return_value=stale
        ), patch.object(autoplay.stsctl, "load_state", return_value=current):
            self.assertEqual({}, autoplay.active_error_context())

    def test_operational_error_does_not_attach_terminal_context(self):
        candidate = {"attempt_id": "completed"}
        terminal = {
            "in_game": True,
            "game_state": {"screen_type": "GAME_OVER"},
        }
        with patch.object(
            autoplay.run_context_lib, "load_context", return_value=candidate
        ), patch.object(autoplay.stsctl, "load_state", return_value=terminal):
            self.assertEqual({}, autoplay.active_error_context())

    def test_preflight_error_does_not_overwrite_completed_result(self):
        with patch.object(autoplay.stsctl, "atomic_write_json") as write_result:
            persisted = autoplay.persist_operational_result(
                {"termination_kind": "operational_error"},
                active_context={},
                validation=False,
            )
        self.assertFalse(persisted)
        write_result.assert_not_called()

    def test_active_preflight_error_replaces_completed_result_from_prior_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "run-result.json"
            history_path = Path(directory) / "run-history.jsonl"
            completed = {
                "termination_kind": "game_over",
                "attempt_id": "completed-attempt",
                "heart_defeated": False,
                "victory": False,
            }
            result_path.write_text(json.dumps(completed), encoding="utf-8")
            error = {
                "schema_version": 2,
                "termination_kind": "operational_error",
                "attempt_id": "new-attempt",
            }
            with patch.object(
                autoplay, "RESULT_PATH", result_path
            ), patch.object(
                autoplay, "RESULT_HISTORY_PATH", history_path
            ), patch.object(autoplay.stsctl, "atomic_write_json") as write_result:
                persisted = autoplay.persist_operational_result(
                    error,
                    active_context={"attempt_id": "new-attempt"},
                    validation=False,
                )

            self.assertTrue(persisted)
            write_result.assert_called_once_with(result_path, error)
            self.assertEqual(
                [{**error, "record_type": "terminal_result"}],
                [
                    json.loads(line)
                    for line in history_path.read_text(encoding="utf-8").splitlines()
                ],
            )

    def test_active_preflight_error_preserves_completed_same_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "run-result.json"
            completed = {
                "termination_kind": "game_over",
                "attempt_id": "same-attempt",
                "heart_defeated": True,
                "victory": True,
            }
            result_path.write_text(json.dumps(completed), encoding="utf-8")
            with patch.object(
                autoplay, "RESULT_PATH", result_path
            ), patch.object(autoplay.stsctl, "atomic_write_json") as write_result:
                persisted = autoplay.persist_operational_result(
                    {
                        "termination_kind": "operational_error",
                        "attempt_id": "same-attempt",
                    },
                    active_context={"attempt_id": "same-attempt"},
                    validation=False,
                )

            self.assertFalse(persisted)
            self.assertEqual(completed, json.loads(result_path.read_text(encoding="utf-8")))
            write_result.assert_not_called()

    def test_second_operational_terminal_for_same_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "run-result.json"
            first = {
                "schema_version": 2,
                "termination_kind": "operational_error",
                "attempt_id": "same-operational-attempt",
                "error": "TimeoutError",
                "state_seq": 44,
            }
            result_path.write_text(json.dumps(first), encoding="utf-8")
            with patch.object(
                autoplay, "RESULT_PATH", result_path
            ), patch.object(
                autoplay.stsctl, "atomic_write_json"
            ) as write_result:
                persisted = autoplay.persist_operational_result(
                    {
                        **first,
                        "error": "SafetyError",
                        "state_seq": 45,
                    },
                    active_context={"attempt_id": "same-operational-attempt"},
                    validation=False,
                )

            self.assertFalse(persisted)
            self.assertEqual(
                first, json.loads(result_path.read_text(encoding="utf-8"))
            )
            write_result.assert_not_called()

    def test_result_history_exact_dedup_finds_nonadjacent_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "run-history.jsonl"
            operational = {
                "schema_version": 2,
                "attempt_id": "attempt-a",
                "termination_kind": "operational_error",
                "state_seq": 10,
                "error": "TimeoutError",
            }
            completed = {
                "schema_version": 2,
                "attempt_id": "attempt-b",
                "termination_kind": "game_over",
                "state_seq": 50,
            }
            with patch.object(autoplay, "RESULT_HISTORY_PATH", history_path):
                self.assertTrue(autoplay.append_result_history(operational))
                self.assertTrue(autoplay.append_result_history(completed))
                self.assertFalse(autoplay.append_result_history(dict(operational)))

            records = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([operational, completed], records)

    def test_result_history_rejects_distinct_terminals_for_same_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "run-history.jsonl"
            first = {
                "schema_version": 2,
                "attempt_id": "attempt-a",
                "termination_kind": "operational_error",
                "state_seq": 10,
                "error": "SafetyError",
                "message": "first exact failure",
            }
            second = {**first, "message": "second exact failure"}

            with patch.object(autoplay, "RESULT_HISTORY_PATH", history_path):
                self.assertTrue(autoplay.append_result_history(first))
                with self.assertRaisesRegex(
                    autoplay.SafetyError, "different terminal"
                ):
                    autoplay.append_result_history(second)

            records = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([first], records)

    def test_final_result_rejects_context_from_another_seed_and_character(self):
        def state(character, seed, screen_type=None):
            return {
                "in_game": True,
                "protocol_version": 2,
                "state_seq": 10,
                "key_system_unlocked": True,
                "silent_third_act_win": True,
                "defect_third_act_win": True,
                "ironclad_third_act_win": True,
                "game_state": {
                    "class": character,
                    "ascension_level": 0,
                    "seed": seed,
                    "is_standard_run": True,
                    "screen_type": screen_type,
                },
            }

        ironclad = state("IRONCLAD", 1)
        selection = {
            "selection_id": "selection-1",
            "algorithm": "beta-thompson-v1",
            "decision_hash": autoplay.DECISION_HASH,
            "controller_hash": autoplay.CONTROLLER_HASH,
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
            "character": "IRONCLAD",
        }
        context = autoplay.run_context_lib.create_context(
            ironclad, autoplay.DECISION_HASH, autoplay.CONTROLLER_HASH, selection
        )
        defect_terminal = state("DEFECT", 2, screen_type="GAME_OVER")

        with self.assertRaises(autoplay.run_context_lib.RunContextError):
            autoplay.final_result(defect_terminal, 5, context)

    def test_loop_rejects_seed_change_before_issuing_an_action(self):
        def state(seed, seq):
            return {
                "in_game": True,
                "protocol_version": 2,
                "ready_for_command": True,
                "state_seq": seq,
                "key_system_unlocked": True,
                "silent_third_act_win": True,
                "defect_third_act_win": True,
                "ironclad_third_act_win": True,
                "game_state": {
                    "class": "DEFECT",
                    "ascension_level": 0,
                    "seed": seed,
                    "is_standard_run": True,
                },
            }

        initial = state(1, 10)
        selection = {
            "selection_id": "selection-loop",
            "algorithm": "beta-thompson-v1",
            "decision_hash": autoplay.DECISION_HASH,
            "controller_hash": autoplay.CONTROLLER_HASH,
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
            "character": "DEFECT",
        }
        context = autoplay.run_context_lib.create_context(
            initial, autoplay.DECISION_HASH, autoplay.CONTROLLER_HASH, selection
        )
        with patch.object(
            autoplay.stsctl, "load_state", side_effect=[initial, state(2, 11)]
        ), patch.object(
            autoplay.run_context_lib, "load_or_create_context", return_value=context
        ), patch.object(
            autoplay, "SimpleAgent", return_value=SimpleNamespace()
        ), patch.object(
            autoplay, "append_trace"
        ), patch.object(autoplay.stsctl, "send_payload") as send_payload:
            with self.assertRaises(autoplay.run_context_lib.RunContextError):
                autoplay.run(1, cache_warmup=False)

        send_payload.assert_not_called()

    def test_only_confirmed_combat_potion_use_updates_policy_limit(self):
        class AgentStub:
            def __init__(self):
                self.confirmed = []

            def confirm_potion_use(self, game):
                self.confirmed.append(game)

        agent = AgentStub()
        game = object()
        combat = {"game_state": {"room_phase": "COMBAT"}}
        use = {"action": "potion", "operation": "use"}

        autoplay.record_confirmed_effect(agent, game, combat, use)
        autoplay.record_confirmed_effect(
            agent, game, combat, {"action": "potion", "operation": "discard"}
        )
        autoplay.record_confirmed_effect(
            agent,
            game,
            {"game_state": {"room_phase": "COMPLETE"}},
            use,
        )

        self.assertEqual([game], agent.confirmed)

    def test_verified_grid_choice_confirms_exact_policy_card_binding(self):
        class AgentStub:
            def __init__(self):
                self.confirmed = []

            def confirm_card_selection(self, game, card_uuid):
                self.confirmed.append((game, card_uuid))

        agent = AgentStub()
        game = object()
        before = {
            "phase": "GRID",
            "options": [{
                "option_id": "option:bound",
                "target": {
                    "kind": "card",
                    "card_instance_id": "card-bound",
                },
            }],
        }

        autoplay.record_confirmed_effect(
            agent,
            game,
            before,
            {"action": "choose", "option_id": "option:bound"},
        )

        self.assertEqual([(game, "card-bound")], agent.confirmed)

    def test_verified_play_consumes_exact_duplication_card_binding(self):
        class AgentStub:
            def __init__(self):
                self.confirmed = []

            def confirm_card_play(self, game, card_uuid):
                self.confirmed.append((game, card_uuid))

        agent = AgentStub()
        game = object()

        autoplay.record_confirmed_effect(
            agent,
            game,
            {"phase": "COMBAT_TURN_1"},
            {"action": "play", "card_instance_id": "heavy-bound"},
        )

        self.assertEqual([(game, "heavy-bound")], agent.confirmed)

    def test_invalid_command_failure_is_recoverable(self):
        self.assertTrue(
            autoplay.recoverable_protocol_error(
                {"status": "failed", "error": "Invalid command: end. Possible commands: [wait, state]"}
            )
        )

    def test_transient_target_binding_rejection_is_recoverable(self):
        """A bridge race must re-sync and re-plan, never replay the payload."""

        self.assertTrue(
            autoplay.recoverable_protocol_error({
                "status": "rejected",
                "error": "enemy_instance_id does not resolve uniquely",
            })
        )
        self.assertTrue(
            autoplay.recoverable_protocol_error({
                "status": "rejected",
                "error": "bound enemy is not a legal target",
            })
        )

    def test_rejected_or_ambiguous_accepted_failure_is_not_recoverable(self):
        self.assertFalse(
            autoplay.recoverable_protocol_error(
                {"status": "rejected", "error": "Invalid command: end"}
            )
        )
        self.assertFalse(
            autoplay.recoverable_protocol_error(
                {"status": "failed", "error": "accepted action produced no changed decision state"}
            )
        )

    def test_game_over_detection_uses_authoritative_screen(self):
        self.assertTrue(autoplay.is_game_over({"game_state": {"screen_type": "GAME_OVER"}}))
        self.assertFalse(autoplay.is_game_over({"game_state": {"screen_type": "NONE"}}))

    def test_compact_option_keeps_reward_identity_without_full_card_payload(self):
        option = {
            "option_id": "option:card",
            "choice_index": 1,
            "label": "Predator",
            "target": {
                "kind": "card",
                "card_instance_id": "card-1",
                "card": {
                    "id": "Predator",
                    "name": "Predator",
                    "rarity": "UNCOMMON",
                    "upgrades": 0,
                    "description": "large field intentionally omitted",
                },
            },
        }
        compact = autoplay.compact_option(option)
        self.assertEqual("Predator", compact["target"]["card"]["id"])
        self.assertEqual(
            3, compact["target"]["audit_projection_version"]
        )
        self.assertNotIn("description", compact["target"]["card"])
        self.assertEqual("Predator", compact["label"])
        self.assertNotIn("label", compact["target"])

    def test_compact_option_keeps_complete_typed_event_contract(self):
        contract = goop_event_contract(1, gold_loss=27)
        contract["future_typed_container"] = {
            "private_variant": True,
            "outcomes": [{"typed_count": 2}],
        }
        target = goop_event_target(0, 1, gold_loss=27)
        target["event_contract"] = contract
        option = {
            "option_id": "option:goop-leave",
            "choice_index": 0,
            "label": "display-only",
            "target": target,
        }

        compact = autoplay.compact_option(option)

        self.assertEqual("display-only", compact["label"])
        self.assertNotIn("label", compact["target"])
        self.assertEqual(1, compact["target"]["original_button_index"])
        self.assertEqual(
            {"gold_loss": 27},
            compact["target"]["event_contract"]["parameters"],
        )
        self.assertEqual(
            contract["future_typed_container"],
            compact["target"]["event_contract"][
                "future_typed_container"
            ],
        )
        self.assertTrue(
            compact["target"]["mechanism_id"].startswith(
                "event-mechanism:"
            )
        )

    def test_compact_option_keeps_library_grid_selection_domain(self):
        option = {
            "option_id": "option:library-card",
            "choice_index": 0,
            "label": "Flame Barrier",
            "target": {
                "kind": "card",
                "card_instance_id": "library:flame-barrier",
                "card": {
                    "id": "Flame Barrier",
                    "card_instance_id": "library:flame-barrier",
                },
                "parent_choice_context": {
                    "authority": "accepted_protocol_choice",
                    "parent_phase": "EVENT",
                    "event_id": "The Library",
                    "operation": "gain",
                    "select_count": 1,
                    "selection_domain": "library_card_offering",
                },
            },
        }

        compact = autoplay.compact_option(option)

        self.assertEqual(
            "library_card_offering",
            compact["target"]["parent_choice_context"][
                "selection_domain"
            ],
        )

    def test_named_event_choice_survives_disabled_option_reindexing(self):
        state = {
            "state_seq": 17,
            "decision_id": "decision-17",
            "phase": "EVENT",
            "options": [
                {"option_id": "event:0", "choice_index": 0, "label": "Pay"},
                {"option_id": "event:1", "choice_index": 1, "label": "Leave"},
            ],
        }

        # The parsed event keeps original button index 2, while the bridge's
        # legal choice list has reindexed the second visible option to 1.
        payload = autoplay.payload_for_action(
            state,
            None,
            autoplay.ChooseAction(choice_index=2, name="Leave"),
        )

        self.assertEqual("event:1", payload["option_id"])
        self.assertEqual(1, payload["choice_index"])

    def test_duplicate_named_event_choice_binds_authoritative_index(self):
        state = {
            "state_seq": 18,
            "decision_id": "decision-sensory-stone",
            "phase": "EVENT",
            "options": [
                {"option_id": "event:0", "choice_index": 0, "label": "Recall"},
                {"option_id": "event:1", "choice_index": 1, "label": "Recall"},
                {"option_id": "event:2", "choice_index": 2, "label": "Recall"},
            ],
        }

        payload = autoplay.payload_for_action(
            state,
            None,
            autoplay.ChooseAction(choice_index=2, name="Recall"),
        )

        self.assertEqual("event:2", payload["option_id"])
        self.assertEqual(2, payload["choice_index"])

    def test_disabled_event_option_round_trips_agent_bridge_and_payload(self):
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "THE_SILENT",
                "current_hp": 70,
                "max_hp": 70,
                "floor": 6,
                "act": 1,
                "gold": 0,
                "seed": 1,
                "ascension_level": 0,
                "is_standard_run": True,
                "relics": [],
                "deck": [],
                "map": [],
                "potions": [],
                "screen_type": "EVENT",
                "screen_state": {
                    "event_name": "The Cleric",
                    "event_id": "TheCleric",
                    "body_text": "",
                    "options": [
                        {
                            "text": "Heal", "label": "Heal",
                            "disabled": False,
                            "original_button_index": 0,
                            "choice_index": 0,
                            "event_contract": staged_event_contract(
                                "The Cleric",
                                "com.megacrit.cardcrawl.events.exordium.Cleric",
                                "MAIN", 0, "HEAL",
                                {
                                    "heal_amount": 17,
                                    "heal_gold_cost": 35,
                                    "purify_cost": 50,
                                },
                                {"gold_cost": 35, "heal_amount": 17},
                            ),
                        },
                        {
                            "text": "Purify", "label": "Purify",
                            "disabled": True,
                            "original_button_index": 1,
                            "event_contract": staged_event_contract(
                                "The Cleric",
                                "com.megacrit.cardcrawl.events.exordium.Cleric",
                                "MAIN", 1, "PURIFY",
                                {
                                    "heal_amount": 17,
                                    "heal_gold_cost": 35,
                                    "purify_cost": 50,
                                },
                                {
                                    "gold_cost_if_purgeable": 50,
                                    "purge_select_count": 1,
                                    "selection_mode": "PLAYER_SELECT",
                                },
                            ),
                        },
                        {
                            "text": "Leave", "label": "Leave",
                            "disabled": False,
                            "original_button_index": 2,
                            "choice_index": 1,
                            "event_contract": staged_event_contract(
                                "The Cleric",
                                "com.megacrit.cardcrawl.events.exordium.Cleric",
                                "MAIN", 2, "LEAVE",
                                {
                                    "heal_amount": 17,
                                    "heal_gold_cost": 35,
                                    "purify_cost": 50,
                                },
                                {},
                            ),
                        },
                    ],
                },
                "choice_list": ["Heal", "Leave"],
                "room_phase": "EVENT",
                "room_type": "EventRoom",
                "is_screen_up": True,
            },
        }
        state = bridge.enrich_state(raw, 18)
        game = autoplay.Game.from_json(
            state["game_state"], state["available_commands"]
        )
        agent = autoplay.SimpleAgent(autoplay.PlayerClass.THE_SILENT)
        state.update(command_context(character="THE_SILENT", seed=1))

        action = agent.get_next_action_in_game(game)
        payload = autoplay.payload_for_action(state, game, action)
        bind_test_command(
            state, payload, character="THE_SILENT", seed=1
        )
        command, receipt = bridge.resolve_command(payload, state)

        self.assertEqual("Leave", action.name)
        self.assertEqual(1, action.choice_index)
        self.assertEqual("CHOOSE 1", command)
        self.assertEqual(receipt["requested_target_id"], receipt["resolved_target_id"])

    def test_event_result_with_none_screen_type_is_actionable(self):
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "THE_SILENT",
                "current_hp": 67,
                "max_hp": 67,
                "floor": 3,
                "act": 1,
                "gold": 324,
                "seed": 1,
                "ascension_level": 0,
                "is_standard_run": True,
                "relics": [],
                "deck": [],
                "map": [],
                "potions": [],
                "screen_type": "NONE",
                "screen_state": {
                    "event_id": "The Cleric",
                    "event_class": "com.megacrit.cardcrawl.events.exordium.Cleric",
                    "event_stage": "RESULT",
                    "screen_num": None,
                    "options": [{
                        "text": "Continue", "label": "Continue",
                        "disabled": False,
                        "original_button_index": 0,
                        "choice_index": 0,
                        "event_contract": {
                            "contract_version": 1,
                            "contract_kind": "BASE_GAME_EVENT_OPTION",
                            "event_id": "The Cleric",
                            "event_class": "com.megacrit.cardcrawl.events.exordium.Cleric",
                            "event_stage": "RESULT",
                            "original_button_index": 0,
                            "option_kind": "CONTINUE",
                            "instance_parameters": {
                                "heal_amount": 17,
                                "heal_gold_cost": 35,
                                "purify_cost": 50,
                            },
                            "parameters": {},
                        },
                    }],
                },
                "choice_list": ["Continue"],
                "room_phase": "EVENT",
                "room_type": "EventRoom",
                "is_screen_up": True,
            },
        }
        state = bridge.enrich_state(raw, 1)
        game = autoplay.Game.from_json(
            state["game_state"], state["available_commands"]
        )
        agent = autoplay.SimpleAgent(autoplay.PlayerClass.THE_SILENT)
        self.assertEqual("EVENT", game.screen_type.name)
        self.assertEqual("The Cleric", game.screen.event_id)
        self.assertEqual(1, len(game.screen.options))
        action = agent.get_next_action_in_game(game)
        self.assertEqual("Continue", action.name)
        self.assertEqual(0, action.choice_index)

    def test_played_card_settle_rechecks_latest_same_sequence_frame(self):
        card_id = "card:double-tap"
        before = {
            "phase": "COMBAT_TURN_5",
            "decision_id": "decision:before",
            "state_seq": 10,
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {
                    "turn": 5,
                    "hand": [{"id": "Double Tap", "card_instance_id": card_id}],
                },
            },
        }
        stale_after = copy.deepcopy(before)
        stale_after.update({
            "decision_id": "decision:after",
            "state_seq": 11,
        })
        settled_after = copy.deepcopy(stale_after)
        settled_after["game_state"]["combat_state"]["hand"] = []
        settled_after["game_state"]["combat_state"]["draw_pile"] = [{
            "id": "Double Tap", "card_instance_id": card_id,
        }]
        payload = {
            "action": "play",
            "card_instance_id": card_id,
        }
        receipt = {
            "success": True,
            "accepted_state_seq": 11,
            "result_state_seq": 11,
        }

        with patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ), patch.object(
            autoplay.stsctl, "bound_payload", return_value={"action": "wait"}
        ), patch.object(
            autoplay.stsctl, "load_state",
            side_effect=[stale_after] * 24 + [settled_after],
        ), patch.object(autoplay, "verify_effect"), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(autoplay, "append_trace"):
            settled = autoplay.settle_played_card_resolution(
                before, stale_after, payload
            )

        self.assertIs(settled_after, settled)
        self.assertEqual([], settled["game_state"]["combat_state"]["hand"])

    def test_duplicate_shop_potion_ids_bind_the_selected_occurrence(self):
        raw = {
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "THE_SILENT",
                "current_hp": 70,
                "max_hp": 70,
                "floor": 12,
                "act": 1,
                "gold": 100,
                "seed": 1,
                "ascension_level": 0,
                "is_standard_run": True,
                "relics": [],
                "deck": [],
                "map": [],
                "potions": [],
                "screen_type": "SHOP_SCREEN",
                "screen_state": {
                    "cards": [],
                    "relics": [],
                    "potions": [
                        {
                            "id": "DexterityPotion",
                            "name": "Dexterity Potion",
                            "can_use": False,
                            "can_discard": False,
                            "requires_target": False,
                            "price": 50,
                        },
                        {
                            "id": "DexterityPotion",
                            "name": "Dexterity Potion",
                            "can_use": False,
                            "can_discard": False,
                            "requires_target": False,
                            "price": 50,
                        },
                    ],
                    "purge_available": False,
                    "purge_cost": 75,
                },
                "choice_list": ["Dexterity Potion", "Dexterity Potion"],
                "room_phase": "INCOMPLETE",
                "room_type": "ShopRoom",
                "is_screen_up": True,
            },
        }
        state = bridge.enrich_state(raw, 19)
        game = autoplay.Game.from_json(
            state["game_state"], state["available_commands"]
        )
        action = autoplay.BuyPotionAction(game.screen.potions[1])
        state.update(command_context(character="THE_SILENT", seed=1))

        payload = autoplay.payload_for_action(state, game, action)
        bind_test_command(
            state, payload, character="THE_SILENT", seed=1
        )
        command, _ = bridge.resolve_command(payload, state)

        self.assertEqual(1, payload["choice_index"])
        self.assertEqual("CHOOSE 1", command)

    def test_targeted_card_payload_recovers_only_unique_live_enemy(self):
        """A targeted card may not cross the bridge without an enemy id."""

        card = SimpleNamespace(uuid="card-pommel", has_target=True)
        enemy = SimpleNamespace(
            monster_index=0, current_hp=10, is_gone=False, half_dead=False
        )
        game = SimpleNamespace(monsters=[enemy])
        state = {
            "game_state": {
                "combat_state": {
                    "monsters": [{"enemy_instance_id": "enemy:only"}],
                },
            },
        }
        action = autoplay.PlayCardAction(card=card, target_monster=None)
        with patch.object(
            autoplay.stsctl,
            "bound_payload",
            side_effect=lambda _state, _action, **fields: fields,
        ):
            payload = autoplay.payload_for_action(state, game, action)

        self.assertEqual("card-pommel", payload["card_instance_id"])
        self.assertEqual("enemy:only", payload["enemy_instance_id"])

    def test_targeted_card_payload_rejects_ambiguous_missing_enemy(self):
        card = SimpleNamespace(uuid="card-pommel", has_target=True)
        enemies = [
            SimpleNamespace(
                monster_index=0, current_hp=10, is_gone=False, half_dead=False
            ),
            SimpleNamespace(
                monster_index=1, current_hp=10, is_gone=False, half_dead=False
            ),
        ]
        game = SimpleNamespace(monsters=enemies)
        state = {
            "game_state": {
                "combat_state": {
                    "monsters": [
                        {"enemy_instance_id": "enemy:first"},
                        {"enemy_instance_id": "enemy:second"},
                    ],
                },
            },
        }
        with self.assertRaises(autoplay.SafetyError):
            autoplay.payload_for_action(
                state,
                game,
                autoplay.PlayCardAction(card=card, target_monster=None),
            )

    def test_grid_binding_skips_card_already_selected_before_controller_restart(self):
        first = SimpleNamespace(uuid="card-first")
        second = SimpleNamespace(uuid="card-second")
        state = {
            "state_seq": 20,
            "decision_id": "decision:grid",
            "phase": "GRID",
            "options": [
                {
                    "option_id": "option:first",
                    "choice_index": 0,
                    "target": {"kind": "card", "card_instance_id": "card-first"},
                },
                {
                    "option_id": "option:second",
                    "choice_index": 1,
                    "target": {"kind": "card", "card_instance_id": "card-second"},
                },
            ],
            "game_state": {
                "screen_state": {
                    "selected_cards": [{"card_instance_id": "card-first"}],
                },
            },
        }

        payload = autoplay.payload_for_action(
            state,
            None,
            autoplay.CardSelectAction([first, second]),
        )

        self.assertEqual("option:second", payload["option_id"])
        self.assertEqual(1, payload["choice_index"])

    def test_grid_selection_accepts_new_same_size_surface_after_card_effect(self):
        # Headbutt and several other cards can open a second GRID with the
        # same number of candidates.  The selected instance disappears while
        # the authoritative decision surface changes, so this is a successful
        # selection rather than a stale-card protocol error.
        before = {
            "state_seq": 21,
            "decision_id": "decision:grid:first",
            "phase": "GRID",
            "options": [
                {
                    "option_id": "option:discard-a",
                    "target": {"kind": "card", "card_instance_id": "discard-a"},
                },
                {
                    "option_id": "option:discard-b",
                    "target": {"kind": "card", "card_instance_id": "discard-b"},
                },
            ],
            "game_state": {
                "screen_state": {
                    "cards": [
                        {"card_instance_id": "discard-a"},
                        {"card_instance_id": "discard-b"},
                    ],
                    "selected_cards": [],
                },
            },
        }
        after = {
            "state_seq": 22,
            "decision_id": "decision:grid:replacement",
            "phase": "GRID",
            "options": [
                {
                    "option_id": "option:deck-a",
                    "target": {"kind": "card", "card_instance_id": "deck-a"},
                },
                {
                    "option_id": "option:deck-b",
                    "target": {"kind": "card", "card_instance_id": "deck-b"},
                },
            ],
            "game_state": {
                "screen_state": {
                    "cards": [
                        {"card_instance_id": "deck-a"},
                        {"card_instance_id": "deck-b"},
                    ],
                    "selected_cards": [],
                },
            },
        }

        autoplay.verify_effect(
            before,
            {"action": "choose", "option_id": "option:discard-a"},
            after,
        )

    def test_settings_overlay_uses_bound_cancel_instead_of_wait(self):
        raw = {
            "available_commands": ["key", "click", "wait", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "IRONCLAD",
                "ascension_level": 0,
                "seed": 123,
                "is_standard_run": True,
                "screen_type": "NONE",
                "screen_name": "SETTINGS",
                "is_screen_up": True,
                "room_phase": "COMPLETE",
                "action_phase": "WAITING_ON_USER",
                "potions": [],
            },
        }
        state = bridge.enrich_state(raw, 20)
        state.update(command_context())

        payload = autoplay.overlay_recovery_payload(state)
        bind_test_command(state, payload)
        command, receipt = bridge.resolve_command(payload, state)

        self.assertEqual("key", payload["action"])
        self.assertEqual("KEY CANCEL", command)
        self.assertEqual(receipt["requested_target_id"], receipt["resolved_target_id"])

    def test_combat_generated_card_may_disappear_when_selection_ends_combat(self):
        source = {"id": "After Image", "upgrades": 0, "card_instance_id": "reward-card"}
        before = {
            "state_seq": 30,
            "phase": "CARD_REWARD",
            "options": [{
                "option_id": "option:after-image",
                "target": {"kind": "card", "card_instance_id": "reward-card", "card": source},
            }],
            "game_state": {"room_phase": "COMBAT", "deck": [], "combat_state": {}},
        }
        after = {
            "state_seq": 31,
            "phase": "COMBAT_REWARD",
            "game_state": {
                "room_phase": "COMPLETE",
                "deck": [],
                "combat_state": {},
            },
        }

        autoplay.verify_effect(
            before,
            {"action": "choose", "option_id": "option:after-image"},
            after,
        )

    def test_played_card_may_be_redrawn_after_forced_turn_transition(self):
        card = {"id": "Cold Snap", "card_instance_id": "card:cold-snap"}
        before = {
            "state_seq": 40,
            "phase": "COMBAT_TURN_8",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 8, "hand": [card]},
            },
        }
        after = {
            "state_seq": 41,
            "phase": "COMBAT_TURN_9",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 9, "hand": [card]},
            },
        }

        autoplay.verify_effect(
            before,
            {"action": "play", "card_instance_id": "card:cold-snap"},
            after,
        )

    def test_discard_selection_may_be_redrawn_after_authoritative_reshuffle(self):
        selected = {"id": "Reflex", "card_instance_id": "card:reflex"}
        kept = {"id": "Defend_G", "card_instance_id": "card:defend"}
        old_discard = [
            {"id": f"Discard{i}", "card_instance_id": f"discard:{i}"}
            for i in range(11)
        ]
        redrawn = [
            {"id": f"Draw{i}", "card_instance_id": f"draw:{i}"}
            for i in range(2)
        ]
        before = {
            "state_seq": 10,
            "phase": "HAND_SELECT",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "HAND_SELECT",
                "current_action": "DiscardAction",
                "screen_state": {
                    "hand": [copy.deepcopy(kept)],
                    "selected": [copy.deepcopy(selected)],
                    "max_cards": 1,
                    "can_pick_zero": False,
                },
                "combat_state": {
                    "hand": [copy.deepcopy(kept)],
                    "draw_pile": [],
                    "discard_pile": copy.deepcopy(old_discard),
                    "exhaust_pile": [],
                    "limbo": [],
                },
            },
        }
        after = {
            "state_seq": 11,
            "phase": "COMBAT_TURN_14",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "NONE",
                "screen_state": {},
                "combat_state": {
                    "hand": [
                        copy.deepcopy(kept), copy.deepcopy(selected),
                        *copy.deepcopy(redrawn),
                    ],
                    "draw_pile": [
                        {"id": f"Tail{i}", "card_instance_id": f"tail:{i}"}
                        for i in range(9)
                    ],
                    "discard_pile": [],
                    "exhaust_pile": [],
                    "limbo": [],
                },
            },
        }
        pending = [{**copy.deepcopy(selected), "_selection_action": "DiscardAction"}]

        autoplay.verify_effect(
            before,
            {"action": "proceed"},
            after,
            pending,
        )

    def test_discard_selection_still_in_hand_without_reshuffle_is_rejected(self):
        selected = {"id": "Reflex", "card_instance_id": "card:reflex"}
        kept = {"id": "Defend_G", "card_instance_id": "card:defend"}
        before = {
            "state_seq": 20,
            "phase": "HAND_SELECT",
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": "HAND_SELECT",
                "current_action": "DiscardAction",
                "screen_state": {
                    "hand": [copy.deepcopy(kept)],
                    "selected": [copy.deepcopy(selected)],
                    "max_cards": 1,
                },
                "combat_state": {
                    "hand": [copy.deepcopy(kept)],
                    "draw_pile": [],
                    "discard_pile": [],
                    "exhaust_pile": [],
                    "limbo": [],
                },
            },
        }
        after = copy.deepcopy(before)
        after["state_seq"] = 21
        after["phase"] = "COMBAT_TURN_2"
        after["game_state"]["screen_type"] = "NONE"
        after["game_state"]["screen_state"] = {}
        after["game_state"]["combat_state"]["hand"] = [
            copy.deepcopy(kept), copy.deepcopy(selected),
        ]
        pending = [{**copy.deepcopy(selected), "_selection_action": "DiscardAction"}]

        with self.assertRaisesRegex(autoplay.SafetyError, "left requested card in hand"):
            autoplay.verify_effect(
                before,
                {"action": "proceed"},
                after,
                pending,
            )

    def test_played_card_waits_for_async_exhaust_before_verifying_hand(self):
        card = {"id": "Second Wind", "card_instance_id": "card:second-wind"}
        before = {
            "state_seq": 10,
            "phase": "COMBAT_TURN_4",
            "game_state": {
                "room_phase": "COMBAT",
                "current_hp": 43,
                "combat_state": {
                    "turn": 4,
                    "hand": [card],
                    "player": {"energy": 4},
                },
            },
        }
        stale = copy.deepcopy(before)
        stale["state_seq"] = 11
        settled = copy.deepcopy(before)
        settled["state_seq"] = 12
        settled["game_state"]["combat_state"]["hand"] = []
        payload = {
            "action": "play",
            "card_instance_id": "card:second-wind",
        }
        wait_payload = {
            "id": "settle-card-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 11,
            "result_state_seq": 12,
        }
        context = {
            "attempt_id": "attempt-card-settle",
            "run_id": "IRONCLAD:0:1",
        }
        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ) as send, patch.object(
            autoplay.stsctl, "load_state", return_value=settled
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ):
            result = autoplay.settle_played_card_resolution(
                before, stale, payload, context
            )

        self.assertIs(settled, result)
        send.assert_called_once_with(wait_payload, stale, context)

    def test_played_card_wait_bound_allows_long_async_tail_but_stays_bounded(self):
        card = {"id": "Biased Cognition", "card_instance_id": "card:biased"}
        before = {
            "state_seq": 10,
            "phase": "COMBAT_TURN_3",
            "decision_id": "decision:before",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {
                    "turn": 3,
                    "hand": [card],
                    "player": {"energy": 3},
                },
            },
        }
        stale_states = []
        for seq in range(11, 24):
            state = copy.deepcopy(before)
            state["state_seq"] = seq
            state["decision_id"] = f"decision:stale-{seq}"
            stale_states.append(state)
        settled = copy.deepcopy(before)
        settled["state_seq"] = 24
        settled["decision_id"] = "decision:settled"
        settled["game_state"]["combat_state"]["hand"] = []
        payload = {"action": "play", "card_instance_id": "card:biased"}
        wait_payload = {"id": "settle-card-wait", "action": "wait", "frames": 1}
        context = {
            "attempt_id": "attempt-card-settle-long-tail",
            "run_id": "DEFECT:0:1",
        }
        responses = iter(stale_states[1:] + [settled])
        receipt_calls = []

        def send_wait(wait, state, run_context):
            receipt_calls.append((wait, state))
            next_state = next(responses)
            return {
                "success": True,
                "status": "succeeded",
                "accepted_state_seq": state["state_seq"],
                "result_state_seq": next_state["state_seq"],
            }

        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", side_effect=send_wait
        ), patch.object(
            autoplay.stsctl, "load_state", side_effect=stale_states[1:] + [settled]
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ):
            result = autoplay.settle_played_card_resolution(
                before, stale_states[0], payload, context
            )

        self.assertIs(settled, result)
        self.assertEqual(13, len(receipt_calls))
        self.assertEqual(23, receipt_calls[-1][1]["state_seq"])

    def test_entropic_brew_waits_until_consumed_instance_leaves_slot(self):
        brew = {
            "id": "EntropicBrew",
            "potion_instance_id": "potion:brew-slot-0",
            "slot": 0,
        }
        empty = {
            "id": "Potion Slot",
            "potion_instance_id": "potion:empty-slot-1",
            "slot": 1,
        }
        before = {
            "state_seq": 30,
            "phase": "COMBAT_TURN_2",
            "game_state": {
                "room_phase": "COMBAT",
                "potions": [brew, empty],
            },
        }
        generated = {
            "id": "PowerPotion",
            "potion_instance_id": "potion:power-slot-1",
            "slot": 1,
        }
        stale = copy.deepcopy(before)
        stale["state_seq"] = 31
        stale["game_state"]["potions"] = [brew, generated]
        settled = copy.deepcopy(before)
        settled["state_seq"] = 32
        settled["game_state"]["potions"] = [
            {
                "id": "Potion Slot",
                "potion_instance_id": "potion:empty-slot-0",
                "slot": 0,
            },
            generated,
        ]
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": "potion:brew-slot-0",
        }
        wait_payload = {
            "id": "settle-potion-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 31,
            "result_state_seq": 32,
        }
        context = {
            "attempt_id": "attempt-potion-settle",
            "run_id": "IRONCLAD:0:1",
        }

        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ) as send, patch.object(
            autoplay.stsctl, "load_state", return_value=settled
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ) as trace:
            result = autoplay.settle_potion_resolution(
                before, stale, payload, context
            )

        self.assertIs(settled, result)
        send.assert_called_once_with(wait_payload, stale, context)
        self.assertEqual(
            "potion_resolution_settle",
            trace.call_args.args[0]["record_type"],
        )
        autoplay.verify_effect(before, payload, settled)

    def test_entropic_brew_accepts_generated_potions_with_stale_source(self):
        brew = {
            "id": "EntropicBrew",
            "potion_instance_id": "potion:brew-slot-2",
            "slot": 2,
        }
        before = {
            "state_seq": 50,
            "phase": "COMBAT_TURN_3",
            "ready_for_command": True,
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": None,
                "potions": [
                    {"id": "Potion Slot", "potion_instance_id": "empty:0", "slot": 0},
                    {"id": "ElixirPotion", "potion_instance_id": "potion:elixir", "slot": 1},
                    brew,
                    {"id": "Potion Slot", "potion_instance_id": "empty:3", "slot": 3},
                ],
            },
        }
        after = copy.deepcopy(before)
        after["state_seq"] = 51
        after["game_state"]["potions"] = [
            {"id": "Swift Potion", "potion_instance_id": "potion:swift", "slot": 0},
            {"id": "ElixirPotion", "potion_instance_id": "potion:elixir", "slot": 1},
            brew,
            {"id": "SkillPotion", "potion_instance_id": "potion:skill", "slot": 3},
        ]
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": "potion:brew-slot-2",
        }

        with patch.object(autoplay, "append_trace") as trace:
            result = autoplay.settle_potion_resolution(
                before, after, payload, {"run_id": "IRONCLAD:0:stale-brew"}
            )

        self.assertIs(after, result)
        trace.assert_called_once()
        self.assertEqual(
            "entropic_brew_effect_settled_with_stale_source",
            trace.call_args.args[0]["kind"],
        )
        autoplay.verify_effect(before, payload, after)

    def test_smoke_bomb_waits_until_escape_leaves_combat(self):
        smoke_bomb = {
            "id": "SmokeBomb",
            "potion_instance_id": "potion:smoke-slot-0",
            "slot": 0,
        }
        before = {
            "state_seq": 35,
            "phase": "COMBAT_TURN_6",
            "game_state": {
                "room_phase": "COMBAT",
                "potions": [smoke_bomb],
            },
        }
        escaping = copy.deepcopy(before)
        escaping["state_seq"] = 36
        escaping["game_state"]["potions"] = [{
            "id": "Potion Slot",
            "potion_instance_id": "potion:empty-slot-0",
            "slot": 0,
        }]
        settled = copy.deepcopy(escaping)
        settled["state_seq"] = 37
        settled["phase"] = "COMBAT_REWARD"
        settled["game_state"]["room_phase"] = "COMPLETE"
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": "potion:smoke-slot-0",
        }
        wait_payload = {
            "id": "settle-smoke-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 36,
            "result_state_seq": 37,
        }
        context = {
            "attempt_id": "attempt-smoke-settle",
            "run_id": "IRONCLAD:0:1",
        }

        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ) as send, patch.object(
            autoplay.stsctl, "load_state", return_value=settled
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ) as trace:
            result = autoplay.settle_potion_resolution(
                before, escaping, payload, context
            )

        self.assertIs(settled, result)
        send.assert_called_once_with(wait_payload, escaping, context)
        self.assertEqual(
            "smoke_bomb_escape_pending",
            trace.call_args.args[0]["kind"],
        )

    def test_smoke_bomb_stale_combat_frame_is_done_when_actionable(self):
        smoke_bomb = {
            "id": "SmokeBomb",
            "potion_instance_id": "potion:smoke-slot-0",
            "slot": 0,
        }
        before = {
            "state_seq": 45,
            "phase": "COMBAT_TURN_5",
            "ready_for_command": True,
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": None,
                "potions": [smoke_bomb],
            },
        }
        after = copy.deepcopy(before)
        after["state_seq"] = 46
        after["game_state"]["potions"] = [{
            "id": "Potion Slot",
            "potion_instance_id": "potion:empty-slot-0",
            "slot": 0,
        }]
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": "potion:smoke-slot-0",
        }

        self.assertFalse(
            autoplay.smoke_bomb_escape_pending(before, after, payload)
        )

        with patch.object(
            autoplay, "send_payload_exactly_once"
        ) as send, patch.object(
            autoplay, "append_trace"
        ) as trace:
            result = autoplay.settle_potion_resolution(
                before, after, payload, {"run_id": "IRONCLAD:0:1"}
            )

        self.assertIs(after, result)
        send.assert_not_called()
        trace.assert_not_called()

    def test_potion_settlement_fails_closed_after_bounded_waits(self):
        stale = {
            "state_seq": 41,
            "phase": "COMBAT_TURN_2",
            "game_state": {
                "room_phase": "COMBAT",
                "potions": [{
                    "id": "EntropicBrew",
                    "potion_instance_id": "potion:brew-slot-0",
                    "slot": 0,
                }],
            },
        }
        later = copy.deepcopy(stale)
        later["state_seq"] = 42
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": "potion:brew-slot-0",
        }
        wait_payload = {
            "id": "settle-potion-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 41,
            "result_state_seq": 42,
        }

        with patch.object(
            autoplay, "MAX_POTION_SETTLE_ATTEMPTS", 1
        ), patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ), patch.object(
            autoplay.stsctl, "load_state", return_value=later
        ), patch.object(
            autoplay, "validate_bound_context"
        ):
            with self.assertRaisesRegex(
                autoplay.SafetyError, "bounded waits"
            ):
                autoplay.settle_potion_resolution(
                    stale, stale, payload, {}
                )

    def test_rebound_sweeping_beam_same_turn_redraw_is_completed_play(self):
        sweeping = {
            "id": "Sweeping Beam",
            "upgrades": 0,
            "card_instance_id": "card:sweeping",
        }
        before = {
            "state_seq": 60,
            "decision_id": "decision:before",
            "phase": "COMBAT_TURN_3",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {
                    "turn": 3,
                    "player": {
                        "energy": 2,
                        "powers": [{"id": "Rebound", "amount": 1}],
                    },
                    "hand": [sweeping],
                    "draw_pile": [],
                    "discard_pile": [],
                    "exhaust_pile": [],
                    "limbo": [],
                    "monsters": [
                        {
                            "enemy_instance_id": "enemy:repulsor",
                            "current_hp": 5,
                            "block": 0,
                        },
                        {
                            "enemy_instance_id": "enemy:spiker",
                            "current_hp": 14,
                            "block": 0,
                        },
                    ],
                },
            },
        }
        stale = copy.deepcopy(before)
        stale["state_seq"] = 61
        settled = copy.deepcopy(before)
        settled["state_seq"] = 62
        settled["decision_id"] = "decision:after"
        combat = settled["game_state"]["combat_state"]
        combat["player"]["powers"] = []
        combat["hand"] = [
            sweeping,
            {"id": "Defend_B", "card_instance_id": "card:drawn"},
        ]
        combat["monsters"][0]["current_hp"] = 0
        combat["monsters"][1]["current_hp"] = 8
        payload = {
            "action": "play",
            "card_instance_id": "card:sweeping",
        }
        wait_payload = {
            "id": "settle-rebound-wait",
            "action": "wait",
            "frames": 1,
        }
        receipt = {
            "success": True,
            "status": "succeeded",
            "accepted_state_seq": 61,
            "result_state_seq": 62,
        }
        context = {
            "attempt_id": "attempt-rebound-settle",
            "run_id": "DEFECT:0:1",
        }

        with patch.object(
            autoplay.stsctl, "bound_payload", return_value=wait_payload
        ), patch.object(
            autoplay, "send_payload_exactly_once", return_value=receipt
        ) as send, patch.object(
            autoplay.stsctl, "load_state", return_value=settled
        ), patch.object(
            autoplay, "validate_bound_context"
        ), patch.object(
            autoplay, "append_trace"
        ):
            result = autoplay.settle_played_card_resolution(
                before, stale, payload, context
            )

        self.assertIs(settled, result)
        send.assert_called_once_with(wait_payload, stale, context)
        autoplay.verify_effect(before, payload, settled)

        unproven = copy.deepcopy(before)
        unproven["state_seq"] = 62
        unproven["decision_id"] = "decision:after"
        unproven["game_state"]["combat_state"]["player"]["powers"] = []
        unproven["game_state"]["combat_state"]["hand"].append({
            "id": "Defend_B", "card_instance_id": "card:drawn",
        })
        self.assertFalse(
            autoplay._rebound_sweeping_beam_same_turn_redraw(
                before, unproven, payload
            )
        )
        with self.assertRaisesRegex(autoplay.SafetyError, "still in hand"):
            autoplay.verify_effect(before, payload, unproven)

    def test_played_card_still_in_hand_on_same_turn_is_rejected(self):
        card = {"id": "Cold Snap", "card_instance_id": "card:cold-snap"}
        before = {
            "state_seq": 50,
            "phase": "COMBAT_TURN_8",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 8, "hand": [card]},
            },
        }
        after = {
            "state_seq": 51,
            "phase": "COMBAT_TURN_8",
            "game_state": {
                "room_phase": "COMBAT",
                "combat_state": {"turn": 8, "hand": [card]},
            },
        }

        with self.assertRaisesRegex(autoplay.SafetyError, "still in hand"):
            autoplay.verify_effect(
                before,
                {"action": "play", "card_instance_id": "card:cold-snap"},
                after,
            )

    def test_action_cycle_detector_catches_shop_open_close_loop(self):
        open_shop = ("seed", 1, 3, "SHOP_ROOM", "room", "choose", "shop", None, None, None, None)
        leave_shop = ("seed", 1, 3, "SHOP_SCREEN", "shop", "return", None, None, None, None, "action:return")
        signatures = [open_shop, leave_shop] * 4

        self.assertEqual(
            [open_shop, leave_shop],
            autoplay.repeating_action_cycle(signatures),
        )

    def test_action_cycle_detector_allows_short_transition_pair(self):
        open_shop = ("seed", 1, 3, "SHOP_ROOM", "room", "choose", "shop", None, None, None, None)
        leave_shop = ("seed", 1, 3, "SHOP_SCREEN", "shop", "return", None, None, None, None, "action:return")

        self.assertIsNone(
            autoplay.repeating_action_cycle([open_shop, leave_shop])
        )

    def test_action_cycle_detector_catches_shop_loop_with_unchanged_progress(self):
        def state(phase, decision):
            return {
                "phase": phase,
                "decision_id": decision,
                "game_state": {
                    "seed": "seed",
                    "act": 1,
                    "floor": 3,
                    "current_hp": 70,
                    "max_hp": 70,
                    "gold": 100,
                    "relics": [],
                    "potions": [],
                },
            }

        open_shop = autoplay.action_cycle_signature(
            state("SHOP_ROOM", "room"),
            {"action": "choose", "option_id": "shop"},
        )
        leave_shop = autoplay.action_cycle_signature(
            state("SHOP_SCREEN", "shop"),
            {"action": "return", "target_id": "action:return"},
        )

        self.assertEqual(
            [open_shop, leave_shop],
            autoplay.repeating_action_cycle([open_shop, leave_shop] * 4),
        )

    def test_action_cycle_detector_allows_dropkick_loop_that_reduces_enemy_hp(self):
        def combat_state(enemy_hp, card_id):
            return {
                "phase": "COMBAT_TURN_3",
                "decision_id": f"decision:{card_id}",
                "game_state": {
                    "seed": "seed",
                    "act": 1,
                    "floor": 8,
                    "current_hp": 50,
                    "max_hp": 70,
                    "gold": 100,
                    "relics": [],
                    "potions": [],
                    "combat_state": {
                        "turn": 3,
                        "player": {"energy": 1, "block": 0, "powers": []},
                        "hand": [{"card_instance_id": card_id}],
                        "draw_pile": [],
                        "discard_pile": [],
                        "exhaust_pile": [],
                        "monsters": [{
                            "enemy_instance_id": "enemy:slaver",
                            "id": "SlaverRed",
                            "current_hp": enemy_hp,
                            "block": 0,
                            "half_dead": False,
                            "is_gone": False,
                            "intent": "ATTACK",
                            "move_adjusted_damage": 14,
                            "move_hits": 1,
                            "powers": [{"id": "Vulnerable", "amount": 1}],
                        }],
                    },
                },
            }

        signatures = []
        for index, enemy_hp in enumerate(range(100, 52, -6)):
            card_id = "dropkick-a" if index % 2 == 0 else "dropkick-b"
            signatures.append(autoplay.action_cycle_signature(
                combat_state(enemy_hp, card_id),
                {
                    "action": "play",
                    "card_instance_id": card_id,
                    "enemy_instance_id": "enemy:slaver",
                },
            ))

        self.assertIsNone(autoplay.repeating_action_cycle(signatures))

    @staticmethod
    def _spheric_guardian_liveness_state(
        turn, *, hp=20, block=31, power_id="Barricade",
        enemy_id="enemy:spheric-live", room_phase="COMBAT",
        room_type="MonsterRoom", extra_monsters=None,
    ):
        monsters = [{
            "enemy_instance_id": enemy_id,
            "id": "SphericGuardian",
            "current_hp": hp,
            "max_hp": 20,
            "block": block,
            "half_dead": False,
            "is_gone": False,
            "powers": ([{"id": power_id, "amount": -1}] if power_id else []),
        }]
        monsters.extend(extra_monsters or [])
        return {
            "attempt_id": "05658fd1-f133-471e-aa79-cd9ec11db1f7",
            "run_id": "THE_SILENT:0:-5674782150640595338",
            "phase": f"COMBAT_TURN_{turn}",
            "game_state": {
                "class": "THE_SILENT",
                "seed": -5674782150640595338,
                "act": 2,
                "floor": 18,
                "room_phase": room_phase,
                "room_type": room_type,
                "combat_state": {
                    "turn": turn,
                    "monsters": monsters,
                },
            },
        }

    def test_barricade_liveness_guard_rejects_real_turn_33_to_45_stall(self):
        tracker = None
        for turn in range(33, 45):
            tracker = autoplay.advance_barricade_liveness_guard(
                tracker,
                self._spheric_guardian_liveness_state(
                    turn, block=31 + (turn - 33) * 2
                ),
            )

        with self.assertRaisesRegex(
            autoplay.SafetyError,
            "Barricade liveness guard.*12 complete turns",
        ):
            autoplay.advance_barricade_liveness_guard(
                tracker,
                self._spheric_guardian_liveness_state(45, block=61),
            )

    def test_barricade_liveness_guard_grants_grace_for_attack_or_lethal_move(self):
        state = self._spheric_guardian_liveness_state(45, block=61)
        game = state["game_state"]
        game["current_hp"] = 5
        combat = game["combat_state"]
        combat["player"] = {"energy": 3, "block": 0, "powers": []}
        combat["hand"] = [{
            "type": "ATTACK",
            "cost": 2,
            "damage": 14,
            "base_damage": 14,
            "is_playable": True,
        }]
        combat["monsters"][0].update({
            "intent": "ATTACK",
            "move_adjusted_damage": 10,
            "move_hits": 1,
        })
        tracker = None
        for turn in range(33, 45):
            tracker = autoplay.advance_barricade_liveness_guard(
                tracker,
                {
                    **state,
                    "phase": f"COMBAT_TURN_{turn}",
                    "game_state": {
                        **game,
                        "combat_state": {
                            **combat,
                            "turn": turn,
                        },
                    },
                },
            )
        recovered = autoplay.advance_barricade_liveness_guard(
            tracker,
            {
                **state,
                "phase": "COMBAT_TURN_45",
                "game_state": {
                    **game,
                    "combat_state": {
                        **combat,
                        "turn": 45,
                    },
                },
            },
        )
        self.assertEqual(45, recovered["anchor_turn"])

    def test_barricade_liveness_guard_allows_short_and_non_barricade_fights(self):
        tracker = None
        for turn in range(33, 45):
            tracker = autoplay.advance_barricade_liveness_guard(
                tracker,
                self._spheric_guardian_liveness_state(turn, block=40 + turn),
            )
        self.assertEqual(44, tracker["last_turn"])

        for turn in range(1, 30):
            tracker = autoplay.advance_barricade_liveness_guard(
                tracker,
                self._spheric_guardian_liveness_state(
                    turn, power_id=None, block=100 + turn
                ),
            )
            self.assertIsNone(tracker)
        self.assertIsNone(autoplay.advance_barricade_liveness_guard(
            None,
            self._spheric_guardian_liveness_state(
                50, block=500, room_type="MonsterRoomBoss"
            ),
        ))

    def test_barricade_liveness_guard_resets_on_poison_or_block_progress(self):
        tracker = autoplay.advance_barricade_liveness_guard(
            None, self._spheric_guardian_liveness_state(33, block=40)
        )
        tracker = autoplay.advance_barricade_liveness_guard(
            tracker, self._spheric_guardian_liveness_state(38, block=55)
        )
        tracker = autoplay.advance_barricade_liveness_guard(
            tracker, self._spheric_guardian_liveness_state(39, hp=16, block=58)
        )
        self.assertEqual((39, 16), (
            tracker["anchor_turn"], tracker["anchor_hp"]
        ))
        tracker = autoplay.advance_barricade_liveness_guard(
            tracker, self._spheric_guardian_liveness_state(45, hp=16, block=35)
        )
        self.assertEqual((45, 35), (
            tracker["anchor_turn"], tracker["anchor_block"]
        ))

    def test_barricade_liveness_guard_resets_scope_and_samples_once_per_turn(self):
        tracker = autoplay.advance_barricade_liveness_guard(
            None, self._spheric_guardian_liveness_state(33, block=40)
        )
        same_turn = autoplay.advance_barricade_liveness_guard(
            tracker, self._spheric_guardian_liveness_state(33, block=0)
        )
        self.assertIs(tracker, same_turn)

        changed = autoplay.advance_barricade_liveness_guard(
            tracker,
            self._spheric_guardian_liveness_state(
                34, block=50, enemy_id="enemy:replacement"
            ),
        )
        self.assertEqual(34, changed["anchor_turn"])
        regressed = autoplay.advance_barricade_liveness_guard(
            changed,
            self._spheric_guardian_liveness_state(
                2, block=50, enemy_id="enemy:replacement"
            ),
        )
        self.assertEqual(2, regressed["anchor_turn"])
        self.assertIsNone(autoplay.advance_barricade_liveness_guard(
            regressed,
            self._spheric_guardian_liveness_state(
                2, block=50, enemy_id="enemy:replacement",
                room_phase="COMPLETE",
            ),
        ))

    def test_run_checks_barricade_liveness_before_constructing_game_or_planning(self):
        initial = {
            "in_game": True,
            "protocol_version": 2,
            "ready_for_command": True,
            "state_seq": 10,
            "phase": "COMBAT_TURN_45",
            "available_commands": ["play", "end"],
            "game_state": {
                "class": "THE_SILENT",
                "screen_type": "NONE",
                "room_phase": "COMBAT",
                "seed": 7,
            },
        }
        context = {
            "attempt_id": "attempt-barricade-preplan",
            "goal_mode": "HEART",
        }
        planner_calls = []
        agent = SimpleNamespace(
            get_next_action_in_game=lambda _game: planner_calls.append(True),
        )
        advisor = SimpleNamespace(close=lambda wait=True: None)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            autoplay.stsctl, "COMMAND_PATH", Path(directory) / "command.json"
        ), patch.object(
            autoplay.stsctl, "load_state", side_effect=[initial, initial]
        ), patch.object(
            autoplay.run_context_lib, "load_or_create_context", return_value=context
        ), patch.object(
            autoplay, "validate_bound_context", return_value=context
        ), patch.object(
            autoplay, "DeepSeekMacroAdvisor", return_value=advisor
        ), patch.object(
            autoplay, "SimpleAgent", return_value=agent
        ), patch.object(
            autoplay.Game, "from_json"
        ) as from_json, patch.object(
            autoplay,
            "advance_barricade_liveness_guard",
            side_effect=autoplay.SafetyError("Barricade liveness pre-plan"),
        ), patch.object(
            autoplay, "append_trace"
        ), patch.object(
            autoplay, "_ACTIVE_MACRO_ADVISOR", None
        ):
            with self.assertRaisesRegex(
                autoplay.SafetyError, "Barricade liveness pre-plan"
            ):
                autoplay.run(1, cache_warmup=False)

        from_json.assert_not_called()
        self.assertEqual([], planner_calls)


class ExtendedConsequenceRegressionTests(unittest.TestCase):
    def test_singing_bowl_projects_current_and_max_hp_gain(self):
        consequence = autoplay._structured_option_consequence(
            "CARD_REWARD", {"kind": "bowl"}, "Singing Bowl",
            state={"game_state": {}},
        )

        self.assertEqual(2, consequence["hp_delta"])
        self.assertEqual(2, consequence["max_hp_delta"])

    def test_spire_heart_act_four_transition_projects_full_heal(self):
        event_class = "com.megacrit.cardcrawl.events.beyond.SpireHeart"
        target = {
            "kind": "event_option", "event_id": "Spire Heart",
            "original_button_index": 0,
            "event_class": event_class,
            "event_stage": "GO_TO_ENDING",
            "audit_projection_version": autoplay.AUDIT_PROJECTION_VERSION,
        }
        option = {
            "option_id": "spire-heart:0", "choice_index": 0,
            "original_button_index": 0, "label": "Continue",
            "target": copy.deepcopy(target),
        }
        state = {
            "phase": "EVENT", "options": [copy.deepcopy(option)],
            "game_state": {
                "ascension_level": 0, "act": 3,
                "current_hp": 33, "max_hp": 90,
                "has_ruby_key": True,
                "has_emerald_key": True,
                "has_sapphire_key": True,
                "screen_state": {
                    "event_id": "Spire Heart",
                    "event_class": event_class,
                    "event_stage": "GO_TO_ENDING",
                    "options": [copy.deepcopy(option)],
                },
            },
        }

        consequence = autoplay._structured_option_consequence(
            "EVENT", target, "Continue", state=state,
        )

        self.assertEqual(57, consequence["hp_delta"])
        self.assertEqual("spire_heart_enter_act_four", consequence["operation"])
        self.assertEqual("enter_act_four", consequence["event_outcome_id"])

    def test_liquid_memories_grid_binds_discard_to_hand_action(self):
        card = {
            "id": "Heavy Blade", "type": "ATTACK", "upgrades": 0,
            "card_instance_id": "card-liquid-memory",
        }
        state = {
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": "BetterDiscardPileToHandAction",
                "screen_state": {
                    "for_upgrade": False,
                    "for_transform": False,
                    "for_purge": False,
                    "cards": [copy.deepcopy(card)],
                    "parent_choice_context": {},
                },
                "combat_state": {
                    "discard_pile": [copy.deepcopy(card)],
                },
            },
        }

        operation, selected, error = autoplay._validated_grid_target(
            {
                "kind": "card",
                "card_instance_id": card["card_instance_id"],
                "card": copy.deepcopy(card),
            },
            state,
        )

        self.assertIsNone(error)
        self.assertEqual("grid_combat_discard_to_hand", operation)
        self.assertEqual(card["card_instance_id"], selected["card_instance_id"])

    def test_headbutt_grid_binds_discard_to_draw_top_action(self):
        card = {
            "id": "Heavy Blade", "type": "ATTACK", "upgrades": 0,
            "card_instance_id": "card-headbutt-target",
        }
        state = {
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": "DiscardPileToTopOfDeckAction",
                "screen_state": {
                    "for_upgrade": False,
                    "for_transform": False,
                    "for_purge": False,
                    "cards": [copy.deepcopy(card)],
                    "parent_choice_context": {},
                },
                "combat_state": {
                    "discard_pile": [copy.deepcopy(card)],
                },
            },
        }

        operation, selected, error = autoplay._validated_grid_target(
            {
                "kind": "card",
                "card_instance_id": card["card_instance_id"],
                "card": copy.deepcopy(card),
            },
            state,
        )

        self.assertIsNone(error)
        self.assertEqual("grid_combat_discard_to_top", operation)
        self.assertEqual(card["card_instance_id"], selected["card_instance_id"])

    def test_secret_technique_grid_binds_draw_pile_to_hand_action(self):
        card = {
            "id": "Deadly Poison", "type": "SKILL", "upgrades": 0,
            "card_instance_id": "card-secret-technique-target",
        }
        state = {
            "game_state": {
                "room_phase": "COMBAT",
                "current_action": "SkillFromDeckToHandAction",
                "screen_state": {
                    "for_upgrade": False,
                    "for_transform": False,
                    "for_purge": False,
                    "cards": [copy.deepcopy(card)],
                    "parent_choice_context": {},
                },
                "combat_state": {"draw_pile": [copy.deepcopy(card)]},
            },
        }

        operation, selected, error = autoplay._validated_grid_target(
            {
                "kind": "card",
                "card_instance_id": card["card_instance_id"],
                "card": copy.deepcopy(card),
            },
            state,
        )

        self.assertIsNone(error)
        self.assertEqual("grid_combat_draw_to_hand", operation)
        self.assertEqual(card["card_instance_id"], selected["card_instance_id"])

    def test_all_bottled_relic_grid_targets_bind_card_type(self):
        cases = (
            ("Bottled Flame", "bottle_attack", "ATTACK"),
            ("Bottled Lightning", "bottle_skill", "SKILL"),
            ("Bottled Tornado", "bottle_power", "POWER"),
        )
        for relic_id, operation, card_type in cases:
            with self.subTest(relic_id=relic_id):
                card = {
                    "id": f"card-{card_type.lower()}",
                    "type": card_type,
                    "upgrades": 0,
                    "card_instance_id": f"uuid-{card_type.lower()}",
                }
                state = {
                    "game_state": {
                        "room_phase": "INCOMPLETE",
                        "current_action": None,
                        "screen_state": {
                            "for_upgrade": False,
                            "for_transform": False,
                            "for_purge": False,
                            "cards": [copy.deepcopy(card)],
                            "parent_choice_context": {
                                "authority": "accepted_protocol_choice",
                                "parent_phase": "COMBAT_REWARD",
                                "relic_id": relic_id,
                                "operation": operation,
                                "card_type": card_type,
                                "select_count": 1,
                            },
                        },
                    },
                }
                bound_operation, _selected, error = (
                    autoplay._validated_grid_target(
                        {
                            "kind": "card",
                            "card_instance_id": card["card_instance_id"],
                            "card": copy.deepcopy(card),
                        },
                        state,
                    )
                )
                self.assertIsNone(error)
                self.assertEqual(f"grid_{operation}", bound_operation)


class AuditGapClosureRegressionTests(unittest.TestCase):
    def test_golden_idol_leave_is_bound_from_authoritative_surface(self):
        cases = (
            (2, 1),
            (1, 0),
        )
        for option_count, original_index in cases:
            with self.subTest(
                option_count=option_count, original_index=original_index
            ):
                target = {
                    "kind": "event_option",
                    "event_id": "Golden Idol",
                    "original_button_index": original_index,
                }
                state = {
                    "game_state": {
                        "screen_state": {
                            "event_id": "Golden Idol",
                            "options": [
                                {"choice_index": index}
                                for index in range(option_count)
                            ],
                        },
                    },
                }

                consequence = autoplay._structured_option_consequence(
                    "EVENT", target, "localized", state=state
                )

                self.assertTrue(consequence["leave"])
                self.assertEqual(
                    "golden_idol_leave", consequence["operation"]
                )
                self.assertEqual([], consequence["uncertainty"])
                self.assertEqual(
                    "golden_idol_typed_branch",
                    consequence["uncertainty_classification"]["reason"],
                )

    def test_astrolabe_binds_exact_followup_grid_consequence(self):
        target = {
            "kind": "relic",
            "relic": {
                "id": "Astrolabe", "name": "Astrolabe",
                "counter": -1, "tier": "BOSS",
            },
            "audit_projection_version": 3,
        }
        state = {
            "phase": "BOSS_REWARD",
            "game_state": {
                "current_hp": 61,
                "max_hp": 74,
                "gold": 182,
                "deck": [],
                "relics": [],
                "potions": [],
            },
        }

        consequence = autoplay._structured_option_consequence(
            "BOSS_REWARD", target, "Astrolabe", state=state,
        )
        raw = {
            "choice_id": "relic:Astrolabe:1",
            "choice_index": 1,
            "raw_text": "Astrolabe",
            "target": copy.deepcopy(target),
        }
        expected = independent_oracle._expected_visible_consequence(
            "BOSS_REWARD",
            raw,
            {"authoritative_state_before": copy.deepcopy(state)},
        )
        matched, mismatches = independent_oracle._consequence_matches_visible(
            consequence, expected,
        )

        self.assertTrue(matched, mismatches)
        self.assertEqual(
            "classified_future",
            consequence["uncertainty_classification"]["status"],
        )
        self.assertEqual(3, consequence["future_costs"][0]["select_count"])

        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "rarity": "BASIC", "upgrades": 0,
            "card_instance_id": "card:strike:1",
        }
        parent = {
            "authority": "accepted_protocol_choice",
            "operation": "transform", "parent_phase": "BOSS_REWARD",
            "relic_id": "Astrolabe", "select_count": 3,
            "source_choice_index": 1, "source_option_id": "relic:astrolabe",
        }
        grid_target = {
            "kind": "card", "card_instance_id": card["card_instance_id"],
            "card": copy.deepcopy(card), "audit_projection_version": 3,
        }
        grid_game = {
            "deck": [copy.deepcopy(card)], "relics": [{"id": "Astrolabe"}],
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": False, "cards": [copy.deepcopy(card)],
                "selected_cards": [], "num_cards": 3,
                "any_number": False, "confirm_up": False,
                "parent_choice_context": copy.deepcopy(parent),
            },
        }
        grid_state = {"phase": "GRID", "game_state": grid_game}
        grid_production = autoplay._structured_option_consequence(
            "GRID", grid_target, "Strike", state=grid_state,
        )
        grid_expected = independent_oracle._expected_visible_consequence(
            "GRID", {"target": copy.deepcopy(grid_target), "raw_text": "Strike"},
            {"phase": "GRID", "authoritative_state_before": grid_state},
        )
        grid_matched, grid_mismatches = (
            independent_oracle._consequence_matches_visible(
                grid_production, grid_expected, {"phase": "GRID"},
            )
        )
        self.assertTrue(grid_matched, grid_mismatches)
        self.assertEqual("grid_transform", grid_production["operation"])
        self.assertEqual(
            1, grid_production["random_effects"][0]["result_upgrades"]
        )

        tampered = copy.deepcopy(grid_state)
        tampered["game_state"]["screen_state"]["parent_choice_context"][
            "select_count"
        ] = 2
        rejected = autoplay._structured_option_consequence(
            "GRID", grid_target, "Strike", state=tampered,
        )
        self.assertIn(
            "grid_parent_choice_context_invalid", rejected["uncertainty"]
        )

    def test_forced_wheel_and_exact_falling_are_classified(self):
        wheel_target = {
            "kind": "event_option", "event_id": "Wheel of Change",
            "original_button_index": 0, "audit_projection_version": 3,
        }
        wheel_state = {"game_state": {"screen_state": {"options": [
            {"choice_index": 0, "target": copy.deepcopy(wheel_target)},
        ]}}}
        wheel = autoplay._structured_option_consequence(
            "EVENT", wheel_target, "spin", state=wheel_state
        )
        self.assertEqual(
            "wheel_of_change_forced_progress", wheel["operation"]
        )
        self.assertEqual(
            "protocol_hidden",
            wheel["uncertainty_classification"]["status"],
        )
        self.assertTrue(all(
            value["status"] == "not_observable"
            for value in wheel["field_knowledge"].values()
        ))

        offered = {
            "id": "Perfected Strike", "type": "ATTACK",
            "rarity": "COMMON", "upgrades": 0,
            "card_instance_id": "falling:preview",
        }
        falling_target = {
            "kind": "event_option", "event_id": "Falling",
            "original_button_index": 2, "card": offered,
            "audit_projection_version": 3,
        }
        falling = autoplay._structured_option_consequence(
            "EVENT", falling_target, "lose card",
            state={"game_state": {"screen_state": {"options": [
                {"choice_index": index} for index in range(3)
            ]}}},
        )
        self.assertEqual(
            "falling_sacrifice_offered_card", falling["operation"]
        )
        self.assertEqual(
            "Perfected Strike",
            falling["card_changes"]["remove"][0]["id"],
        )
        self.assertNotIn(
            "card_instance_id",
            falling["card_changes"]["remove"][0],
        )

    def test_fresh_terminal_continuation_binds_recomputed_first_hit(self):
        before = {
            "room_phase": "COMBAT",
            "combat_state": {"monsters": [{
                "id": "GremlinWarrior",
                "enemy_instance_id": "enemy:last",
                "monster_index": 2,
                "current_hp": 1,
                "is_gone": False,
                "half_dead": False,
            }]},
        }
        decision = {
            "reason": "terminal_plan_continuation",
            "plan_binding": "terminal_combat_end",
            "planned_sequence": [{
                "card_id": "Anger",
                "card_uuid": "card:anger",
                "target_key": ["gremlinwarrior", 2],
            }],
            "search": {
                "first_action_enemy_hp_loss": 1,
                "first_action_expected_enemy_hp_loss": 1,
                "first_action_enemy_hp_loss_is_expected": False,
            },
        }
        with patch.object(
            autoplay, "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_ATTACK_UNRESOLVED,
        ):
            model = autoplay.damage_model_context(
                "play", decision, {"enemy_hp_loss": 1},
                before_game=before,
                available_commands=["play"],
                card_id="Anger",
                card_instance_id="card:anger",
                target_id="enemy:last",
            )
        self.assertEqual(
            "turn_search_bound_current_first_action_hp_loss",
            model["hero_to_monsters_prediction_basis"],
        )
        self.assertEqual(1, model["hero_to_monsters_predicted"])

    def test_all_bottled_relic_pickups_open_typed_grid(self):
        cases = (
            ("Bottled Flame", "bottle_attack", "current_deck_attacks"),
            ("Bottled Lightning", "bottle_skill", "current_deck_skills"),
            ("Bottled Tornado", "bottle_power", "current_deck_powers"),
        )
        for relic_id, operation, domain in cases:
            with self.subTest(relic_id=relic_id):
                consequence = autoplay._empty_consequence()
                applied = autoplay._apply_relic_pickup_package(
                    consequence, {"game_state": {"deck": []}},
                    {"id": relic_id},
                    autoplay._normalized_game_id(relic_id),
                    "test_bottled_relic",
                )
                self.assertTrue(applied)
                self.assertEqual(
                    operation, consequence["future_costs"][0]["operation"]
                )
                self.assertEqual(
                    domain, consequence["future_costs"][0]["domain"]
                )

    def test_note_parent_binds_exchange_grid(self):
        offered = {
            "id": "Apotheosis", "name": "Apotheosis", "type": "SKILL",
            "upgrades": 0, "card_instance_id": "offered:apotheosis",
        }
        mechanism = {
            "event_id": "NoteForYourself",
            "event_class": (
                "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
            ),
            "original_button_index": 0,
            "operation": "note_exchange", "select_count": 1,
            "offered_card": offered,
        }
        selected = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "card:strike",
        }
        state = {"game_state": {"screen_state": {
            "for_upgrade": False, "for_transform": False,
            "for_purge": False, "cards": [copy.deepcopy(selected)],
            "parent_choice_context": {
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "mechanism_id": autoplay._stable_id(
                    "event-grid-mechanism", mechanism
                ),
                **mechanism,
            },
        }}}

        operation, card, error = autoplay._validated_grid_target({
            "kind": "card", "card_instance_id": "card:strike",
            "card": copy.deepcopy(selected),
        }, state)

        self.assertIsNone(error)
        self.assertEqual("grid_note_exchange", operation)
        self.assertEqual("card:strike", card["card_instance_id"])

    def test_drug_dealer_parent_binds_transform_grid(self):
        mechanism = {
            "event_id": "Drug Dealer",
            "event_class": "com.megacrit.cardcrawl.events.city.DrugDealer",
            "original_button_index": 1,
            "operation": "transform",
            "select_count": 2,
        }
        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "card:strike",
        }
        state = {"game_state": {"screen_state": {
            "for_upgrade": False, "for_transform": False,
            "for_purge": False, "cards": [copy.deepcopy(card)],
            "parent_choice_context": {
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "mechanism_id": autoplay._stable_id(
                    "event-grid-mechanism", mechanism
                ),
                **mechanism,
            },
        }}}

        operation, selected, error = autoplay._validated_grid_target({
            "kind": "card", "card_instance_id": "card:strike",
            "card": copy.deepcopy(card),
        }, state)

        self.assertIsNone(error)
        self.assertEqual("grid_transform", operation)
        self.assertEqual("card:strike", selected["card_instance_id"])

    def test_blood_potion_structured_delta_is_exact(self):
        potion = {
            "id": "BloodPotion", "name": "Blood Potion",
            "potion_instance_id": "potion:blood", "slot": 0,
        }
        consequence = autoplay._structured_option_consequence(
            "SHOP_SCREEN",
            {
                "kind": "potion_resource", "operation": "use",
                "potion_id": "BloodPotion", "potion_instance_id": "potion:blood",
                "slot": 0, "potion": potion,
            },
            "use Blood Potion",
            state={"game_state": {
                "current_hp": 20, "max_hp": 80,
                "relics": [{"id": "Sacred Bark"}],
            }},
        )

        self.assertEqual(32, consequence["hp_delta"])
        self.assertEqual(0, consequence["max_hp_delta"])


if __name__ == "__main__":
    unittest.main()
