import copy
import unittest
from copy import deepcopy
from unittest.mock import patch

import decision_case_replay


def structured_consequence(gold_delta):
    return {
        "schema_version": 1,
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": gold_delta,
        "card_changes": {},
        "relic_changes": {},
        "potion_changes": {},
        "curse": {},
        "probabilistic_outcomes": [],
        "current_cost": {"gold": 0, "hp": 0, "max_hp": 0},
        "future_costs": [],
        "raw_effect_text": "",
        "uncertainty": [],
    }


def permutation_invariant(candidate_count, protocol_count):
    kinds = []
    if candidate_count >= 2:
        kinds.append("candidate")
    if protocol_count >= 2:
        kinds.append("protocol")
    if candidate_count >= 2 and protocol_count >= 2:
        kinds.append("candidate+protocol")
    digests = [character * 64 for character in "bcd"[:len(kinds)]]

    def axis(count):
        if count >= 2:
            return {
                "status": "clear",
                "authority": "production_container_permutation_v1",
                "reason": (
                    "container_has_at_least_two_items_and_reorder_"
                    "preserved_semantic_selection"
                ),
            }
        return {
            "status": "not_applicable",
            "authority": "production_container_cardinality_v1",
            "reason": "container_has_fewer_than_two_items",
        }

    selected = ["option:selected"]
    return {
        "candidate_count": candidate_count,
        "protocol_count": protocol_count,
        "permutation_axes": {
            "candidate": axis(candidate_count),
            "protocol": axis(protocol_count),
        },
        "positive_case_sha256": "a" * 64,
        "reordered_case_sha256": digests[0] if digests else None,
        "positive_permutation_count": len(kinds),
        "positive_permutation_kinds": kinds,
        "positive_permutation_sha256s": digests,
        "selected_choice_ids": selected,
        "positive_permutation_selected_choice_ids": [
            list(selected) for _kind in kinds
        ],
    }


def case(phase="EVENT", *, decision_hash="fixture"):
    choices = [
        {
            "choice_schema_version": 1,
            "choice_id": "choice:a", "choice_index": 7,
            "action": "choose", "semantic_id": "gain:a",
            "legal": True, "visible": True, "selection_eligible": True,
            "label": "A", "raw_text": "A", "target": {},
            "probability_outcomes": [], "local_score": 1.0,
            "model_score": None, "model_confidence": None,
            "final_source": "local", "override": {},
            "uncertainty": None, "selected": False,
            "consequences": structured_consequence(0),
        },
        {
            "choice_schema_version": 1,
            "choice_id": "choice:b", "choice_index": 3,
            "action": "choose", "semantic_id": "gain:b",
            "legal": True, "visible": True, "selection_eligible": True,
            "label": "B", "raw_text": "B", "target": {},
            "probability_outcomes": [], "local_score": 2.0,
            "model_score": None, "model_confidence": None,
            "final_source": "local", "override": {},
            "uncertainty": None, "selected": True,
            "consequences": structured_consequence(10),
        },
    ]
    candidates = [
        {
            "choice_id": "choice:a", "choice_index": 7,
            "action": "choose", "semantic_id": "gain:a",
            "selection_eligible": True, "score": 1.0,
            "consequences": structured_consequence(0),
        },
        {
            "choice_id": "choice:b", "choice_index": 3,
            "action": "choose", "semantic_id": "gain:b",
            "selection_eligible": True, "score": 2.0,
            "consequences": structured_consequence(10),
        },
    ]
    return {
        "case_schema_version": 1,
        "attempt_id": "fixture-attempt",
        "run_id": "IRONCLAD:0:1",
        "decision_hash": decision_hash,
        "before_seq": 10,
        "phase": phase,
        "action": "choose",
        "chosen": {
            "requested_target_id": "choice:b",
            "resolved_target_id": "choice:b",
        },
        "final_choice_ids": ["choice:b"],
        "canonical_choices": choices,
        "candidates": candidates,
    }


