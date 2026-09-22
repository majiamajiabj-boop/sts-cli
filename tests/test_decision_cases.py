import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import decision_cases
import decision_case_replay
import autoplay


def score_contract(score):
    return {
        "score_rule_id": "fixture_linear_v1",
        "score_formula": {"kind": "sum_components_v1"},
        "score_inputs": {"fixture_value": score},
        "score_components": [{
            "name": "fixture_value",
            "input": "fixture_value",
            "coefficient": 1.0,
            "value": score,
        }],
    }


def cleric_purify_target(choice_index=1):
    instance = {
        "heal_amount": 17,
        "heal_gold_cost": 35,
        "purify_cost": 50,
    }
    contract = {
        "contract_version": 1,
        "contract_kind": "BASE_GAME_EVENT_OPTION",
        "event_id": "The Cleric",
        "event_class": "com.megacrit.cardcrawl.events.exordium.Cleric",
        "event_stage": "MAIN",
        "original_button_index": choice_index,
        "option_kind": "PURIFY",
        "instance_parameters": instance,
        "parameters": {
            "gold_cost_if_purgeable": 50,
            "purge_select_count": 1,
            "selection_mode": "PLAYER_SELECT",
        },
    }
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:20]
    return {
        "kind": "event_option",
        "event_id": "The Cleric",
        "choice_index": choice_index,
        "original_button_index": choice_index,
        "event_contract": contract,
        "mechanism_id": f"event-mechanism:{digest}",
    }


