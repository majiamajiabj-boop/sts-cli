import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import decision_case_replay
import decision_case_resolution
import decision_case_corpus
import freeze_manifest
from tests.test_decision_case_replay import case as canonical_case


TARGET_HASH = "target-resolution-hash"


def fixture_catalog():
    return json.loads(
        Path(
            "test_fixtures/decision-case-resolution-invariants-v1.json"
        ).read_text(encoding="utf-8")
    )


def fixture_cases():
    return [
        canonical_case(phase)
        for phase in decision_case_replay.REQUIRED_FIXTURE_PHASES
    ]


def blocked_case(invariant="legacy_candidates_missing"):
    value = decision_case_resolution._negative_fixture(invariant)
    value.update({
        "attempt_id": "legacy-resolution-attempt",
        "run_id": "IRONCLAD:0:legacy-resolution",
        "decision_hash": "legacy-source-hash",
        "before_seq": 77,
        "phase": "EVENT",
    })
    return value


def trace_record(value):
    return {
        "record_type": "decision",
        "decision_schema_version": 2,
        "attempt_id": value["attempt_id"],
        "run_id": value["run_id"],
        "decision_hash": value["decision_hash"],
        "before_seq": value["before_seq"],
        "phase": value["phase"],
        "available_options_before": copy.deepcopy(
            value.get("available_options", [])
        ),
        "requested_target_id": value["chosen"]["requested_target_id"],
        "resolved_target_id": value["chosen"]["resolved_target_id"],
        "decision": {
            "candidates": copy.deepcopy(value.get("candidates", [])),
        },
    }


def build_bundle(root, invariant="legacy_candidates_missing"):
    root = Path(root)
    value = blocked_case(invariant)
    trace = root / "autoplay.log"
    trace.write_bytes(
        decision_case_resolution.canonical_bytes(trace_record(value)) + b"\n"
    )
    result = decision_case_replay._audit_one(value, historical=True)
    evidence = decision_case_resolution.extract_trace_evidence(
        trace, [value], [result]
    )
    catalog = fixture_catalog()
    document = decision_case_resolution.generate_resolution_document(
        [value],
        [result],
        fixture_catalog=catalog,
        trace_evidence=evidence,
        trace_path=trace,
        audit_one=decision_case_replay._audit_one,
    )
    return value, trace, evidence, catalog, document


def audit_bundle(value, trace, evidence, catalog, document):
    return decision_case_replay.audit_cases(
        [value],
        TARGET_HASH,
        fixture_cases=fixture_cases(),
        resolution_document=document,
        resolution_fixture_catalog=catalog,
        trace_evidence=evidence,
        trace_path=trace,
    )