def fixtures():
    return [case(phase) for phase in decision_case_replay.REQUIRED_FIXTURE_PHASES]


class DecisionCaseReplayTests(unittest.TestCase):
    @staticmethod
    def _forced_wheel_v2_case():
        target = {
            "kind": "event_option",
            "event_id": "Wheel of Change",
            "original_button_index": 0,
            "card": None,
        }
        producer = {
            "id": "0",
            "choice_id": "event:0",
            "choice_index": 0,
            "action": "choose",
            "operation": None,
            "semantic_id": "event:0",
            "legal": True,
            "visible": True,
            "selection_eligible": True,
            "consequences": {"hp_delta": -8},
        }
        choice = {
            "choice_id": "option:wheel:0",
            "choice_index": 0,
            "action": "choose",
            "operation": None,
            "semantic_id": "event:0",
            "legal": True,
            "visible": True,
            "selection_eligible": True,
            "selected": True,
            "candidate_binding": "unique",
            "candidate_ids": ["event:0"],
            "target": deepcopy(target),
            "producer_candidate_raw": deepcopy(producer),
        }
        candidate = {
            "choice_id": "option:wheel:0",
            "choice_index": 0,
            "action": "choose",
            "operation": None,
            "semantic_id": "event:0",
            "legal": True,
            "visible": True,
            "selection_eligible": True,
            "target": deepcopy(target),
            "producer_candidate_index": 0,
            "producer_candidate_id": "event:0",
            "producer_candidate_raw": deepcopy(producer),
        }
        return {
            "case_schema_version": 2,
            "phase": "EVENT",
            "action": "choose",
            "decision_surface_kind": "strategic_choice",
            "parent_choice_surface_pending": False,
            "resource_preparation_options": [],
            "requested_target_id": "option:wheel:0",
            "resolved_target_id": "option:wheel:0",
            "chosen": {
                "requested_target_id": "option:wheel:0",
                "resolved_target_id": "option:wheel:0",
                "choice_index": 0,
            },
            "final_choice_ids": ["option:wheel:0"],
            "available_options": [{
                "option_id": "option:wheel:0",
                "choice_index": 0,
                "target": deepcopy(target),
            }],
            "canonical_choices": [choice],
            "candidates": [candidate],
            "producer_candidates": [producer],
        }

    def test_v2_forced_wheel_singleton_is_only_a_nonranking_classification(self):
        value = self._forced_wheel_v2_case()
        classification = (
            decision_case_replay._v2_forced_singleton_event_nonchoice_classification(
                value, value["available_options"], value["candidates"],
            )
        )
        self.assertEqual(
            "forced_singleton_wheel_of_change_transition",
            classification["reason"],
        )
        with patch.object(
            decision_case_replay,
            "_v2_contract_problems",
            return_value=([], ["v2_independent_consequence_unknown"]),
        ):
            issues, unknowns, selected, evidence = (
                decision_case_replay._canonical_case(value, schema_v2=True)
            )
        self.assertEqual([], issues)
        self.assertEqual([], unknowns)
        self.assertIsNone(selected)
        self.assertEqual("not_applicable", evidence["classification"])

    def test_v2_forced_wheel_does_not_widen_to_other_or_malformed_events(self):
        value = self._forced_wheel_v2_case()
        for mutator in (
            lambda row: row["available_options"][0]["target"].update(
                event_id="The Cleric"
            ),
            lambda row: row["canonical_choices"][0]["target"].update(
                original_button_index=1
            ),
            lambda row: row.update(final_choice_ids=[]),
            lambda row: row["candidates"].append(deepcopy(row["candidates"][0])),
        ):
            with self.subTest(mutator=mutator):
                changed = deepcopy(value)
                mutator(changed)
                self.assertIsNone(
                    decision_case_replay.
                    _v2_forced_singleton_event_nonchoice_classification(
                        changed,
                        changed["available_options"],
                        changed["candidates"],
                    )
                )
    def test_axis_aware_permutation_evidence_contract(self):
        validator = (
            decision_case_replay.resolution_permutation_invariant_is_valid
        )
        for candidate_count, protocol_count, expected_count in (
            (1, 1, 0),
            (2, 1, 1),
            (1, 2, 1),
            (2, 2, 3),
        ):
            with self.subTest(
                candidate_count=candidate_count,
                protocol_count=protocol_count,
            ):
                value = permutation_invariant(
                    candidate_count, protocol_count
                )
                self.assertEqual(
                    expected_count, value["positive_permutation_count"]
                )
                self.assertTrue(validator(value))

        forged = permutation_invariant(2, 1)
        forged["permutation_axes"]["protocol"] = {
            "status": "clear",
            "authority": "production_container_permutation_v1",
            "reason": (
                "container_has_at_least_two_items_and_reorder_"
                "preserved_semantic_selection"
            ),
        }
        self.assertFalse(validator(forged))

    def test_score_replay_preserves_fractional_and_negative_components(self):
        row = {
            "local_score": 1.0 / 3.0 - 0.1 - 2.5,
            "score_rule_id": "precision_fixture_v1",
            "score_formula": {"kind": "sum_components_v1"},
            "score_inputs": {
                "third": 1.0 / 3.0,
                "small_penalty": 0.1,
                "large_penalty": 2.5,
            },
            "score_components": [
                {
                    "name": "third", "input": "third",
                    "coefficient": 1.0, "value": 1.0 / 3.0,
                },
                {
                    "name": "small_penalty", "input": "small_penalty",
                    "coefficient": -1.0, "value": -0.1,
                },
                {
                    "name": "large_penalty", "input": "large_penalty",
                    "coefficient": -1.0, "value": -2.5,
                },
            ],
        }
        issues, unknowns = decision_case_replay._recompute_local_score(row)
        self.assertEqual([], issues)
        self.assertEqual([], unknowns)

        rounded = copy.deepcopy(row)
        rounded["local_score"] = round(row["local_score"], 3)
        issues, unknowns = decision_case_replay._recompute_local_score(
            rounded
        )
        self.assertIn("candidate_score_formula_result_mismatch", issues)
        self.assertEqual([], unknowns)

    def test_complete_fixture_matrix_and_historical_case_is_actually_audited(self):
        report = decision_case_replay.audit_cases(
            [case("MAP", decision_hash="old")],
            "current",
            fixture_cases=fixtures(),
        )
        self.assertEqual("clear", report["status"])
        self.assertEqual(1, report["legacy_cases"]["count"])
        self.assertEqual(1, report["historical_audited_count"])
        self.assertEqual(0, report["historical_unresolved_count"])
        self.assertEqual("audited", report["case_results"][0]["classification"])
        self.assertEqual([], report["missing_fixture_phases"])

    def test_fixture_only_zero_history_cannot_self_prove_replay(self):
        report = decision_case_replay.audit_cases(
            [], "brand-new-hash", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        self.assertFalse(report["release_gate_passed"])
        self.assertEqual(0, report["source_case_count"])
        self.assertIn(
            "historical_case_corpus_empty",
            {item["kind"] for item in report["eligible_unknowns"]},
        )

    def test_deleted_duplicate_or_extra_candidate_is_detected(self):
        mutations = []
        deleted = case()
        deleted["candidates"] = deleted["candidates"][:-1]
        mutations.append(deleted)
        duplicate = case()
        duplicate["candidates"][1]["choice_id"] = "choice:a"
        mutations.append(duplicate)
        extra = case()
        extra["candidates"].append({
            "choice_id": "choice:invisible", "score": 3,
            "consequences": {},
        })
        mutations.append(extra)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                report = decision_case_replay.audit_cases(
                    [mutation], "fixture", fixture_cases=fixtures()
                )
                self.assertEqual("issues", report["status"])

    def test_container_reorder_keeps_semantic_selection(self):
        for surface in ("canonical_choices", "candidates"):
            reordered = copy.deepcopy(case())
            reordered[surface].reverse()
            report = decision_case_replay.audit_cases(
                [reordered], "fixture", fixture_cases=fixtures()
            )
            self.assertEqual("clear", report["status"])

    def test_producer_selection_eligible_flag_cannot_hide_better_choice(self):
        value = case()
        value["candidates"][0]["score"] = 5.0
        value["canonical_choices"][0]["local_score"] = 5.0
        value["canonical_choices"][1]["consequences"] = (
            structured_consequence(0)
        )
        value["candidates"][1]["consequences"] = structured_consequence(0)
        value["candidates"][0]["selection_eligible"] = False
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("issues", report["status"])
        self.assertIn(
            "candidate_selection_eligible_binding",
            {item["kind"] for item in report["issues"]},
        )

    def test_vetoed_high_score_is_excluded_from_argmax(self):
        value = case()
        value["canonical_choices"][0]["local_score"] = 99.0
        value["canonical_choices"][0]["selection_eligible"] = False
        value["canonical_choices"][0]["veto_reason"] = "mechanism_veto"
        value["candidates"][0]["score"] = 99.0
        value["candidates"][0]["selection_eligible"] = False
        value["candidates"][0]["veto_reason"] = "mechanism_veto"
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("clear", report["status"])

    def test_vetoed_choice_cannot_be_selected(self):
        value = case()
        value["canonical_choices"][0]["selection_eligible"] = False
        value["canonical_choices"][0]["veto_reason"] = "mechanism_veto"
        value["candidates"][0]["selection_eligible"] = False
        value["candidates"][0]["veto_reason"] = "mechanism_veto"
        value["canonical_choices"][0]["selected"] = True
        value["canonical_choices"][1]["selected"] = False
        value["final_choice_ids"] = ["choice:a"]
        value["chosen"]["requested_target_id"] = "choice:a"
        value["chosen"]["resolved_target_id"] = "choice:a"
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("issues", report["status"])
        self.assertIn(
            "selected_choice_ineligible",
            {item["kind"] for item in report["issues"]},
        )

    def test_tampered_final_choice_is_detected(self):
        value = case()
        value["final_choice_ids"] = ["choice:a"]
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("issues", report["status"])
        self.assertIn(
            "final_choice_binding",
            {item["kind"] for item in report["issues"]},
        )

    def test_producer_labeled_blind_review_is_ignored(self):
        value = case()
        value["candidates"][0]["score"] = 5.0
        value["canonical_choices"][0]["local_score"] = 5.0
        value["canonical_choices"][1]["consequences"] = (
            structured_consequence(0)
        )
        value["candidates"][1]["consequences"] = structured_consequence(0)
        value["independent_blind_review"] = {
            "status": "clear",
            "attempt_id": value["attempt_id"],
            "decision_hash": value["decision_hash"],
            "before_seq": value["before_seq"],
            "blinded_to_local_choice": True,
            "blinded_to_local_scores": True,
            "reviewer": "independent-fixture-v1",
            "candidate_choice_ids": ["choice:a", "choice:b"],
            "recommended_choice_id": "choice:b",
        }
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "below_argmax_without_independent_review",
            {item["kind"] for item in report["eligible_unknowns"]},
        )
        self.assertTrue(
            report["case_results"][0]["producer_blind_review_ignored"]
        )

    def test_consequence_only_blind_reviewer_can_resolve_dominance(self):
        value = case()
        value["candidates"][0]["score"] = 5.0
        value["canonical_choices"][0]["local_score"] = 5.0
        consequences = (structured_consequence(0), structured_consequence(10))
        for choice, candidate, consequence in zip(
            value["canonical_choices"], value["candidates"], consequences
        ):
            choice["consequences"] = copy.deepcopy(consequence)
            candidate["consequences"] = copy.deepcopy(consequence)
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("clear", report["status"])

    def test_missing_candidate_semantics_and_empty_consequence_fail_closed(self):
        value = case()
        for field in ("choice_index", "action", "semantic_id"):
            value["candidates"][0].pop(field)
        value["candidates"][0]["consequences"] = {}
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        kinds = {item["kind"] for item in report["eligible_unknowns"]}
        self.assertIn("candidate_semantic_choice_index_missing", kinds)
        self.assertIn("candidate_consequences_missing", kinds)

    def test_incomplete_or_malformed_structured_consequence_fails_closed(self):
        value = case()
        for row in (value["canonical_choices"][0], value["candidates"][0]):
            row["consequences"].pop("future_costs")
            row["consequences"]["current_cost"]["gold"] = "zero"
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        kinds = {item["kind"] for item in report["eligible_unknowns"]}
        self.assertIn("choice_consequence_future_costs_missing", kinds)
        self.assertIn("candidate_consequence_current_cost_gold_invalid", kinds)

    def test_legacy_missing_candidate_surface_is_unresolved_not_hash_n_a(self):
        value = case(decision_hash="old")
        value.pop("canonical_choices")
        value["available_options"] = [
            {
                "option_id": "choice:a", "choice_index": 7,
                "label": "A", "target": {"kind": "event_option"},
            },
            {
                "option_id": "choice:b", "choice_index": 3,
                "label": "B", "target": {"kind": "event_option"},
            },
        ]
        value["candidates"] = []
        report = decision_case_replay.audit_cases(
            [value], "current", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        self.assertEqual(1, report["historical_unresolved_count"])
        self.assertEqual("unresolved", report["case_results"][0]["classification"])

    def test_bound_proceed_transition_has_explicit_not_applicable_class(self):
        value = {
            "case_schema_version": 1,
            "attempt_id": "old-attempt",
            "run_id": "IRONCLAD:0:1",
            "decision_hash": "old",
            "before_seq": 11,
            "phase": "COMBAT_REWARD",
            "action": "proceed",
            "chosen": {
                "requested_target_id": "action:proceed",
                "resolved_target_id": "action:proceed",
            },
        }
        report = decision_case_replay.audit_cases(
            [value], "current", fixture_cases=fixtures()
        )
        self.assertEqual("clear", report["status"])
        self.assertEqual(1, report["historical_classified_not_applicable_count"])
        row = report["case_results"][0]
        self.assertEqual("not_applicable", row["classification"])
        self.assertEqual("non_choice_protocol_transition", row["reason"])

    def test_collectible_combat_rewards_are_not_false_ranked_as_exclusive(self):
        value = case("COMBAT_REWARD", decision_hash="old")
        value["available_options"] = [
            {
                "option_id": "choice:a", "choice_index": 7,
                "label": "Gold", "target": {"kind": "reward"},
            },
            {
                "option_id": "choice:b", "choice_index": 3,
                "label": "Potion", "target": {"kind": "reward"},
            },
        ]
        report = decision_case_replay.audit_cases(
            [value], "current", fixture_cases=fixtures()
        )
        row = report["case_results"][0]
        self.assertEqual("not_applicable", row["classification"])
        self.assertEqual(
            "independent_collectible_rewards_not_mutually_exclusive",
            row["reason"],
        )

    def test_missing_consequences_or_score_is_unknown(self):
        value = case()
        value["candidates"][0].pop("consequences")
        value["candidates"][1].pop("score")
        report = decision_case_replay.audit_cases(
            [value], "fixture", fixture_cases=fixtures()
        )
        self.assertEqual("inconclusive", report["status"])
        self.assertGreater(report["eligible_unknown_count"], 0)

    def test_missing_fixture_phase_is_unknown(self):
        partial = fixtures()[:-1]
        report = decision_case_replay.audit_cases(
            [], "current", fixture_cases=partial
        )
        self.assertEqual("inconclusive", report["status"])
        self.assertEqual(1, len(report["missing_fixture_phases"]))


if __name__ == "__main__":
    unittest.main()
