import os
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import decision_case_replay
import decision_case_resolution
import decision_case_corpus
import freeze_manifest
from tests.test_decision_case_replay import case as canonical_case
from tests.test_decision_case_resolution import (
    TARGET_HASH as RESOLUTION_TARGET_HASH,
    audit_bundle,
    build_bundle,
)


def bind_bridge_runtime_sources(root):
    """Make a synthetic bridge instance attest the exact current sources."""

    root = Path(root)
    path = root / "bridge-instance.json"
    instance = json.loads(path.read_text(encoding="utf-8"))
    sources = freeze_manifest.source_snapshot(root)
    instance["runtime_source_digest"] = freeze_manifest.snapshot_digest(
        sources
    )
    instance["runtime_source_file_count"] = len(sources)
    path.write_text(
        json.dumps(instance, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return instance


def verification(*, tests=4, test_code=0, diff_code=0):
    return {
        "full_tests": {
            "returncode": test_code,
            "test_count": tests,
            "stdout_size": 0,
            "stderr_size": 0,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        },
        "git_diff_check": {
            "returncode": diff_code,
            "test_count": None,
            "stdout_size": 0,
            "stderr_size": 0,
            "stdout_sha256": "c" * 64,
            "stderr_sha256": "d" * 64,
        },
        "runtime_artifacts_unchanged": True,
        "runtime_artifacts_before_sha256": "e" * 64,
        "runtime_artifacts_after_sha256": "e" * 64,
    }


def replay(
    *, status="clear", issues=0, unknown=0, decision_hash="decision-a"
):
    report = decision_case_replay.audit_cases(
        source_cases(decision_hash),
        decision_hash,
        fixture_cases=replay_fixtures(),
    )
    report.update({
        "generated_at": 1.0,
        "status": status,
        "release_gate_passed": status == "clear",
        "issue_count": issues,
        "eligible_unknown_count": unknown,
    })
    return report


def source_cases(decision_hash="decision-a"):
    current = canonical_case("EVENT", decision_hash=decision_hash)
    current.update({"attempt_id": "current-case", "before_seq": 10})
    historical = canonical_case("MAP", decision_hash="legacy-source-hash")
    historical.update({"attempt_id": "historical-case", "before_seq": 20})
    return [current, historical]


def replay_fixtures():
    return [
        canonical_case(phase)
        for phase in decision_case_replay.REQUIRED_FIXTURE_PHASES
    ]


def write_jsonl(path, rows):
    Path(path).write_text(
        "".join(
            json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def install_replay_bundle(root, decision_hash="decision-a"):
    root = Path(root)
    evidence = replay(decision_hash=decision_hash)
    write_jsonl(root / "decision-cases.jsonl", source_cases(decision_hash))
    manifest = decision_case_corpus.build_manifest(
        root / "decision-cases.jsonl", []
    )
    decision_case_corpus.write_manifest(
        root / decision_case_corpus.DEFAULT_MANIFEST_NAME, manifest
    )
    fixtures = root / "test_fixtures"
    fixtures.mkdir(exist_ok=True)
    write_jsonl(fixtures / "decision-cases-v2.jsonl", replay_fixtures())
    (root / "decision-case-replay.json").write_text(
        json.dumps(evidence, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return evidence


class FreezeManifestTests(unittest.TestCase):
    def setUp(self):
        self.policy_hash_patch = patch.object(
            freeze_manifest,
            "_fresh_policy_hashes",
            return_value={
                "decision_hash": "decision-a",
                "controller_hash": "controller-a",
            },
        )
        self.policy_hash_patch.start()

    def tearDown(self):
        self.policy_hash_patch.stop()

    def make_root(self, directory):
        root = Path(directory)
        (root / "autoplay.py").write_text("POLICY = 'v5'\n", encoding="utf-8")
        (root / "bridge.py").write_text("BRIDGE = 2\n", encoding="utf-8")
        (root / "launch_game.py").write_text(
            "LAUNCHER = 2\n", encoding="utf-8"
        )
        jar = b"communication-mod"
        (root / "CommunicationMod.jar").write_bytes(jar)
        game = root / "runtime-game"
        (game / "mods").mkdir(parents=True)
        (game / "jre" / "bin").mkdir(parents=True)
        (game / "mods" / "CommunicationMod.jar").write_bytes(jar)
        stderr = game / "communication_mod_errors.log"
        stderr.write_bytes(b"old-prefix")
        launch_log = root / "logs" / "launches" / "launch-test.log"
        launch_log.parent.mkdir(parents=True)
        launch_log.write_bytes(b"")
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
                "-jar",
                str((game / "ModTheSpire.jar").resolve()),
                "--skip-intro",
                "--mods",
                "basemod,CommunicationMod",
            ],
            "game_dir": str(game.resolve()),
            "root_communication_mod_jar_sha256": hashlib.sha256(jar).hexdigest(),
            "installed_communication_mod_jar_sha256": hashlib.sha256(jar).hexdigest(),
            "bridge_stderr_path": str(stderr.resolve()),
            "bridge_stderr_start_offset": len(b"old-prefix"),
            "bridge_stderr_start_sha256": hashlib.sha256(b"old-prefix").hexdigest(),
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
        (root / "run-result.json").write_text("{}", encoding="utf-8")
        (root / "logs" / "autoplay.log").write_text("dynamic", encoding="utf-8")
        install_replay_bundle(root)
        bind_bridge_runtime_sources(root)
        return root

    def make_resolved_root(self, directory):
        root = self.make_root(directory)
        value, trace, evidence, catalog, document = build_bundle(root)
        report = audit_bundle(value, trace, evidence, catalog, document)
        report["generated_at"] = 2.0
        write_jsonl(root / "decision-cases.jsonl", [value])
        manifest = decision_case_corpus.build_manifest(
            root / "decision-cases.jsonl", []
        )
        decision_case_corpus.write_manifest(
            root / decision_case_corpus.DEFAULT_MANIFEST_NAME, manifest
        )
        write_jsonl(
            root / "test_fixtures" / "decision-cases-v2.jsonl",
            replay_fixtures(),
        )
        artifacts = {
            "decision-case-resolutions.json": document,
            "decision-case-trace-evidence.json": evidence,
            (
                "test_fixtures/"
                "decision-case-resolution-invariants-v1.json"
            ): catalog,
            "decision-case-replay.json": report,
        }
        for relative, value_to_write in artifacts.items():
            (root / relative).write_text(
                json.dumps(
                    value_to_write,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        bind_bridge_runtime_sources(root)
        return root, report, document, evidence

    def build_resolved_manifest(self, root, report):
        with patch.object(
            freeze_manifest,
            "_fresh_policy_hashes",
            return_value={
                "decision_hash": RESOLUTION_TARGET_HASH,
                "controller_hash": "controller-a",
            },
        ):
            return freeze_manifest.build_manifest(
                root,
                RESOLUTION_TARGET_HASH,
                "controller-a",
                verification=verification(),
                decision_case_replay=report,
            )

    def test_manifest_binds_exact_source_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
                generated_at=1.0,
            )
            self.assertIs(
                manifest,
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                ),
            )
            self.assertEqual("launch-test", manifest[
                "launch_evidence"
            ]["launch_id"])
            self.assertEqual(
                "logs/launches/launch-test.start.json",
                manifest["launch_evidence"][
                    "launch_start_record_path"
                ],
            )
            self.assertEqual(
                manifest["launch_evidence"]["launch_record_sha256"],
                manifest["launch_evidence"][
                    "launch_start_record_sha256"
                ],
            )
            self.assertEqual(0, manifest[
                "launch_evidence"
            ]["bridge_stderr_delta_size"])
            self.assertEqual(
                hashlib.sha256(b"").hexdigest(),
                manifest["launch_evidence"][
                    "bridge_stderr_delta_sha256"
                ],
            )
        self.assertEqual(7, manifest["source_file_count"])
        self.assertIn("decision-case-replay.json", manifest["sources"])
        self.assertNotIn("run-result.json", manifest["sources"])

    def test_static_preflight_finalizes_without_rerunning_expensive_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            preflight = freeze_manifest.build_static_preflight(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
                generated_at=0.5,
            )
            freeze_manifest.write_manifest(
                root / "release-preflight.json", preflight
            )
            with patch.object(
                freeze_manifest,
                "run_verification",
                side_effect=AssertionError("full tests reran"),
            ), patch.object(
                freeze_manifest,
                "_live_revalidate_replay",
                side_effect=AssertionError("DecisionCase replay reran"),
            ):
                manifest = freeze_manifest.build_manifest(
                    root,
                    "decision-a",
                    "controller-a",
                    static_preflight=preflight,
                    generated_at=1.0,
                )
            self.assertTrue(manifest["release_gate_passed"])
            self.assertEqual(
                preflight["source_digest"], manifest["source_digest"]
            )
            self.assertNotIn(
                "release-preflight.json", manifest["sources"]
            )

    def test_static_preflight_does_not_require_a_live_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with patch.object(
                freeze_manifest,
                "capture_launch_evidence",
                side_effect=AssertionError("runtime was inspected"),
            ):
                preflight = freeze_manifest.build_static_preflight(
                    root,
                    "decision-a",
                    "controller-a",
                    verification=verification(),
                    decision_case_replay=replay(),
                )
            self.assertTrue(preflight["preflight_passed"])

    def test_static_preflight_runs_full_suite_and_replay_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            rendezvous = threading.Barrier(2)

            def verify_in_parallel(observed_root):
                self.assertEqual(root, observed_root)
                rendezvous.wait(timeout=2.0)
                return verification()

            def replay_in_parallel(observed_root, report, decision_hash):
                self.assertEqual(root, observed_root)
                self.assertEqual(replay(), report)
                self.assertEqual("decision-a", decision_hash)
                rendezvous.wait(timeout=2.0)
                return {"parallel": True}

            with patch.object(
                freeze_manifest,
                "run_verification",
                side_effect=verify_in_parallel,
            ), patch.object(
                freeze_manifest,
                "_live_revalidate_replay",
                side_effect=replay_in_parallel,
            ):
                preflight = freeze_manifest.build_static_preflight(
                    root,
                    "decision-a",
                    "controller-a",
                    decision_case_replay=replay(),
                )
            self.assertEqual(
                {"parallel": True},
                preflight["decision_case_replay_revalidation"],
            )

    def test_static_preflight_is_invalidated_by_any_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            preflight = freeze_manifest.build_static_preflight(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            (root / "new_policy_surface.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "source changed after the static preflight",
            ):
                freeze_manifest.validate_static_preflight(
                    preflight, root, "decision-a", "controller-a"
                )

    def test_static_preflight_cannot_mix_cached_and_new_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            preflight = freeze_manifest.build_static_preflight(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "cannot be mixed",
            ):
                freeze_manifest.build_manifest(
                    root,
                    "decision-a",
                    "controller-a",
                    static_preflight=preflight,
                    verification=verification(),
                )



    def test_identical_manifest_bytes_cache_only_static_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            path = root / "freeze-manifest.json"
            freeze_manifest.write_manifest(path, manifest)
            freeze_manifest._VALIDATED_MANIFEST_CACHE.clear()
            original = freeze_manifest.validate_manifest
            with patch.object(
                freeze_manifest,
                "validate_manifest",
                wraps=original,
            ) as full_validation:
                first = freeze_manifest.load_validated_manifest(
                    path, root, "decision-a", "controller-a"
                )
                second = freeze_manifest.load_validated_manifest(
                    path, root, "decision-a", "controller-a"
                )
            self.assertIs(first, second)
            self.assertEqual(1, full_validation.call_count)

    def test_cached_manifest_still_reproves_source_and_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            path = root / "freeze-manifest.json"
            freeze_manifest.write_manifest(path, manifest)
            freeze_manifest._VALIDATED_MANIFEST_CACHE.clear()
            freeze_manifest.load_validated_manifest(
                path, root, "decision-a", "controller-a"
            )
            (root / "bridge.py").write_text(
                "BRIDGE = 3\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError, "source changed"
            ):
                freeze_manifest.load_validated_manifest(
                    path, root, "decision-a", "controller-a"
                )

            (root / "bridge.py").write_text(
                "BRIDGE = 2\n", encoding="utf-8"
            )
            raw = path.read_text(encoding="utf-8")
            path.write_text(
                raw.replace('"decision-a"', '"decision-b"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "decision_hash mismatch",
            ):
                freeze_manifest.load_validated_manifest(
                    path, root, "decision-a", "controller-a"
                )

    def test_resolved_report_is_live_recomputed_before_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root, report, _document, _evidence = self.make_resolved_root(
                directory
            )
            resolution = report["case_results"][0]["resolution"]
            self.assertNotEqual(
                resolution["original_classification"],
                resolution["invariant"]["negative_classification"],
            )
            self.assertNotEqual(
                resolution["original_problem_kinds"],
                resolution["invariant"]["negative_problem_kinds"],
            )
            manifest = self.build_resolved_manifest(root, report)
            self.assertTrue(manifest[
                "decision_case_replay_revalidation"
            ]["trace_prefix_verified"])
            self.assertEqual(
                1,
                manifest["decision_case_replay"]["resolved_case_count"],
            )

    def test_resolved_report_and_sidecar_tampering_fail_closed(self):
        def report_mutation(name, report):
            resolution = report["case_results"][0]["resolution"]
            invariant = resolution["invariant"]
            if name == "legacy_original_profile":
                problems = ["invented_legacy_problem"]
                resolution["original_problem_kinds"] = problems
                resolution["original"]["problem_kinds"] = problems
                resolution["original"]["unknowns"] = problems
            elif name == "fixture_id":
                resolution["fixture_id"] = "event-score-tie-v2"
                invariant["fixture_id"] = "event-score-tie-v2"
                report["case_results"][0]["reason"] = (
                    "resolved_by_current_regression:event-score-tie-v2"
                )
            elif name == "source_hash":
                path = next(iter(invariant["source_sha256"]))
                invariant["source_sha256"][path] = "0" * 64
            elif name == "positive_evidence":
                invariant["positive_case_sha256"] = "1" * 64
            elif name == "permutation_evidence":
                invariant["positive_permutation_sha256s"][1] = "2" * 64
            elif name == "permutation_cardinality":
                invariant["candidate_count"] = 1
            elif name == "permutation_both_relation":
                invariant["positive_permutation_kinds"][-1] = "candidate"
            elif name == "permutation_selection":
                invariant[
                    "positive_permutation_selected_choice_ids"
                ][1] = ["option:forged"]
            elif name == "negative_evidence":
                invariant["negative_problem_kinds"] = [
                    "final_choice_binding"
                ]
            elif name == "case_row":
                report["case_results"][0]["authority"] = "forged authority"
            else:
                raise AssertionError(name)

        for name in (
            "legacy_original_profile",
            "fixture_id",
            "source_hash",
            "positive_evidence",
            "permutation_evidence",
            "permutation_cardinality",
            "permutation_both_relation",
            "permutation_selection",
            "negative_evidence",
            "case_row",
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root, report, _document, _evidence = (
                        self.make_resolved_root(directory)
                    )
                    tampered = copy.deepcopy(report)
                    report_mutation(name, tampered)
                    (root / "decision-case-replay.json").write_text(
                        json.dumps(
                            tampered,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(
                        freeze_manifest.FreezeManifestError
                    ):
                        self.build_resolved_manifest(root, tampered)

        for name in ("resolution_document", "trace_evidence"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root, report, document, evidence = (
                        self.make_resolved_root(directory)
                    )
                    if name == "resolution_document":
                        changed = copy.deepcopy(document)
                        changed["resolutions"][0]["fixture_id"] = (
                            "event-score-tie-v2"
                        )
                        path = root / "decision-case-resolutions.json"
                    else:
                        changed = copy.deepcopy(evidence)
                        changed["records"][0]["trace_record_sha256"] = (
                            "3" * 64
                        )
                        path = root / "decision-case-trace-evidence.json"
                    path.write_text(
                        json.dumps(
                            changed,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(
                        freeze_manifest.FreezeManifestError
                    ):
                        self.build_resolved_manifest(root, report)

    def test_live_revalidation_rejects_forged_permutation_cardinality(self):
        with tempfile.TemporaryDirectory() as directory:
            root, report, _document, _evidence = self.make_resolved_root(
                directory
            )
            tampered = copy.deepcopy(report)
            invariant = tampered["case_results"][0]["resolution"][
                "invariant"
            ]
            invariant["protocol_count"] = 1
            (root / "decision-case-replay.json").write_text(
                json.dumps(
                    tampered,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                decision_case_replay.ReplayError,
                "permutation invariant is malformed",
            ):
                decision_case_replay.revalidate_persisted_report(
                    root,
                    tampered,
                    RESOLUTION_TARGET_HASH,
                )

    def test_fake_policy_hashes_are_rejected_before_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with patch.object(
                freeze_manifest,
                "_fresh_policy_hashes",
                return_value={
                    "decision_hash": "fresh-decision",
                    "controller_hash": "fresh-controller",
                },
            ):
                with self.assertRaisesRegex(
                    freeze_manifest.FreezeManifestError,
                    "fresh source fingerprints",
                ):
                    freeze_manifest.build_manifest(
                        root,
                        "self-consistent-fake",
                        "self-consistent-controller",
                        verification=verification(),
                        decision_case_replay=replay(
                            decision_hash="self-consistent-fake"
                        ),
                    )

    def test_any_frozen_source_change_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            (root / "autoplay.py").write_text("POLICY = 'changed'\n", encoding="utf-8")
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_bridge_runtime_source_drift_blocks_freeze_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "autoplay.py").write_text(
                "POLICY = 'changed-after-bridge-import'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "bridge runtime source snapshot",
            ):
                freeze_manifest.capture_launch_evidence(root)

    def test_launch_evidence_rejects_tampered_restart_predecessor_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            predecessor_path = (
                root
                / "logs"
                / "launches"
                / "launch-test.restart-predecessor.json"
            )
            predecessor = {
                "schema_version": 2,
                "record_type": "restart_predecessor",
            }
            predecessor_path.write_text(
                json.dumps(predecessor), encoding="utf-8"
            )
            launch_path = root / "logs" / "launches" / "launch-test.start.json"
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            launch["restart_predecessor_record_path"] = (
                predecessor_path.relative_to(root).as_posix()
            )
            launch["restart_predecessor_record_sha256"] = (
                freeze_manifest._canonical_sha256(predecessor)
            )
            launch_path.write_text(json.dumps(launch), encoding="utf-8")
            (root / "launch-latest.json").write_text(
                json.dumps(launch), encoding="utf-8"
            )
            freeze_manifest.capture_launch_evidence(root)

            predecessor["tampered"] = True
            predecessor_path.write_text(
                json.dumps(predecessor), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "restart predecessor launch anchor changed",
            ):
                freeze_manifest.capture_launch_evidence(root)

    def test_audit_repair_source_migration_allows_only_non_runtime_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "independent_oracle.py").write_text(
                "ORACLE = 1\n", encoding="utf-8"
            )
            bind_bridge_runtime_sources(root)
            baseline = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            freeze_manifest.write_manifest(root / "freeze-manifest.json", baseline)

            (root / "independent_oracle.py").write_text(
                "ORACLE = 2\n", encoding="utf-8"
            )
            migrated = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            migration = migrated["launch_evidence"][
                "runtime_source_migration"
            ]
            self.assertEqual(
                migration["changed_paths"], ["independent_oracle.py"]
            )
            freeze_manifest.write_manifest(root / "freeze-manifest.json", migrated)
            freeze_manifest.validate_manifest(
                migrated, root, "decision-a", "controller-a"
            )

    def test_pending_restart_target_migrates_when_bridge_is_already_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            for relative in (
                "freeze_manifest.py",
                "quick_start.py",
                "test_campaign_attempt.py",
                "test_launch_game.py",
                "test_quick_start.py",
            ):
                (root / relative).write_text(
                    f"{relative!r}\n", encoding="utf-8"
                )
            previous = freeze_manifest.source_snapshot(root)
            previous_digest = freeze_manifest.snapshot_digest(previous)
            (root / "freeze_manifest.py").write_text(
                "AUDIT_REPAIR = 2\n", encoding="utf-8"
            )
            (root / "test_campaign_attempt.py").write_text(
                "MAINTENANCE_TEST = 2\n", encoding="utf-8"
            )
            bind_bridge_runtime_sources(root)

            attempt_id = "pending-maintenance-attempt"
            attempt_dir = root / "logs" / "attempts" / attempt_id
            attempt_dir.mkdir(parents=True)
            (root / "run-result.json").write_text(
                json.dumps({"attempt_id": attempt_id}), encoding="utf-8"
            )
            intent_path = attempt_dir / "maintenance-restart-intent.json"
            intent = {
                "schema_version": 2,
                "record_type": "maintenance_restart_intent",
                "intent_status": "prepared_before_runtime_restart",
                "attempt_id": attempt_id,
                "target_source_digest": previous_digest,
                "target_source_file_count": len(previous),
                "created_at": 2.0,
            }
            intent_path.write_text(
                json.dumps(
                    intent,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            snapshot = {
                "schema_version": 2,
                "record_type": "maintenance_target_source_snapshot",
                "snapshot_status": (
                    "precommitted_target_digest_reconstructed"
                ),
                "attempt_id": attempt_id,
                "restart_intent_record_path": (
                    intent_path.relative_to(root).as_posix()
                ),
                "restart_intent_sha256": hashlib.sha256(
                    intent_path.read_bytes()
                ).hexdigest(),
                "target_source_digest": previous_digest,
                "target_source_file_count": len(previous),
                "sources": previous,
                "reconstruction_authority": (
                    "precommitted_source_digest_v1"
                ),
                "reconstruction_commit": "a" * 40,
                "reconstructed_paths": list(
                    freeze_manifest._MAINTENANCE_TARGET_RECONSTRUCTED_PATHS
                ),
                "decision_case_replay_generated_at": 1.0,
                "decision_case_replay_sha256": previous[
                    "decision-case-replay.json"
                ]["sha256"],
                "created_at": 3.0,
            }
            (attempt_dir / (
                freeze_manifest._PARKED_RUNTIME_SOURCE_SNAPSHOT_NAME
            )).write_text(
                json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )

            evidence = freeze_manifest.capture_launch_evidence(root)
            migration = evidence["runtime_source_migration"]
            self.assertEqual(previous_digest, migration[
                "previous_source_digest"
            ])
            self.assertEqual(
                ["freeze_manifest.py", "test_campaign_attempt.py"],
                migration["changed_paths"],
            )

            intent_bytes = intent_path.read_bytes()
            intent["created_at"] = 2.5
            intent_path.write_text(json.dumps(intent), encoding="utf-8")
            self.assertIsNone(
                freeze_manifest.capture_launch_evidence(root)[
                    "runtime_source_migration"
                ]
            )
            intent_path.write_bytes(intent_bytes)

            manifest = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            freeze_manifest.validate_manifest(
                manifest, root, "decision-a", "controller-a"
            )
            freeze_manifest.write_manifest(
                root / "freeze-manifest.json", manifest
            )
            (attempt_dir / "menu-transition.json").write_text(
                "{}", encoding="utf-8"
            )
            freeze_manifest.validate_manifest(
                manifest, root, "decision-a", "controller-a"
            )

    def test_jsonl_fixture_is_frozen_but_top_level_history_is_runtime_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            fixtures = root / "test_fixtures"
            fixtures.mkdir(exist_ok=True)
            fixture = fixtures / "contracts.jsonl"
            fixture.write_text('{"case":1}\n', encoding="utf-8")
            history = root / "run-history.jsonl"
            history.write_text('{"runtime":1}\n', encoding="utf-8")
            bind_bridge_runtime_sources(root)
            manifest = freeze_manifest.build_manifest(
                root,
                "decision-a",
                "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )

            self.assertIn("test_fixtures/contracts.jsonl", manifest["sources"])
            self.assertNotIn("run-history.jsonl", manifest["sources"])
            history.write_text('{"runtime":2}\n', encoding="utf-8")
            freeze_manifest.validate_manifest(
                manifest, root, "decision-a", "controller-a"
            )
            fixture.write_text('{"case":2}\n', encoding="utf-8")
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_dynamic_artifact_change_does_not_fake_source_but_launch_tamper_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            (root / "run-result.json").write_text("{\"new\":true}", encoding="utf-8")
            (root / "fixed-seed-replay.json").write_text(
                "{\"status\":\"clear\"}", encoding="utf-8"
            )
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                (root / "launch-latest.json").write_text(
                    "{\"pid\":123}", encoding="utf-8"
                )
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_installed_jar_change_invalidates_frozen_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            game = Path(json.loads(
                (root / "launch-latest.json").read_text(encoding="utf-8")
            )["game_dir"])
            (game / "mods" / "CommunicationMod.jar").write_bytes(b"old")
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "CommunicationMod.jar runtime provenance mismatch",
            ):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_startup_bridge_stderr_append_blocks_freeze_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            stderr = Path(json.loads(
                (root / "launch-latest.json").read_text(encoding="utf-8")
            )["bridge_stderr_path"])
            with stderr.open("ab") as handle:
                handle.write(b"startup failure\n")
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "startup errors",
            ):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "startup errors",
            ):
                freeze_manifest.build_manifest(
                    root, "decision-a", "controller-a",
                    verification=verification(),
                    decision_case_replay=replay(),
                )

    def test_finished_launch_record_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            launch_path = root / "launch-latest.json"
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            launch.update({"finished_at": 20.0, "exit_code": 0})
            launch_path.write_text(json.dumps(launch), encoding="utf-8")
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "running schema-v2",
            ):
                freeze_manifest.build_manifest(
                    root, "decision-a", "controller-a",
                    verification=verification(),
                    decision_case_replay=replay(),
                )

    def test_semantically_valid_launch_self_report_tamper_breaks_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            launch_path = root / "launch-latest.json"
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            launch["java_pid"] = 112
            launch_path.write_text(json.dumps(launch), encoding="utf-8")
            (root / launch["launch_start_record_path"]).write_text(
                json.dumps(launch), encoding="utf-8"
            )
            instance_path = root / "bridge-instance.json"
            instance = json.loads(instance_path.read_text(encoding="utf-8"))
            instance["parent_java_pid"] = 112
            instance_path.write_text(json.dumps(instance), encoding="utf-8")
            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "runtime launch changed",
            ):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_immutable_start_sidecar_tamper_blocks_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            launch = json.loads((root / "launch-latest.json").read_text(
                encoding="utf-8"
            ))
            sidecar = root / launch["launch_start_record_path"]
            changed = dict(launch)
            changed["java_pid"] = 999
            sidecar.write_text(json.dumps(changed), encoding="utf-8")

            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "latest differs from immutable",
            ):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_bridge_instance_from_old_launch_cannot_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            instance_path = root / "bridge-instance.json"
            instance = json.loads(instance_path.read_text(encoding="utf-8"))
            instance["launch_id"] = "old-launch"
            instance_path.write_text(json.dumps(instance), encoding="utf-8")

            with self.assertRaisesRegex(
                freeze_manifest.FreezeManifestError,
                "does not belong to the active launch",
            ):
                freeze_manifest.build_manifest(
                    root, "decision-a", "controller-a",
                    verification=verification(),
                    decision_case_replay=replay(),
                )

    def test_failed_test_diff_or_replay_is_a_hard_block(self):
        cases = (
            (verification(test_code=1), replay()),
            (verification(diff_code=1), replay()),
            (verification(), replay(status="inconclusive", unknown=1)),
        )
        for evidence, replay_evidence in cases:
            with self.subTest(evidence=evidence, replay=replay_evidence):
                with tempfile.TemporaryDirectory() as directory:
                    root = self.make_root(directory)
                    with self.assertRaises(freeze_manifest.FreezeManifestError):
                        freeze_manifest.build_manifest(
                            root, "decision-a", "controller-a",
                            verification=evidence,
                            decision_case_replay=replay_evidence,
                        )

    def test_new_untracked_source_after_freeze_is_detected_by_full_set_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            (root / "new_policy.py").write_text("x = 1\n", encoding="utf-8")
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_tampered_or_cross_hash_evidence_is_rejected_on_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            manifest["decision_case_replay"]["target_decision_hash"] = (
                "decision-b"
            )
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_runtime_artifact_snapshot_detects_history_and_cache_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            before = freeze_manifest.runtime_artifact_snapshot(root)
            (root / "run-history.jsonl").write_text("{}\n", encoding="utf-8")
            (root / "launch-latest.json").write_text("{}", encoding="utf-8")
            cache = root / ".deepseek-advisor-cache"
            cache.mkdir()
            (cache / "entry.json").write_text("{}", encoding="utf-8")
            after = freeze_manifest.runtime_artifact_snapshot(root)
            self.assertNotEqual(before, after)

    def test_autoplay_log_snapshot_detects_same_size_same_mtime_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            trace = root / "autoplay.log"
            trace.write_bytes(b"aaaa")
            original = trace.stat()
            before = freeze_manifest.runtime_artifact_snapshot(root)
            trace.write_bytes(b"bbbb")
            os.utime(
                trace,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            after = freeze_manifest.runtime_artifact_snapshot(root)
            self.assertEqual(
                before["autoplay.log"]["size"],
                after["autoplay.log"]["size"],
            )
            self.assertEqual(
                before["autoplay.log"]["mtime_ns"],
                after["autoplay.log"]["mtime_ns"],
            )
            self.assertNotEqual(
                before["autoplay.log"]["sha256"],
                after["autoplay.log"]["sha256"],
            )

    def test_post_run_validation_does_not_rescan_runtime_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            original_path_open = Path.open

            def reject_runtime_evidence(path, *args, **kwargs):
                candidate = Path(path)
                if (
                    candidate.name in {"autoplay.log", "decision-cases.jsonl"}
                    or candidate.name.endswith(".jsonl.gz")
                ):
                    raise AssertionError("post-run runtime evidence read")
                return original_path_open(path, *args, **kwargs)

            with (
                patch.object(
                    freeze_manifest,
                    "runtime_artifact_snapshot",
                    side_effect=AssertionError("post-run trace scan"),
                ),
                patch.object(
                    decision_case_replay,
                    "revalidate_persisted_report",
                    side_effect=AssertionError("post-run onsite replay"),
                ),
                patch.object(
                    decision_case_resolution,
                    "validate_trace_evidence",
                    side_effect=AssertionError("post-run trace validation"),
                ),
                patch.object(Path, "open", reject_runtime_evidence),
            ):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )

    def test_placeholder_and_empty_history_cannot_self_prove(self):
        cases = []
        placeholder = "placeholder-new-hash"
        cases.append((placeholder, replay(decision_hash=placeholder)))
        empty = replay()
        for field in (
            "source_case_count", "current_case_count", "historical_case_count",
            "audited_case_count", "classified_not_applicable_count",
            "historical_audited_count",
            "historical_classified_not_applicable_count",
        ):
            empty[field] = 0
        empty["case_results"] = []
        empty["not_applicable_classification_counts"] = {}
        empty["historical_not_applicable_classification_counts"] = {}
        empty["not_applicable_check_counts"] = {}
        empty["historical_not_applicable_check_counts"] = {}
        cases.append(("decision-a", empty))
        for decision_hash, evidence in cases:
            with self.subTest(decision_hash=decision_hash):
                with tempfile.TemporaryDirectory() as directory:
                    root = self.make_root(directory)
                    with self.assertRaises(freeze_manifest.FreezeManifestError):
                        freeze_manifest.build_manifest(
                            root, decision_hash, "controller-a",
                            verification=verification(),
                            decision_case_replay=evidence,
                        )

    def test_manifest_rejects_reported_test_pollution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            polluted = verification()
            polluted["runtime_artifacts_unchanged"] = False
            polluted["runtime_artifacts_after_sha256"] = "f" * 64
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.build_manifest(
                    root, "decision-a", "controller-a",
                    verification=polluted,
                    decision_case_replay=replay(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            manifest = freeze_manifest.build_manifest(
                root, "decision-a", "controller-a",
                verification=verification(),
                decision_case_replay=replay(),
            )
            manifest["full_tests"]["stdout_sha256"] = "not-a-digest"
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest.validate_manifest(
                    manifest, root, "decision-a", "controller-a"
                )


if __name__ == "__main__":
    unittest.main()