class DecisionCaseTests(unittest.TestCase):
    def _event_record(self):
        record = {
            "record_type": "decision",
            "trace_schema_version": 5,
            "time": 1.5,
            "attempt_id": "attempt-a",
            "run_id": "IRONCLAD:0:1",
            "seed": 1,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "policy_version": "fast-policy-v5",
            "decision_hash": "hash-a",
            "controller_hash": "controller-a",
            "selection_id": "selection-a",
            "selection_digest": "d" * 64,
            "before_seq": 42,
            "after_seq": 43,
            "phase": "EVENT",
            "action": "choose",
            "act": 2,
            "floor": 20,
            "hp_before": 60,
            "requested_target_id": "option:fight",
            "resolved_target_id": "option:fight",
            "decision": {
                "reason": "event_net_utility",
                "goal_mode": "HEART",
                "event_id": "maskedbandits",
                "chosen_index": 1,
                "candidates": [
                    {
                        "id": "event:0", "choice_id": "event:0",
                        "choice_index": 0, "action": "choose",
                        "operation": None,
                        "semantic_id": "event:0", "score": -200.0,
                        "consequences": {"gold_delta": -200},
                        **score_contract(-200.0),
                    },
                    {
                        "id": "event:1", "choice_id": "event:1",
                        "choice_index": 1, "action": "choose",
                        "operation": None,
                        "semantic_id": "event:1", "score": 25.0,
                        "consequences": {"combat_delta": 1},
                        **score_contract(25.0),
                    },
                ],
                "candidate_contract": {
                    "score_source": "explicit",
                    "strategy_quality_auditable": True,
                },
                "model_advice": {
                    "status": "applied",
                    "rule_choice_id": "event:0",
                    "model_choice_id": "event:1",
                    "final_choice_ids": ["event:1"],
                    "applied": True,
                    "confidence": 0.9,
                    "replay": {"state": {"large": "do-not-copy"}},
                    "fusion": {
                        "model_margin": 20.0,
                        "candidates": [{"candidate_id": "event:1"}],
                    },
                },
            },
            "chosen_option_before": {
                "option_id": "option:fight",
                "choice_index": 1,
                "label": "Fight",
                "target": {
                    "kind": "event_option",
                    "event_id": "Masked Bandits",
                    "label": "Fight",
                    "text": "Fight the bandits.",
                },
            },
            "available_options_before": [
                {
                    "option_id": "option:pay", "choice_index": 0,
                    "label": "Pay", "target": {
                        "kind": "event_option",
                        "event_id": "Masked Bandits", "label": "Pay",
                        "text": "Lose all Gold.",
                    },
                },
                {
                    "option_id": "option:fight", "choice_index": 1,
                    "label": "Fight", "target": {
                        "kind": "event_option",
                        "event_id": "Masked Bandits", "label": "Fight",
                        "text": "Fight the bandits.",
                    },
                },
            ],
            "available_commands_before": ["choose"],
            "decision_context": {"gold": 200, "max_hp": 80},
            "decision_outcome": {"gold_delta": 0},
            "authoritative_state_before": {"state_seq": 42},
            "authoritative_state_after": {"state_seq": 43},
            "authoritative_choice_settlement": {"status": "clear"},
        }
        self._refresh_canonical(record)
        return record

    def _resource_preparation_record(self):
        record = self._event_record()
        instance_id = "held-fruit-uuid"
        parent_option = {
            "option_id": "shop:potion:new:0",
            "choice_index": 0,
            "label": "Buy new potion",
            "target": {
                "kind": "potion",
                "item": {
                    "id": "new-potion", "name": "New Potion",
                    "price": 45, "can_use": False,
                    "can_discard": True, "requires_target": False,
                },
            },
        }
        state = {
            "phase": "SHOP_SCREEN",
            "options": [parent_option],
            "available_commands": ["potion"],
            "game_state": {
                "potions": [{
                    "id": "FruitJuice",
                    "name": "Fruit Juice",
                    "potion_instance_id": instance_id,
                    "slot": 0,
                    "can_discard": True,
                    "can_use": True,
                    "requires_target": False,
                }],
            },
        }
        payload = {
            "action": "potion",
            "operation": "use",
            "potion_instance_id": instance_id,
        }
        candidates = []
        for operation, score in (("discard", 5.0), ("use", 6.0)):
            candidates.append({
                "id": f"potion:{operation}:0:FruitJuice",
                "choice_id": f"potion:{operation}:0:FruitJuice",
                "choice_index": 0,
                "action": "potion",
                "operation": operation,
                "semantic_id": f"potion-{operation}:{instance_id}:0",
                "score": score,
                "legal": True,
                "visible": True,
                "selection_eligible": True,
                "local_reason": f"fixture_{operation}",
                "reason_codes": [f"fixture_{operation}"],
                "consequences": {
                    "operation": (
                        f"{operation}_held_potion_for_bound_purchase"
                    ),
                    "potion_id": "FruitJuice",
                    "potion_instance_id": instance_id,
                    "potion_slot": 0,
                    "bound_new_potion_id": "new-potion",
                    "bound_purchase_listing_id": parent_option["option_id"],
                    "bound_purchase_choice_index": parent_option[
                        "choice_index"
                    ],
                    "bound_purchase_item_id": "new-potion",
                    "bound_purchase_price": 45,
                },
                **score_contract(score),
            })
        decision = {
            "reason": "shop_potion_replacement_resource_preparation",
            "goal_mode": "HEART",
            "chosen_id": "potion:use:0:FruitJuice",
            "bound_purchase_listing_id": parent_option["option_id"],
            "bound_purchase_choice_index": parent_option["choice_index"],
            "bound_purchase_item_id": "new-potion",
            "bound_purchase_price": 45,
            "candidates": candidates,
            "candidate_contract": {
                "strategy_quality_auditable": True,
                "all_visible_options_scored": True,
            },
            "model_advice": {
                "status": "not_consulted",
                "applied": False,
                "confidence": None,
                "reason": "fixture_local_resource_preparation",
            },
        }
        choices = autoplay.canonical_legal_choices(state, payload, decision)
        selected = [
            row["choice_id"] for row in choices if row.get("selected") is True
        ]
        resource_options = autoplay._resource_preparation_options(
            state, payload
        )
        selected_option = next(
            option for option in resource_options
            if (option.get("target") or {}).get("operation") == "use"
        )
        record.update({
            "phase": "SHOP_SCREEN",
            "action": "potion",
            "requested_target_id": instance_id,
            "resolved_target_id": instance_id,
            "decision": decision,
            "available_options_before": [parent_option],
            "available_commands_before": ["potion"],
            "legal_choices_before": choices,
            "selected_choice_ids": selected,
            "final_choice_ids": list(selected),
            "chosen_option_before": selected_option,
            "decision_surface_kind": "resource_preparation",
            "parent_choice_surface_pending": True,
            "resource_preparation_options_before": resource_options,
            "decision_context": {
                "gold": 100,
                "max_hp": 80,
                "parent_operation": "bound_purchase",
            },
            "decision_outcome": {
                "current_hp_delta": 5,
                "hp_delta": 5,
                "max_hp_delta": 5,
                "gold_delta": 0,
                "block_delta": 0,
                "deck": {"added": [], "removed": [], "changed": []},
                "relics": {"added": [], "removed": [], "changed": []},
                "potions": {
                    "added": [],
                    "removed": [{
                        "id": "FruitJuice", "name": "Fruit Juice",
                        "potion_instance_id": instance_id, "slot": 0,
                        "can_discard": True, "can_use": True,
                        "requires_target": False,
                    }],
                    "changed": [],
                },
                "keys_before": {
                    "ruby": False, "emerald": False, "sapphire": False,
                },
                "keys_after": {
                    "ruby": False, "emerald": False, "sapphire": False,
                },
            },
            "authoritative_choice_settlement": {
                "status": "clear",
                "authority": "protocol_state_delta",
                "fully_observable": True,
                "choice_id": selected[0],
                "before_seq": 42,
                "after_seq": 43,
            },
        })
        before_game = {
            "current_hp": 70, "max_hp": 70, "gold": 100,
            "room_phase": "INCOMPLETE", "deck": [], "relics": [],
            "potions": [{
                "id": "FruitJuice", "name": "Fruit Juice",
                "potion_instance_id": instance_id, "slot": 0,
                "can_discard": True, "can_use": True,
                "requires_target": False,
            }],
            "has_ruby_key": False, "has_emerald_key": False,
            "has_sapphire_key": False,
        }
        after_game = copy.deepcopy(before_game)
        after_game.update({"current_hp": 75, "max_hp": 75, "potions": []})
        record["authoritative_state_before"] = {
            "state_seq": 42, "game_state": before_game,
        }
        record["authoritative_state_after"] = {
            "state_seq": 43, "game_state": after_game,
        }
        record["authoritative_choice_settlement"]["observed_outcome"] = (
            copy.deepcopy(record["decision_outcome"])
        )
        decision["outcome_facts"] = {
            "selected_choice_ids": list(selected),
            "parent_choice_surface_pending": True,
        }
        return record

    @staticmethod
    def _refresh_canonical(record):
        state = {
            "phase": record["phase"],
            "options": record.get("available_options_before", []),
            "available_commands": record.get(
                "available_commands_before", []
            ),
        }
        payload = {
            "action": record["action"],
            "option_id": record.get("requested_target_id"),
        }
        choices = autoplay.canonical_legal_choices(
            state, payload, record.get("decision") or {}
        )
        record["legal_choices_before"] = choices
        selected = [
            row["choice_id"] for row in choices if row.get("selected") is True
        ]
        record["selected_choice_ids"] = selected
        record["final_choice_ids"] = list(selected)
        record["decision"]["outcome_facts"] = {
            "selected_choice_ids": list(selected)
        }

    def test_only_high_impact_noncombat_decisions_become_cases(self):
        combat = self._event_record()
        combat.update({"phase": "COMBAT", "action": "play"})
        self.assertFalse(decision_cases.is_replay_candidate(combat))
        self.assertIsNone(decision_cases.build_decision_case(combat))

    def test_shop_acquired_benefit_is_a_typed_effect_not_unclassified(self):
        benefit = {
            "kind": "potion", "id": "SpeedPotion",
            "potion_id": "SpeedPotion",
        }
        partition = decision_cases._independent_producer_partition({
            "consequences": {
                "gold_delta": -75,
                "acquired_benefit": benefit,
            },
        })

        self.assertEqual(
            benefit,
            partition["producer_consequence_claim"]["acquired_benefit"],
        )
        self.assertEqual([], partition["unclassified_producer_fields"])

    def test_mausoleum_typed_effect_and_score_fields_partition_without_loss(self):
        random_relic = {
            "kind": "random_relic_gain",
            "domain": "base_game_non_boss_relic_pool",
            "excluded_rarities": ["BOSS"],
            "count": 1,
            "timing": "immediate",
            "selection_mode": "random",
        }
        partition = decision_cases._independent_producer_partition({
            "consequences": {
                "event_id": "The Mausoleum",
                "mechanism_id": "base_game_the_mausoleum_v1",
                "operation": "mausoleum_open_coffin",
                "event_outcome_id": "open_coffin",
                "hp_delta": 0,
                "max_hp_delta": 0,
                "gold_delta": 0,
                "card_changes": {"gain": [], "conditional_gain": []},
                "relic_changes": {
                    "gain": [], "random_gain": [random_relic],
                    "remove": [], "counter": [],
                },
                "potion_changes": {
                    "gain": [], "remove": [], "replace": [],
                },
                "curse": {"gain": [], "probability": 0.5},
                "probabilistic_outcomes": [
                    {"id": "writhe_added", "probability": 0.5},
                    {"id": "no_writhe", "probability": 0.5},
                ],
                "current_cost": {"gold": 0, "hp": 0, "max_hp": 0},
                "future_costs": [],
                "random_effects": [random_relic],
                "strategic_value": {"random_relic_value": 16.0},
            },
        })

        self.assertEqual([], partition["unclassified_producer_fields"])
        self.assertEqual(
            "base_game_non_boss_relic_pool",
            partition["producer_consequence_claim"]["relic_changes"][
                "random_gain"
            ][0]["domain"],
        )
        self.assertEqual(
            {"random_relic_value": 16.0},
            partition["producer_scoring_facts"]["strategic_value"],
        )

    def test_typed_base_game_event_effects_partition_and_bind_exact_event(self):
        consequences = {
            "event_id": "The Cleric",
            "mechanism_id": cleric_purify_target()["mechanism_id"],
            "original_button_index": 1,
            "operation": "cleric_open_purge_grid",
            "event_outcome_id": "purify",
            "hp_delta": 0,
            "max_hp_delta": 0,
            "gold_delta": -50,
            "card_changes": {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
            },
            "relic_changes": {"gain": [], "remove": [], "counter": []},
            "potion_changes": {"gain": [], "remove": [], "replace": []},
            "curse": {"gain": [], "remove": [], "probability": 0.0},
            "probabilistic_outcomes": [],
            "current_cost": {"gold": 50, "hp": 0, "max_hp": 0},
            "future_costs": [{
                "kind": "cleric_grid_selection",
                "operation": "grid_purge",
                "select_count": 1,
            }],
            "random_effects": [],
            "reason_codes": ["typed_cleric"],
            "uncertainty": ["card UUID binds downstream"],
        }
        partition = decision_cases._independent_producer_partition({
            "consequences": consequences,
        })

        self.assertEqual([], partition["unclassified_producer_fields"])
        choice = {"target": cleric_purify_target()}
        producer = {"consequences": consequences}
        self.assertTrue(
            decision_cases._producer_consequence_target_matches(
                choice, producer
            )
        )
        forged = {
            "consequences": {
                **consequences,
                "event_id": "Designer",
            },
        }
        self.assertFalse(
            decision_cases._producer_consequence_target_matches(
                choice, forged
            )
        )

    def test_typed_event_score_envelope_is_shape_complete_for_veto_candidate(self):
        row = {
            "score_rule_id": "base_game_designer_unclassified_a0_v1",
            "score_formula": {"kind": "sum_components_v1"},
            "score_inputs": {"fail_closed": -1000000.0},
            "score_components": [{
                "name": "fail_closed",
                "input": "fail_closed",
                "coefficient": 1.0,
                "value": -1000000.0,
            }],
        }

        self.assertTrue(decision_cases._score_evidence_shape_complete(row))

    def test_goop_producer_binds_reflection_contract_and_rejects_tampering(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "World of Goop",
            "event_class": (
                "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
            ),
            "original_button_index": 1,
            "option_kind": "LEAVE",
            "parameters": {"gold_loss": 27},
        }
        digest = hashlib.sha256(json.dumps(
            contract, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:20]
        mechanism_id = f"event-mechanism:{digest}"
        target = {
            "kind": "event_option",
            "event_id": "World of Goop",
            "original_button_index": 1,
            "event_contract": contract,
            "mechanism_id": mechanism_id,
        }
        consequence = {
            "event_id": "World of Goop",
            "original_button_index": 1,
            "mechanism_id": mechanism_id,
            "operation": "world_of_goop_leave",
        }
        choice = {"target": target}

        self.assertTrue(
            decision_cases._producer_consequence_target_matches(
                choice, {"consequences": consequence}
            )
        )
        self.assertFalse(
            decision_cases._producer_consequence_target_matches(
                choice,
                {"consequences": {
                    **consequence,
                    "mechanism_id": "event-mechanism:tampered",
                }},
            )
        )
        forged_target = copy.deepcopy(target)
        forged_target["event_contract"]["parameters"]["gold_loss"] = 28
        self.assertFalse(
            decision_cases._producer_consequence_target_matches(
                {"target": forged_target},
                {"consequences": consequence},
            )
        )

    def test_neow_sapphire_key_and_chest_are_persisted(self):
        for phase in ("NEOW", "SAPPHIRE_KEY", "CHEST"):
            with self.subTest(phase=phase):
                record = self._event_record()
                record["phase"] = phase
                case = decision_cases.build_decision_case(record)
                self.assertIsNotNone(case)
                self.assertEqual(2, case["case_schema_version"])

    def test_new_case_is_schema_v2_with_canonical_audit_surface(self):
        case = decision_cases.build_decision_case(self._event_record())

        self.assertEqual(2, case["case_schema_version"])
        self.assertEqual(
            set(decision_cases.V2_CANONICAL_FIELDS),
            set(case["v2_contract"]["required_fields"]),
        )
        self.assertTrue(
            set(decision_cases.V2_CANONICAL_FIELDS) <= set(case)
        )
        self.assertEqual([], [
            field for field in decision_cases.V2_CANONICAL_FIELDS
            if field not in case
        ])

    def test_loader_preserves_legacy_v1_for_explicit_classification(self):
        legacy = {
            "case_schema_version": 1,
            "attempt_id": "legacy-attempt",
            "decision_hash": "legacy-hash",
            "phase": "EVENT",
        }
        current = decision_cases.build_decision_case(self._event_record())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(
                json.dumps(legacy) + "\n" + json.dumps(current) + "\n",
                encoding="utf-8",
            )

            loaded = decision_cases.load_cases(path)

        self.assertEqual([1, 2], [
            case["case_schema_version"] for case in loaded
        ])
        self.assertNotIn("v2_contract", loaded[0])
        self.assertIn("v2_contract", loaded[1])
        self.assertTrue(
            decision_cases.is_replay_candidate(self._event_record())
        )

    def test_append_fails_closed_before_case_corpus_can_grow_unbounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decision-cases.jsonl"
            with patch.object(decision_cases, "DECISION_CASES_MAX_BYTES", 1):
                with self.assertRaises(
                    decision_cases.DecisionCaseCorpusLimitError
                ):
                    decision_cases.append_decision_case(
                        path, self._event_record()
                    )
        self.assertFalse(path.exists())

    def test_attempt_shard_keeps_the_independent_corpus_budget(self):
        self.assertEqual(
            128 * 1024 * 1024,
            decision_cases.ATTEMPT_DECISION_CASES_MAX_BYTES,
        )
        self.assertEqual(
            decision_cases.DECISION_CASES_MAX_BYTES,
            decision_cases.ATTEMPT_DECISION_CASES_MAX_BYTES,
        )

    def test_shop_screen_trace_phase_is_a_replay_candidate(self):
        shop = self._event_record()
        shop.update({"phase": "SHOP_SCREEN", "action": "choose"})

        self.assertTrue(decision_cases.is_replay_candidate(shop))
        self.assertEqual(
            "SHOP_SCREEN",
            decision_cases.build_decision_case(shop)["phase"],
        )

    def test_resource_preparation_use_discard_share_slot_but_replay_exactly(self):
        record = self._resource_preparation_record()
        self.assertTrue(decision_cases.is_replay_candidate(record))

        case = decision_cases.build_decision_case(record)
        result = decision_case_replay._audit_one(
            case, historical=False
        )

        self.assertEqual("audited", result["classification"])
        self.assertEqual([], result["issues"])
        self.assertEqual([], result["unknowns"])
        self.assertEqual(
            {(0, "potion", "discard"), (0, "potion", "use")},
            {
                (
                    row["choice_index"], row["action"], row["operation"]
                )
                for row in case["canonical_choices"]
            },
        )

    def test_resource_preparation_operation_swap_or_deletion_is_blocking(self):
        for mutation in ("delete", "swap"):
            with self.subTest(mutation=mutation):
                record = self._resource_preparation_record()
                if mutation == "delete":
                    record["decision"]["candidates"][0].pop("operation")
                else:
                    record["decision"]["candidates"][0]["operation"] = "use"
                case = decision_cases.build_decision_case(record)
                result = decision_case_replay._audit_one(
                    case, historical=False
                )
                self.assertNotEqual("audited", result["classification"])
                self.assertTrue(result["issues"] or result["unknowns"])

    def test_case_keeps_choice_evidence_without_full_prompt_snapshot(self):
        case = decision_cases.build_decision_case(self._event_record())
        self.assertEqual("maskedbandits", case["event_id"])
        self.assertEqual(1, case["chosen"]["semantic_id"])
        self.assertEqual(2, len(case["candidates"]))
        self.assertEqual("applied", case["model_advice"]["status"])
        self.assertIn("fusion", case["model_advice"])
        self.assertNotIn("replay", case["model_advice"])

    def test_case_downgrades_unproven_partial_candidate_contract(self):
        record = self._event_record()
        record["available_options_before"].append({
            "option_id": "option:2",
            "choice_index": 2,
            "label": "Third",
        })
        record["decision"]["candidate_contract"].update({
            "all_visible_options_scored": True,
        })
        self._refresh_canonical(record)

        case = decision_cases.build_decision_case(record)
        contract = case["candidate_contract"]

        self.assertFalse(contract["strategy_quality_auditable"])
        self.assertTrue(contract["producer_strategy_quality_auditable"])
        self.assertFalse(contract["case_visible_option_coverage_complete"])
        self.assertEqual(2, contract["case_bound_visible_option_count"])
        self.assertEqual(
            ["option:2"], contract["unmatched_visible_option_ids"]
        )

    def test_scored_event_dict_candidates_bind_by_visible_choice_index(self):
        record = self._event_record()
        record["decision"]["candidates"] = [
            {
                "id": "event:0", "choice_id": "event:0",
                "choice_index": 0, "action": "choose",
                "operation": None,
                "semantic_id": "event:0", "score": -200.0, "label": "Pay",
                "text": "Lose all Gold.",
                "consequences": {"gold_delta": -200},
                **score_contract(-200.0),
            },
            {
                "id": "event:1", "choice_id": "event:1",
                "choice_index": 1, "action": "choose",
                "operation": None,
                "semantic_id": "event:1", "score": 25.0, "label": "Fight",
                "text": "Fight the bandits.",
                "consequences": {"combat_delta": 1},
                **score_contract(25.0),
            },
        ]
        self._refresh_canonical(record)

        contract = decision_cases.build_decision_case(record)[
            "candidate_contract"
        ]

        self.assertTrue(contract["strategy_quality_auditable"])
        self.assertTrue(contract["case_visible_option_coverage_complete"])
        self.assertEqual(2, contract["case_bound_visible_option_count"])

    def test_return_skip_is_a_replay_case_and_keeps_canonical_choice(self):
        record = self._event_record()
        record.update({
            "phase": "CARD_REWARD",
            "action": "return",
            "requested_target_id": "action:return",
            "resolved_target_id": "action:return",
            "chosen_option_before": {
                "option_id": "action:return",
                "choice_index": None,
                "label": "return",
                "target": {
                    "kind": "protocol_action", "action": "return"
                },
            },
            "available_options_before": [{
                "option_id": "option:card-a",
                "choice_index": 0,
                "label": "Card A",
            }],
            "available_commands_before": ["choose", "return"],
            "legal_choices_before": [
                {
                    "choice_id": "option:card-a",
                    "choice_index": 0,
                    "action": "choose",
                    "semantic_id": "card:a",
                    "label": "Card A",
                },
                {
                    "choice_id": "action:return",
                    "choice_index": None,
                    "action": "return",
                    "semantic_id": "skip",
                    "label": "return",
                },
            ],
        })
        record["decision"].update({
            "reason": "card_reward_skip_nonpositive",
            "chosen": "Skip",
            "chosen_id": "skip",
            "candidates": [
                {
                    "id": "card:a", "choice_id": "card:a",
                    "label": "Card A", "score": -1.0,
                },
                {
                    "id": "skip", "choice_id": "action:return",
                    "action": "return", "label": "Skip", "score": 0.0,
                },
            ],
        })

        self.assertTrue(decision_cases.is_replay_candidate(record))
        case = decision_cases.build_decision_case(record)

        self.assertEqual("action:return", case["chosen"]["requested_target_id"])
        self.assertEqual(2, len(case["canonical_choices"]))
        self.assertEqual(["choose", "return"], case["available_commands"])
        self.assertTrue(
            case["candidate_contract"][
                "case_visible_option_coverage_complete"
            ]
        )

    def test_candidate_reorder_preserves_semantic_binding(self):
        record = self._event_record()
        record["legal_choices_before"] = [
            {
                "choice_id": "event:0", "choice_index": 0,
                "action": "choose", "semantic_id": "pay", "label": "Pay",
            },
            {
                "choice_id": "event:1", "choice_index": 1,
                "action": "choose", "semantic_id": "fight", "label": "Fight",
            },
        ]
        record["decision"]["candidates"] = [
            {"id": "event:0", "choice_id": "event:0", "score": -200.0},
            {"id": "event:1", "choice_id": "event:1", "score": 25.0},
        ]
        first = decision_cases.build_decision_case(record)
        record["legal_choices_before"].reverse()
        record["decision"]["candidates"].reverse()
        second = decision_cases.build_decision_case(record)

        self.assertEqual(first["chosen"], second["chosen"])
        self.assertTrue(
            second["candidate_contract"][
                "case_visible_option_coverage_complete"
            ]
        )

    def test_duplicate_or_extra_candidate_downgrades_contract(self):
        for mutation in ("duplicate", "extra"):
            with self.subTest(mutation=mutation):
                record = self._event_record()
                if mutation == "duplicate":
                    record["decision"]["candidates"].append(
                        dict(record["decision"]["candidates"][0])
                    )
                else:
                    record["decision"]["candidates"].append({
                        "id": "ghost", "choice_id": "ghost", "score": 99,
                    })
                contract = decision_cases.build_decision_case(record)[
                    "candidate_contract"
                ]
                self.assertFalse(contract["strategy_quality_auditable"])
                self.assertFalse(
                    contract["case_visible_option_coverage_complete"]
                )

    def test_proceed_transition_is_not_persisted_as_strategy_case(self):
        record = self._event_record()
        record.update({
            "phase": "COMBAT_REWARD",
            "action": "proceed",
            "requested_target_id": "action:proceed",
            "resolved_target_id": "action:proceed",
            "available_options_before": [],
        })
        record["decision"] = {}
        self.assertFalse(decision_cases.is_replay_candidate(record))
        self.assertIsNone(decision_cases.build_decision_case(record))

    def test_real_trace_binding_types_and_selection_digest_are_preserved(self):
        built = decision_cases.build_decision_case(self._event_record())
        self.assertEqual(5, built["trace_schema_version"])
        self.assertEqual(1, built["seed"])
        self.assertEqual("d" * 64, built["selection_digest"])
        result = decision_case_replay._audit_one(built, historical=False)
        self.assertNotIn("v2_binding_seed", result["issues"])
        self.assertNotIn("v2_trace_schema_version", result["issues"])
        self.assertNotIn("v2_binding_selection_digest", result["issues"])

        invalid = copy.deepcopy(built)
        invalid["selection_digest"] = "not-a-digest"
        result = decision_case_replay._audit_one(invalid, historical=False)
        self.assertEqual("audited_issues", result["classification"])
        self.assertIn("v2_binding_selection_digest", result["issues"])

    def test_append_and_load_are_cohort_filterable_and_bad_line_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            self.assertTrue(
                decision_cases.append_decision_case(
                    path, self._event_record()
                )
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
                handle.write(json.dumps({"case_schema_version": 99}) + "\n")
            self.assertEqual(
                1,
                len(
                    decision_cases.load_cases(
                        path,
                        attempt_id="attempt-a",
                        decision_hash="hash-a",
                    )
                ),
            )
            self.assertEqual(
                [], decision_cases.load_cases(path, attempt_id="other")
            )

    def test_export_trace_cases_filters_to_the_requested_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            output = Path(directory) / "cases.jsonl"
            first = self._event_record()
            second = self._event_record()
            second["attempt_id"] = "attempt-b"
            combat = self._event_record()
            combat.update({"phase": "COMBAT", "action": "play"})
            with trace.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(first) + "\n")
                handle.write("bad-json\n")
                handle.write(json.dumps(second) + "\n")
                handle.write(json.dumps(combat) + "\n")
            exported = decision_cases.export_trace_cases(
                trace, output, attempt_id="attempt-a"
            )
            self.assertEqual(1, exported)
            self.assertEqual(
                ["attempt-a"],
                [case["attempt_id"] for case in decision_cases.load_cases(output)],
            )


if __name__ == "__main__":
    unittest.main()
