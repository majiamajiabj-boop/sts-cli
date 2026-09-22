import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import campaign_selector


def result(character, index, *, decision_hash="decision-a", win=False, **overrides):
    selection_id = f"selection-{character}-{index}"
    controller_hash = overrides.get("controller_hash", "controller-a")
    value = {
        "schema_version": 2,
        "record_type": "terminal_result",
        "attempt_id": f"attempt-{character}-{index}",
        "goal_mode": "HEART",
        "policy_version": "fast-policy-v5",
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "ascension_level": 0,
        "run_type": "standard",
        "character": character,
        "class": character,
        "seed": index,
        "run_id": f"{character}:0:{index}",
        "selection_id": selection_id,
        "state_seq": 1000 + index,
        "terminal_state_seq": 1000 + index,
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "heart_defeated": win,
        "victory": win,
        "act": 4 if win else 2,
        "floor": 55 if win else 20,
        "observed_max_act": 4 if win else 2,
        "actions": 100 + index,
        "keys": {"ruby": win, "emerald": win, "sapphire": win},
        "code_changed": False,
        "selection": {
            "selection_id": selection_id,
            "algorithm": "beta-thompson-v1",
            "created_at": 100.0 + index,
            "decision_hash": decision_hash,
            "controller_hash": controller_hash,
            "goal_mode": "HEART",
            "policy_version": "fast-policy-v5",
            "ascension_level": 0,
            "run_type": "standard",
            "character": character,
        },
    }
    value.update(overrides)
    if "selection_digest" not in overrides:
        value["selection_digest"] = (
            campaign_selector.selection_digest(value["selection"])
            if isinstance(value.get("selection"), dict)
            else "0" * 64
        )
    return value


def audit(terminal, **overrides):
    value = {
        "record_type": "run_audit",
        "schema_version": 2,
        "policy_version": terminal.get("policy_version"),
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character"),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get("terminal_state_seq"),
        "termination_kind": "game_over",
        "audit_status": "clear",
        "release_gate_passed": True,
        "protocol_events": 0,
        "operational_error_attempts": 0,
        "operational_error_events": 0,
        "protocol_correctness": {"status": "clear"},
        "mechanics_coverage": {"status": "clear"},
        "strategy_quality": {"status": "clear"},
        "issue_count": 0,
        "review_finding_count": 0,
        "oracle_disagreement_count": 0,
        "eligible_unknown": 0,
        "model_conflict_count": 0,
        "independent_oracle": {
            "status": "clear",
            "issue_count": 0,
            "eligible_unknown_count": 0,
            "disagreement_count": 0,
        },
        "death_observed": False,
        "death_replay": {
            "status": "not_applicable",
            "issue_count": 0,
            "eligible_unknown_count": 0,
            "death_observed": False,
        },
        "code_changed": False,
        "generated_at": 2000 + int(terminal.get("state_seq") or 0),
    }
    if "performance_hash" in terminal:
        value["performance_hash"] = terminal.get("performance_hash")
    value.update(overrides)
    return value


def controller_exit(terminal, **overrides):
    empty_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    value = {
        "record_type": "controller_exit",
        "schema_version": 2,
        "policy_version": terminal.get("policy_version"),
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character"),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get("terminal_state_seq"),
        "controller_exit_status": "clear",
        "exit_code": 0,
        "stdout_size": 3,
        "stdout_sha256": empty_hash,
        "stdout_line_count": 1,
        "stdout_semantic_sha256": "a" * 64,
        "stderr_size": 0,
        "stderr_sha256": empty_hash,
        "freeze_manifest_sha256": "b" * 64,
        "freeze_source_digest": "c" * 64,
        "freeze_generated_at": 100.0,
    }
    value.update(overrides)
    return value


def audited(*terminals):
    records = []
    for terminal in terminals:
        records.extend((terminal, audit(terminal), controller_exit(terminal)))
    return records


