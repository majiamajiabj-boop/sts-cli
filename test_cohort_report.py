import unittest

import cohort_report
import campaign_selector


def terminal(character, index, **overrides):
    selection_id = f"selection-{index}"
    decision_hash = overrides.get("decision_hash", "decision-a")
    controller_hash = overrides.get("controller_hash", "controller-a")
    value = {
        "record_type": "terminal_result",
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "attempt_id": f"attempt-{index}",
        "run_id": f"{character}:0:{index}",
        "seed": index,
        "character": character,
        "class": character,
        "ascension_level": 0,
        "run_type": "standard",
        "goal_mode": "HEART",
        "decision_hash": decision_hash,
        "controller_hash": controller_hash,
        "selection_id": selection_id,
        "state_seq": 1000 + index,
        "terminal_state_seq": 1000 + index,
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "victory": False,
        "heart_defeated": False,
        "act": 3,
        "floor": 40,
        "observed_max_act": 3,
        "keys": {"ruby": True, "emerald": True, "sapphire": True},
        "actions": 300 + index,
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
        value["selection_digest"] = campaign_selector.selection_digest(
            value["selection"]
        )
    return value


def run_audit(result, **overrides):
    value = {
        "record_type": "run_audit",
        "schema_version": 2,
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
        "termination_kind": "game_over",
        "audit_status": "clear",
        "release_gate_passed": True,
        "protocol_correctness": {"status": "clear"},
        "mechanics_coverage": {"status": "clear"},
        "strategy_quality": {"status": "clear"},
        "issue_count": 0,
        "review_finding_count": 0,
        "oracle_disagreement_count": 0,
        "eligible_unknown": 0,
        "model_conflict_count": index_or_zero(result),
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
        "generated_at": 3000.0 + result["state_seq"],
        "code_changed": False,
    }
    if "performance_hash" in result:
        value["performance_hash"] = result.get("performance_hash")
    value.update(overrides)
    return value


def controller_exit(result, **overrides):
    empty_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    value = {
        "record_type": "controller_exit",
        "schema_version": 2,
        "policy_version": result["policy_version"],
        "attempt_id": result["attempt_id"],
        "run_id": result["run_id"],
        "seed": result["seed"],
        "character": result["character"],
        "ascension_level": result["ascension_level"],
        "run_type": result["run_type"],
        "decision_hash": result["decision_hash"],
        "controller_hash": result["controller_hash"],
        "selection_id": result["selection_id"],
        "selection_digest": result["selection_digest"],
        "terminal_state_seq": result["terminal_state_seq"],
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


def index_or_zero(result):
    seed = result.get("seed")
    return seed if type(seed) is int and seed >= 0 else 0


def history(*attempts):
    records = []
    for result in attempts:
        records.extend((
            result,
            run_audit(result),
            controller_exit(result),
        ))
    return records


class CohortReportTests(unittest.TestCase):
    def test_report_contains_exact_bound_attempt_fields(self):
        first = terminal("IRONCLAD", 1)
        hidden = terminal(
            "DEFECT", 2,
            act=3,
            observed_max_act=4,
            victory=True,
            heart_defeated=True,
        )

        report = cohort_report.build_cohort_report(
            history(first, hidden), "decision-a", generated_at=9999.0
        )

        self.assertEqual(2, report["schema_version"])
        self.assertEqual("fast-policy-v5", report["policy_version"])
        self.assertEqual("decision-a", report["decision_hash"])
        self.assertEqual("controller-a", report["controller_hash"])
        self.assertEqual(101.0, report["cohort_started_at"])
        self.assertEqual(2, report["cohort_attempt_count"])
        self.assertEqual(["attempt-1", "attempt-2"], report["valid_attempt_ids"])
        self.assertEqual(1, report["hidden_entry_count"])
        self.assertFalse(report["attempts"][0]["hidden_entry"])
        self.assertTrue(report["attempts"][1]["hidden_entry"])
        self.assertEqual(1002, report["attempts"][1]["terminal_state_seq"])
        self.assertEqual("selection-2", report["attempts"][1]["selection_id"])
        self.assertEqual(2, report["attempts"][1]["model_conflict_count"])
        self.assertFalse(report["attempts"][1]["code_changed"])

    def test_report_keeps_performance_cohort_across_decision_hash_changes(self):
        first = terminal(
            "IRONCLAD", 3,
            decision_hash="decision-old",
            controller_hash="controller-old",
            performance_hash="policy-stable",
        )
        current = terminal(
            "DEFECT", 4,
            decision_hash="decision-new",
            controller_hash="controller-new",
            performance_hash="policy-stable",
            observed_max_act=4,
        )

        report = cohort_report.build_cohort_report(
            history(first, current),
            "decision-new",
            performance_hash="policy-stable",
        )

        self.assertEqual([current["attempt_id"]], report["valid_attempt_ids"])
        self.assertEqual(
            [first["attempt_id"], current["attempt_id"]],
            report["performance_valid_attempt_ids"],
        )
        self.assertEqual(2, report["performance_cohort_attempt_count"])
        self.assertEqual(1, report["performance_hidden_entry_count"])

    def test_invalid_attempt_resets_the_continuous_cohort(self):
        first = terminal("IRONCLAD", 10)
        invalid = terminal("THE_SILENT", 11)
        last = terminal("DEFECT", 12)
        records = [
            first, run_audit(first), controller_exit(first),
            invalid, run_audit(invalid, eligible_unknown=1), controller_exit(invalid),
            last, run_audit(last), controller_exit(last),
        ]

        report = cohort_report.build_cohort_report(records, "decision-a")

        self.assertEqual([last["attempt_id"]], report["valid_attempt_ids"])
        self.assertEqual(1, report["cohort_attempt_count"])

    def test_operational_error_and_duplicate_terminal_reset_cohort(self):
        first = terminal("IRONCLAD", 20)
        operational = terminal(
            "THE_SILENT", 21,
            termination_kind="operational_error",
            error="TimeoutError",
        )
        last = terminal("DEFECT", 22)
        records = [
            first, run_audit(first), controller_exit(first),
            operational, run_audit(operational), controller_exit(operational),
            last, run_audit(last), controller_exit(last),
        ]
        report = cohort_report.build_cohort_report(records, "decision-a")
        self.assertEqual([last["attempt_id"]], report["valid_attempt_ids"])

        records.extend((dict(last),))
        report = cohort_report.build_cohort_report(records, "decision-a")
        self.assertEqual([], report["valid_attempt_ids"])

    def test_controller_hash_change_starts_a_new_cohort(self):
        first = terminal("IRONCLAD", 30)
        current = terminal("DEFECT", 31, controller_hash="controller-b")
        report = cohort_report.build_cohort_report(
            history(first, current), "decision-a"
        )
        self.assertEqual([current["attempt_id"]], report["valid_attempt_ids"])
        self.assertEqual("controller-b", report["controller_hash"])

    def test_missing_or_failed_controller_exit_resets_cohort(self):
        first = terminal("IRONCLAD", 32)
        missing = terminal("THE_SILENT", 33)
        failed = terminal("DEFECT", 34)
        last = terminal("IRONCLAD", 35)
        records = [
            first, run_audit(first), controller_exit(first),
            missing, run_audit(missing),
            failed, run_audit(failed), controller_exit(
                failed,
                controller_exit_status="issues",
                exit_code=1,
            ),
            last, run_audit(last), controller_exit(last),
        ]
        report = cohort_report.build_cohort_report(records, "decision-a")
        self.assertEqual([last["attempt_id"]], report["valid_attempt_ids"])
        self.assertTrue(report["attempts"][0]["controller_exit_clean"])

    def test_six_valid_attempts_complete_one_batch(self):
        attempts = [
            terminal(
                ("IRONCLAD", "THE_SILENT", "DEFECT")[index % 3],
                40 + index,
                observed_max_act=4 if index in {1, 4} else 3,
            )
            for index in range(6)
        ]
        report = cohort_report.build_cohort_report(
            history(*attempts), "decision-a"
        )
        self.assertEqual(6, report["cohort_attempt_count"])
        self.assertEqual(1, report["completed_batches"])
        self.assertEqual(6, report["current_batch_count"])
        self.assertEqual(2, report["hidden_entry_count"])

    def test_empty_or_wrong_hash_history_produces_empty_report(self):
        old = terminal("IRONCLAD", 60, decision_hash="old")
        report = cohort_report.build_cohort_report(history(old), "decision-a")
        self.assertEqual(0, report["cohort_attempt_count"])
        self.assertIsNone(report["controller_hash"])
        self.assertIsNone(report["cohort_started_at"])


if __name__ == "__main__":
    unittest.main()