class DecisionCaseResolutionTests(unittest.TestCase):
    def test_current_fixture_catalog_and_new_profiles_are_exact(self):
        catalog = fixture_catalog()
        self.assertEqual(44, len(catalog["fixtures"]))
        self.assertEqual(
            44, len(decision_case_resolution._EXPECTED_FIXTURE_ROWS)
        )
        decision_case_resolution.validate_fixture_catalog(catalog)

        sapphire = (
            "SAPPHIRE_KEY",
            "audited_issues",
            ("v2_producer_operation_contradicted",),
        )
        combat_reward = (
            "COMBAT_REWARD",
            "audited_issues",
            (
                "chosen_selected_binding",
                "score_cutoff_tie_is_semantically_ambiguous",
                "v2_producer_bound_new_potion_id_not_protocol_visible",
            ),
        )
        bloody_idol_gold = (
            "SAPPHIRE_KEY",
            "audited_issues",
            tuple(sorted({
                "candidate_consequence_gold_delta_invalid",
                "candidate_consequence_hp_delta_invalid",
                "candidate_consequence_max_hp_delta_invalid",
                "choice_consequence_gold_delta_invalid",
                "choice_consequence_hp_delta_invalid",
                "choice_consequence_max_hp_delta_invalid",
                "v2_independent_consequence_mismatch:hp_delta",
            })),
        )
        sapphire_legacy_field_status = (
            "SAPPHIRE_KEY",
            "audited_issues",
            tuple(sorted({
                *decision_case_resolution._LEGACY_NUMERIC_CONSEQUENCE_INVALID,
                "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
                "v2_independent_consequence_mismatch:field_knowledge.curse.status",
                "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
                "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
                "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
                "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
            })),
        )
        chest_random_curse_legacy = (
            "CHEST",
            "audited_issues",
            decision_case_resolution._CHEST_RANDOM_CURSE_LEGACY_PROBLEMS,
        )
        self.assertEqual(
            "sapphire-consequence-v2",
            decision_case_resolution._profile_fixture_id(*sapphire),
        )
        self.assertEqual(
            "combat-reward-resource-preparation-score-v2",
            decision_case_resolution._profile_fixture_id(*combat_reward),
        )
        self.assertEqual(
            "sapphire-bloody-idol-gold-consequence-v1",
            decision_case_resolution._profile_fixture_id(*bloody_idol_gold),
        )
        self.assertEqual(
            "sapphire-consequence-v2",
            decision_case_resolution._profile_fixture_id(
                *sapphire_legacy_field_status
            ),
        )
        self.assertEqual(
            "chest-consequence-v2",
            decision_case_resolution._profile_fixture_id(
                *chest_random_curse_legacy
            ),
        )
        self.assertIsNone(
            decision_case_resolution._profile_fixture_id(
                "SHOP_SCREEN", "unresolved",
                ("score_cutoff_tie_is_semantically_ambiguous",),
            ),
        )
        self.assertIsNone(
            decision_case_resolution._profile_fixture_id(
                "COMBAT_REWARD", "unresolved",
                ("score_cutoff_tie_is_semantically_ambiguous",),
            ),
        )
        for phase, classification, problems in (
            sapphire, combat_reward, bloody_idol_gold,
            sapphire_legacy_field_status, chest_random_curse_legacy,
        ):
            with self.subTest(phase=phase):
                self.assertIsNone(
                    decision_case_resolution._profile_fixture_id(
                        phase,
                        classification,
                        tuple(sorted((*problems, "unrelated_problem"))),
                    )
                )

    def test_cursed_tome_historical_profile_drift_requires_exact_event_fixture(self):
        legacy_uncertainty = (
            "EVENT",
            "audited_issues",
            tuple(sorted({
                *decision_case_resolution._V2_NUMERIC_CONSEQUENCE_UNKNOWN,
                "v2_independent_consequence_mismatch:uncertainty",
                "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
                "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
                "v2_settlement_authority_binding",
                "v2_settlement_not_fully_observable",
            })),
        )
        stale_partition = (
            "EVENT",
            "audited_issues",
            tuple(sorted({
                "canonical_choice_local_reason_invalid",
                "v2_producer_effect_field_unclassified:original_button_index",
                "v2_producer_operation_not_independently_classified",
                "v2_projected_producer_consequence_claim_recompute",
                "v2_projected_unclassified_producer_fields_recompute",
            })),
        )
        self.assertEqual(
            "event-consequence-v2",
            decision_case_resolution._PROFILE_FIXTURES.get(legacy_uncertainty),
        )
        self.assertEqual(
            "event-consequence-v2",
            decision_case_resolution._PROFILE_FIXTURES.get(stale_partition),
        )

    def test_knowing_skull_dialog_advance_allowlist_is_case_exact(self):
        problems = (
            decision_case_resolution._KNOWING_SKULL_DIALOG_ADVANCE_PROBLEMS
        )
        identities = tuple(
            identity
            for identity, binding in
            decision_case_resolution._CASE_FIXTURE_ALLOWLIST.items()
            if (
                binding[2] == "event-consequence-v2"
                and binding[1] == problems
            )
        )

        self.assertEqual(2, len(identities))
        for identity in identities:
            with self.subTest(before_seq=identity[1]):
                self.assertEqual(
                    "event-consequence-v2",
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, "audited_issues", problems
                    ),
                )
        self.assertEqual(
            "event-consequence-v2",
            decision_case_resolution._profile_fixture_id(
                "EVENT", "audited_issues", problems
            ),
        )
        self.assertEqual(
            44, len(decision_case_resolution._EXPECTED_FIXTURE_ROWS)
        )

        baseline = identities[0]
        mutations = {
            "attempt": ("different-attempt", *baseline[1:]),
            "sequence": (
                baseline[0], baseline[1] + 1, *baseline[2:]
            ),
            "phase": (*baseline[:2], "NEOW", *baseline[3:]),
            "event": (*baseline[:3], "sensory_stone", baseline[4]),
            "digest": (*baseline[:4], "0" * 64),
        }
        for field, identity in mutations.items():
            with self.subTest(changed_identity=field):
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, "audited_issues", problems
                    )
                )

        self.assertNotIn(
            "v2_independent_consequence_mismatch:uncertainty_classification",
            problems,
        )
        self.assertEqual(
            "event-consequence-v2",
            decision_case_resolution._profile_fixture_id(
                "EVENT", "audited_issues", problems
            ),
        )
        legacy_uncertainty = tuple(sorted((
            *problems,
            "v2_independent_consequence_mismatch:uncertainty_classification",
        )))
        for classification, candidate_problems in (
            ("unresolved", problems),
            (
                "audited_issues",
                tuple(sorted((*problems, "unrelated_problem"))),
            ),
            ("audited_issues", legacy_uncertainty),
        ):
            with self.subTest(
                classification=classification,
                candidate_problems=candidate_problems,
            ):
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *baseline, classification, candidate_problems
                    )
                )

    def test_golden_wing_legacy_consequence_allowlist_is_case_exact(self):
        identity = (
            "e0d39f61-8147-4590-bcfb-bf42be299bc1",
            232398,
            "EVENT",
            "goldenwing",
            "019149090ad508322f876b8c8d4fd7823cf0e7c2359ffb4cb383e93e09651857",
        )
        problems = (
            decision_case_resolution._GOLDEN_WING_LEGACY_CONSEQUENCE_PROBLEMS
        )
        self.assertEqual(
            "event-consequence-v2",
            decision_case_resolution._case_fixture_id_from_binding(
                *identity, "audited_issues", problems
            ),
        )
        self.assertIsNone(
            decision_case_resolution._case_fixture_id_from_binding(
                *identity[:-1], "0" * 64, "audited_issues", problems
            )
        )
        self.assertIsNone(
            decision_case_resolution._case_fixture_id_from_binding(
                *identity, "audited_issues",
                tuple(sorted((*problems, "taxonomy_drift"))),
            )
        )

    def test_drug_dealer_dialog_advance_allowlist_is_case_exact(self):
        problems = (
            decision_case_resolution._DRUG_DEALER_DIALOG_ADVANCE_PROBLEMS
        )
        cases = decision_case_resolution._DRUG_DEALER_DIALOG_ADVANCE_CASES
        self.assertEqual(6, len(cases))
        for attempt_id, before_seq, case_sha256 in cases:
            with self.subTest(attempt_id=attempt_id, before_seq=before_seq):
                identity = (
                    attempt_id, before_seq, "EVENT", "drugdealer",
                    case_sha256,
                )
                self.assertEqual(
                    "event-consequence-v2",
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, "audited_issues", problems
                    ),
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity[:-1], "0" * 64,
                        "audited_issues", problems,
                    )
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, "audited_issues",
                        tuple(sorted((*problems, "taxonomy_drift"))),
                    )
                )

    def test_resource_ties_are_case_exact_and_shape_locked(self):
        identities = tuple(
            decision_case_resolution._EXACT_RESOURCE_TIE_CASE_IDENTITIES
        )
        self.assertEqual(2, len(identities))
        for identity in identities:
            classification, problems, fixture_id = (
                decision_case_resolution._CASE_FIXTURE_ALLOWLIST[identity]
            )
            with self.subTest(fixture_id=fixture_id):
                self.assertEqual(
                    fixture_id,
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, classification, problems
                    ),
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        "generic-same-phase-case", *identity[1:],
                        classification, problems
                    )
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, "audited_issues", problems
                    )
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *identity, classification,
                        tuple(sorted((*problems, "taxonomy_drift"))),
                    )
                )

        for fixture_id in (
            "shop-resource-preparation-score-v2",
            "combat-reward-resource-preparation-score-v2",
        ):
            fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                fixture_id
            ]
            kwargs = (
                {"production_variant": "baseline"}
                if fixture_id == "shop-resource-preparation-score-v2"
                else {"production_variant": "baseline"}
            )
            case = decision_case_resolution._build_probe_case(
                decision_case_resolution._production_probe(
                    fixture["producer_probe"], **kwargs
                )
            )
            self.assertTrue(
                decision_case_resolution._exact_resource_case_shape(
                    case, fixture_id
                )
            )
            wrong_reason = copy.deepcopy(case)
            wrong_reason["reason"] = "different_reason"
            self.assertFalse(
                decision_case_resolution._exact_resource_case_shape(
                    wrong_reason, fixture_id
                )
            )
            wrong_surface = copy.deepcopy(case)
            wrong_surface["decision_surface_kind"] = "strategic_choice"
            self.assertFalse(
                decision_case_resolution._exact_resource_case_shape(
                    wrong_surface, fixture_id
                )
            )

    def test_seven_legacy_shop_parents_are_exact_and_fail_closed(self):
        staging = (
            Path(__file__).resolve().parents[1]
            / "artifact-staging"
            / "new-six-b3951d2d4031632b"
            / "decision-case-corpus"
        )
        sources = (
            (
                "root-02e8d6ffe6176ed2.jsonl.gz", 870,
                "052a66b0-cbb4-4512-be6d-4994f2e8ed50", 210575,
            ),
            (
                "0098b71d-c5a7-44c2-ae20-95d9d4b29d98-"
                "16ace714460a116f.jsonl.gz", 41,
                "0098b71d-c5a7-44c2-ae20-95d9d4b29d98", 224073,
            ),
            (
                "b3091ec2-c434-44b3-b11f-bdf6168c979d-"
                "0ff923fdee5b38db.jsonl.gz", 52,
                "b3091ec2-c434-44b3-b11f-bdf6168c979d", 225173,
            ),
            (
                "239934eb-9d25-40d8-9e4e-3fea9e5333dd-"
                "d0702840e4a8c0bb.jsonl.gz", 51,
                "239934eb-9d25-40d8-9e4e-3fea9e5333dd", 233188,
            ),
            (
                "ea5fb7a3-dce9-4a7d-906d-d0c82b08b38e-"
                "e432deec67e69b8d.jsonl.gz", 132,
                "ea5fb7a3-dce9-4a7d-906d-d0c82b08b38e", 234204,
            ),
            (
                "6ebf12ee-b412-4bec-b2fa-3250e032e335-"
                "647bd0a3cf4fd418.jsonl.gz", 48,
                "6ebf12ee-b412-4bec-b2fa-3250e032e335", 235768,
            ),
            (
                "7e2d8be6-cbc4-4824-908d-979c4d82a530-"
                "94f6a016b56450c1.jsonl.gz", 65,
                "7e2d8be6-cbc4-4824-908d-979c4d82a530", 237025,
            ),
        )
        if any(not (staging / name).exists() for name, *_rest in sources):
            self.skipTest("bounded staged legacy shop archives unavailable")
        import gzip

        values = []
        for name, offset, attempt_id, before_seq in sources:
            with gzip.open(staging / name, "rt", encoding="utf-8") as handle:
                value = next(
                    json.loads(line)
                    for index, line in enumerate(handle)
                    if index == offset
                )
            self.assertEqual(attempt_id, value.get("attempt_id"))
            self.assertEqual(before_seq, value.get("before_seq"))
            identity = (
                attempt_id, before_seq, "SHOP_SCREEN", None,
                decision_case_resolution.object_digest(value),
            )
            self.assertIn(
                identity,
                decision_case_resolution.
                _EXACT_LEGACY_SHOP_PARENT_CASE_IDENTITIES,
            )
            result = decision_case_replay._audit_one(
                value, historical=False
            )
            problems = decision_case_resolution.problem_kinds(result)
            self.assertEqual("unresolved", result["classification"])
            self.assertEqual(
                list(decision_case_resolution._LEGACY_SHOP_PARENT_PROBLEMS),
                problems,
            )
            self.assertTrue(
                decision_case_resolution._legacy_shop_parent_case_shape(value)
            )
            self.assertEqual(
                "shop-resource-preparation-score-v2",
                decision_case_resolution._case_fixture_id(
                    value, result["classification"], problems
                ),
            )
            values.append((value, identity, problems))
        self.assertEqual(7, len(values))

        baseline, identity, problems = values[0]
        for field, changed in (
            ("attempt", ("wrong-attempt", *identity[1:])),
            (
                "sequence",
                (identity[0], identity[1] + 1, *identity[2:]),
            ),
            ("digest", (*identity[:4], "0" * 64)),
        ):
            with self.subTest(identity_tamper=field):
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id_from_binding(
                        *changed, "unresolved", problems
                    )
                )
        for classification, changed_problems in (
            ("audited_issues", problems),
            ("unresolved", [*problems, "taxonomy_drift"]),
        ):
            self.assertIsNone(
                decision_case_resolution._case_fixture_id_from_binding(
                    *identity, classification, changed_problems
                )
            )

        ambiguous_parent = copy.deepcopy(baseline)
        parent_id = next(iter({
            (row.get("consequences") or {}).get("bound_new_potion_id")
            for row in ambiguous_parent["producer_candidates"]
        }))
        parent = next(
            row for row in ambiguous_parent["available_options"]
            if ((row.get("target") or {}).get("item") or {}).get("id")
            == parent_id
        )
        duplicate = copy.deepcopy(parent)
        duplicate["option_id"] = "option:duplicate-legacy-parent"
        duplicate["choice_index"] = max(
            row["choice_index"]
            for row in ambiguous_parent["available_options"]
        ) + 1
        ambiguous_parent["available_options"].append(duplicate)

        canonical_score_tie = copy.deepcopy(baseline)
        selected_instance = next(
            row["target"]["potion_instance_id"]
            for row in canonical_score_tie["canonical_choices"]
            if row.get("selected") is True
        )
        selected_canonical = next(
            row for row in canonical_score_tie["canonical_choices"]
            if row.get("selected") is True
        )
        canonical_runner_up = next(
            row for row in canonical_score_tie["canonical_choices"]
            if row is not selected_canonical
        )
        canonical_runner_up["local_score"] = selected_canonical["local_score"]

        hp_delta = copy.deepcopy(baseline)
        for surface in (
            hp_delta["decision_outcome"],
            hp_delta["authoritative_choice_settlement"]["observed_outcome"],
        ):
            surface["current_hp_delta"] = 1
            surface["hp_delta"] = 1

        resource_instance_drift = copy.deepcopy(baseline)
        resource_instance_drift["resource_preparation_options"][0][
            "target"
        ]["potion_instance_id"] = "potion:drifted-resource-child"

        bad_settlement = copy.deepcopy(baseline)
        bad_settlement["authoritative_choice_settlement"][
            "choice_id"
        ] = "potion:wrong:settlement"
        wrong_reason = copy.deepcopy(baseline)
        wrong_reason["reason"] = "different_reason"
        for name, mutation in (
            ("ambiguous_parent", ambiguous_parent),
            ("canonical_score_tie", canonical_score_tie),
            ("hp_delta", hp_delta),
            ("resource_instance_drift", resource_instance_drift),
            ("settlement", bad_settlement),
            ("reason", wrong_reason),
        ):
            with self.subTest(shape_tamper=name):
                self.assertFalse(
                    decision_case_resolution.
                    _legacy_shop_parent_case_shape(mutation)
                )
                self.assertIsNone(
                    decision_case_resolution._case_fixture_id(
                        mutation, "unresolved", problems
                    )
                )

    def _compact_bundle(self, root):
        value, trace, evidence, catalog, _document = build_bundle(root)
        evidence_path = Path(root) / "decision-case-trace-evidence.json"
        decision_case_resolution._write_json(evidence_path, evidence)
        freeze = {
            "release_gate_passed": True,
            "decision_case_replay_revalidation": {
                "trace_prefix_verified": True,
                "artifact_sha256": {
                    "decision-case-trace-evidence.json": (
                        decision_case_resolution.file_digest(evidence_path)
                    ),
                },
            },
        }
        freeze_path = Path(root) / "freeze-manifest.json"
        decision_case_resolution._write_json(freeze_path, freeze)
        result = decision_case_replay._audit_one(value, historical=True)
        compact = decision_case_resolution.compact_trace_evidence(
            trace,
            evidence,
            [value],
            [result],
            freeze_manifest=freeze,
            legacy_evidence_sha256=(
                decision_case_resolution.file_digest(evidence_path)
            ),
            freeze_manifest_sha256=(
                decision_case_resolution.file_digest(freeze_path)
            ),
        )
        document = decision_case_resolution.generate_resolution_document(
            [value],
            [result],
            fixture_catalog=catalog,
            trace_evidence=compact,
            trace_path=None,
            audit_one=decision_case_replay._audit_one,
        )
        return value, trace, compact, catalog, document

    def _multi_source_bundle(self, root, *, archived_result=None):
        root = Path(root)
        old, _old_trace, compact, catalog, _document = self._compact_bundle(
            root
        )
        new = blocked_case()
        new.update({
            "attempt_id": "new-attempt",
            "run_id": "IRONCLAD:0:new-attempt",
            "before_seq": 88,
        })
        cases = [old, new]
        results = [
            archived_result or decision_case_replay._audit_one(
                old, historical=True
            ),
            decision_case_replay._audit_one(new, historical=True),
        ]
        trace = root / "logs" / "attempts" / "new-attempt" / "autoplay.log"
        trace.parent.mkdir(parents=True)
        prefix = b'{"irrelevant":true}\n' + (b"x" * 137) + b"\n"
        target = (
            decision_case_resolution.canonical_bytes(trace_record(new)) + b"\n"
        )
        suffix = b'{"later":true}\n'
        trace.write_bytes(prefix + target + suffix)
        trace_digest = hashlib.sha256(trace.read_bytes()).hexdigest()
        evidence = decision_case_resolution.extend_compact_trace_evidence(
            compact,
            trace,
            cases,
            results,
            source_trace_label=(
                "logs/attempts/new-attempt/autoplay.log"
            ),
        )
        return {
            "cases": cases,
            "results": results,
            "trace": trace,
            "trace_digest": trace_digest,
            "prefix_length": len(prefix),
            "archived_evidence": compact,
            "evidence": evidence,
            "catalog": catalog,
        }

    def test_compact_trace_evidence_survives_source_trace_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, catalog, document = self._compact_bundle(
                directory
            )
            released = trace.stat().st_size
            trace.unlink()
            report = audit_bundle(
                value, None, evidence, catalog, document
            )
            self.assertTrue(report["release_gate_passed"])
            self.assertEqual("clear", report["status"])
            self.assertEqual(2, evidence["schema_version"])
            self.assertEqual(
                "embedded_raw_lines_v1", evidence["evidence_mode"]
            )
            self.assertGreater(released, 0)

    def test_compact_trace_evidence_rejects_embedded_line_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            value, _trace, evidence, _catalog, _document = (
                self._compact_bundle(directory)
            )
            result = decision_case_replay._audit_one(value, historical=True)
            evidence["records"][0]["raw_line_base64"] = "e30K"
            with self.assertRaises(decision_case_resolution.ResolutionError):
                decision_case_resolution.validate_trace_evidence(
                    None, evidence, [value], [result]
                )

    def test_compaction_requires_prior_verified_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, _catalog, _document = build_bundle(directory)
            result = decision_case_replay._audit_one(value, historical=True)
            with self.assertRaises(decision_case_resolution.ResolutionError):
                decision_case_resolution.compact_trace_evidence(
                    trace,
                    evidence,
                    [value],
                    [result],
                    freeze_manifest={"release_gate_passed": True},
                    legacy_evidence_sha256="0" * 64,
                    freeze_manifest_sha256="1" * 64,
                )

    def test_schema3_preserves_archive_and_embeds_only_new_exact_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._multi_source_bundle(directory)
            evidence = bundle["evidence"]
            source = evidence["sources"][0]
            row = source["records"][0]
            self.assertEqual(3, evidence["schema_version"])
            self.assertEqual(
                bundle["archived_evidence"],
                evidence["archived_schema2_evidence"],
            )
            self.assertEqual(
                "multi_source_embedded_gzip_v1", evidence["evidence_mode"]
            )
            self.assertEqual(2, evidence["target_case_count"])
            self.assertEqual(1, evidence[
                "archived_schema2_summary"
            ]["active_record_count"])
            self.assertEqual(1, source["found_case_count"])
            self.assertEqual(1, source["source_scan_pass_count"])
            self.assertEqual(bundle["trace_digest"], source[
                "source_trace_sha256"
            ])
            self.assertEqual(bundle["prefix_length"], row[
                "trace_byte_offset"
            ])
            self.assertEqual(0, row["payload_byte_offset"])
            self.assertNotEqual(
                row["trace_byte_offset"], row["payload_byte_offset"]
            )
            self.assertNotIn("record", row)
            self.assertNotIn("raw_line_base64", row)

            bundle["trace"].unlink()
            observed = decision_case_resolution.validate_trace_evidence(
                None,
                evidence,
                bundle["cases"],
                bundle["results"],
            )
            self.assertEqual(2, len(observed))
            document = decision_case_resolution.generate_resolution_document(
                bundle["cases"],
                bundle["results"],
                fixture_catalog=bundle["catalog"],
                trace_evidence=evidence,
                trace_path=None,
                audit_one=decision_case_replay._audit_one,
            )
            report = decision_case_replay.audit_cases(
                bundle["cases"],
                TARGET_HASH,
                fixture_cases=fixture_cases(),
                resolution_document=document,
                resolution_fixture_catalog=bundle["catalog"],
                trace_evidence=evidence,
                trace_path=None,
            )
            self.assertEqual("clear", report["status"])
            self.assertEqual(2, report["resolved_case_count"])

    def test_schema3_attempt_trace_is_opened_once_for_one_full_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _trace, compact, _catalog, _document = self._compact_bundle(
                root
            )
            new = blocked_case()
            new.update({
                "attempt_id": "new-attempt",
                "run_id": "IRONCLAD:0:new-attempt",
                "before_seq": 88,
            })
            trace = (
                root / "logs" / "attempts" / "new-attempt" / "autoplay.log"
            )
            trace.parent.mkdir(parents=True)
            trace.write_bytes(
                b'{"noise":true}\n'
                + decision_case_resolution.canonical_bytes(trace_record(new))
                + b"\n"
            )
            real_open = Path.open
            opened = []

            def counted_open(path, *args, **kwargs):
                if Path(path) == trace:
                    opened.append((args, kwargs))
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=counted_open):
                evidence = decision_case_resolution.extend_compact_trace_evidence(
                    compact,
                    trace,
                    [old, new],
                    [
                        decision_case_replay._audit_one(old, historical=True),
                        decision_case_replay._audit_one(new, historical=True),
                    ],
                    source_trace_label=(
                        "logs/attempts/new-attempt/autoplay.log"
                    ),
                )
            self.assertEqual(1, len(opened))
            self.assertEqual(1, evidence["sources"][0][
                "source_scan_pass_count"
            ])

    def test_schema3_extends_multiple_attempt_traces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _trace, compact, _catalog, _document = self._compact_bundle(
                root
            )
            new_cases = []
            trace_sources = []
            for offset, attempt_id in enumerate(("attempt-a", "attempt-b")):
                value = blocked_case()
                value.update({
                    "attempt_id": attempt_id,
                    "run_id": f"IRONCLAD:0:{attempt_id}",
                    "before_seq": 88 + offset,
                })
                trace = (
                    root / "logs" / "attempts" / attempt_id / "autoplay.log"
                )
                trace.parent.mkdir(parents=True)
                trace.write_bytes(
                    b'{"noise":true}\n'
                    + decision_case_resolution.canonical_bytes(
                        trace_record(value)
                    )
                    + b"\n"
                )
                new_cases.append(value)
                trace_sources.append((
                    trace, f"logs/attempts/{attempt_id}/autoplay.log"
                ))
            cases = [old, *new_cases]
            results = [
                decision_case_replay._audit_one(value, historical=True)
                for value in cases
            ]
            real_open = Path.open
            opened = []

            def counted_open(path, *args, **kwargs):
                if any(Path(path) == trace for trace, _label in trace_sources):
                    opened.append(Path(path))
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=counted_open):
                evidence = (
                    decision_case_resolution.extend_compact_trace_evidence_many(
                        compact, trace_sources, cases, results
                    )
                )
            self.assertEqual(2, len(opened))
            self.assertEqual(2, len(set(opened)))
            self.assertEqual(2, len(evidence["sources"]))
            self.assertEqual(3, evidence["target_case_count"])
            self.assertEqual(
                3,
                len(decision_case_resolution.validate_trace_evidence(
                    None, evidence, cases, results
                )),
            )

    def test_schema3_multi_attempt_extension_never_returns_partial_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _trace, compact, _catalog, _document = self._compact_bundle(
                root
            )
            new_cases = []
            first_trace = None
            for offset, attempt_id in enumerate(("attempt-a", "attempt-b")):
                value = blocked_case()
                value.update({
                    "attempt_id": attempt_id,
                    "run_id": f"IRONCLAD:0:{attempt_id}",
                    "before_seq": 88 + offset,
                })
                trace = (
                    root / "logs" / "attempts" / attempt_id / "autoplay.log"
                )
                trace.parent.mkdir(parents=True)
                trace.write_bytes(
                    decision_case_resolution.canonical_bytes(
                        trace_record(value)
                    )
                    + b"\n"
                )
                if first_trace is None:
                    first_trace = trace
                new_cases.append(value)
            cases = [old, *new_cases]
            results = [
                decision_case_replay._audit_one(value, historical=True)
                for value in cases
            ]
            real_open = Path.open
            opened = []

            def counted_open(path, *args, **kwargs):
                if Path(path) == first_trace:
                    opened.append(Path(path))
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=counted_open):
                with self.assertRaises(
                    decision_case_resolution.ResolutionError
                ):
                    decision_case_resolution.extend_compact_trace_evidence_many(
                        compact,
                        [(first_trace, "logs/attempts/attempt-a/autoplay.log")],
                        cases,
                        results,
                    )
            self.assertEqual([], opened)

    def test_schema3_extension_refreshes_derived_problems_without_old_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._multi_source_bundle(root)
            base = bundle["evidence"]
            old_source_id = base["sources"][0]["source_id"]
            bundle["trace"].unlink()

            latest = blocked_case()
            latest.update({
                "attempt_id": "latest-attempt",
                "run_id": "IRONCLAD:0:latest-attempt",
                "before_seq": 99,
            })
            cases = [*bundle["cases"], latest]
            refreshed_result = copy.deepcopy(bundle["results"][1])
            refreshed_result["issues"] = [
                *refreshed_result.get("issues", []),
                "new_independent_auditor_problem",
            ]
            results = [
                bundle["results"][0],
                refreshed_result,
                decision_case_replay._audit_one(latest, historical=True),
            ]
            latest_trace = (
                root / "logs" / "attempts" / "latest-attempt" / "autoplay.log"
            )
            latest_trace.parent.mkdir(parents=True)
            latest_trace.write_bytes(
                b'{"noise":true}\n'
                + decision_case_resolution.canonical_bytes(trace_record(latest))
                + b"\n"
            )

            evidence = decision_case_resolution.extend_compact_trace_evidence(
                base,
                latest_trace,
                cases,
                results,
                source_trace_label=(
                    "logs/attempts/latest-attempt/autoplay.log"
                ),
            )

            self.assertEqual(2, len(evidence["sources"]))
            self.assertNotEqual(old_source_id, evidence["sources"][0]["source_id"])
            refreshed_row = evidence["sources"][0]["records"][0]
            self.assertIn(
                "new_independent_auditor_problem",
                refreshed_row["original_problem_kinds"],
            )
            observed = decision_case_resolution.validate_trace_evidence(
                None, evidence, cases, results
            )
            self.assertEqual(3, len(observed))

    def test_schema3_refreshes_derived_problems_without_a_new_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._multi_source_bundle(directory)
            base = bundle["evidence"]
            old_source = copy.deepcopy(base["sources"][0])
            bundle["trace"].unlink()
            refreshed_result = copy.deepcopy(bundle["results"][1])
            refreshed_result["issues"] = [
                *refreshed_result.get("issues", []),
                "new_independent_auditor_problem",
            ]
            results = [bundle["results"][0], refreshed_result]

            evidence = (
                decision_case_resolution.refresh_compact_trace_evidence(
                    base, bundle["cases"], results
                )
            )

            source = evidence["sources"][0]
            self.assertNotEqual(old_source["source_id"], source["source_id"])
            self.assertEqual(
                old_source["source_trace_sha256"],
                source["source_trace_sha256"],
            )
            self.assertEqual(
                old_source["payload_compressed_sha256"],
                source["payload_compressed_sha256"],
            )
            self.assertIn(
                "new_independent_auditor_problem",
                source["records"][0]["original_problem_kinds"],
            )
            observed = decision_case_resolution.validate_trace_evidence(
                None, evidence, bundle["cases"], results
            )
            self.assertEqual(2, len(observed))

    def test_schema3_keeps_nonblocking_archived_rows_but_not_active_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            archived_result = {
                "classification": "not_applicable",
                "issues": [],
                "unknowns": [],
            }
            bundle = self._multi_source_bundle(
                directory, archived_result=archived_result
            )
            evidence = bundle["evidence"]
            self.assertEqual(1, evidence[
                "archived_schema2_summary"
            ]["record_count"])
            self.assertEqual(0, evidence[
                "archived_schema2_summary"
            ]["active_record_count"])
            self.assertEqual(1, evidence["target_case_count"])
            self.assertEqual(1, len(evidence["records"]))
            self.assertEqual(
                evidence["sources"][0]["source_id"],
                evidence["records"][0]["source_id"],
            )
            observed = decision_case_resolution.validate_trace_evidence(
                None,
                evidence,
                bundle["cases"],
                bundle["results"],
            )
            self.assertEqual({("new-attempt", 88, "EVENT")}, set(observed))

    def test_schema3_tampering_fails_closed_without_source_trace(self):
        mutations = {
            "archive": lambda evidence: evidence[
                "archived_schema2_evidence"
            ]["records"][0].update(trace_byte_offset=999),
            "archive_digest": lambda evidence: evidence.update(
                archived_schema2_evidence_sha256="0" * 64
            ),
            "source_digest": lambda evidence: evidence["sources"][0].update(
                source_trace_sha256="1" * 64
            ),
            "source_path": lambda evidence: evidence["sources"][0].update(
                source_trace="autoplay.log"
            ),
            "original_offset": lambda evidence: evidence[
                "sources"
            ][0]["records"][0].update(trace_byte_offset=0),
            "payload": lambda evidence: evidence["sources"][0].update(
                payload_base64="H4sIAAAAAAACAwMAAAAAAAAAAAA="
            ),
            "active_reference": lambda evidence: evidence["records"][0].update(
                source_id="invented-source"
            ),
            "manifest": lambda evidence: evidence.update(
                source_manifest_sha256="2" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    bundle = self._multi_source_bundle(directory)
                    evidence = copy.deepcopy(bundle["evidence"])
                    bundle["trace"].unlink()
                    mutate(evidence)
                    with self.assertRaises(
                        decision_case_resolution.ResolutionError
                    ):
                        decision_case_resolution.validate_trace_evidence(
                            None,
                            evidence,
                            bundle["cases"],
                            bundle["results"],
                        )

    def test_schema3_rejects_root_autoplay_source_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _trace, compact, _catalog, _document = self._compact_bundle(
                root
            )
            new = blocked_case()
            new.update({
                "attempt_id": "new-attempt",
                "run_id": "IRONCLAD:0:new-attempt",
                "before_seq": 88,
            })
            trace = root / "new.log"
            trace.write_bytes(
                decision_case_resolution.canonical_bytes(trace_record(new))
                + b"\n"
            )
            with self.assertRaises(decision_case_resolution.ResolutionError):
                decision_case_resolution.extend_compact_trace_evidence(
                    compact,
                    trace,
                    [old, new],
                    [
                        decision_case_replay._audit_one(old, historical=True),
                        decision_case_replay._audit_one(new, historical=True),
                    ],
                    source_trace_label="autoplay.log",
                )

    def test_schema3_live_revalidation_never_reads_any_autoplay_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._multi_source_bundle(root)
            evidence = bundle["evidence"]
            document = decision_case_resolution.generate_resolution_document(
                bundle["cases"],
                bundle["results"],
                fixture_catalog=bundle["catalog"],
                trace_evidence=evidence,
                trace_path=None,
                audit_one=decision_case_replay._audit_one,
            )
            fixtures = root / "test_fixtures"
            fixtures.mkdir(exist_ok=True)

            def write_jsonl(path, rows):
                Path(path).write_text(
                    "".join(
                        json.dumps(
                            row, ensure_ascii=True, separators=(",", ":")
                        ) + "\n"
                        for row in rows
                    ),
                    encoding="utf-8",
                )

            write_jsonl(root / "decision-cases.jsonl", bundle["cases"])
            write_jsonl(
                fixtures / "decision-cases-v2.jsonl", fixture_cases()
            )
            manifest = decision_case_corpus.build_manifest(
                root / "decision-cases.jsonl", []
            )
            manifest_path = root / decision_case_corpus.DEFAULT_MANIFEST_NAME
            decision_case_corpus.write_manifest(manifest_path, manifest)
            _cases, corpus_evidence = decision_case_replay.load_case_corpus(
                root / "decision-cases.jsonl", manifest_path,
                with_evidence=True,
            )
            report = decision_case_replay.audit_cases(
                bundle["cases"],
                TARGET_HASH,
                fixture_cases=fixture_cases(),
                resolution_document=document,
                resolution_fixture_catalog=bundle["catalog"],
                trace_evidence=evidence,
                trace_path=None,
                case_corpus_evidence=corpus_evidence,
            )
            artifacts = {
                "decision-case-resolutions.json": document,
                "decision-case-trace-evidence.json": evidence,
                "decision-case-replay.json": report,
                "test_fixtures/decision-case-resolution-invariants-v1.json": (
                    bundle["catalog"]
                ),
            }
            for relative, value in artifacts.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        value, ensure_ascii=True, separators=(",", ":")
                    ),
                    encoding="utf-8",
                )
            bundle["trace"].unlink()
            (root / "autoplay.log").write_bytes(b"")
            original_open = Path.open

            def reject_trace_open(path, *args, **kwargs):
                if Path(path).name == "autoplay.log":
                    raise AssertionError("schema3 replay read an autoplay.log")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=reject_trace_open):
                result = decision_case_replay.revalidate_persisted_report(
                    root, report, TARGET_HASH
                )
            self.assertTrue(result["trace_prefix_verified"])
            self.assertEqual(
                "multi_source_embedded_gzip_v1",
                result["trace_evidence_mode"],
            )
            self.assertIs(
                result,
                freeze_manifest._validate_replay_revalidation(
                    result, report, root, TARGET_HASH
                ),
            )
            forged = copy.deepcopy(result)
            forged["trace_evidence_mode"] = "embedded_raw_lines_v1"
            with self.assertRaises(freeze_manifest.FreezeManifestError):
                freeze_manifest._validate_replay_revalidation(
                    forged, report, root, TARGET_HASH
                )

    @staticmethod
    def _production_fixture_cases():
        return [
            (
                row["fixture_id"],
                decision_case_resolution._build_probe_case(
                    decision_case_resolution._production_probe(
                        row["producer_probe"]
                    )
                ),
            )
            for row in decision_case_resolution._EXPECTED_FIXTURE_ROWS.values()
        ]

    @staticmethod
    def _effect_mutation_value(field):
        return {
            "hp_delta": 987654,
            "max_hp_delta": 987654,
            "gold_delta": 987654,
            "current_cost": {"gold": 987654, "hp": 0, "max_hp": 0},
            "card_changes": {
                "gain": [{"id": "FORGED_CARD"}], "remove": [],
                "upgrade": [], "transform": [],
            },
            "relic_changes": {
                "gain": [{"id": "FORGED_RELIC"}], "remove": [],
                "counter": [],
            },
            "potion_changes": {
                "gain": [{"id": "FORGED_POTION"}], "remove": [],
                "replace": [],
            },
            "probabilistic_outcomes": [{
                "probability": 1.0, "gold_delta": 987654,
            }],
        }[field]

    def _assert_case_blocks(self, value, label):
        result = decision_case_replay._audit_one(
            value, historical=False
        )
        self.assertNotEqual("audited", result["classification"], label)
        self.assertTrue(result["issues"] or result["unknowns"], label)

    def test_all_production_fixtures_detect_consequence_and_binding_mutations(self):
        effect_fields = (
            "hp_delta", "max_hp_delta", "gold_delta", "current_cost",
            "card_changes", "relic_changes", "potion_changes",
            "probabilistic_outcomes",
        )
        fixture_cases = self._production_fixture_cases()
        self.assertEqual(
            len(decision_case_resolution._EXPECTED_FIXTURE_ROWS),
            len(fixture_cases),
        )
        mutation_count = 0
        for fixture_id, value in fixture_cases:
            positive = decision_case_replay._audit_one(
                value, historical=False
            )
            self.assertEqual("audited", positive["classification"], (
                fixture_id, positive["issues"], positive["unknowns"]
            ))

            for field in effect_fields:
                producer_only = copy.deepcopy(value)
                producer_only["producer_candidates"][0].setdefault(
                    "consequences", {}
                )[field] = copy.deepcopy(
                    self._effect_mutation_value(field)
                )
                self._assert_case_blocks(
                    producer_only, (fixture_id, "producer", field)
                )
                mutation_count += 1

                canonical = copy.deepcopy(value)
                canonical["canonical_choices"][0]["consequences"][field] = (
                    copy.deepcopy(self._effect_mutation_value(field))
                )
                canonical["candidates"][0]["consequences"][field] = (
                    copy.deepcopy(self._effect_mutation_value(field))
                )
                if field == "probabilistic_outcomes":
                    canonical["canonical_choices"][0][
                        "probability_outcomes"
                    ] = copy.deepcopy(self._effect_mutation_value(field))
                    canonical["candidates"][0]["probability_outcomes"] = (
                        copy.deepcopy(self._effect_mutation_value(field))
                    )
                self._assert_case_blocks(
                    canonical, (fixture_id, "canonical_projected", field)
                )
                mutation_count += 1

            for field in (
                "hp_delta", "max_hp_delta", "gold_delta",
                "card_changes", "probabilistic_outcomes",
            ):
                synchronized = copy.deepcopy(value)
                producer = synchronized["producer_candidates"][0]
                producer.setdefault("consequences", {})[field] = copy.deepcopy(
                    self._effect_mutation_value(field)
                )
                choice = synchronized["canonical_choices"][0]
                candidate = synchronized["candidates"][0]
                choice["consequences"][field] = copy.deepcopy(
                    self._effect_mutation_value(field)
                )
                candidate["consequences"][field] = copy.deepcopy(
                    self._effect_mutation_value(field)
                )
                if field == "probabilistic_outcomes":
                    choice["probability_outcomes"] = copy.deepcopy(
                        self._effect_mutation_value(field)
                    )
                    candidate["probability_outcomes"] = copy.deepcopy(
                        self._effect_mutation_value(field)
                    )
                choice["producer_candidate_raw"] = copy.deepcopy(producer)
                choice["producer_consequence_raw"] = {
                    "present": True,
                    "value": copy.deepcopy(producer["consequences"]),
                }
                choice.setdefault("producer_consequence_claim", {})[field] = (
                    copy.deepcopy(self._effect_mutation_value(field))
                )
                partition = (
                    decision_case_replay._independent_producer_partition(
                        producer
                    )
                )
                for key, item in partition.items():
                    candidate[key] = copy.deepcopy(item)
                self._assert_case_blocks(
                    synchronized, (fixture_id, "all_three", field)
                )
                mutation_count += 1

            forged_target = copy.deepcopy(value)
            forged = {"kind": "event_option", "event_id": "FORGED"}
            forged_target["canonical_choices"][0]["target"] = copy.deepcopy(
                forged
            )
            forged_target["candidates"][0]["target"] = copy.deepcopy(forged)
            self._assert_case_blocks(
                forged_target, (fixture_id, "raw_target_binding")
            )
            mutation_count += 1

            settlement = copy.deepcopy(value)
            settlement["authoritative_choice_settlement"][
                "observed_outcome"
            ]["gold_delta"] = 987654
            self._assert_case_blocks(
                settlement, (fixture_id, "settlement_delta")
            )
            mutation_count += 1

            outcome = copy.deepcopy(value)
            outcome["decision_outcome"]["gold_delta"] = 987654
            self._assert_case_blocks(outcome, (fixture_id, "decision_outcome")
            )
            mutation_count += 1

        self.assertEqual(24 * len(fixture_cases), mutation_count)

    def test_empty_cage_uncertainty_metadata_is_narrowly_audited(self):
        baseline = decision_case_resolution._build_probe_case(
            decision_case_resolution._production_probe(
                "boss_relic_empty_cage"
            )
        )
        baseline_result = decision_case_replay._audit_one(
            baseline, historical=False
        )
        self.assertEqual(
            "audited", baseline_result["classification"], baseline_result
        )

        forged_probe = decision_case_resolution._production_probe(
            "boss_relic_empty_cage"
        )
        forged_probe["decision"]["candidates"][0]["consequences"][
            "uncertainty_classification"
        ]["status"] = "unresolved"
        forged = decision_case_resolution._build_probe_case(forged_probe)
        forged_result = decision_case_replay._audit_one(
            forged, historical=False
        )
        self.assertIn(
            "v2_producer_uncertainty_classification_contradicted",
            forged_result["issues"],
        )

        unknown_probe = decision_case_resolution._production_probe(
            "boss_relic_empty_cage"
        )
        unknown_probe["decision"]["candidates"][0]["consequences"][
            "forged_metadata"
        ] = {"status": "classified_future"}
        unknown = decision_case_resolution._build_probe_case(unknown_probe)
        unknown_result = decision_case_replay._audit_one(
            unknown, historical=False
        )
        self.assertNotEqual("audited", unknown_result["classification"])
        self.assertIn("candidates_missing", unknown_result["issues"])

    def test_sapphire_and_linked_relic_targets_require_exact_claims(self):
        probe = decision_case_resolution._production_probe("sapphire_link")
        value = decision_case_resolution._build_probe_case(probe)
        positive = decision_case_replay._audit_one(value, historical=False)
        self.assertEqual("audited", positive["classification"])
        self.assertEqual([], positive["issues"])
        self.assertEqual([], positive["unknowns"])

        key_row = next(
            row for row in value["producer_candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_sapphire_key"
        )
        linked_row = next(
            row for row in value["producer_candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_linked_relic"
        )
        self.assertEqual(4.0, key_row["score"])
        self.assertEqual(
            "combat_reward_key_opportunity_v2", key_row["score_rule_id"]
        )
        self.assertIs(False, key_row["selection_eligible"])
        self.assertEqual("DataDisk", linked_row["consequences"]["relic_id"])
        selected = next(
            row for row in value["canonical_choices"]
            if row["choice_id"] in value["selected_choice_ids"]
        )
        self.assertEqual(
            "gain_linked_relic", selected["consequences"]["operation"]
        )
        self.assertEqual(
            "DataDisk",
            selected["consequences"]["relic_changes"]["gain"][0]["id"],
        )

        invariant = decision_case_resolution.run_fixture_invariant(
            decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                "sapphire-consequence-v2"
            ],
            decision_case_replay._audit_one,
        )
        self.assertEqual("clear", invariant["status"])
        self.assertEqual(
            ["candidate", "protocol", "candidate+protocol"],
            invariant["positive_permutation_kinds"],
        )
        self.assertEqual(
            [invariant["selected_choice_ids"]] * 3,
            invariant["positive_permutation_selected_choice_ids"],
        )
        self.assertEqual(
            {
                "status": "clear",
                "authority": "production_sapphire_link_eligibility_v2",
                "ineligible_key_score": 4.0,
                "key_operation": "gain_sapphire_key",
                "linked_operation": "gain_linked_relic",
                "linked_relic_id": "DataDisk",
                "selected_choice_ids": invariant["selected_choice_ids"],
            },
            invariant["sapphire_link_contract"],
        )

        mutations = {}
        bad_key = copy.deepcopy(value)
        key_row = next(
            row for row in bad_key["producer_candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_sapphire_key"
        )
        key_row["consequences"]["operation"] = "collect_combat_reward"
        mutations["key_operation"] = (
            bad_key,
            {
                "v2_producer_consequence_target_binding",
                "v2_producer_operation_contradicted",
            },
        )

        bad_link_operation = copy.deepcopy(value)
        relic_row = next(
            row for row in bad_link_operation["producer_candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_linked_relic"
        )
        relic_row["consequences"]["operation"] = "collect_combat_reward"
        mutations["linked_operation"] = (
            bad_link_operation,
            {"v2_producer_operation_contradicted"},
        )

        bad_link = copy.deepcopy(value)
        relic_row = next(
            row for row in bad_link["producer_candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_linked_relic"
        )
        relic_row["consequences"]["relic_id"] = "Anchor"
        mutations["linked_relic_identity"] = (
            bad_link,
            {
                "v2_producer_consequence_target_binding",
                "v2_producer_relic_id_target_mismatch",
            },
        )

        for label, (mutation, expected_issues) in mutations.items():
            with self.subTest(label=label):
                result = decision_case_replay._audit_one(
                    mutation, historical=False
                )
                self.assertEqual("audited_issues", result["classification"])
                self.assertTrue(
                    expected_issues.issubset(set(result["issues"])),
                    result,
                )

    def test_plan_protection_negative_removes_veto_and_numeric_term(self):
        probe = decision_case_resolution._production_probe(
            "hand_select_plan_protection"
        )
        value = decision_case_resolution._mutated_probe_case(
            probe, "plan_protection_removed"
        )
        protected = next(
            row for row in value["candidates"]
            if (row.get("target") or {}).get("card_instance_id")
            == "hand-plan-protected-defend"
        )
        self.assertIs(True, protected["selection_eligible"])
        self.assertIsNone(protected["veto_reason"])
        self.assertNotIn(
            "bound_plan_preservation_value", protected["score_inputs"]
        )
        self.assertEqual(10.0, protected["score"])
        self.assertEqual(10.0, protected["local_score"])

        result = decision_case_replay._audit_one(
            value, historical=False
        )
        self.assertEqual("unresolved", result["classification"])
        self.assertEqual([], result["issues"])
        self.assertEqual(
            ["below_argmax_without_independent_review"],
            result["unknowns"],
        )

    def test_combat_reward_resource_preparation_is_exact_and_fail_closed(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "combat-reward-resource-preparation-score-v2"
        ]
        probe = decision_case_resolution._production_probe(
            fixture["producer_probe"]
        )
        value = decision_case_resolution._build_probe_case(probe)
        positive = decision_case_replay._audit_one(value, historical=False)
        self.assertEqual("audited", positive["classification"])
        self.assertEqual([], positive["issues"])
        self.assertEqual([], positive["unknowns"])
        self.assertEqual("COMBAT_REWARD", value["phase"])
        self.assertEqual(
            "resource_preparation", value["decision_surface_kind"]
        )
        self.assertIs(True, value["parent_choice_surface_pending"])

        before_game = value["authoritative_state_before"]["game_state"]
        after_game = value["authoritative_state_after"]["game_state"]
        self.assertEqual(
            before_game["screen_state"]["rewards"],
            after_game["screen_state"]["rewards"],
        )
        reward_potion = before_game["screen_state"]["rewards"][0]["potion"]
        self.assertEqual("LiquidMemories", reward_potion["id"])
        self.assertNotIn(
            "LiquidMemories", {row["id"] for row in after_game["potions"]}
        )

        held = before_game["potions"]
        self.assertEqual([0, 1, 2], [row["slot"] for row in held])
        self.assertEqual(3, len({row["potion_instance_id"] for row in held}))
        selected_id = value["selected_choice_ids"][0]
        selected = next(
            row for row in value["canonical_choices"]
            if row["choice_id"] == selected_id
        )
        selected_instance = selected["target"]["potion_instance_id"]
        self.assertEqual(held[1]["potion_instance_id"], selected_instance)
        self.assertEqual(1, selected["target"]["slot"])
        removed = value["decision_outcome"]["potions"]["removed"]
        self.assertEqual([selected_instance], [
            row["potion_instance_id"] for row in removed
        ])

        producer = value["producer_candidates"]
        self.assertEqual(
            [10.0, 20.0, 30.0, 30.0],
            sorted(row["score_inputs"]["held_keep_value"] for row in producer),
        )
        self.assertEqual(
            {"LiquidMemories"},
            {
                row["consequences"]["bound_new_potion_id"]
                for row in producer
            },
        )
        selected_producer = next(
            row for row in producer
            if row["consequences"]["potion_instance_id"] == selected_instance
        )
        self.assertEqual(10.0, selected_producer["score_inputs"][
            "held_keep_value"
        ])
        self.assertEqual(30.0, selected_producer["score"])

    def test_shop_resource_preparation_uses_fresh_typed_parent_joins(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "shop-resource-preparation-score-v2"
        ]
        invariant = decision_case_resolution.run_fixture_invariant(
            fixture, decision_case_replay._audit_one
        )
        self.assertTrue(
            decision_case_replay.resolution_permutation_invariant_is_valid(
                invariant
            )
        )
        contract = invariant[
            "shop_resource_preparation_permutation_contract"
        ]
        self.assertEqual(4, contract["producer_execution_count"])
        self.assertEqual(
            ["LiquidMemories", "LiquidMemories"],
            contract["baseline_surface_order"]["parent_potions"],
        )
        self.assertEqual(1, contract["selected_potion_slot"])
        self.assertEqual(
            "LiquidMemories", contract["bound_parent_potion_id"]
        )
        self.assertEqual(
            [1, 1, 0, 0],
            [
                contract["baseline_selection_binding"][
                    "bound_parent_choice_index"
                ],
                *[
                    row["selection_binding"]["bound_parent_choice_index"]
                    for row in contract["production_variants"]
                ],
            ],
        )
        controls = {
            row["kind"]: row
            for row in contract["typed_parent_negative_controls"]
        }
        self.assertEqual(
            {"missing_bound_parent", "ambiguous_bound_parent"}, set(controls)
        )
        for row in controls.values():
            self.assertIn(
                "v2_producer_bound_new_potion_id_not_protocol_visible",
                row["problem_kinds"],
            )

        mutations = []
        wrong_parent = copy.deepcopy(invariant)
        wrong_parent["shop_resource_preparation_permutation_contract"][
            "bound_parent_potion_id"
        ] = "BlockPotion"
        mutations.append(wrong_parent)
        missing_control = copy.deepcopy(invariant)
        missing_control["shop_resource_preparation_permutation_contract"][
            "typed_parent_negative_controls"
        ].pop()
        mutations.append(missing_control)
        reused_execution = copy.deepcopy(invariant)
        reused_execution["shop_resource_preparation_permutation_contract"][
            "producer_execution_digests"
        ][1] = reused_execution[
            "shop_resource_preparation_permutation_contract"
        ]["producer_execution_digests"][0]
        mutations.append(reused_execution)
        for mutation in mutations:
            self.assertFalse(
                decision_case_replay.resolution_permutation_invariant_is_valid(
                    mutation
                )
            )

        # Keep the actual producer's top-level selected listing intact while
        # switching every child mirror to the other visible same-id parent.
        # An id-only join would accept this; the listing tuple must reject it.
        probe = decision_case_resolution._production_probe(
            fixture["producer_probe"], production_variant="baseline"
        )
        baseline_case = decision_case_resolution._build_probe_case(probe)
        actual = baseline_case["resource_parent_listing"]
        other = next(
            row for row in baseline_case["available_options"]
            if row.get("option_id") != actual["listing_id"]
            and (row.get("target") or {}).get("kind") == "potion"
        )
        other_item = (other.get("target") or {}).get("item") or {}
        wrong_probe = copy.deepcopy(probe)
        for candidate in wrong_probe["decision"]["candidates"]:
            consequence = candidate["consequences"]
            consequence.update({
                "bound_purchase_listing_id": other["option_id"],
                "bound_purchase_choice_index": other["choice_index"],
                "bound_purchase_item_id": other_item["id"],
                "bound_purchase_price": other_item["price"],
                "bound_new_potion_id": other_item["id"],
            })
        wrong_case = decision_case_resolution._build_probe_case(wrong_probe)
        wrong_result = decision_case_replay._audit_one(
            wrong_case, historical=False
        )
        self.assertIn(
            wrong_result["classification"], {"unresolved", "audited_issues"}
        )
        self.assertIn(
            "v2_producer_bound_purchase_listing_id_not_protocol_visible",
            decision_case_resolution.problem_kinds(wrong_result),
        )

    def test_bowl_strictly_dominates_return_but_no_other_score_tie(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "card_reward_bowl_dominates_skip_v1"
        ]
        probe = decision_case_resolution._production_probe(
            fixture["producer_probe"]
        )
        value = decision_case_resolution._build_probe_case(probe)
        result = decision_case_replay._audit_one(value, historical=False)
        self.assertEqual("audited", result["classification"])
        self.assertEqual([], result["issues"])
        self.assertEqual([], result["unknowns"])
        observed = value["authoritative_choice_settlement"][
            "observed_outcome"
        ]
        self.assertEqual(2, observed["current_hp_delta"])
        self.assertEqual(2, observed["max_hp_delta"])

        candidates = {
            row["choice_id"]: row for row in value["candidates"]
        }
        selected, reason = decision_case_replay._score_selection(
            candidates, 1, case=value
        )
        self.assertIsNone(reason)
        self.assertEqual(value["selected_choice_ids"], selected)
        event_case = copy.deepcopy(value)
        event_case["phase"] = "EVENT"
        picked, failure = decision_case_replay._score_selection(
            candidates, 1, case=event_case
        )
        self.assertIsNone(picked)
        self.assertEqual(
            "score_cutoff_tie_is_semantically_ambiguous", failure
        )
        mutations = []
        no_bowl_gain = copy.deepcopy(candidates)
        next(
            row for row in no_bowl_gain.values()
            if (row.get("target") or {}).get("kind") == "bowl"
        )["consequences"]["max_hp_delta"] = 0
        mutations.append(no_bowl_gain)
        costly_bowl = copy.deepcopy(candidates)
        next(
            row for row in costly_bowl.values()
            if (row.get("target") or {}).get("kind") == "bowl"
        )["consequences"]["current_cost"]["gold"] = 1
        mutations.append(costly_bowl)
        third_tie = copy.deepcopy(candidates)
        card = next(
            row for row in third_tie.values()
            if (row.get("target") or {}).get("kind") == "card"
        )
        card["score"] = next(
            row["score"] for row in third_tie.values()
            if (row.get("target") or {}).get("kind") == "bowl"
        )
        mutations.append(third_tie)
        for mutation in mutations:
            picked, failure = decision_case_replay._score_selection(
                mutation, 1, case=value
            )
            self.assertIsNone(picked)
            self.assertEqual(
                "score_cutoff_tie_is_semantically_ambiguous", failure
            )

        unobserved_gain = copy.deepcopy(value)
        before_game = unobserved_gain["authoritative_state_before"][
            "game_state"
        ]
        after_game = unobserved_gain["authoritative_state_after"][
            "game_state"
        ]
        after_game["current_hp"] = before_game["current_hp"]
        after_game["max_hp"] = before_game["max_hp"]
        for envelope in (
            unobserved_gain["decision_outcome"],
            unobserved_gain["authoritative_choice_settlement"][
                "observed_outcome"
            ],
        ):
            envelope["current_hp_delta"] = 0
            envelope["hp_delta"] = 0
            envelope["max_hp_delta"] = 0
        unobserved_result = decision_case_replay._audit_one(
            unobserved_gain, historical=False
        )
        self.assertEqual("unresolved", unobserved_result["classification"])
        self.assertIn(
            "score_cutoff_tie_is_semantically_ambiguous",
            unobserved_result["unknowns"],
        )

        invariant = decision_case_resolution.run_fixture_invariant(
            fixture, decision_case_replay._audit_one
        )
        self.assertTrue(
            decision_case_replay.resolution_permutation_invariant_is_valid(
                invariant
            )
        )
        self.assertIn(
            "score_cutoff_tie_is_semantically_ambiguous",
            invariant["negative_problem_kinds"],
        )
        forged = copy.deepcopy(invariant)
        forged["bowl_dominance_contract"]["observed_max_hp_delta"] = 0
        self.assertFalse(
            decision_case_replay.resolution_permutation_invariant_is_valid(
                forged
            )
        )

    def test_combat_resource_preparation_full_invariant_contract(self):
        # Retain the pre-existing full combat-resource invariant assertions
        # independently from the baseline producer-shape test above.
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "combat-reward-resource-preparation-score-v2"
        ]
        probe = decision_case_resolution._production_probe(
            fixture["producer_probe"]
        )
        value = decision_case_resolution._build_probe_case(probe)
        selected_id = value["selected_choice_ids"][0]
        selected = next(
            row for row in value["canonical_choices"]
            if row["choice_id"] == selected_id
        )
        selected_instance = selected["target"]["potion_instance_id"]
        invariant = decision_case_resolution.run_fixture_invariant(
            fixture, decision_case_replay._audit_one
        )
        self.assertEqual("clear", invariant["status"])
        contract = invariant["resource_preparation_permutation_contract"]
        self.assertEqual("clear", contract["status"])
        self.assertEqual(selected_instance, contract[
            "selected_potion_instance_id"
        ])
        self.assertEqual(1, contract["selected_potion_slot"])
        self.assertEqual(
            "fresh_live_producer_per_variant_v1", contract["authority"]
        )
        self.assertEqual(4, contract["producer_execution_count"])
        self.assertEqual(
            ["baseline", "candidate", "protocol", "candidate+protocol"],
            contract["producer_execution_variants"],
        )
        self.assertIs(False, contract["parent_reward_consumed"])
        baseline_surface = contract["baseline_surface_order"]
        baseline_instances = baseline_surface[
            "authoritative_held_instance_ids"
        ]
        self.assertEqual(
            contract["baseline_selection_binding"]["potion_slot"],
            baseline_instances.index(selected_instance),
        )
        self.assertEqual(4, len(set(contract["producer_execution_nonces"])))
        self.assertEqual(4, len(set(contract["producer_execution_digests"])))
        for index, variant in enumerate(contract["production_variants"]):
            kind = variant["kind"]
            self.assertEqual(kind, variant["producer_execution_variant"])
            binding = variant["selection_binding"]
            self.assertEqual(selected_instance, binding[
                "potion_instance_id"
            ])
            variant_held = variant["surface_order"]["held"]
            self.assertEqual(
                [
                    child
                    for group in reversed(
                        baseline_surface["held_groups"]
                    )
                    for child in group
                ]
                if kind in {"candidate", "candidate+protocol"}
                else baseline_surface["held"],
                variant_held,
            )
            for surface in (
                "resource", "canonical", "candidate", "producer",
            ):
                self.assertEqual(
                    variant_held, variant["surface_order"][surface]
                )
            reward_order = variant["surface_order"]["parent_rewards"]
            self.assertEqual(
                ["gold", "potion"]
                if kind in {"protocol", "candidate+protocol"}
                else ["potion", "gold"],
                reward_order,
            )
            self.assertEqual(
                reward_order, variant["surface_order"]["protocol_options"]
            )
            self.assertEqual(
                1 if kind in {"protocol", "candidate+protocol"} else 0,
                binding["bound_reward_choice_index"],
            )
            instance_ids = variant["surface_order"][
                "authoritative_held_instance_ids"
            ]
            self.assertEqual(1, instance_ids.count(selected_instance))
            self.assertEqual(
                instance_ids.index(selected_instance),
                binding["potion_slot"],
            )
            self.assertEqual(
                contract["producer_execution_nonces"][index + 1],
                variant["producer_execution_nonce"],
            )
            self.assertEqual(
                contract["producer_execution_digests"][index + 1],
                variant["producer_execution_digest"],
            )
            self.assertEqual(
                invariant["positive_permutation_sha256s"][index],
                variant["case_sha256"],
            )
        self.assertEqual(
            [invariant["selected_choice_ids"]] * 3,
            invariant["positive_permutation_selected_choice_ids"],
        )
        self.assertTrue(
            decision_case_replay.resolution_permutation_invariant_is_valid(
                invariant
            )
        )

        tie = decision_case_resolution._mutated_probe_case(
            probe, "resource_score_tie"
        )
        tie_result = decision_case_replay._audit_one(tie, historical=False)
        self.assertEqual(
            fixture["expected_negative_classification"],
            tie_result["classification"],
        )
        self.assertEqual(
            fixture["expected_negative_problem_kinds"],
            decision_case_resolution.problem_kinds(tie_result),
        )

        other_id = next(
            row["choice_id"] for row in value["canonical_choices"]
            if row["choice_id"] != selected_id
        )
        binding_mutations = {}
        wrong_selected = copy.deepcopy(value)
        wrong_selected["selected_choice_ids"] = [other_id]
        binding_mutations["selected"] = (
            wrong_selected, "v2_settlement_choice_id_binding"
        )
        wrong_final = copy.deepcopy(value)
        wrong_final["final_choice_ids"] = [other_id]
        binding_mutations["final"] = (wrong_final, "final_choice_binding")
        wrong_requested = copy.deepcopy(value)
        wrong_requested["requested_target_id"] = other_id
        binding_mutations["requested"] = (
            wrong_requested, "v2_target_binding"
        )
        for label, (mutation, expected_issue) in binding_mutations.items():
            with self.subTest(binding=label):
                result = decision_case_replay._audit_one(
                    mutation, historical=False
                )
                self.assertEqual("audited_issues", result["classification"])
                self.assertIn(expected_issue, result["issues"])

    def test_combat_reward_permutations_call_four_fresh_live_producers(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "combat-reward-resource-preparation-score-v2"
        ]
        modules = decision_case_resolution._load_production_probe_modules()
        agent_class = modules["SimpleAgent"]
        real_action = agent_class.get_next_action_in_game
        real_probe = decision_case_resolution._production_probe
        live_calls = []
        probe_variants = []

        def counted_action(agent, game):
            action = real_action(agent, game)
            # Strong references deliberately survive the whole assertion;
            # object ids cannot be recycled between the four executions.
            live_calls.append((agent, game, game.screen, action))
            return action

        def counted_probe(probe_id, *, production_variant=None):
            probe_variants.append(production_variant)
            return real_probe(
                probe_id, production_variant=production_variant
            )

        with mock.patch.object(
            agent_class,
            "get_next_action_in_game",
            autospec=True,
            side_effect=counted_action,
        ), mock.patch.object(
            decision_case_resolution,
            "_production_probe",
            side_effect=counted_probe,
        ):
            invariant = decision_case_resolution.run_fixture_invariant(
                fixture, decision_case_replay._audit_one
            )

        self.assertEqual(
            ["baseline", "candidate", "protocol", "candidate+protocol"],
            probe_variants,
        )
        self.assertEqual(4, len(live_calls))
        for column in range(4):
            objects = [row[column] for row in live_calls]
            self.assertEqual(4, len({id(value) for value in objects}))
            for left in range(4):
                for right in range(left + 1, 4):
                    self.assertIsNot(objects[left], objects[right])
        self.assertEqual(
            4,
            invariant["resource_preparation_permutation_contract"][
                "producer_execution_count"
            ],
        )

    def test_resource_preparation_receipt_and_vetoed_use_fail_closed(self):
        probe = decision_case_resolution._production_probe(
            "combat_reward_resource_preparation",
            production_variant="baseline",
        )
        value = decision_case_resolution._build_probe_case(probe)
        self.assertEqual(
            "audited",
            decision_case_replay._audit_one(
                value, historical=False
            )["classification"],
        )

        selected_id = value["selected_choice_ids"][0]
        selected = next(
            row for row in value["canonical_choices"]
            if row["choice_id"] == selected_id
        )
        selected_instance = selected["target"]["potion_instance_id"]
        other_instance = next(
            row["target"]["potion_instance_id"]
            for row in value["canonical_choices"]
            if row["target"]["potion_instance_id"] != selected_instance
        )
        use_id = next(
            row["choice_id"] for row in value["canonical_choices"]
            if row.get("operation") == "use"
        )

        receipt_mutations = {}
        typed_receipt = copy.deepcopy(value)
        typed_receipt["requested_target_id"] = selected_id
        typed_receipt["resolved_target_id"] = selected_id
        typed_receipt["chosen"]["requested_target_id"] = selected_id
        typed_receipt["chosen"]["resolved_target_id"] = selected_id
        receipt_mutations["typed_child_receipt"] = typed_receipt

        wrong_instance = copy.deepcopy(value)
        wrong_instance["requested_target_id"] = other_instance
        wrong_instance["resolved_target_id"] = other_instance
        wrong_instance["chosen"]["requested_target_id"] = other_instance
        wrong_instance["chosen"]["resolved_target_id"] = other_instance
        receipt_mutations["wrong_parent_instance"] = wrong_instance

        top_chosen_mismatch = copy.deepcopy(value)
        top_chosen_mismatch["chosen"]["requested_target_id"] = other_instance
        top_chosen_mismatch["chosen"]["resolved_target_id"] = other_instance
        receipt_mutations["top_chosen_mismatch"] = top_chosen_mismatch

        wrong_settlement = copy.deepcopy(value)
        wrong_settlement["authoritative_choice_settlement"][
            "choice_id"
        ] = use_id
        receipt_mutations["wrong_settlement_child"] = wrong_settlement

        for label, mutation in receipt_mutations.items():
            with self.subTest(receipt=label):
                result = decision_case_replay._audit_one(
                    mutation, historical=False
                )
                self.assertEqual("audited_issues", result["classification"])
                self.assertTrue(result["issues"], result)

        def vetoed_use_rows(case):
            choice = next(
                row for row in case["canonical_choices"]
                if row["choice_id"] == use_id
            )
            candidate = next(
                row for row in case["candidates"]
                if row["choice_id"] == use_id
            )
            producer = next(
                row for row in case["producer_candidates"]
                if row.get("operation") == "use"
            )
            return choice, candidate, producer

        na_mutations = {}
        eligible = copy.deepcopy(value)
        for row in vetoed_use_rows(eligible):
            row["selection_eligible"] = True
        na_mutations["eligible"] = eligible

        selected_use = copy.deepcopy(value)
        choice, _candidate, _producer = vetoed_use_rows(selected_use)
        choice["selected"] = True
        na_mutations["selected"] = selected_use

        missing_veto = copy.deepcopy(value)
        for row in vetoed_use_rows(missing_veto):
            row["veto_reason"] = None
        na_mutations["missing_veto"] = missing_veto

        partial_numeric = copy.deepcopy(value)
        choice, candidate, _producer = vetoed_use_rows(partial_numeric)
        choice["consequences"]["hp_delta"] = 0
        candidate["consequences"]["hp_delta"] = 0
        na_mutations["partial_numeric"] = partial_numeric

        knowledge_drift = copy.deepcopy(value)
        choice, candidate, _producer = vetoed_use_rows(knowledge_drift)
        for row in (choice, candidate):
            row["consequences"]["field_knowledge"]["hp_delta"][
                "status"
            ] = "known"
        na_mutations["knowledge_drift"] = knowledge_drift

        producer_veto_drift = copy.deepcopy(value)
        choice, candidate, producer = vetoed_use_rows(
            producer_veto_drift
        )
        producer["veto_reason"] = "different_veto"
        choice["producer_candidate_raw"]["veto_reason"] = (
            "different_veto"
        )
        candidate["producer_candidate_raw"]["veto_reason"] = (
            "different_veto"
        )
        na_mutations["producer_veto_drift"] = producer_veto_drift

        for label, mutation in na_mutations.items():
            with self.subTest(numeric_na=label):
                result = decision_case_replay._audit_one(
                    mutation, historical=False
                )
                self.assertNotEqual("audited", result["classification"])
                self.assertTrue(
                    result["issues"] or result["unknowns"], result
                )

        missing_child = copy.deepcopy(value)
        for surface in (
            "resource_preparation_options", "canonical_choices",
            "candidates", "producer_candidates",
        ):
            missing_child[surface] = [
                row for row in missing_child[surface]
                if (
                    row.get("option_id")
                    or row.get("choice_id")
                ) != use_id
            ]
        result = decision_case_replay._audit_one(
            missing_child, historical=False
        )
        self.assertEqual("audited_issues", result["classification"])
        self.assertIn(
            "v2_resource_preparation_choice_bijection",
            result["issues"],
        )

        duplicate_child = copy.deepcopy(value)
        for surface in (
            "resource_preparation_options", "canonical_choices",
            "candidates", "producer_candidates",
        ):
            row = next(
                item for item in duplicate_child[surface]
                if (
                    (item.get("option_id") or item.get("choice_id"))
                    == use_id
                    or (
                        surface == "producer_candidates"
                        and item.get("operation") == "use"
                    )
                )
            )
            duplicate_child[surface].append(copy.deepcopy(row))
        result = decision_case_replay._audit_one(
            duplicate_child, historical=False
        )
        self.assertEqual("audited_issues", result["classification"])
        self.assertTrue(
            {
                "duplicate_choice_id",
                "v2_choice_typed_key_duplicate",
                "v2_resource_preparation_choice_bijection",
            }
            & set(result["issues"]),
            result,
        )

    def test_staged_e1_resource_preparation_case_is_now_audited(self):
        archive = (
            Path(__file__).resolve().parents[1]
            / "artifact-staging"
            / "new-six-b3951d2d4031632b"
            / "decision-case-corpus"
            / (
                "e1deeec6-9d46-4015-acf8-ccb9672244f0-"
                "bcd8ac111c47d70d.jsonl.gz"
            )
        )
        if not archive.exists():
            self.skipTest("bounded staged e1 decision-case shard unavailable")
        import gzip

        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            matches = [
                json.loads(line) for line in handle
                if '"before_seq":240848' in line
            ]
        self.assertEqual(1, len(matches))
        value = matches[0]
        self.assertEqual(
            "11934d63edab2607e61663754fbae2167da2b96dee151a5556f4ddd53afbfbf3",
            decision_case_resolution.object_digest(value),
        )
        result = decision_case_replay._audit_one(value, historical=False)
        self.assertEqual("audited", result["classification"])
        self.assertEqual([], result["issues"])
        self.assertEqual([], result["unknowns"])

    def test_order_dependent_fake_live_producer_is_rejected(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "combat-reward-resource-preparation-score-v2"
        ]
        real_probe = decision_case_resolution._production_probe

        def order_dependent_probe(probe_id, *, production_variant=None):
            probe = real_probe(
                probe_id, production_variant=production_variant
            )
            if production_variant != "candidate":
                return probe
            rows = probe["decision"]["candidates"]
            wrong = rows[0]
            prior = next(
                row for row in rows
                if row["reason"] == "selected_resource_preparation_operation"
            )
            # Make the order-dependent result internally self-consistent so a
            # standalone canonical audit passes.  The multi-run invariant
            # must still reject its changed immutable selection.
            for field in ("score", "score_inputs", "score_components"):
                wrong[field], prior[field] = (
                    copy.deepcopy(prior[field]),
                    copy.deepcopy(wrong[field]),
                )
            wrong["reason"] = wrong["local_reason"] = (
                "selected_resource_preparation_operation"
            )
            wrong["reason_codes"] = [
                "selected_resource_preparation_operation"
            ]
            prior["reason"] = prior["local_reason"] = (
                "visible_resource_preparation_alternative"
            )
            prior["reason_codes"] = [
                "visible_resource_preparation_alternative"
            ]
            probe["decision"]["chosen"] = (
                "discard:" + wrong["consequences"]["potion_id"]
            )
            probe["decision"]["chosen_id"] = wrong["choice_id"]
            probe["selected_payload"] = {
                "action": "potion",
                "operation": "discard",
                "potion_instance_id": wrong["consequences"][
                    "potion_instance_id"
                ],
            }
            standalone = decision_case_resolution._build_probe_case(probe)
            result = decision_case_replay._audit_one(
                standalone, historical=False
            )
            self.assertEqual("audited", result["classification"])
            return probe

        with mock.patch.object(
            decision_case_resolution,
            "_production_probe",
            side_effect=order_dependent_probe,
        ):
            with self.assertRaisesRegex(
                decision_case_resolution.ResolutionError,
                "producer container order changed choice",
            ):
                decision_case_resolution.run_fixture_invariant(
                    fixture, decision_case_replay._audit_one
                )

    def test_replay_rejects_forged_fresh_producer_contract(self):
        invariant = decision_case_resolution.run_fixture_invariant(
            decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                "combat-reward-resource-preparation-score-v2"
            ],
            decision_case_replay._audit_one,
        )
        validator = (
            decision_case_replay.resolution_permutation_invariant_is_valid
        )
        self.assertTrue(validator(invariant))
        mutations = []
        missing_contract = copy.deepcopy(invariant)
        missing_contract.pop("resource_preparation_permutation_contract")
        mutations.append(missing_contract)
        fake_call_count = copy.deepcopy(invariant)
        fake_call_count["resource_preparation_permutation_contract"][
            "producer_execution_count"
        ] = 1
        mutations.append(fake_call_count)
        reused_digest = copy.deepcopy(invariant)
        reused_digest["resource_preparation_permutation_contract"][
            "production_variants"
        ][1]["case_sha256"] = invariant["positive_case_sha256"]
        mutations.append(reused_digest)
        wrong_reward_index = copy.deepcopy(invariant)
        wrong_reward_index["resource_preparation_permutation_contract"][
            "production_variants"
        ][1]["selection_binding"]["bound_reward_choice_index"] = 0
        mutations.append(wrong_reward_index)
        wrong_variant_slot = copy.deepcopy(invariant)
        wrong_variant_slot["resource_preparation_permutation_contract"][
            "production_variants"
        ][0]["selection_binding"]["potion_slot"] = 0
        mutations.append(wrong_variant_slot)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(validator(mutation))

    def test_current_regression_resolution_is_recomputed_and_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, catalog, document = build_bundle(directory)
            report = audit_bundle(value, trace, evidence, catalog, document)
        self.assertEqual("clear", report["status"])
        self.assertEqual(1, report["resolved_case_count"])
        self.assertEqual(0, report["unresolved_case_count"])
        self.assertEqual(0, report["issue_case_count"])
        self.assertEqual("resolved", report["case_results"][0]["classification"])
        resolution = report["case_results"][0]["resolution"]
        self.assertEqual(TARGET_HASH, resolution["target_decision_hash"])
        self.assertEqual("clear", resolution["invariant"]["status"])
        self.assertEqual(
            3, resolution["invariant"]["positive_permutation_count"]
        )
        self.assertEqual(
            3,
            len(resolution["invariant"][
                "positive_permutation_sha256s"
            ]),
        )
        self.assertEqual(
            "match_keep", resolution["invariant"]["producer_probe"]
        )
        self.assertNotEqual(
            resolution["invariant"]["positive_case_sha256"],
            resolution["invariant"]["reordered_case_sha256"],
        )

    def test_permutation_specs_are_axis_aware(self):
        self.assertEqual(
            [], decision_case_resolution._permutation_specs(0, 1)
        )
        self.assertEqual(
            [("candidate", True, False)],
            decision_case_resolution._permutation_specs(2, 1),
        )
        self.assertEqual(
            [("protocol", False, True)],
            decision_case_resolution._permutation_specs(1, 2),
        )
        self.assertEqual(
            [
                ("candidate", True, False),
                ("protocol", False, True),
                ("candidate+protocol", True, True),
            ],
            decision_case_resolution._permutation_specs(2, 2),
        )

    def test_single_protocol_option_only_marks_protocol_axis_na(self):
        for fixture_id in (
            "chest-consequence-v2",
            "shop-room-consequence-v2",
            "combat-reward-consequence-v2",
        ):
            with self.subTest(fixture_id=fixture_id):
                invariant = decision_case_resolution.run_fixture_invariant(
                    decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                        fixture_id
                    ],
                    decision_case_replay._audit_one,
                )
                self.assertEqual(2, invariant["candidate_count"])
                self.assertEqual(1, invariant["protocol_count"])
                self.assertEqual(
                    "clear",
                    invariant["permutation_axes"]["candidate"]["status"],
                )
                self.assertEqual(
                    "not_applicable",
                    invariant["permutation_axes"]["protocol"]["status"],
                )
                self.assertEqual(1, invariant["positive_permutation_count"])
                self.assertEqual(
                    ["candidate"],
                    invariant["positive_permutation_kinds"],
                )
                self.assertEqual(
                    [invariant["selected_choice_ids"]],
                    invariant[
                        "positive_permutation_selected_choice_ids"
                    ],
                )
                self.assertNotEqual(
                    invariant["positive_case_sha256"],
                    invariant["reordered_case_sha256"],
                )
                validator = (
                    decision_case_replay.resolution_permutation_invariant_is_valid
                )
                self.assertTrue(validator(invariant))

    def test_two_axis_fixture_has_three_real_distinct_permutations(self):
        invariant = decision_case_resolution.run_fixture_invariant(
            decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                "event-match-candidates-missing-v2"
            ],
            decision_case_replay._audit_one,
        )
        self.assertEqual(4, invariant["candidate_count"])
        self.assertEqual(4, invariant["protocol_count"])
        self.assertEqual(
            ["candidate", "protocol", "candidate+protocol"],
            invariant["positive_permutation_kinds"],
        )
        digests = invariant["positive_permutation_sha256s"]
        self.assertEqual(3, len(set(digests)))
        self.assertNotIn(invariant["positive_case_sha256"], digests)
        self.assertEqual(
            [invariant["selected_choice_ids"]] * 3,
            invariant["positive_permutation_selected_choice_ids"],
        )

    def test_permutation_generation_rejects_fake_digest_or_selection(self):
        fixture = decision_case_resolution._EXPECTED_FIXTURE_ROWS[
            "event-match-candidates-missing-v2"
        ]
        with mock.patch.object(
            decision_case_resolution,
            "object_digest",
            return_value="a" * 64,
        ):
            with self.assertRaisesRegex(
                decision_case_resolution.ResolutionError,
                "permutations are not distinct",
            ):
                decision_case_resolution.run_fixture_invariant(
                    fixture, decision_case_replay._audit_one
                )

        real_builder = decision_case_resolution._build_probe_case

        def changed_selection(probe, **kwargs):
            value = real_builder(probe, **kwargs)
            if kwargs.get("reverse_candidates"):
                value["selected_choice_ids"] = ["option:forged"]
            return value

        def audit_clear(_case, *, historical):
            self.assertFalse(historical)
            return {"classification": "audited", "issues": [], "unknowns": []}

        with mock.patch.object(
            decision_case_resolution,
            "_build_probe_case",
            side_effect=changed_selection,
        ):
            with self.assertRaisesRegex(
                decision_case_resolution.ResolutionError,
                "container order changed choice",
            ):
                decision_case_resolution.run_fixture_invariant(
                    fixture, audit_clear
                )

    def test_replay_rejects_forged_axis_permutation_evidence(self):
        invariant = decision_case_resolution.run_fixture_invariant(
            decision_case_resolution._EXPECTED_FIXTURE_ROWS[
                "event-match-candidates-missing-v2"
            ],
            decision_case_replay._audit_one,
        )
        mutations = []
        fake_cardinality = copy.deepcopy(invariant)
        fake_cardinality["candidate_count"] = 1
        mutations.append(fake_cardinality)
        duplicate_digest = copy.deepcopy(invariant)
        duplicate_digest["positive_permutation_sha256s"][1] = (
            duplicate_digest["positive_permutation_sha256s"][0]
        )
        mutations.append(duplicate_digest)
        missing_both = copy.deepcopy(invariant)
        missing_both["positive_permutation_kinds"][-1] = "candidate"
        mutations.append(missing_both)
        changed_selection = copy.deepcopy(invariant)
        changed_selection[
            "positive_permutation_selected_choice_ids"
        ][1] = ["option:forged"]
        mutations.append(changed_selection)
        validator = (
            decision_case_replay.resolution_permutation_invariant_is_valid
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(validator(mutation))

    def test_static_resolution_has_no_target_hash_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            _value, _trace, _evidence, _catalog, document = build_bundle(
                directory
            )
        self.assertNotIn("target_decision_hash", document)
        self.assertNotIn(TARGET_HASH, json.dumps(document, sort_keys=True))

    def test_trace_prefix_allows_append_but_rejects_same_length_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, catalog, document = build_bundle(directory)
            trace.write_bytes(trace.read_bytes() + b'{"later":true}\n')
            appended = audit_bundle(value, trace, evidence, catalog, document)
            self.assertEqual("clear", appended["status"])

            raw = bytearray(trace.read_bytes())
            raw[0] = ord("[") if raw[0] != ord("[") else ord("{")
            trace.write_bytes(bytes(raw))
            tampered = audit_bundle(value, trace, evidence, catalog, document)
            self.assertEqual("issues", tampered["status"])
            self.assertEqual(0, tampered["resolved_case_count"])
            self.assertEqual(
                "decision_case_resolution_invalid",
                tampered["resolution_evidence"]["failures"][0]["kind"],
            )

    def test_sidecar_binding_tampering_fails_closed(self):
        mutations = {
            "case_digest": lambda document: document["resolutions"][0].update(
                original_case_sha256="0" * 64
            ),
            "sequence": lambda document: document["resolutions"][0].update(
                before_seq=78
            ),
            "problem_class": lambda document: document["resolutions"][0].update(
                original_problem_kinds=["invented_problem"]
            ),
            "fixture_digest": lambda document: document["resolutions"][0].update(
                fixture_sha256="1" * 64
            ),
            "fixture_id": lambda document: document["resolutions"][0].update(
                fixture_id="event-score-tie-v2"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    value, trace, evidence, catalog, document = build_bundle(
                        directory
                    )
                    mutate(document)
                    report = audit_bundle(
                        value, trace, evidence, catalog, document
                    )
                self.assertEqual("issues", report["status"])
                self.assertEqual(0, report["resolved_case_count"])

    def test_tampered_trace_or_fixture_catalog_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, catalog, document = build_bundle(directory)
            bad_evidence = copy.deepcopy(evidence)
            bad_evidence["records"][0]["trace_record_sha256"] = "2" * 64
            report = audit_bundle(
                value, trace, bad_evidence, catalog, document
            )
            self.assertEqual("issues", report["status"])

        with tempfile.TemporaryDirectory() as directory:
            value, trace, evidence, catalog, document = build_bundle(directory)
            bad_catalog = copy.deepcopy(catalog)
            bad_catalog["fixtures"][0]["producer_probe"] = "self_reported_pass"
            document["fixture_catalog_sha256"] = (
                decision_case_resolution.object_digest(bad_catalog)
            )
            report = audit_bundle(
                value, trace, evidence, bad_catalog, document
            )
            self.assertEqual("issues", report["status"])

    def test_original_candidate_or_score_mutation_invalidates_resolution(self):
        for invariant, mutate in (
            (
                "legacy_score_tie",
                lambda value: value["candidates"].pop(),
            ),
            (
                "legacy_score_tie",
                lambda value: value["candidates"][0].update(score=9.0),
            ),
        ):
            with self.subTest(mutate=mutate):
                with tempfile.TemporaryDirectory() as directory:
                    value, trace, evidence, catalog, document = build_bundle(
                        directory, invariant
                    )
                    mutate(value)
                    report = audit_bundle(
                        value, trace, evidence, catalog, document
                    )
                self.assertEqual("issues", report["status"])
                self.assertEqual(0, report["resolved_case_count"])


if __name__ == "__main__":
    unittest.main()