class CampaignSelectorTests(unittest.TestCase):
    @staticmethod
    def _nonclear_completed(terminal):
        return [
            terminal,
            audit(
                terminal,
                audit_status="inconclusive",
                release_gate_passed=False,
                mechanics_coverage={"status": "inconclusive"},
                strategy_quality={"status": "issues"},
                issue_count=1,
                eligible_unknown=1,
                independent_oracle={
                    "status": "inconclusive",
                    "issue_count": 0,
                    "eligible_unknown_count": 1,
                    "disagreement_count": 0,
                },
            ),
            controller_exit(
                terminal, controller_exit_status="issues"
            ),
        ]

    def test_fixed_six_run_quota_stops_at_first_nonclear_audit(self):
        selection = campaign_selector.select_character(
            [], "decision-a", random.Random(0),
            controller_hash="controller-a",
        )
        terminal = result(
            "IRONCLAD", 100, selection=selection,
            selection_id=selection["selection_id"],
        )
        records = self._nonclear_completed(terminal)

        self.assertEqual([], campaign_selector.cohort_quota_attempts(
            records, "decision-a", "controller-a",
        ))
        with self.assertRaisesRegex(
            campaign_selector.HistoryValidationError,
            "same-hash unresolved attempt",
        ):
            campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(1),
                controller_hash="controller-a",
            )

    def test_followup_validation_batch_round_robins_after_fixed_cohort(self):
        records = []
        for index, character in enumerate(
            ("IRONCLAD", "THE_SILENT", "DEFECT") * 2
        ):
            selection = campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(index),
                controller_hash="controller-a",
            )
            terminal = result(
                character,
                100 + index,
                selection=selection,
                selection_id=selection["selection_id"],
            )
            records.extend(audited(terminal))

        expected = ("IRONCLAD", "THE_SILENT", "DEFECT") * 2
        for index, character in enumerate(expected):
            selection = campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(index),
                controller_hash="controller-a",
                validation_batch=True,
            )
            self.assertEqual(character, selection["character"])
            self.assertEqual(
                campaign_selector.FOLLOWUP_VALIDATION_REASON,
                selection["reason"],
            )
            terminal = result(
                character,
                200 + index,
                selection=selection,
                selection_id=selection["selection_id"],
            )
            records.extend(audited(terminal))

        with self.assertRaisesRegex(
            campaign_selector.HistoryValidationError,
            "follow-up validation batch is complete",
        ):
            campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(9),
                controller_hash="controller-a",
                validation_batch=True,
            )

    def test_p0_only_batches_never_count_other_decision_hashes(self):
        records = []
        expected = ("IRONCLAD", "THE_SILENT", "DEFECT") * 2
        for index, character in enumerate(expected):
            terminal = result(
                character, index, decision_hash="decision-old",
            )
            terminal["selection"]["reason"] = (
                "fixed_six_run_character_quota"
            )
            terminal["selection_digest"] = (
                campaign_selector.selection_digest(terminal["selection"])
            )
            issue_audit = audit(
                terminal,
                audit_status="issues",
                release_gate_passed=False,
                issue_count=1,
                issues=[{"severity": "P1", "kind": "strategy"}],
            )
            issue_exit = controller_exit(
                terminal,
                controller_exit_status="issues",
                controller_exit_audit={
                    "audit_status": "issues",
                    "release_gate_passed": False,
                    "issues": [{"severity": "P1", "kind": "strategy"}],
                },
            )
            records.extend((terminal, issue_audit, issue_exit))

        for index, character in enumerate(expected, start=len(expected)):
            terminal = result(
                character, index, decision_hash="decision-old",
            )
            terminal["selection"]["reason"] = (
                campaign_selector.FOLLOWUP_VALIDATION_REASON
            )
            terminal["selection_digest"] = (
                campaign_selector.selection_digest(terminal["selection"])
            )
            issue_audit = audit(
                terminal,
                audit_status="issues",
                release_gate_passed=False,
                issue_count=1,
                issues=[{"severity": "P1", "kind": "strategy"}],
            )
            issue_exit = controller_exit(
                terminal,
                controller_exit_status="issues",
                controller_exit_audit={
                    "audit_status": "issues",
                    "release_gate_passed": False,
                    "issues": [{"severity": "P1", "kind": "strategy"}],
                },
            )
            records.extend((terminal, issue_audit, issue_exit))

        self.assertEqual([], campaign_selector.cohort_quota_attempts(
            records,
            "decision-new",
            "controller-a",
            p0_only_batch=True,
        ))
        self.assertEqual([], campaign_selector.followup_validation_attempts(
            records,
            "decision-new",
            "controller-a",
            p0_only_batch=True,
        ))
        selection = campaign_selector.select_character(
            records,
            "decision-new",
            random.Random(9),
            controller_hash="controller-a",
            p0_only_batch=True,
        )
        self.assertEqual("IRONCLAD", selection["character"])
        self.assertEqual(
            "fixed_six_run_character_quota", selection["reason"]
        )

    def test_p0_only_batch_allows_four_six_run_subcohorts(self):
        records = []
        expected = (
            campaign_selector.FIXED_QUOTA_SEQUENCE
            * campaign_selector.P0_ONLY_SUBCOHORTS
        )
        for index, character in enumerate(expected):
            selection = campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(index),
                controller_hash="controller-a",
                p0_only_batch=True,
            )
            self.assertEqual(character, selection["character"])
            self.assertEqual(
                index // campaign_selector.P0_ONLY_SUBCOHORT_SIZE,
                selection["p0_only_subcohort_index"],
            )
            self.assertEqual(
                index % campaign_selector.P0_ONLY_SUBCOHORT_SIZE,
                selection["p0_only_subcohort_position"],
            )
            terminal = result(
                character,
                index,
                selection=selection,
                selection_id=selection["selection_id"],
            )
            issue_audit = audit(
                terminal,
                audit_status="issues",
                release_gate_passed=False,
                issue_count=1,
                issues=[{"severity": "P1", "kind": "strategy"}],
            )
            issue_exit = controller_exit(
                terminal,
                controller_exit_status="issues",
                controller_exit_audit={
                    "audit_status": "issues",
                    "release_gate_passed": False,
                    "issues": [{"severity": "P1", "kind": "strategy"}],
                },
            )
            records.extend((terminal, issue_audit, issue_exit))

        self.assertEqual(
            campaign_selector.P0_ONLY_MAX_ATTEMPTS,
            len(campaign_selector.cohort_quota_attempts(
                records,
                "decision-a",
                "controller-a",
                p0_only_batch=True,
            )),
        )
        with self.assertRaisesRegex(
            campaign_selector.HistoryValidationError,
            "p0-only 24-run batch is complete",
        ):
            campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(24),
                controller_hash="controller-a",
                p0_only_batch=True,
            )

    def test_p0_only_requires_explicit_zero_protocol_and_operational_counts(self):
        terminal = result("IRONCLAD", 299)
        issue_audit = audit(
            terminal,
            audit_status="issues",
            release_gate_passed=False,
            issue_count=1,
            issues=[{"severity": "P1", "kind": "strategy"}],
        )
        issue_exit = controller_exit(
            terminal,
            controller_exit_status="issues",
            controller_exit_audit={
                "audit_status": "issues",
                "release_gate_passed": False,
                "issues": [{"severity": "P1", "kind": "strategy"}],
            },
        )
        assessment = {
            "terminal": terminal,
            "audit": issue_audit,
            "controller_exit": issue_exit,
        }
        self.assertIsNone(
            campaign_selector.p0_only_batch_assessment_error(assessment)
        )
        for field in (
            "protocol_events",
            "operational_error_attempts",
            "operational_error_events",
        ):
            with self.subTest(field=field, value="missing"):
                missing = dict(issue_audit)
                missing.pop(field)
                self.assertEqual(
                    field,
                    campaign_selector.p0_only_batch_assessment_error({
                        **assessment,
                        "audit": missing,
                    }),
                )
            with self.subTest(field=field, value=1):
                nonzero = {**issue_audit, field: 1}
                self.assertEqual(
                    field,
                    campaign_selector.p0_only_batch_assessment_error({
                        **assessment,
                        "audit": nonzero,
                    }),
                )

    def test_p0_only_accepts_strategy_only_inconclusive_audit(self):
        terminal = result("THE_SILENT", 300)
        inconclusive_audit = audit(
            terminal,
            audit_status="inconclusive",
            release_gate_passed=False,
            issue_count=0,
            strategy_quality={"status": "inconclusive"},
        )
        nonclear_exit = controller_exit(
            terminal,
            controller_exit_status="issues",
            controller_exit_audit={
                "audit_status": "issues",
                "release_gate_passed": False,
                "issues": [{
                    "severity": "P1",
                    "kind": "controller_exit_failure",
                }],
            },
        )
        assessment = {
            "terminal": terminal,
            "audit": inconclusive_audit,
            "controller_exit": nonclear_exit,
        }

        self.assertIsNone(
            campaign_selector.p0_only_batch_assessment_error(assessment)
        )

        for dimension in ("protocol_correctness", "mechanics_coverage"):
            with self.subTest(dimension=dimension):
                blocked_audit = dict(inconclusive_audit)
                blocked_audit[dimension] = {"status": "inconclusive"}
                self.assertEqual(
                    f"audit_{dimension}",
                    campaign_selector.p0_only_batch_assessment_error({
                        **assessment,
                        "audit": blocked_audit,
                    }),
                )

    def test_fixed_six_run_quota_rejects_p0_and_out_of_order_history(self):
        selection = campaign_selector.select_character(
            [], "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        terminal = result(
            "IRONCLAD", 300, selection=selection,
            selection_id=selection["selection_id"],
        )
        p0_audit = audit(terminal)
        p0_audit["issues"] = [{"severity": "P0", "kind": "runtime"}]
        records = [terminal, p0_audit, controller_exit(terminal)]
        self.assertEqual(
            [],
            campaign_selector.cohort_quota_attempts(
                records, "decision-a", "controller-a"
            ),
        )

        forged_selection = campaign_selector.select_character(
            [], "decision-a", random.Random(2),
            controller_hash="controller-a",
        )
        forged_selection["character"] = "THE_SILENT"
        forged_terminal = result(
            "THE_SILENT", 301, selection=forged_selection,
            selection_id=forged_selection["selection_id"],
        )
        with self.assertRaisesRegex(
            campaign_selector.HistoryValidationError,
            "character history is not the exact",
        ):
            campaign_selector.cohort_quota_attempts(
                audited(forged_terminal),
                "decision-a", "controller-a",
            )

    def test_cohort_quota_evidence_and_chosen_character_are_fail_closed(self):
        selection = campaign_selector.select_character(
            [], "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        forged = json.loads(json.dumps(selection))
        forged["character"] = "DEFECT"
        with self.assertRaisesRegex(ValueError, "violates cohort quota"):
            campaign_selector.validate_selection(
                forged, "decision-a", "controller-a",
                eligible_attempt_ids=[],
            )

    def test_macro_profile_is_propagated_and_mixed_profile_fails_closed(self):
        first = result(
            "DEFECT", 1,
            macro_policy={"mode": "assist", "model": "deepseek-v4-flash"},
        )
        selection = campaign_selector.select_character(
            audited(first), "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        self.assertEqual("assist", selection["macro_policy"]["mode"])

        second = result(
            "THE_SILENT", 2,
            macro_policy={"mode": "shadow", "model": "deepseek-v4-flash"},
        )
        with self.assertRaises(ValueError):
            campaign_selector.select_character(
                audited(first, second), "decision-a", random.Random(1),
                controller_hash="controller-a",
            )

    def test_old_and_mixed_policy_records_are_excluded(self):
        terminals = [
            result("DEFECT", 2, schema_version=1),
            result("THE_SILENT", 1, decision_hash="old"),
            result("IRONCLAD", 1, goal_mode="UNLOCK"),
            result("IRONCLAD", 2, run_type="daily"),
            result("DEFECT", 1),
        ]
        eligible = campaign_selector.eligible_attempts(
            audited(*terminals), "decision-a"
        )
        self.assertEqual(["attempt-DEFECT-1"], [item["attempt_id"] for item in eligible])

    def test_bootstrap_includes_ironclad_and_balances_all_characters(self):
        records = []
        first = campaign_selector.select_character(
            records, "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        self.assertEqual("IRONCLAD", first["character"])
        records.extend(audited(*(result("IRONCLAD", index) for index in range(3))))
        second = campaign_selector.select_character(
            records, "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        self.assertEqual("THE_SILENT", second["character"])

    def test_bootstrap_threshold_can_be_raised_for_balanced_test_matrix(self):
        records = []
        records.extend(audited(*(result("IRONCLAD", index) for index in range(4))))
        records.extend(audited(*(result("THE_SILENT", 10 + index) for index in range(5))))
        records.extend(audited(*(result("DEFECT", 20 + index) for index in range(5))))

        selection = campaign_selector.select_character(
            records,
            "decision-a",
            random.Random(1),
            bootstrap_runs=5,
            controller_hash="controller-a",
        )

        self.assertEqual("IRONCLAD", selection["character"])
        self.assertEqual("bootstrap_equal_coverage", selection["reason"])
        self.assertEqual(5, selection["bootstrap_runs"])

    def test_starvation_guard_precedes_posterior_sampling(self):
        records = []
        for character in campaign_selector.CHARACTERS:
            records.extend(audited(*(result(character, index) for index in range(3))))
        records.extend(audited(*(result("DEFECT", 10 + index) for index in range(6))))

        selection = campaign_selector.select_character(
            records, "decision-a", random.Random(2),
            controller_hash="controller-a",
        )

        self.assertEqual("IRONCLAD", selection["character"])
        self.assertEqual("starvation_guard", selection["reason"])

    def test_thompson_uses_only_homogeneous_heart_outcome(self):
        records = []
        for character in campaign_selector.CHARACTERS:
            records.extend(audited(*(result(character, index) for index in range(3))))
        # Put all classes in the recent window so starvation does not trigger.
        records.extend(audited(*[
            result("IRONCLAD", 20, win=True),
            result("THE_SILENT", 20),
            result("DEFECT", 20),
            result("IRONCLAD", 21, win=True),
            result("THE_SILENT", 21),
            result("DEFECT", 21),
        ]))
        selection = campaign_selector.select_character(
            records, "decision-a", random.Random(4),
            controller_hash="controller-a",
        )
        self.assertEqual("beta_thompson_sample", selection["reason"])
        self.assertEqual(set(campaign_selector.CHARACTERS), set(selection["posterior_samples"]))

    def test_attempt_id_and_matching_selection_chain_are_required(self):
        valid = result("DEFECT", 1)
        missing_attempt = result("DEFECT", 2, attempt_id=None, run_id="DEFECT:0:2")
        missing_selection = result("DEFECT", 3, selection=None)
        wrong_character = result("DEFECT", 4)
        wrong_character["selection"] = {
            **wrong_character["selection"],
            "character": "IRONCLAD",
        }
        missing_seed = result("DEFECT", 5, seed=None)
        wrong_run_id = result("DEFECT", 6, run_id="DEFECT:0:other-seed")
        empty_seed = result("DEFECT", 7, seed="", run_id="DEFECT:0:")
        whitespace_seed = result("DEFECT", 8, seed="  ", run_id="DEFECT:0:  ")
        string_outcome = result("DEFECT", 9, heart_defeated="false")
        malformed_seeds = [
            result(
                "DEFECT",
                10 + index,
                seed=value,
                run_id=f"DEFECT:0:{value}",
            )
            for index, value in enumerate((True, False, 1.5, [], {}))
        ]

        eligible = campaign_selector.eligible_attempts(
            audited(*[
                missing_attempt,
                missing_selection,
                wrong_character,
                missing_seed,
                wrong_run_id,
                empty_seed,
                whitespace_seed,
                string_outcome,
                *malformed_seeds,
                valid,
            ]),
            "decision-a",
        )

        self.assertEqual([valid["attempt_id"]], [item["attempt_id"] for item in eligible])

    def test_numeric_zero_seed_remains_eligible(self):
        zero_seed = result("DEFECT", 0)
        eligible = campaign_selector.eligible_attempts(
            audited(zero_seed), "decision-a"
        )
        self.assertEqual([zero_seed["attempt_id"]], [item["attempt_id"] for item in eligible])

    def test_inconsistent_or_malformed_terminal_results_are_excluded(self):
        valid_loss = result("DEFECT", 50, win=False)
        malformed = [
            result("DEFECT", 51, win=True, victory=False),
            result("DEFECT", 52, win=False, victory=None),
            result("DEFECT", 53, win=False, victory="false"),
            result("DEFECT", 54, win=False, error="stale failure"),
        ]

        eligible = campaign_selector.eligible_attempts(
            audited(*malformed, valid_loss), "decision-a"
        )

        self.assertEqual(
            [valid_loss["attempt_id"]],
            [item["attempt_id"] for item in eligible],
        )

    def test_operational_error_taints_attempt_even_after_later_game_over(self):
        operational = result(
            "DEFECT",
            60,
            termination_kind="operational_error",
            error="TimeoutError",
        )
        completed = result("DEFECT", 60, win=True)

        self.assertEqual(
            [], campaign_selector.eligible_attempts(
                audited(operational), "decision-a"
            )
        )
        eligible = campaign_selector.eligible_attempts(
            audited(operational, completed), "decision-a"
        )
        self.assertEqual([], eligible)

    def test_later_operational_error_supersedes_earlier_game_over(self):
        completed = result("DEFECT", 61, win=True)
        operational = result(
            "DEFECT",
            61,
            termination_kind="operational_error",
            error="SafetyError",
        )

        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                audited(completed, operational), "decision-a"
            ),
        )

    def test_malformed_history_id_types_are_skipped_without_aborting(self):
        valid = result("DEFECT", 20)
        malformed_attempts = [
            result("DEFECT", 21 + index, attempt_id=value)
            for index, value in enumerate(([], {}, 7, "", "   "))
        ]
        malformed_selections = []
        for index, value in enumerate(([], {}, 8, "", "\t")):
            item = result("DEFECT", 30 + index)
            item["selection"] = {**item["selection"], "selection_id": value}
            malformed_selections.append(item)

        eligible = campaign_selector.eligible_attempts(
            [None, [], *audited(*malformed_attempts, *malformed_selections, valid)],
            "decision-a",
        )

        self.assertEqual([valid["attempt_id"]], [item["attempt_id"] for item in eligible])

    def test_ascension_level_requires_an_integer_zero(self):
        valid = result("DEFECT", 40)
        malformed = [
            result("DEFECT", 41 + index, ascension_level=value)
            for index, value in enumerate((False, "0", 0.0))
        ]
        nested = result("DEFECT", 50)
        nested["selection"] = {
            **nested["selection"],
            "ascension_level": False,
        }

        eligible = campaign_selector.eligible_attempts(
            audited(*malformed, nested, valid), "decision-a"
        )

        self.assertEqual([valid["attempt_id"]], [item["attempt_id"] for item in eligible])

    def test_terminal_requires_top_level_character_and_selection_id(self):
        missing_character = result("DEFECT", 70)
        missing_character.pop("character")
        missing_selection_id = result("DEFECT", 71)
        missing_selection_id.pop("selection_id")
        valid = result("DEFECT", 72)

        self.assertEqual(
            [valid["attempt_id"]],
            [
                item["attempt_id"]
                for item in campaign_selector.eligible_attempts(
                    audited(missing_character, missing_selection_id, valid),
                    "decision-a",
                )
            ],
        )

    def test_missing_or_nonclear_audit_breaks_continuous_cohort(self):
        first = result("IRONCLAD", 80)
        unaudited = result("THE_SILENT", 81)
        last = result("DEFECT", 82)
        records = [
            first, audit(first), controller_exit(first),
            unaudited,
            last, audit(last), controller_exit(last),
        ]

        self.assertEqual(
            [last["attempt_id"]],
            [
                item["attempt_id"]
                for item in campaign_selector.eligible_attempts(
                    records, "decision-a"
                )
            ],
        )

        bad = result("IRONCLAD", 83)
        records.extend((
            bad,
            audit(bad, audit_status="inconclusive"),
            controller_exit(bad),
        ))
        self.assertEqual(
            [], campaign_selector.eligible_attempts(records, "decision-a")
        )

    def test_audit_must_precede_the_next_terminal(self):
        first = result("IRONCLAD", 84)
        second = result("DEFECT", 85)
        records = [
            first,
            second,
            audit(first),
            controller_exit(first),
            audit(second),
            controller_exit(second),
        ]

        self.assertEqual(
            [second["attempt_id"]],
            [
                item["attempt_id"]
                for item in campaign_selector.eligible_attempts(
                    records, "decision-a"
                )
            ],
        )

    def test_duplicate_terminal_and_audit_binding_mismatch_are_rejected(self):
        duplicate = result("DEFECT", 90)
        duplicate_records = [
            duplicate,
            audit(duplicate),
            controller_exit(duplicate),
            dict(duplicate),
        ]
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                duplicate_records, "decision-a"
            ),
        )

        mismatched = result("DEFECT", 91)
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                [
                    mismatched,
                    audit(mismatched, controller_hash="wrong"),
                    controller_exit(mismatched),
                ],
                "decision-a",
            ),
        )
        typed = result("DEFECT", 1)
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                [
                    typed,
                    audit(typed, seed=True),
                    controller_exit(typed),
                ], "decision-a"
            ),
        )

        earlier = result("IRONCLAD", 92)
        later = result("DEFECT", 93)
        duplicate_audit = audit(earlier)
        records = audited(earlier, later) + [duplicate_audit]
        self.assertEqual(
            [], campaign_selector.eligible_attempts(records, "decision-a")
        )

    def test_malformed_terminal_result_is_a_cohort_barrier(self):
        valid = result("DEFECT", 94)
        malformed = {
            "record_type": "terminal_result",
            "attempt_id": "attempt-malformed",
        }
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                audited(valid) + [malformed], "decision-a"
            ),
        )

    def test_terminal_state_sequence_is_explicitly_bound(self):
        missing = result("DEFECT", 95)
        missing.pop("terminal_state_seq")
        mismatch = result("DEFECT", 96, terminal_state_seq=999999)
        valid = result("DEFECT", 97)

        eligible = campaign_selector.eligible_attempts(
            audited(missing, mismatch, valid), "decision-a"
        )

        self.assertEqual(
            [valid["attempt_id"]], [item["attempt_id"] for item in eligible]
        )

    def test_every_release_gate_dimension_is_required(self):
        mutations = (
            {"release_gate_passed": False},
            {"protocol_correctness": {"status": "issues"}},
            {"mechanics_coverage": {"status": "inconclusive"}},
            {"strategy_quality": {"status": "issues"}},
            {"issue_count": 1},
            {"review_finding_count": 1},
            {"oracle_disagreement_count": 1},
            {"eligible_unknown": 1},
        )
        for index, mutation in enumerate(mutations, start=100):
            with self.subTest(mutation=mutation):
                terminal = result("DEFECT", index)
                self.assertEqual(
                    [],
                    campaign_selector.eligible_attempts(
                        [
                            terminal,
                            audit(terminal, **mutation),
                            controller_exit(terminal),
                        ],
                        "decision-a",
                    ),
                )

    def test_controller_exit_is_required_unique_clean_and_exactly_bound(self):
        terminal = result("DEFECT", 110)
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                [terminal, audit(terminal)], "decision-a"
            ),
        )
        for mutation in (
            {"exit_code": 1, "controller_exit_status": "issues"},
            {"stderr_size": 1},
            {"decision_hash": "wrong"},
            {"terminal_state_seq": terminal["terminal_state_seq"] + 1},
            {"stdout_line_count": 2},
            {"stdout_semantic_sha256": None},
            {"freeze_manifest_sha256": None},
            {"freeze_source_digest": None},
            {"freeze_generated_at": None},
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    [],
                    campaign_selector.eligible_attempts(
                        [
                            terminal,
                            audit(terminal),
                            controller_exit(terminal, **mutation),
                        ],
                        "decision-a",
                    ),
                )
        duplicate = controller_exit(terminal)
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                [terminal, audit(terminal), duplicate, dict(duplicate)],
                "decision-a",
            ),
        )

    def test_controller_exit_must_land_before_the_next_terminal(self):
        first = result("IRONCLAD", 111)
        second = result("DEFECT", 112)
        records = [
            first,
            audit(first),
            second,
            controller_exit(first),
            audit(second),
            controller_exit(second),
        ]
        self.assertEqual(
            [second["attempt_id"]],
            [
                item["attempt_id"]
                for item in campaign_selector.eligible_attempts(
                    records, "decision-a"
                )
            ],
        )

    def test_selection_id_cannot_be_reused_across_attempts(self):
        first = result("IRONCLAD", 113)
        second = result("DEFECT", 114)
        reused = first["selection_id"]
        second["selection_id"] = reused
        second["selection"] = {
            **second["selection"],
            "selection_id": reused,
        }
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                audited(first, second), "decision-a"
            ),
        )

    def test_hash_change_resets_to_latest_continuous_cohort(self):
        old = result("IRONCLAD", 120, decision_hash="decision-a")
        other = result("THE_SILENT", 121, decision_hash="decision-b")
        current = result("DEFECT", 122, decision_hash="decision-a")
        eligible = campaign_selector.eligible_attempts(
            audited(old, other, current), "decision-a"
        )
        self.assertEqual(
            [current["attempt_id"]], [item["attempt_id"] for item in eligible]
        )

    def test_performance_cohort_survives_audit_and_controller_hash_changes(self):
        first = result(
            "IRONCLAD", 125,
            decision_hash="decision-a",
            controller_hash="controller-a",
            performance_hash="policy-a",
        )
        second = result(
            "DEFECT", 126,
            decision_hash="decision-b",
            controller_hash="controller-b",
            performance_hash="policy-a",
        )

        cohort = campaign_selector.eligible_performance_attempt_triples(
            audited(first, second), "policy-a"
        )

        self.assertEqual(
            [first["attempt_id"], second["attempt_id"]],
            [terminal["attempt_id"] for terminal, _, _ in cohort],
        )

        changed = result(
            "THE_SILENT", 127,
            decision_hash="decision-c",
            controller_hash="controller-c",
            performance_hash="policy-b",
        )
        self.assertEqual(
            [],
            campaign_selector.eligible_performance_attempt_triples(
                audited(first, second, changed), "policy-a"
            ),
        )

    def test_requested_controller_hash_is_a_strict_cohort_boundary(self):
        old = result("IRONCLAD", 123, controller_hash="controller-old")
        current = result("DEFECT", 124, controller_hash="controller-a")

        eligible = campaign_selector.eligible_attempts(
            audited(old, current),
            "decision-a",
            "controller-a",
        )

        self.assertEqual(
            [current["attempt_id"]], [item["attempt_id"] for item in eligible]
        )
        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                audited(current), "decision-a", "controller-other"
            ),
        )

    def test_same_pair_unresolved_attempt_kinds_block_new_selection(self):
        def cases():
            operational = result(
                "DEFECT", 200,
                termination_kind="operational_error",
                error="TimeoutError",
            )
            missing_audit = result("DEFECT", 201)
            nonclear = result("DEFECT", 202)
            duplicate = result("DEFECT", 203)
            out_of_order = result("DEFECT", 204)
            orphan_audit_terminal = result("DEFECT", 205)
            orphan_exit_terminal = result("DEFECT", 206)
            missing_hash = result("DEFECT", 207)
            missing_hash_audit = audit(missing_hash)
            missing_hash_audit.pop("controller_hash")
            return {
                "operational": audited(operational),
                "missing_audit": [
                    missing_audit, controller_exit(missing_audit),
                ],
                "inconclusive": [
                    nonclear,
                    audit(nonclear, audit_status="inconclusive"),
                    controller_exit(nonclear),
                ],
                "duplicate_terminal": audited(duplicate) + [dict(duplicate)],
                "bad_order": [
                    audit(out_of_order), out_of_order,
                    controller_exit(out_of_order),
                ],
                "orphan_audit": [audit(orphan_audit_terminal)],
                "orphan_exit": [controller_exit(orphan_exit_terminal)],
                "companion_hash_missing": [
                    missing_hash, missing_hash_audit,
                    controller_exit(missing_hash),
                ],
            }

        for kind, records in cases().items():
            with self.subTest(kind=kind):
                unresolved = campaign_selector.unresolved_attempt_assessments(
                    records, "decision-a", "controller-a"
                )
                self.assertTrue(unresolved)
                with self.assertRaises(
                    campaign_selector.HistoryValidationError
                ):
                    campaign_selector.select_character(
                        records,
                        "decision-a",
                        random.Random(1),
                        controller_hash="controller-a",
                    )

    def test_unresolved_gate_is_scoped_to_exact_decision_controller_pair(self):
        broken = result("DEFECT", 210)
        records = [broken]

        with self.assertRaises(campaign_selector.HistoryValidationError):
            campaign_selector.select_character(
                records,
                "decision-a",
                random.Random(1),
                controller_hash="controller-a",
            )

        new_hash = campaign_selector.select_character(
            records,
            "decision-b",
            random.Random(1),
            controller_hash="controller-a",
        )
        new_controller = campaign_selector.select_character(
            records,
            "decision-a",
            random.Random(1),
            controller_hash="controller-b",
        )
        self.assertEqual("decision-b", new_hash["decision_hash"])
        self.assertEqual("controller-b", new_controller["controller_hash"])

    def test_non_core_selection_tamper_is_not_eligible(self):
        terminal = result("DEFECT", 211)
        terminal["selection"]["reason"] = "tampered-after-acceptance"
        records = audited(terminal)

        self.assertEqual(
            [],
            campaign_selector.eligible_attempts(
                records, "decision-a", "controller-a"
            ),
        )
        unresolved = campaign_selector.unresolved_attempt_assessments(
            records, "decision-a", "controller-a"
        )
        self.assertEqual(
            ["selection_digest_binding"],
            [item["reason"] for item in unresolved if item["terminal"]],
        )

    def test_selection_validation_and_pending_write_fail_closed(self):
        terminal = result("DEFECT", 130)
        selection = campaign_selector.select_character(
            audited(terminal), "decision-a", random.Random(1),
            controller_hash="controller-a",
        )
        self.assertIs(
            selection,
            campaign_selector.validate_selection(
                selection,
                "decision-a",
                "controller-a",
                eligible_attempt_ids=[terminal["attempt_id"]],
            ),
        )
        with self.assertRaises(ValueError):
            campaign_selector.validate_selection(
                {**selection, "eligible_attempt_ids": []},
                "decision-a",
                "controller-a",
                eligible_attempt_ids=[terminal["attempt_id"]],
            )
        with self.assertRaises(ValueError):
            campaign_selector.validate_selection(
                {**selection, "controller_hash": "controller-other"},
                "decision-a",
                "controller-a",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "next-run-selection.json"
            campaign_selector.write_selection(path, selection)
            self.assertEqual(
                selection, json.loads(path.read_text(encoding="utf-8"))
            )
            with self.assertRaises(FileExistsError):
                campaign_selector.write_selection(path, selection)

    def test_malformed_json_history_fails_closed_with_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text('{}\n{"broken"\n', encoding="utf-8")
            with self.assertRaisesRegex(
                campaign_selector.HistoryValidationError, "line 2"
            ):
                campaign_selector.load_history(path)

    def test_non_object_history_record_fails_closed_with_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text('{}\n[]\n', encoding="utf-8")
            with self.assertRaisesRegex(
                campaign_selector.HistoryValidationError, "line 2"
            ):
                campaign_selector.load_history(path)

    def test_unchanged_history_reuses_exact_digest_parse_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text('{"value":1}\n', encoding="utf-8")
            campaign_selector._HISTORY_PARSE_CACHE.clear()
            original = json.loads
            with patch.object(
                campaign_selector.json,
                "loads",
                wraps=original,
            ) as parse:
                first = campaign_selector.load_history(path)
                second = campaign_selector.load_history(path)
            self.assertEqual(first, second)
            self.assertIsNot(first, second)
            self.assertEqual(1, parse.call_count)

    def test_history_cache_detects_same_size_same_mtime_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text('{"value":1}\n', encoding="utf-8")
            campaign_selector._HISTORY_PARSE_CACHE.clear()
            first = campaign_selector.load_history(path)
            timestamp = path.stat().st_mtime_ns
            path.write_text('{"value":2}\n', encoding="utf-8")
            path.touch()
            import os
            os.utime(path, ns=(timestamp, timestamp))
            second = campaign_selector.load_history(path)
            self.assertEqual([{"value": 1}], first)
            self.assertEqual([{"value": 2}], second)


if __name__ == "__main__":
    unittest.main()
