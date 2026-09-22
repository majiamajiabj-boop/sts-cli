import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import campaign_selector
import decision_case_replay
import freeze_manifest
from tests.test_freeze_manifest import (
    bind_bridge_runtime_sources,
    install_replay_bundle,
)


def verification():
    return {
        "full_tests": {
            "returncode": 0, "test_count": 1,
            "stdout_size": 0, "stderr_size": 0,
            "stdout_sha256": "a" * 64, "stderr_sha256": "b" * 64,
        },
        "git_diff_check": {
            "returncode": 0, "test_count": None,
            "stdout_size": 0, "stderr_size": 0,
            "stdout_sha256": "c" * 64, "stderr_sha256": "d" * 64,
        },
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": "e" * 64,
        "runtime_artifacts_after_sha256": "e" * 64,
    }


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


def replay_case(phase, decision_hash="decision-a"):
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
            "score": 1.0, "consequences": structured_consequence(0),
        },
        {
            "choice_id": "choice:b", "choice_index": 3,
            "action": "choose", "semantic_id": "gain:b",
            "score": 2.0, "consequences": structured_consequence(10),
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


def replay_evidence(decision_hash="decision-a"):
    fixtures = [
        replay_case(phase, decision_hash)
        for phase in decision_case_replay.REQUIRED_FIXTURE_PHASES
    ]
    return decision_case_replay.audit_cases(
        [replay_case("MAP", decision_hash)],
        decision_hash,
        fixture_cases=fixtures,
    )


def create_manifest(root):
    (root / "bridge.py").write_text("BRIDGE = 2\n", encoding="utf-8")
    (root / "launch_game.py").write_text(
        "LAUNCHER = 2\n", encoding="utf-8"
    )
    jar = b"communication-mod"
    (root / "CommunicationMod.jar").write_bytes(jar)
    game = root / "runtime-game"
    (game / "mods").mkdir(parents=True, exist_ok=True)
    (game / "jre" / "bin").mkdir(parents=True, exist_ok=True)
    (game / "mods" / "CommunicationMod.jar").write_bytes(jar)
    stderr = game / "communication_mod_errors.log"
    stderr.write_bytes(b"old-prefix")
    launch_log = root / "logs" / "launches" / "launch-test.log"
    launch_log.parent.mkdir(parents=True, exist_ok=True)
    launch_log.write_bytes(b"")
    jar_hash = hashlib.sha256(jar).hexdigest()
    launch = {
        "schema_version": 2,
        "launch_id": "launch-test",
        "started_at": 10.0,
        "launch_log_path": "logs/launches/launch-test.log",
        "launch_start_record_path": (
            "logs/launches/launch-test.start.json"
        ),
        "launcher_pid": 110,
        "java_pid": 111,
        "command": [
            str((game / "jre" / "bin" / "java.exe").resolve()),
            "-jar", str((game / "ModTheSpire.jar").resolve()),
            "--skip-intro", "--mods", "basemod,CommunicationMod",
        ],
        "game_dir": str(game.resolve()),
        "root_communication_mod_jar_sha256": jar_hash,
        "installed_communication_mod_jar_sha256": jar_hash,
        "bridge_stderr_path": str(stderr.resolve()),
        "bridge_stderr_start_offset": len(b"old-prefix"),
        "bridge_stderr_start_sha256": hashlib.sha256(
            b"old-prefix"
        ).hexdigest(),
        "restart_predecessor_record_path": None,
        "restart_predecessor_record_sha256": None,
    }
    (root / launch["launch_start_record_path"]).write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "launch-latest.json").write_text(
        json.dumps(launch), encoding="utf-8"
    )
    (root / "bridge-instance.json").write_text(json.dumps({
        "schema_version": 2,
        "protocol_version": 2,
        "instance_token": "bridge-test",
        "bridge_pid": 222,
        "parent_java_pid": 111,
        "launch_id": "launch-test",
        "bridge_sha256": hashlib.sha256(
            (root / "bridge.py").read_bytes()
        ).hexdigest(),
        "started_at": 11.0,
    }), encoding="utf-8")
    (root / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    replay_report = install_replay_bundle(root)
    bind_bridge_runtime_sources(root)
    with patch.object(
        freeze_manifest,
        "_fresh_policy_hashes",
        return_value={
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
        },
    ):
        manifest = freeze_manifest.build_manifest(
            root, "decision-a", "controller-a",
            verification=verification(),
            decision_case_replay=replay_report,
        )
    path = root / "freeze-manifest.json"
    freeze_manifest.write_manifest(path, manifest)
    return path


class CampaignLaunchGateTests(unittest.TestCase):
    def test_selection_creation_requires_current_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "freeze manifest is missing"):
                campaign_selector.validate_launch_prerequisites(
                    [], "decision-a", "controller-a", root,
                    freeze_manifest_path=root / "freeze-manifest.json",
                    cohort_review_path=root / "cohort-review.json",
                )
            manifest_path = create_manifest(root)
            result = campaign_selector.validate_launch_prerequisites(
                [], "decision-a", "controller-a", root,
                freeze_manifest_path=manifest_path,
                cohort_review_path=root / "cohort-review.json",
            )
            self.assertIsNone(result["review"])
            (root / "policy.py").write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source changed"):
                campaign_selector.validate_launch_prerequisites(
                    [], "decision-a", "controller-a", root,
                    freeze_manifest_path=manifest_path,
                    cohort_review_path=root / "cohort-review.json",
                )

    def test_six_run_gate_blocks_before_selection_is_created(self):
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": [
                {"attempt_id": f"attempt-{index}",
                 "hidden_entry": False, "heart_defeated": False}
                for index in range(6)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_manifest(root)
            with patch(
                "cohort_report.build_cohort_report", return_value=cohort
            ):
                with self.assertRaisesRegex(ValueError, "review is missing"):
                    campaign_selector.validate_launch_prerequisites(
                        [], "decision-a", "controller-a", root,
                        freeze_manifest_path=manifest_path,
                        cohort_review_path=root / "cohort-review.json",
                    )

    def test_exact_clear_review_allows_next_batch(self):
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": [
                {"attempt_id": f"attempt-{index}",
                 "hidden_entry": False, "heart_defeated": False}
                for index in range(6)
            ],
        }
        review_path = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_manifest(root)
            review_path = root / "cohort-review.json"
            review_path.write_text(json.dumps({"review_status": "clear"}), encoding="utf-8")
            with patch(
                "cohort_report.build_cohort_report", return_value=cohort
            ), patch(
                "cohort_review.validate_review_gate", return_value=True
            ) as validate:
                result = campaign_selector.validate_launch_prerequisites(
                    [], "decision-a", "controller-a", root,
                    freeze_manifest_path=manifest_path,
                    cohort_review_path=review_path,
                )
            self.assertEqual("clear", result["review"]["review_status"])
            validate.assert_called_once()

    def test_twelve_run_deep_review_blocks_thirteenth_selection(self):
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": [
                {"attempt_id": f"attempt-{index}",
                 "hidden_entry": False, "heart_defeated": False}
                for index in range(12)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_manifest(root)
            with patch(
                "cohort_report.build_cohort_report", return_value=cohort
            ):
                with self.assertRaisesRegex(
                    ValueError, "twelve_without_hidden_deep.*missing"
                ):
                    campaign_selector.validate_launch_prerequisites(
                        [], "decision-a", "controller-a", root,
                        freeze_manifest_path=manifest_path,
                        cohort_review_path=root / "cohort-review.json",
                    )

    def test_heart_victory_requires_final_review_before_any_selection(self):
        cohort = {
            "schema_version": 2,
            "decision_hash": "decision-a",
            "controller_hash": "controller-a",
            "attempts": [{
                "attempt_id": "heart-attempt",
                "hidden_entry": True,
                "victory": True,
                "heart_defeated": True,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_manifest(root)
            with patch(
                "cohort_report.build_cohort_report", return_value=cohort
            ):
                with self.assertRaisesRegex(
                    ValueError, "heart_victory_final.*missing"
                ):
                    campaign_selector.validate_launch_prerequisites(
                        [], "decision-a", "controller-a", root,
                        freeze_manifest_path=manifest_path,
                        cohort_review_path=root / "cohort-review.json",
                    )


if __name__ == "__main__":
    unittest.main()
