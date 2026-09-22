"""Bound evidence and regression resolutions for legacy DecisionCases.

The module never edits the append-only case corpus.  It can extract an exact,
bounded set of source records from the historical trace in one streaming pass
and later validate explicit resolutions against independently recomputed case
problems and executable invariants.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import decision_case_corpus


LEGACY_TRACE_EVIDENCE_SCHEMA_VERSION = 1
TRACE_EVIDENCE_SCHEMA_VERSION = 2
COMPACT_TRACE_EVIDENCE_MODE = "embedded_raw_lines_v1"
MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION = 3
MULTI_SOURCE_TRACE_EVIDENCE_MODE = "multi_source_embedded_gzip_v1"
COMPRESSED_TRACE_SOURCE_SCHEMA_VERSION = 1
COMPRESSED_TRACE_SOURCE_MODE = "gzip_exact_raw_lines_v1"
RESOLUTION_SCHEMA_VERSION = 1
FIXTURE_CATALOG_SCHEMA_VERSION = 1
BLOCKING_CLASSIFICATIONS = {"unresolved", "audited_issues"}
AUTHORITY_VERSION = "decision-case-current-regression-v1"
PERMUTATION_CLEAR_AUTHORITY = "production_container_permutation_v1"
PERMUTATION_CLEAR_REASON = (
    "container_has_at_least_two_items_and_reorder_preserved_semantic_selection"
)
PERMUTATION_NOT_APPLICABLE_AUTHORITY = (
    "production_container_cardinality_v1"
)
PERMUTATION_NOT_APPLICABLE_REASON = (
    "container_has_fewer_than_two_items"
)

# Exact canonical digests of the nine historical profiles first emitted by
# the fixed-hash six-run cohort.  A taxonomy change changes the digest and
# becomes unmapped again; this is deliberately not a phase-wide wildcard.
_PROFILE_DIGEST_FIXTURES = {
    "a3ea3cb7dd30d6de18747dd0e0c674fbc7ad6d1caabe2a7d1367b2bfe6703dd3": "event-consequence-v2",
    "1e7eb2e923c4a63a877b98f185ca68745e0f6b5f60f4dd21e805d1be51c757b7": "event-consequence-v2",
    "330cbd60f751479d28129f061952e4eb9c73167b98e777fe7aeb8b611e703ae9": "event-consequence-v2",
    "50bdc5e904b2fe175bb723f6c7f0d2c6e5bf66c849548977979262151168e96a": "event-consequence-v2",
    "2158bca276542369804b2490e5241b0adeb308069a9cb12f7dec1c27fca99aca": "combat-reward-consequence-v2",
    "b4fafb23ebc759351c807f8b3c683dd56c0c8c1c9d82c1a81671498064b2490e": "event-consequence-v2",
    "035db03aabae8072554adc48ddf068e24efb96c5c4e470b79ab95b2aa1a0f786": "event-consequence-v2",
    "4667de286c41384269335485a4d504f38ef3306fb96ca5095610c7c3424a3eb0": "event-consequence-v2",
    "6367c47861d1c4ab006403ab1c8a366ee7fa8190d53718d5874aa52e6bdab80b": "event-consequence-v2",
}

_NEGATIVE_CONTRACTS = {
    "score_tie": (
        "candidate_local_score_binding",
        "container_order_invariant",
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_missing_value_contract",
        "v2_projected_candidate_score_formula_result_mismatch",
    ),
    "resource_score_tie": (
        "candidate_local_score_binding",
        "container_order_invariant",
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_projected_candidate_score_formula_result_mismatch",
    ),
    "below_argmax": (
        "below_argmax_without_independent_review",
        "candidate_local_score_binding",
        "container_order_invariant",
        "v2_projected_candidate_score_formula_result_mismatch",
    ),
    "delete_candidate": (
        "candidates_missing",
        "v2_projected_candidates_missing",
        "v2_required_candidates_missing_value",
    ),
    "typed_binding_missing": (
        "candidates_missing",
        "v2_projected_candidates_missing",
        "v2_required_candidates_missing_value",
    ),
    "typed_binding_ambiguous": (
        "candidates_missing",
        "v2_projected_candidates_missing",
        "v2_required_candidates_missing_value",
    ),
    "swap_uuid_index": (
        "candidates_missing",
        "v2_projected_candidates_missing",
        "v2_required_candidates_missing_value",
    ),
    "delete_all_candidates": (
        "candidates_missing",
        "v2_projected_candidates_missing",
        "v2_required_candidates_missing_value",
        "v2_required_producer_candidates_missing_value",
    ),
    "final_binding": ("final_choice_binding",),
    "consequence_tamper": (
        "v2_producer_effect_claim_contradicted:gold_delta",
        "v2_projected_producer_consequence_claim_recompute",
    ),
    "plan_protection_removed": (
        "below_argmax_without_independent_review",
    ),
    "settlement_unobserved": (
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    ),
}

def _fixture_row(
    fixture_id, phase, producer_probe, mutation, resolution_class,
    *, current_phase=None, expected_negative_classification="audited_issues",
):
    row = {
        "fixture_id": fixture_id,
        "phase": phase,
        "current_phase": current_phase or phase,
        "producer_probe": producer_probe,
        "mutation": mutation,
        "resolution_class": resolution_class,
        "expected_negative_classification": expected_negative_classification,
        "expected_negative_problem_kinds": list(
            _NEGATIVE_CONTRACTS[mutation]
        ),
    }
    if current_phase is not None and current_phase != phase:
        row["phase_migration_authority"] = (
            "bridge_protocol_v2_sapphire_reward_phase_classification"
        )
    return row


_EXPECTED_FIXTURE_ROWS = {
    row["fixture_id"]: row
    for row in (
        _fixture_row("boss-reward-below-argmax-v2", "BOSS_REWARD", "boss_relic", "below_argmax", "score_ordering"),
        _fixture_row("boss-reward-empty-cage-consequence-v1", "BOSS_REWARD", "boss_relic_empty_cage", "consequence_tamper", "structured_consequence"),
        _fixture_row("boss-reward-starter-replacement-consequence-v1", "BOSS_REWARD", "boss_relic_starter_replacement", "consequence_tamper", "structured_consequence"),
        _fixture_row("combat-reward-bijection-unbound-v2", "COMBAT_REWARD", "sapphire_link", "delete_candidate", "candidate_bijection", current_phase="SAPPHIRE_KEY"),
        _fixture_row("combat-reward-binding-missing-v2", "COMBAT_REWARD", "sapphire_link", "typed_binding_missing", "candidate_binding", current_phase="SAPPHIRE_KEY"),
        _fixture_row("event-binding-ambiguous-v2", "EVENT", "sensory_stone", "typed_binding_ambiguous", "candidate_binding"),
        _fixture_row("event-match-candidates-missing-v2", "EVENT", "match_keep", "delete_all_candidates", "candidate_completeness"),
        _fixture_row("event-score-tie-v2", "EVENT", "sensory_stone", "score_tie", "score_ordering"),
        _fixture_row("grid-uuid-binding-ambiguous-v2", "GRID", "duplicate_uuid_grid", "swap_uuid_index", "candidate_binding"),
        _fixture_row("map-bijection-missing-v2", "MAP", "map_veto", "delete_candidate", "candidate_bijection"),
        _fixture_row("map-below-argmax-v2", "MAP", "map_choices", "below_argmax", "score_ordering"),
        _fixture_row("map-score-tie-v2", "MAP", "map_choices", "score_tie", "score_ordering"),
        _fixture_row("shop-final-binding-v2", "SHOP_SCREEN", "shop_visible", "final_binding", "final_choice_binding"),
        _fixture_row("shop-resource-preparation-score-v2", "SHOP_SCREEN", "shop_resource_preparation", "resource_score_tie", "resource_preparation_score_ordering"),
        _fixture_row("combat-reward-resource-preparation-score-v2", "COMBAT_REWARD", "combat_reward_resource_preparation", "resource_score_tie", "resource_preparation_score_ordering"),
        _fixture_row("boss-reward-candidates-missing-v2", "BOSS_REWARD", "boss_relic", "delete_candidate", "candidate_completeness"),
        _fixture_row("card-reward-candidates-missing-v2", "CARD_REWARD", "card_reward", "delete_candidate", "candidate_completeness"),
        _fixture_row("card-reward-score-order-v2", "CARD_REWARD", "card_reward", "score_tie", "score_ordering"),
        _fixture_row("card_reward_bowl_dominates_skip_v1", "CARD_REWARD", "card_reward_bowl_dominates_skip", "score_tie", "bowl_strict_resource_dominance"),
        _fixture_row("event-candidates-missing-v2", "EVENT", "match_keep", "delete_candidate", "candidate_completeness"),
        _fixture_row("grid-candidates-missing-v2", "GRID", "duplicate_uuid_grid", "delete_candidate", "candidate_completeness"),
        _fixture_row("hand-select-candidates-missing-v2", "HAND_SELECT", "hand_select", "delete_candidate", "candidate_completeness"),
        _fixture_row("map-candidates-missing-v2", "MAP", "map_veto", "delete_candidate", "candidate_completeness"),
        _fixture_row("rest-candidates-missing-v2", "REST", "rest", "delete_candidate", "candidate_completeness"),
        _fixture_row("sapphire-candidates-missing-v2", "SAPPHIRE_KEY", "sapphire_link", "delete_candidate", "candidate_completeness"),
        _fixture_row("shop-candidates-missing-v2", "SHOP_SCREEN", "shop_visible", "delete_candidate", "candidate_completeness"),
        _fixture_row("chest-consequence-v2", "CHEST", "chest", "consequence_tamper", "structured_consequence"),
        _fixture_row("shop-room-consequence-v2", "SHOP_ROOM", "shop_room", "consequence_tamper", "structured_consequence"),
        _fixture_row("event-consequence-v2", "EVENT", "sensory_stone", "consequence_tamper", "structured_consequence"),
        _fixture_row("event-settlement-v2", "EVENT", "match_keep", "settlement_unobserved", "authoritative_settlement"),
        _fixture_row("neow-consequence-v2", "NEOW", "neow", "consequence_tamper", "structured_consequence"),
        _fixture_row("neow-candidates-missing-v2", "NEOW", "neow", "delete_candidate", "candidate_completeness"),
        _fixture_row("grid-consequence-v2", "GRID", "duplicate_uuid_grid", "consequence_tamper", "structured_consequence"),
        _fixture_row("combat-reward-consequence-v2", "COMBAT_REWARD", "combat_reward_skip", "consequence_tamper", "structured_consequence"),
        _fixture_row("sapphire-consequence-v2", "SAPPHIRE_KEY", "sapphire_link", "consequence_tamper", "structured_consequence"),
        _fixture_row("sapphire-bloody-idol-gold-consequence-v1", "SAPPHIRE_KEY", "sapphire_bloody_idol_gold", "consequence_tamper", "structured_consequence"),
        _fixture_row("card-reward-consequence-v2", "CARD_REWARD", "card_reward", "consequence_tamper", "structured_consequence"),
        _fixture_row("map-consequence-v2", "MAP", "map_choices", "consequence_tamper", "structured_consequence"),
        _fixture_row("map-maw-bank-consequence-v2", "MAP", "map_maw_bank", "consequence_tamper", "structured_consequence"),
        _fixture_row("map-maw-bank-score-tie-v2", "MAP", "map_maw_bank", "score_tie", "score_ordering"),
        _fixture_row("hand-select-consequence-v2", "HAND_SELECT", "hand_select", "consequence_tamper", "structured_consequence"),
        _fixture_row("hand-select-score-tie-v2", "HAND_SELECT", "hand_select", "score_tie", "score_ordering"),
        _fixture_row("hand-select-plan-protection-v2", "HAND_SELECT", "hand_select_plan_protection", "plan_protection_removed", "bound_combat_plan_preservation", expected_negative_classification="unresolved"),
        _fixture_row("rest-consequence-v2", "REST", "rest", "consequence_tamper", "structured_consequence"),
    )
}

_PROFILE_FIXTURES = {
    (
        "MAP",
        "audited_issues",
        (
            "legacy_candidate_choice_bijection",
            "legacy_visible_option_coverage_unproven",
        ),
    ): "map-bijection-missing-v2",
    (
        "COMBAT_REWARD",
        "audited_issues",
        (
            "legacy_candidate_choice_bijection",
            "legacy_candidate_option_binding_missing",
            "legacy_visible_option_coverage_unproven",
        ),
    ): "combat-reward-bijection-unbound-v2",
    (
        "GRID",
        "unresolved",
        (
            "legacy_candidate_option_binding_ambiguous",
            "legacy_visible_option_coverage_unproven",
        ),
    ): "grid-uuid-binding-ambiguous-v2",
    (
        "EVENT",
        "unresolved",
        (
            "legacy_candidate_option_binding_ambiguous",
            "legacy_visible_option_coverage_unproven",
        ),
    ): "event-binding-ambiguous-v2",
    (
        "COMBAT_REWARD",
        "unresolved",
        (
            "legacy_candidate_option_binding_missing",
            "legacy_visible_option_coverage_unproven",
        ),
    ): "combat-reward-binding-missing-v2",
    (
        "EVENT",
        "unresolved", ("legacy_candidates_missing",),
    ): "event-match-candidates-missing-v2",
    (
        "SHOP_SCREEN",
        "unresolved", ("legacy_final_choice_binding_unproven",),
    ): "shop-final-binding-v2",
    (
        "EVENT",
        "unresolved", ("score_cutoff_tie_is_semantically_ambiguous",),
    ): "event-score-tie-v2",
    (
        "MAP",
        "unresolved", ("score_cutoff_tie_is_semantically_ambiguous",),
    ): "map-score-tie-v2",
    (
        "BOSS_REWARD",
        "unresolved", ("below_argmax_without_independent_review",),
    ): "boss-reward-below-argmax-v2",
    (
        "MAP",
        "unresolved", ("below_argmax_without_independent_review",),
    ): "map-below-argmax-v2",
}

_V2_CANDIDATES_MISSING = tuple(sorted({
    "candidates_missing", "v2_projected_candidates_missing",
    "v2_required_candidates_missing_value",
}))
_V2_NUMERIC_CONSEQUENCE_UNKNOWN = {
    "candidate_consequence_current_cost_gold_invalid",
    "candidate_consequence_current_cost_hp_invalid",
    "candidate_consequence_current_cost_max_hp_invalid",
    "candidate_consequence_gold_delta_invalid",
    "candidate_consequence_hp_delta_invalid",
    "candidate_consequence_max_hp_delta_invalid",
    "choice_consequence_current_cost_gold_invalid",
    "choice_consequence_current_cost_hp_invalid",
    "choice_consequence_current_cost_max_hp_invalid",
    "choice_consequence_gold_delta_invalid",
    "choice_consequence_hp_delta_invalid",
    "choice_consequence_max_hp_delta_invalid",
}
for _phase, _fixture in {
    "BOSS_REWARD": "boss-reward-candidates-missing-v2",
    "CARD_REWARD": "card-reward-candidates-missing-v2",
    "EVENT": "event-candidates-missing-v2",
    "GRID": "grid-candidates-missing-v2",
    "HAND_SELECT": "hand-select-candidates-missing-v2",
    "MAP": "map-candidates-missing-v2",
    "REST": "rest-candidates-missing-v2",
    "SAPPHIRE_KEY": "sapphire-candidates-missing-v2",
    "SHOP_SCREEN": "shop-candidates-missing-v2",
}.items():
    _PROFILE_FIXTURES[(_phase, "audited_issues", _V2_CANDIDATES_MISSING)] = _fixture

_PROFILE_FIXTURES[(
    "CARD_REWARD", "audited_issues", tuple(sorted({
        *_V2_CANDIDATES_MISSING,
        "v2_decision_context_missing",
        "v2_required_decision_context_missing_value",
    })),
)] = "card-reward-candidates-missing-v2"
_PROFILE_FIXTURES[(
    "MAP", "audited_issues",
    ("v2_independent_consequence_mismatch:gold_delta",),
)] = "map-maw-bank-consequence-v2"
_PROFILE_FIXTURES[(
    "MAP", "audited_issues", tuple(sorted({
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_independent_consequence_mismatch:gold_delta",
    })),
)] = "map-maw-bank-score-tie-v2"
_PROFILE_FIXTURES[(
    "HAND_SELECT", "audited_issues", tuple(sorted({
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "hand-select-consequence-v2"
_PROFILE_FIXTURES[(
    "HAND_SELECT", "audited_issues", tuple(sorted({
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "hand-select-score-tie-v2"
_PROFILE_FIXTURES[(
    "HAND_SELECT", "audited_issues", tuple(sorted({
        "below_argmax_without_independent_review",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "hand-select-plan-protection-v2"
_PROFILE_FIXTURES[(
    "CHEST", "unresolved", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_operation_not_independently_classified",
    })),
)] = "chest-consequence-v2"
_PROFILE_FIXTURES[(
    "SHOP_ROOM", "unresolved", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_operation_not_independently_classified",
    })),
)] = "shop-room-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_effect_claim_contradicted:leave",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_independent_consequence_mismatch:leave",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"

# Designer's historical four-option dialog was recorded with a producer
# consequence sentinel while the independent event projection was being
# tightened.  Keep the two exact fingerprints executable instead of allowing
# a broad EVENT wildcard to absorb unrelated taxonomy changes.
_DESIGNER_LEGACY_PROJECTION_PROBLEMS = {
    "canonical_choice_local_reason_invalid",
    "v2_independent_consequence_mismatch:card_changes:knowledge_status",
    "v2_independent_consequence_mismatch:current_cost:knowledge_status",
    "v2_independent_consequence_mismatch:current_cost:unproven_payload",
    "v2_independent_consequence_mismatch:curse:knowledge_status",
    "v2_independent_consequence_mismatch:curse:unproven_payload",
    "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.current_cost.status",
    "v2_independent_consequence_mismatch:field_knowledge.curse.status",
    "v2_independent_consequence_mismatch:field_knowledge.future_costs.status",
    "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.probabilistic_outcomes.status",
    "v2_independent_consequence_mismatch:field_knowledge.relic_changes.status",
    "v2_independent_consequence_mismatch:future_costs:knowledge_status",
    "v2_independent_consequence_mismatch:gold_delta:knowledge_status",
    "v2_independent_consequence_mismatch:gold_delta:unproven_payload",
    "v2_independent_consequence_mismatch:hp_delta:knowledge_status",
    "v2_independent_consequence_mismatch:hp_delta:unproven_payload",
    "v2_independent_consequence_mismatch:max_hp_delta:knowledge_status",
    "v2_independent_consequence_mismatch:max_hp_delta:unproven_payload",
    "v2_independent_consequence_mismatch:potion_changes:knowledge_status",
    "v2_independent_consequence_mismatch:probabilistic_outcomes:knowledge_status",
    "v2_independent_consequence_mismatch:relic_changes:knowledge_status",
    "v2_independent_consequence_mismatch:uncertainty",
    "v2_independent_consequence_mismatch:uncertainty_classification",
    "v2_producer_current_cost_not_independently_classified",
    "v2_producer_effect_claim_contradicted:card_changes",
    "v2_producer_effect_claim_unresolved:curse:not_independently_classified",
    "v2_producer_effect_claim_unresolved:future_costs:not_independently_classified",
    "v2_producer_effect_claim_unresolved:mechanism_id:not_protocol_visible",
    "v2_producer_effect_claim_unresolved:probabilistic_outcomes:not_independently_classified",
    "v2_producer_effect_claim_unresolved:random_effects:not_protocol_visible",
    "v2_producer_effect_field_unclassified:original_button_index",
    "v2_producer_operation_not_independently_classified",
    "v2_projected_producer_consequence_claim_recompute",
    "v2_projected_unclassified_producer_fields_recompute",
}
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues",
    tuple(sorted(_DESIGNER_LEGACY_PROJECTION_PROBLEMS)),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues",
    tuple(sorted(_DESIGNER_LEGACY_PROJECTION_PROBLEMS | {
        "v2_independent_consequence_mismatch:future_costs:unproven_payload",
        "v2_producer_effect_claim_unresolved:card_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_producer_effect_claim_unresolved:max_hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:potion_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:relic_changes:not_independently_classified",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "NEOW", "audited_issues", tuple(sorted({
        "v2_producer_effect_claim_contradicted:event_id",
        "v2_producer_effect_claim_unresolved:drawback_kind:not_protocol_visible",
        "v2_producer_effect_claim_unresolved:parameters:not_protocol_visible",
        "v2_producer_effect_claim_unresolved:reward_kind:not_protocol_visible",
        "v2_producer_operation_not_independently_classified",
    })),
)] = "neow-consequence-v2"

# Historical records emitted while typed consequences and settlements were
# being deployed are resolvable only through their exact current production
# probes.  Keep every legacy problem fingerprint explicit: adding or removing
# a finding must make the profile unmapped instead of silently broadening it.
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
# Cursed Tome records written while the typed staged-event envelope was being
# introduced can additionally contain the auditor's explicit uncertainty
# mismatch.  The current event consequence probe exercises the replacement
# contract; keep this historical fingerprint exact so taxonomy changes still
# fail closed rather than being silently absorbed.
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
# The first typed Cursed Tome trace predates the DecisionCase partition that
# classifies original_button_index as a target-binding claim.  Its raw record
# is immutable, so resolve exactly this stale partition fingerprint through
# the current structured-event probe instead of rewriting historical evidence.
_PROFILE_FIXTURES[(
    "EVENT", "audited_issues", tuple(sorted({
        "canonical_choice_local_reason_invalid",
        "v2_producer_effect_field_unclassified:original_button_index",
        "v2_producer_operation_not_independently_classified",
        "v2_projected_producer_consequence_claim_recompute",
        "v2_projected_unclassified_producer_fields_recompute",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "NEOW", "audited_issues", _V2_CANDIDATES_MISSING,
)] = "neow-candidates-missing-v2"
for _phase, _fixture in {
    "CHEST": "chest-consequence-v2",
    "SHOP_ROOM": "shop-room-consequence-v2",
}.items():
    _PROFILE_FIXTURES[(
        _phase,
        "audited_issues",
        tuple(sorted({
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        })),
    )] = _fixture
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "unresolved",
    tuple(sorted({
        "candidate_consequence_gold_delta_invalid",
        "candidate_consequence_hp_delta_invalid",
        "candidate_consequence_max_hp_delta_invalid",
        "choice_consequence_gold_delta_invalid",
        "choice_consequence_hp_delta_invalid",
        "choice_consequence_max_hp_delta_invalid",
    })),
)] = "sapphire-consequence-v2"
# Early linked-relic selections on the Sapphire Key reward surface knew the
# relic delta but left every unrelated resource field at the old generic
# ``unknown`` classification.  The exact fingerprint below is resolved by
# the current Sapphire producer/settlement probe, which exercises choosing
# the linked relic while keeping the key option dynamically ineligible.
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "audited_issues",
    tuple(sorted({
        "candidate_consequence_gold_delta_invalid",
        "candidate_consequence_hp_delta_invalid",
        "candidate_consequence_max_hp_delta_invalid",
        "choice_consequence_gold_delta_invalid",
        "choice_consequence_hp_delta_invalid",
        "choice_consequence_max_hp_delta_invalid",
        "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
        "v2_independent_consequence_mismatch:field_knowledge.curse.status",
        "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
        "v2_independent_consequence_mismatch:future_costs",
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_independent_consequence_mismatch:uncertainty_classification",
    })),
)] = "sapphire-consequence-v2"
_PROFILE_FIXTURES[(
    "REST",
    "audited_issues",
    tuple(sorted({
        "below_argmax_without_independent_review",
        "v2_producer_effect_claim_contradicted:key_changes",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "rest-consequence-v2"
_PROFILE_FIXTURES[(
    "GRID",
    "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_decision_context_missing",
        "v2_producer_current_cost_not_independently_classified",
        "v2_producer_effect_claim_unresolved:card_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:future_costs:not_independently_classified",
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:max_hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:potion_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:probabilistic_outcomes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:relic_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:selected_card:not_protocol_visible",
        "v2_producer_operation_not_independently_classified",
        "v2_required_decision_context_missing_value",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
        "v2_settlement_observed_block_delta_missing",
        "v2_settlement_observed_current_hp_delta_missing",
        "v2_settlement_observed_deck_missing",
        "v2_settlement_observed_max_hp_delta_missing",
        "v2_settlement_observed_potions_missing",
        "v2_settlement_observed_relics_missing",
    })),
)] = "grid-consequence-v2"
_PROFILE_FIXTURES[(
    "GRID",
    "unresolved",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_current_cost_not_independently_classified",
        "v2_producer_effect_claim_unresolved:card_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:future_costs:not_independently_classified",
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:max_hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:potion_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:probabilistic_outcomes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:relic_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:selected_card:not_protocol_visible",
        "v2_producer_operation_not_independently_classified",
    })),
)] = "grid-consequence-v2"

# Historical schema-v2 records are resolved only by a phase-matched current
# production probe.  These profiles were emitted by the immediately preceding
# controller while the typed operation/settlement contract was being deployed;
# the current probes exercise the complete replacement contract and a blocking
# negative control.
for _profile, _fixture in {
    (
        "CARD_REWARD", "audited_issues",
        ("v2_independent_consequence_mismatch:operation",),
    ): "card-reward-consequence-v2",
    (
        "CARD_REWARD", "audited_issues",
        (
            "below_argmax_without_independent_review",
            "v2_independent_consequence_mismatch:operation",
        ),
    ): "card-reward-score-order-v2",
    (
        "CHEST", "audited_issues",
        (
            "v2_independent_consequence_mismatch:operation",
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        ),
    ): "chest-consequence-v2",
    (
        "EVENT", "audited_issues",
        tuple(sorted({
            *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
            "v2_producer_combat_delta_not_independently_classified",
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        })),
    ): "event-consequence-v2",
    (
        "EVENT", "audited_issues",
        tuple(sorted({
            *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
            "v2_producer_effect_claim_contradicted:leave",
            "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        })),
    ): "event-consequence-v2",
    (
        "HAND_SELECT", "audited_issues",
        tuple(sorted({
            *_V2_CANDIDATES_MISSING,
            "v2_decision_context_missing",
            "v2_required_decision_context_missing_value",
        })),
    ): "hand-select-candidates-missing-v2",
    (
        "MAP", "audited_issues",
        ("v2_independent_consequence_mismatch:hp_delta",),
    ): "map-consequence-v2",
    (
        "NEOW", "audited_issues",
        tuple(sorted({
            *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
            "v2_candidate_contract_not_auditable",
            "v2_independent_consequence_mismatch:uncertainty",
            "v2_producer_effect_claim_contradicted:future_costs",
            "v2_producer_effect_claim_contradicted:hp_delta",
            "v2_producer_operation_not_independently_classified",
            "v2_projected_candidate_score_rule_unclassified",
        })),
    ): "neow-consequence-v2",
    (
        "NEOW", "unresolved",
        (
            "v2_producer_effect_claim_unresolved:drawback_kind:not_protocol_visible",
            "v2_producer_effect_claim_unresolved:parameters:not_protocol_visible",
            "v2_producer_effect_claim_unresolved:reward_kind:not_protocol_visible",
        ),
    ): "neow-consequence-v2",
    (
        "REST", "audited_issues",
        ("v2_producer_effect_claim_contradicted:key_changes",),
    ): "rest-consequence-v2",
    (
        "REST", "audited_issues",
        (
            "v2_producer_effect_claim_contradicted:key_changes",
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        ),
    ): "rest-consequence-v2",
    (
        "SHOP_ROOM", "audited_issues",
        (
            "v2_independent_consequence_mismatch:operation",
            "v2_settlement_authority_binding",
            "v2_settlement_not_fully_observable",
        ),
    ): "shop-room-consequence-v2",
    (
        "SHOP_SCREEN", "audited_issues",
        tuple(sorted({
            "canonical_choices_missing",
            "v2_authoritative_choice_settlement_missing",
            "v2_canonical_choices_missing",
            "v2_required_authoritative_choice_settlement_missing_value",
            "v2_required_available_commands_missing_value",
            "v2_required_available_options_missing_value",
            "v2_required_candidates_missing_value",
            "v2_required_canonical_choices_missing_value",
            "v2_required_final_choice_ids_missing_value",
            "v2_required_selected_choice_ids_missing_value",
        })),
    ): "shop-candidates-missing-v2",
}.items():
    _PROFILE_FIXTURES[_profile] = _fixture

# Both variants exist in the immutable mixed-contract Neow history: when the
# chosen max-HP-loss option started at full health the stronger oracle also
# proves the stale hp_delta claim false; otherwise that extra contradiction is
# absent.  Each fingerprint remains exact and is exercised by the same current
# typed-Neow consequence probe.
_PROFILE_FIXTURES[(
    "NEOW", "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_candidate_contract_not_auditable",
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_producer_effect_claim_contradicted:future_costs",
        "v2_producer_operation_not_independently_classified",
        "v2_projected_candidate_score_rule_unclassified",
    })),
)] = "neow-consequence-v2"

# Empty Cage gained an exact two-card removal follow-up after these historical
# boss-reward records were written.  Resolve only this complete legacy
# fingerprint through a live producer probe that selects Empty Cage and whose
# negative control tampers with its typed consequence.
_PROFILE_FIXTURES[(
    "BOSS_REWARD", "audited_issues",
    tuple(sorted({
        "candidate_consequence_gold_delta_invalid",
        "candidate_consequence_hp_delta_invalid",
        "candidate_consequence_max_hp_delta_invalid",
        "choice_consequence_gold_delta_invalid",
        "choice_consequence_hp_delta_invalid",
        "choice_consequence_max_hp_delta_invalid",
        "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
        "v2_independent_consequence_mismatch:field_knowledge.curse.status",
        "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
        "v2_independent_consequence_mismatch:future_costs",
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_independent_consequence_mismatch:uncertainty_classification",
    })),
)] = "boss-reward-empty-cage-consequence-v1"
_PROFILE_FIXTURES[(
    "BOSS_REWARD", "audited_issues",
    tuple(sorted({
        "candidate_consequence_gold_delta_invalid",
        "candidate_consequence_hp_delta_invalid",
        "candidate_consequence_max_hp_delta_invalid",
        "choice_consequence_gold_delta_invalid",
        "choice_consequence_hp_delta_invalid",
        "choice_consequence_max_hp_delta_invalid",
        "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
        "v2_independent_consequence_mismatch:field_knowledge.curse.status",
        "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
        "v2_independent_consequence_mismatch:relic_changes",
    })),
)] = "boss-reward-starter-replacement-consequence-v1"

_V2_LEGACY_FIELD_STATUS_MISMATCH = {
    "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.current_cost.status",
    "v2_independent_consequence_mismatch:field_knowledge.curse.status",
    "v2_independent_consequence_mismatch:field_knowledge.future_costs.status",
    "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.probabilistic_outcomes.status",
    "v2_independent_consequence_mismatch:field_knowledge.relic_changes.status",
}
for _phase, _fixture in {
    "CHEST": "chest-consequence-v2",
    "SHOP_ROOM": "shop-room-consequence-v2",
}.items():
    _PROFILE_FIXTURES[(
        _phase,
        "audited_issues",
        tuple(sorted({
            *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
            *_V2_LEGACY_FIELD_STATUS_MISMATCH,
            "v2_independent_consequence_mismatch:operation",
            "v2_independent_consequence_mismatch:uncertainty_classification",
        })),
    )] = _fixture

# Historical Cursed Key chest choices predate the explicit random-curse
# projection.  The current independent projector can now prove the stable
# numeric fields while retaining only the random curse/card partition and the
# unobserved settlement as unknown.  Keep that reduced taxonomy exact so an
# unrelated CHEST regression still fails closed.
_CHEST_RANDOM_CURSE_LEGACY_PROBLEMS = tuple(sorted({
    "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.curse.status",
    "v2_independent_consequence_mismatch:field_knowledge.probabilistic_outcomes.status",
    "v2_independent_consequence_mismatch:random_effects",
    "v2_independent_consequence_mismatch:uncertainty_classification",
    "v2_settlement_authority_binding",
    "v2_settlement_not_fully_observable",
}))
_PROFILE_FIXTURES[(
    "CHEST", "audited_issues", _CHEST_RANDOM_CURSE_LEGACY_PROBLEMS,
)] = "chest-consequence-v2"
_PROFILE_FIXTURES[(
    "COMBAT_REWARD",
    "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        *_V2_LEGACY_FIELD_STATUS_MISMATCH,
        "v2_independent_consequence_mismatch:uncertainty_classification",
    })),
)] = "combat-reward-consequence-v2"
_PROFILE_FIXTURES[(
    "COMBAT_REWARD",
    "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        *_V2_LEGACY_FIELD_STATUS_MISMATCH,
        "v2_independent_consequence_mismatch:operation",
        "v2_independent_consequence_mismatch:uncertainty_classification",
    })),
)] = "combat-reward-consequence-v2"
_PROFILE_FIXTURES[(
    "COMBAT_REWARD",
    "unresolved",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:potion_id:not_protocol_visible",
    })),
)] = "combat-reward-consequence-v2"
_PROFILE_FIXTURES[(
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
)] = "sapphire-bloody-idol-gold-consequence-v1"
_PROFILE_FIXTURES[(
    "NEOW",
    "audited_issues",
    tuple(sorted({
        "v2_producer_effect_claim_contradicted:event_id",
        "v2_producer_effect_claim_unresolved:drawback_kind:not_protocol_visible",
        "v2_producer_effect_claim_unresolved:parameters:not_protocol_visible",
        "v2_producer_effect_claim_unresolved:reward_kind:not_protocol_visible",
    })),
)] = "neow-consequence-v2"
_PROFILE_FIXTURES[(
    "GRID",
    "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_decision_context_missing",
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_producer_current_cost_not_independently_classified",
        "v2_producer_effect_claim_unresolved:card_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:future_costs:not_independently_classified",
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:max_hp_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:potion_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:probabilistic_outcomes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:relic_changes:not_independently_classified",
        "v2_producer_effect_claim_unresolved:selected_card:not_protocol_visible",
        "v2_producer_operation_not_independently_classified",
        "v2_required_decision_context_missing_value",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
        "v2_settlement_observed_block_delta_missing",
        "v2_settlement_observed_current_hp_delta_missing",
        "v2_settlement_observed_deck_missing",
        "v2_settlement_observed_max_hp_delta_missing",
        "v2_settlement_observed_potions_missing",
        "v2_settlement_observed_relics_missing",
    })),
)] = "grid-consequence-v2"
_PROFILE_FIXTURES[(
    "NEOW", "audited_issues", tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_candidate_contract_not_auditable",
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_producer_effect_claim_contradicted:event_id",
        "v2_producer_effect_claim_contradicted:future_costs",
        "v2_producer_effect_claim_contradicted:hp_delta",
        "v2_producer_operation_not_independently_classified",
        "v2_projected_candidate_score_rule_unclassified",
    })),
)] = "neow-consequence-v2"

# The 052a66b0 historical attempt was captured before the typed event,
# resource-preparation and consequence envelopes existed.  These are *exact*
# legacy fingerprints, not wildcards: a changed problem set stays blocking.
# Each mapping points at the current same-phase production invariant, whose
# positive path and mutation control are re-run while a resolution is built.
# That lets a legacy record be classified by its original evidence without
# treating an untyped display string as proof for the current contract.
_LEGACY_NUMERIC_CONSEQUENCE_INVALID = {
    "candidate_consequence_gold_delta_invalid",
    "candidate_consequence_hp_delta_invalid",
    "candidate_consequence_max_hp_delta_invalid",
    "choice_consequence_gold_delta_invalid",
    "choice_consequence_hp_delta_invalid",
    "choice_consequence_max_hp_delta_invalid",
}
_LEGACY_CURRENT_COST_INVALID = {
    "candidate_consequence_current_cost_gold_invalid",
    "candidate_consequence_current_cost_hp_invalid",
    "candidate_consequence_current_cost_max_hp_invalid",
    "choice_consequence_current_cost_gold_invalid",
    "choice_consequence_current_cost_hp_invalid",
    "choice_consequence_current_cost_max_hp_invalid",
}
_LEGACY_NON_GOLD_NUMERIC_CONSEQUENCE_INVALID = {
    "candidate_consequence_hp_delta_invalid",
    "candidate_consequence_max_hp_delta_invalid",
    "choice_consequence_hp_delta_invalid",
    "choice_consequence_max_hp_delta_invalid",
}
_PROFILE_FIXTURES[(
    "BOSS_REWARD",
    "unresolved",
    tuple(sorted(_LEGACY_NUMERIC_CONSEQUENCE_INVALID)),
)] = "boss-reward-below-argmax-v2"
_PROFILE_FIXTURES[(
    "CARD_REWARD",
    "unresolved",
    ("below_argmax_without_independent_review",),
)] = "card-reward-score-order-v2"
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        *_LEGACY_CURRENT_COST_INVALID,
        "v2_candidate_contract_not_auditable",
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_projected_candidate_score_rule_unclassified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        *_LEGACY_CURRENT_COST_INVALID,
        "v2_independent_consequence_mismatch:uncertainty",
        "v2_producer_effect_claim_unresolved:leave:not_independently_classified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        *_LEGACY_CURRENT_COST_INVALID,
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NON_GOLD_NUMERIC_CONSEQUENCE_INVALID,
        *_LEGACY_CURRENT_COST_INVALID,
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"
_PROFILE_FIXTURES[(
    "REST",
    "audited_issues",
    (
        "below_argmax_without_independent_review",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    ),
)] = "rest-consequence-v2"
_PROFILE_FIXTURES[(
    "REST",
    "audited_issues",
    (
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    ),
)] = "rest-consequence-v2"
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        "v2_producer_operation_contradicted",
    })),
)] = "sapphire-consequence-v2"
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
        "v2_independent_consequence_mismatch:field_knowledge.curse.status",
        "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
    })),
)] = "sapphire-consequence-v2"
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "audited_issues",
    tuple(sorted({
        *_LEGACY_NUMERIC_CONSEQUENCE_INVALID,
        "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
        "v2_independent_consequence_mismatch:field_knowledge.curse.status",
        "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
        "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
        "v2_producer_operation_contradicted",
    })),
)] = "sapphire-consequence-v2"
_PROFILE_FIXTURES[(
    "SAPPHIRE_KEY",
    "audited_issues",
    ("v2_producer_operation_contradicted",),
)] = "sapphire-consequence-v2"
_PROFILE_FIXTURES[(
    "SHOP_SCREEN",
    "audited_issues",
    (
        "chosen_selected_binding",
        "v2_producer_bound_new_potion_id_not_protocol_visible",
    ),
)] = "shop-final-binding-v2"

# The fixed-hash six-run cohort exposed three additional exact historical
# profiles.  Keep these fingerprints literal: a taxonomy change must become a
# blocker again.  Both new fixture targets execute the current real producer;
# the Nest profile reuses the existing typed event consequence regression.
_PROFILE_FIXTURES[(
    "SHOP_SCREEN",
    "audited_issues",
    (
        "chosen_selected_binding",
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_producer_bound_new_potion_id_not_protocol_visible",
    ),
)] = "shop-resource-preparation-score-v2"
_PROFILE_FIXTURES[(
    "COMBAT_REWARD",
    "audited_issues",
    (
        "chosen_selected_binding",
        "score_cutoff_tie_is_semantically_ambiguous",
        "v2_producer_bound_new_potion_id_not_protocol_visible",
    ),
)] = "combat-reward-resource-preparation-score-v2"
# After typed receipt and parent-option auditing, these immutable cohort rows
# retain only the exact score-tie blocker.  Resolve them through fresh current
# producers; any additional or renamed problem remains unmapped.
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    (
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    ),
)] = "event-settlement-v2"
_PROFILE_FIXTURES[(
    "EVENT",
    "audited_issues",
    tuple(sorted({
        *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
        "v2_producer_effect_claim_unresolved:gold_delta:not_independently_classified",
        "v2_producer_effect_claim_unresolved:hp_delta:not_independently_classified",
        "v2_settlement_authority_binding",
        "v2_settlement_not_fully_observable",
    })),
)] = "event-consequence-v2"


class ResolutionError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def object_digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _profile_fixture_id(phase, classification, problems):
    profile = (phase, classification, tuple(problems))
    fixture_id = _PROFILE_FIXTURES.get(profile)
    if fixture_id is not None:
        return fixture_id
    digest = object_digest([
        phase, classification, list(profile[2]),
    ])
    return _PROFILE_DIGEST_FIXTURES.get(digest)


_KNOWING_SKULL_DIALOG_ADVANCE_PROBLEMS = tuple(sorted({
    "canonical_choice_local_reason_invalid",
    "v2_producer_effect_field_unclassified:original_button_index",
    "v2_producer_operation_not_independently_classified",
    "v2_projected_producer_consequence_claim_recompute",
    "v2_projected_unclassified_producer_fields_recompute",
}))
_GOLDEN_WING_LEGACY_CONSEQUENCE_PROBLEMS = tuple(sorted({
    *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
    "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.current_cost.status",
    "v2_independent_consequence_mismatch:field_knowledge.curse.status",
    "v2_independent_consequence_mismatch:field_knowledge.future_costs.status",
    "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.probabilistic_outcomes.status",
    "v2_independent_consequence_mismatch:field_knowledge.relic_changes.status",
    "v2_independent_consequence_mismatch:leave",
    "v2_independent_consequence_mismatch:operation",
    "v2_independent_consequence_mismatch:uncertainty",
    "v2_independent_consequence_mismatch:uncertainty_classification",
    "v2_settlement_authority_binding",
    "v2_settlement_not_fully_observable",
}))
_DRUG_DEALER_DIALOG_ADVANCE_PROBLEMS = tuple(sorted({
    *_V2_NUMERIC_CONSEQUENCE_UNKNOWN,
    "v2_independent_consequence_mismatch:event_outcome_id",
    "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.current_cost.status",
    "v2_independent_consequence_mismatch:field_knowledge.curse.status",
    "v2_independent_consequence_mismatch:field_knowledge.future_costs.status",
    "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
    "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
    "v2_independent_consequence_mismatch:field_knowledge.probabilistic_outcomes.status",
    "v2_independent_consequence_mismatch:field_knowledge.relic_changes.status",
    "v2_independent_consequence_mismatch:leave",
    "v2_independent_consequence_mismatch:operation",
    "v2_independent_consequence_mismatch:uncertainty",
    "v2_independent_consequence_mismatch:uncertainty_classification",
    "v2_settlement_authority_binding",
    "v2_settlement_not_fully_observable",
}))
_CASE_FIXTURE_ALLOWLIST = {
    (
        "516f981b-de00-4bae-befe-f03d5e57298c",
        221185,
        "SAPPHIRE_KEY",
        None,
        "625c63a258654b1166fa09c6d48bfddcd651b7da2164c8c6effd931fc659bd2d",
    ): (
        "audited_issues",
        tuple(sorted({
            "candidate_consequence_gold_delta_invalid",
            "candidate_consequence_hp_delta_invalid",
            "candidate_consequence_max_hp_delta_invalid",
            "choice_consequence_gold_delta_invalid",
            "choice_consequence_hp_delta_invalid",
            "choice_consequence_max_hp_delta_invalid",
            "v2_independent_consequence_mismatch:field_knowledge.card_changes.status",
            "v2_independent_consequence_mismatch:field_knowledge.curse.status",
            "v2_independent_consequence_mismatch:field_knowledge.gold_delta.status",
            "v2_independent_consequence_mismatch:field_knowledge.hp_delta.status",
            "v2_independent_consequence_mismatch:field_knowledge.max_hp_delta.status",
            "v2_independent_consequence_mismatch:field_knowledge.potion_changes.status",
            "v2_independent_consequence_mismatch:future_costs",
            "v2_independent_consequence_mismatch:uncertainty",
            "v2_independent_consequence_mismatch:uncertainty_classification",
        })),
        "sapphire-consequence-v2",
    ),
    (
        "ea5fb7a3-dce9-4a7d-906d-d0c82b08b38e",
        234273,
        "EVENT",
        "knowingskull",
        "ee33606e4ea392f8ffe4f555352dd8fe9ca49b2ef78077370d726ceac57de9ba",
    ): (
        "audited_issues",
        _KNOWING_SKULL_DIALOG_ADVANCE_PROBLEMS,
        "event-consequence-v2",
    ),
    (
        "ea5fb7a3-dce9-4a7d-906d-d0c82b08b38e",
        234282,
        "EVENT",
        "knowingskull",
        "f1077845d91c390d6ec1189d1c1d3c7a5acba77f342adefb3bfbc2c11468cddc",
    ): (
        "audited_issues",
        _KNOWING_SKULL_DIALOG_ADVANCE_PROBLEMS,
        "event-consequence-v2",
    ),
    (
        "0098b71d-c5a7-44c2-ae20-95d9d4b29d98",
        224079,
        "SHOP_SCREEN",
        None,
        "efc81eddb5efcb11b0cd436c0a46e79e090f9608c149e7c65292dfc525cd8747",
    ): (
        "unresolved",
        (
            "score_cutoff_tie_is_semantically_ambiguous",
            "v2_producer_bound_new_potion_id_not_protocol_visible",
        ),
        "shop-resource-preparation-score-v2",
    ),
    (
        "da2bc851-db1e-42e3-8800-a66416520e8a",
        231870,
        "COMBAT_REWARD",
        None,
        "b982fb782808fe0401d74e7f58a7305f87d71f0571dfe05b6912f63b167aaf56",
    ): (
        "unresolved",
        ("score_cutoff_tie_is_semantically_ambiguous",),
        "combat-reward-resource-preparation-score-v2",
    ),
    (
        "e0d39f61-8147-4590-bcfb-bf42be299bc1",
        232398,
        "EVENT",
        "goldenwing",
        "019149090ad508322f876b8c8d4fd7823cf0e7c2359ffb4cb383e93e09651857",
    ): (
        "audited_issues",
        _GOLDEN_WING_LEGACY_CONSEQUENCE_PROBLEMS,
        "event-consequence-v2",
    ),
}

_DRUG_DEALER_DIALOG_ADVANCE_CASES = (
    (
        "1fab81e1-d59f-4238-af64-7346a974591a", 221862,
        "048148d2c583d1c1f89bd470b04ceddddfabfd40ab303e57b56c1cc59d8253c4",
    ),
    (
        "d6aa7e04-8aba-4753-9764-d3176ba22f60", 223601,
        "1dab893312e1c48236e22095992080a01296e9c756b3806c8a982e3b7d72071b",
    ),
    (
        "b3091ec2-c434-44b3-b11f-bdf6168c979d", 225312,
        "e7302800dac282c6ec1ca9b9edd8f6cded45c16aa323fce277cb6796bcd2a03e",
    ),
    (
        "427f738d-1236-4d6d-b288-4efa0b621888", 226360,
        "a9ece339e13ccda6b119e2bef970ed5e6a39b3ae3a63f81ee92bd3df577c9a60",
    ),
    (
        "7e2d8be6-cbc4-4824-908d-979c4d82a530", 237140,
        "144998c78067561aebd1a61200a57d589575b42b0f454af75c128d38a08a3afc",
    ),
    (
        "d6e77dd5-425e-4824-916e-1a2c7f7e5c5d", 244143,
        "34f776c197c9832aa3e00e58aefdb4503dabe9d96eca216343fb90231d78c6ef",
    ),
)
for _attempt_id, _before_seq, _case_sha256 in (
    _DRUG_DEALER_DIALOG_ADVANCE_CASES
):
    _CASE_FIXTURE_ALLOWLIST[(
        _attempt_id, _before_seq, "EVENT", "drugdealer", _case_sha256,
    )] = (
        "audited_issues",
        _DRUG_DEALER_DIALOG_ADVANCE_PROBLEMS,
        "event-consequence-v2",
    )

_LEGACY_SHOP_PARENT_CASES = (
    (
        "052a66b0-cbb4-4512-be6d-4994f2e8ed50", 210575,
        "52c15a1e133228277bc033181760adbfab1a965487c659976d2d672fce6f1513",
    ),
    (
        "0098b71d-c5a7-44c2-ae20-95d9d4b29d98", 224073,
        "1e15ace9b1bc82f7054b3bbc91a270862890e14e55445fb30cd1fbf8bc7e4db1",
    ),
    (
        "b3091ec2-c434-44b3-b11f-bdf6168c979d", 225173,
        "1a5eefd85e96d595492a97b5a8a3dc15ca3aca7c461bc18bb61d84029ef139f2",
    ),
    (
        "239934eb-9d25-40d8-9e4e-3fea9e5333dd", 233188,
        "225d8c7f97d77fa0a50c30cca8720a489023eeb461a137fd97b41d0e67164953",
    ),
    (
        "ea5fb7a3-dce9-4a7d-906d-d0c82b08b38e", 234204,
        "37d0264534947c52324f467d6f2c79152a9a8b24bbc41505b52e513b95c7b7c5",
    ),
    (
        "6ebf12ee-b412-4bec-b2fa-3250e032e335", 235768,
        "496b819d29be8c530d133df893e4115ab031293f72cc64fb529d9c744af1736a",
    ),
    (
        "7e2d8be6-cbc4-4824-908d-979c4d82a530", 237025,
        "9e3b4b79718c478832d3e85af97551bfd21bad710da9ed2e85fc1d7c9a92071d",
    ),
)
_LEGACY_SHOP_PARENT_PROBLEMS = (
    "v2_producer_bound_new_potion_id_not_protocol_visible",
)
for _attempt_id, _before_seq, _case_sha256 in _LEGACY_SHOP_PARENT_CASES:
    _CASE_FIXTURE_ALLOWLIST[(
        _attempt_id, _before_seq, "SHOP_SCREEN", None, _case_sha256,
    )] = (
        "unresolved",
        _LEGACY_SHOP_PARENT_PROBLEMS,
        "shop-resource-preparation-score-v2",
    )

_EXACT_LEGACY_SHOP_PARENT_CASE_IDENTITIES = frozenset(
    (
        attempt_id, before_seq, "SHOP_SCREEN", None, case_sha256,
    )
    for attempt_id, before_seq, case_sha256 in _LEGACY_SHOP_PARENT_CASES
)
_EXACT_RESOURCE_TIE_CASE_IDENTITIES = frozenset(
    identity for identity, binding in _CASE_FIXTURE_ALLOWLIST.items()
    if "score_cutoff_tie_is_semantically_ambiguous" in binding[1]
    and binding[2] in {
        "shop-resource-preparation-score-v2",
        "combat-reward-resource-preparation-score-v2",
    }
)

_EXACT_RESOURCE_CASE_IDENTITIES = frozenset(
    identity for identity, binding in _CASE_FIXTURE_ALLOWLIST.items()
    if binding[2] in {
        "shop-resource-preparation-score-v2",
        "combat-reward-resource-preparation-score-v2",
    }
)


def _exact_resource_case_shape(case, fixture_id):
    """Keep exact historical mappings on their typed preparation surface."""

    expected = {
        "shop-resource-preparation-score-v2": (
            "SHOP_SCREEN", "shop_potion_replacement_resource_preparation",
        ),
        "combat-reward-resource-preparation-score-v2": (
            "COMBAT_REWARD", "potion_reward_upgrade_full_belt",
        ),
    }.get(fixture_id)
    if expected is None:
        return False
    if (
        case.get("phase") != expected[0]
        or case.get("reason") != expected[1]
        or case.get("action") != "potion"
        or case.get("decision_surface_kind") != "resource_preparation"
        or case.get("parent_choice_surface_pending") is not True
        or not all(
            isinstance(case.get(field), list) and case[field]
            for field in (
                "available_options", "resource_preparation_options",
                "canonical_choices", "candidates", "producer_candidates",
            )
        )
    ):
        return False
    selected = case.get("selected_choice_ids")
    if (
        not isinstance(selected, list) or len(selected) != 1
        or case.get("final_choice_ids") != selected
    ):
        return False
    selected_rows = [
        row for row in case.get("canonical_choices") or []
        if isinstance(row, dict) and row.get("choice_id") == selected[0]
    ]
    if len(selected_rows) != 1:
        return False
    target = selected_rows[0].get("target")
    target = target if isinstance(target, dict) else {}
    if (
        target.get("kind") != "potion_resource"
        or target.get("operation") not in {"use", "discard"}
        or not isinstance(target.get("potion_instance_id"), str)
        or not target.get("potion_instance_id")
    ):
        return False
    if expected[0] == "SHOP_SCREEN":
        return any(
            isinstance(row, dict)
            and ((row.get("target") or {}).get("kind") == "potion")
            and isinstance(((row.get("target") or {}).get("item") or {}).get("id"), str)
            for row in case["available_options"]
        )
    return any(
        isinstance(row, dict)
        and str(
            (((row.get("target") or {}).get("reward") or {}).get("reward_type") or "")
        ).casefold() == "potion"
        for row in case["available_options"]
    )


def _legacy_shop_parent_case_shape(case):
    """Prove one old shop preparation without inventing its missing tuple.

    These immutable records predate the producer's explicit parent-listing
    tuple.  Their unanimous parent potion id may be joined only when the raw
    typed shop surface contains exactly one matching listing and every child,
    score, receipt and observed removal independently agrees.
    """

    if (
        case.get("phase") != "SHOP_SCREEN"
        or case.get("reason")
        != "shop_potion_replacement_resource_preparation"
        or case.get("action") != "potion"
        or case.get("decision_surface_kind") != "resource_preparation"
        or case.get("parent_choice_surface_pending") is not True
        or case.get("resource_parent_listing") not in (None, {})
    ):
        return False
    surfaces = {
        field: case.get(field)
        for field in (
            "resource_preparation_options", "canonical_choices",
            "candidates", "producer_candidates",
        )
    }
    if any(
        not isinstance(rows, list) or len(rows) != 3
        for rows in surfaces.values()
    ):
        return False

    def target_child_key(row):
        if not isinstance(row, dict):
            return None
        target = row.get("target")
        target = target if isinstance(target, dict) else {}
        instance = target.get("potion_instance_id")
        slot = target.get("slot")
        operation = str(target.get("operation") or "").casefold()
        if (
            target.get("kind") != "potion_resource"
            or not isinstance(instance, str) or not instance
            or type(slot) is not int
            or operation not in {"use", "discard"}
        ):
            return None
        return instance, slot, operation

    def producer_child_key(row):
        if not isinstance(row, dict):
            return None
        consequence = row.get("consequences")
        consequence = consequence if isinstance(consequence, dict) else {}
        instance = consequence.get("potion_instance_id")
        slot = consequence.get("potion_slot")
        operation = str(row.get("operation") or "").casefold()
        if (
            not isinstance(instance, str) or not instance
            or type(slot) is not int
            or operation not in {"use", "discard"}
        ):
            return None
        return instance, slot, operation

    surface_keys = {
        "resource": [
            target_child_key(row)
            for row in surfaces["resource_preparation_options"]
        ],
        "canonical": [
            target_child_key(row) for row in surfaces["canonical_choices"]
        ],
        "candidate": [
            target_child_key(row) for row in surfaces["candidates"]
        ],
        "producer": [
            producer_child_key(row) for row in surfaces["producer_candidates"]
        ],
    }
    if (
        any(None in keys or len(set(keys)) != 3
            for keys in surface_keys.values())
        or any(
            set(keys) != set(surface_keys["resource"])
            for keys in surface_keys.values()
        )
    ):
        return False

    canonical_by_key = dict(zip(
        surface_keys["canonical"], surfaces["canonical_choices"]
    ))
    candidate_by_key = dict(zip(
        surface_keys["candidate"], surfaces["candidates"]
    ))
    producer_by_key = dict(zip(
        surface_keys["producer"], surfaces["producer_candidates"]
    ))
    for child_key in surface_keys["resource"]:
        canonical = canonical_by_key[child_key]
        candidate = candidate_by_key[child_key]
        producer = producer_by_key[child_key]
        candidate_score = candidate.get("score")
        producer_score = producer.get("score")
        canonical_score = canonical.get("local_score")
        if (
            any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                for value in (
                    canonical_score, candidate_score, producer_score,
                )
            )
            or float(canonical_score) != float(candidate_score)
            or float(candidate_score) != float(producer_score)
            or type(canonical.get("selection_eligible")) is not bool
            or type(candidate.get("selection_eligible")) is not bool
            or type(producer.get("selection_eligible")) is not bool
            or canonical.get("selection_eligible")
            is not candidate.get("selection_eligible")
            or candidate.get("selection_eligible")
            is not producer.get("selection_eligible")
        ):
            return False

    producers = surfaces["producer_candidates"]
    legacy_tuple_fields = {
        "bound_purchase_listing_id", "bound_purchase_choice_index",
        "bound_purchase_item_id", "bound_purchase_price",
    }
    producer_consequences = [
        row.get("consequences") if isinstance(row, dict) else None
        for row in producers
    ]
    if any(not isinstance(value, dict) for value in producer_consequences):
        return False
    if any(
        any(field in value for field in legacy_tuple_fields)
        for value in producer_consequences
    ):
        return False
    bound_ids = {
        value.get("bound_new_potion_id")
        for value in producer_consequences
        if isinstance(value.get("bound_new_potion_id"), str)
        and value.get("bound_new_potion_id")
    }
    if len(bound_ids) != 1:
        return False
    bound_id = next(iter(bound_ids))
    if any(value.get("bound_new_potion_id") != bound_id
           for value in producer_consequences):
        return False

    matching_parents = []
    seen_option_ids = set()
    seen_choice_indexes = set()
    for option in case.get("available_options") or []:
        if not isinstance(option, dict):
            return False
        option_id = option.get("option_id")
        choice_index = option.get("choice_index")
        if (
            not isinstance(option_id, str) or not option_id
            or type(choice_index) is not int
            or option_id in seen_option_ids
            or choice_index in seen_choice_indexes
        ):
            return False
        seen_option_ids.add(option_id)
        seen_choice_indexes.add(choice_index)
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        if str(target.get("kind") or "").casefold() != "potion":
            continue
        item = target.get("item")
        item = item if isinstance(item, dict) else {}
        if (
            not isinstance(item.get("id"), str) or not item.get("id")
            or not isinstance(item.get("name"), str) or not item.get("name")
            or type(item.get("price")) is not int or item.get("price") < 0
            or any(
                type(item.get(field)) is not bool
                for field in ("can_use", "can_discard", "requires_target")
            )
        ):
            return False
        if item["id"].casefold() == bound_id.casefold():
            matching_parents.append((option_id, choice_index, item))
    if len(matching_parents) != 1:
        return False

    candidate_join = case.get("candidate_join")
    candidate_join = candidate_join if isinstance(candidate_join, dict) else {}
    candidate_contract = case.get("candidate_contract")
    candidate_contract = (
        candidate_contract if isinstance(candidate_contract, dict) else {}
    )
    if (
        candidate_join.get("status") != "clear"
        or candidate_join.get("canonical_choice_count") != 3
        or candidate_join.get("producer_candidate_count") != 3
        or any(candidate_join.get(field) != [] for field in (
            "missing_choice_indexes", "ambiguous_choice_indexes",
            "unmatched_candidate_indexes", "ambiguous_candidate_indexes",
            "semantic_mismatch_choice_indexes",
            "consequence_target_mismatch_choice_indexes",
            "producer_raw_binding_mismatch_choice_indexes",
            "unclassified_producer_choice_indexes",
            "operation_missing_choice_indexes",
            "operation_missing_candidate_indexes",
        ))
        or candidate_contract.get("strategy_quality_auditable") is not True
        or candidate_contract.get("all_visible_options_scored") is not True
        or candidate_contract.get("parent_screen_resolution_pending") is not True
        or candidate_contract.get("case_visible_option_coverage_complete") is not True
        or candidate_contract.get("strict_identity_join_complete") is not True
        or candidate_contract.get("independent_candidate_evidence_complete") is not True
        or candidate_contract.get("case_candidate_count") != 3
        or candidate_contract.get("case_unmatched_candidate_count") != 0
        or candidate_contract.get("case_duplicate_candidate_ids") != []
    ):
        return False

    selected_ids = case.get("selected_choice_ids")
    if (
        not isinstance(selected_ids, list) or len(selected_ids) != 1
        or case.get("final_choice_ids") != selected_ids
    ):
        return False
    selected_rows = [
        row for row in surfaces["canonical_choices"]
        if isinstance(row, dict)
        and row.get("choice_id") == selected_ids[0]
        and row.get("selected") is True
    ]
    if len(selected_rows) != 1 or sum(
        row.get("selected") is True
        for row in surfaces["canonical_choices"]
        if isinstance(row, dict)
    ) != 1:
        return False
    selected_target = selected_rows[0].get("target")
    selected_target = (
        selected_target if isinstance(selected_target, dict) else {}
    )
    instance_id = selected_target.get("potion_instance_id")
    potion_id = selected_target.get("potion_id")
    slot = selected_target.get("slot")
    operation = str(selected_target.get("operation") or "").casefold()
    if (
        selected_target.get("kind") != "potion_resource"
        or not isinstance(instance_id, str) or not instance_id
        or not isinstance(potion_id, str) or not potion_id
        or type(slot) is not int
        or operation not in {"use", "discard"}
    ):
        return False
    selected_child_key = instance_id, slot, operation
    for rows_by_key, score_field in (
        (canonical_by_key, "local_score"),
        (candidate_by_key, "score"),
        (producer_by_key, "score"),
    ):
        selected_surface_row = rows_by_key.get(selected_child_key)
        if (
            not isinstance(selected_surface_row, dict)
            or selected_surface_row.get("selection_eligible") is not True
        ):
            return False
        eligible_scores = [
            float(row[score_field])
            for row in rows_by_key.values()
            if row.get("selection_eligible") is True
        ]
        selected_surface_score = float(selected_surface_row[score_field])
        if (
            not eligible_scores
            or selected_surface_score != max(eligible_scores)
            or eligible_scores.count(selected_surface_score) != 1
        ):
            return False

    selected_producers = []
    eligible_scores = []
    for row, consequence in zip(producers, producer_consequences):
        score = row.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return False
        row_operation = str(row.get("operation") or "").casefold()
        if row_operation not in {"use", "discard"}:
            return False
        if row.get("selection_eligible") is not False:
            eligible_scores.append(float(score))
        if (
            consequence.get("potion_instance_id") == instance_id
            and consequence.get("potion_id") == potion_id
            and consequence.get("potion_slot") == slot
            and row_operation == operation
        ):
            selected_producers.append(row)
    if (
        len(selected_producers) != 1
        or selected_producers[0].get("selection_eligible") is False
        or selected_producers[0].get("reason")
        != "selected_resource_preparation_operation"
        or not eligible_scores
    ):
        return False
    selected_score = float(selected_producers[0]["score"])
    if (
        selected_score != max(eligible_scores)
        or eligible_scores.count(selected_score) != 1
    ):
        return False

    chosen = case.get("chosen")
    chosen = chosen if isinstance(chosen, dict) else {}
    if (
        case.get("requested_target_id") != instance_id
        or case.get("resolved_target_id") != instance_id
        or chosen.get("requested_target_id") != instance_id
        or chosen.get("resolved_target_id") != instance_id
        or chosen.get("choice_index") != slot
    ):
        return False
    settlement = case.get("authoritative_choice_settlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    observed = settlement.get("observed_outcome")
    observed = observed if isinstance(observed, dict) else {}
    outcome = case.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    zero_delta_fields = (
        "current_hp_delta", "hp_delta", "max_hp_delta", "gold_delta",
    )
    if (
        any(observed.get(field) != 0 for field in zero_delta_fields)
        or any(outcome.get(field) != 0 for field in zero_delta_fields)
        or any(
            observed.get(field) != outcome.get(field)
            for field in zero_delta_fields
        )
    ):
        return False
    observed_potions = observed.get("potions")
    observed_potions = (
        observed_potions if isinstance(observed_potions, dict) else {}
    )
    outcome_potions = outcome.get("potions")
    outcome_potions = outcome_potions if isinstance(outcome_potions, dict) else {}
    removed = observed_potions.get("removed")
    if (
        settlement.get("status") != "observed"
        or settlement.get("authority") != "protocol_state_delta"
        or settlement.get("fully_observable") is not True
        or settlement.get("choice_id") != selected_ids[0]
        or observed_potions != outcome_potions
        or not isinstance(removed, list) or len(removed) != 1
        or removed[0].get("potion_instance_id") != instance_id
        or removed[0].get("id") != potion_id
        or removed[0].get("slot") != slot
    ):
        return False

    before_game = (
        (case.get("authoritative_state_before") or {}).get("game_state") or {}
    )
    after_game = (
        (case.get("authoritative_state_after") or {}).get("game_state") or {}
    )
    before_potions = before_game.get("potions") or []
    after_potions = after_game.get("potions") or []
    before_matches = [
        row for row in before_potions
        if isinstance(row, dict)
        and row.get("potion_instance_id") == instance_id
        and row.get("id") == potion_id
        and row.get("slot") == slot
    ]
    return (
        len(before_matches) == 1
        and not any(
            isinstance(row, dict)
            and row.get("potion_instance_id") == instance_id
            for row in after_potions
        )
    )


def _case_fixture_id_from_binding(
    attempt_id,
    before_seq,
    phase,
    event_id,
    original_case_sha256,
    classification,
    problems,
):
    identity = (
        attempt_id, before_seq, phase, event_id, original_case_sha256,
    )
    binding = _CASE_FIXTURE_ALLOWLIST.get(identity)
    if binding is not None:
        expected_classification, expected_problems, fixture_id = binding
        if (
            classification == expected_classification
            and tuple(problems) == expected_problems
        ):
            return fixture_id
        # An allowlisted immutable case must retain its exact taxonomy.  Do
        # not let a changed problem set fall through to a broader old profile.
        return None
    # A one-field mutation of an allowlisted immutable identity is evidence
    # tampering, not a new profile member.  Fail closed before consulting the
    # broader historical profile table; otherwise changing only the attempt,
    # sequence, phase, event, or case digest could bypass the exact binding.
    if any(
        sum(left == right for left, right in zip(identity, protected))
        == len(identity) - 1
        for protected in _CASE_FIXTURE_ALLOWLIST
    ):
        return None
    return _profile_fixture_id(phase, classification, problems)


def _bowl_tie_case_shape(case):
    """Prove the exact historical Bowl-versus-return dominance boundary."""

    if case.get("phase") != "CARD_REWARD":
        return False
    candidates = case.get("candidates")
    if not isinstance(candidates, list):
        return False
    bowl_rows = [
        row for row in candidates
        if isinstance(row, dict)
        and str((row.get("target") or {}).get("kind") or "").casefold()
        == "bowl"
    ]
    return_rows = [
        row for row in candidates
        if isinstance(row, dict) and row.get("choice_id") == "action:return"
    ]
    if len(bowl_rows) != 1 or len(return_rows) != 1:
        return False
    bowl = bowl_rows[0]
    returned = return_rows[0]
    bowl_id = bowl.get("choice_id")
    score = bowl.get("local_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return False
    other_scores = [
        row.get("local_score") for row in candidates
        if row not in (bowl, returned)
        and row.get("selection_eligible") is not False
    ]
    if (
        returned.get("local_score") != score
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value >= score
            for value in other_scores
        )
        or case.get("selected_choice_ids") != [bowl_id]
        or case.get("final_choice_ids") != [bowl_id]
    ):
        return False
    bowl_effect = bowl.get("consequences")
    return_effect = returned.get("consequences")
    if not isinstance(bowl_effect, dict) or not isinstance(return_effect, dict):
        return False
    projection_v2 = (
        type((bowl.get("target") or {}).get("audit_projection_version"))
        is int
        and (bowl.get("target") or {}).get("audit_projection_version") >= 2
    )
    if (
        bowl_effect.get("operation") != "singing_bowl"
        or bowl_effect.get("hp_delta") != (2 if projection_v2 else 0)
        or bowl_effect.get("max_hp_delta") != 2
        or return_effect.get("operation") != "return"
        or return_effect.get("hp_delta") != 0
        or return_effect.get("max_hp_delta") != 0
    ):
        return False
    settlement = case.get("authoritative_choice_settlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    observed = settlement.get("observed_outcome")
    observed = observed if isinstance(observed, dict) else {}
    before_game = (
        (case.get("authoritative_state_before") or {}).get("game_state") or {}
    )
    after_game = (
        (case.get("authoritative_state_after") or {}).get("game_state") or {}
    )
    return bool(
        settlement.get("status") == "observed"
        and settlement.get("authority") == "protocol_state_delta"
        and settlement.get("fully_observable") is True
        and settlement.get("choice_id") == bowl_id
        and all(
            observed.get(field) == 2
            for field in ("current_hp_delta", "hp_delta", "max_hp_delta")
        )
        and all(
            type(game.get(field)) is int
            for game in (before_game, after_game)
            for field in ("current_hp", "max_hp")
        )
        and after_game["current_hp"] - before_game["current_hp"] == 2
        and after_game["max_hp"] - before_game["max_hp"] == 2
    )


def _case_fixture_id(case, classification, problems):
    key = case_key(case)
    if key is None:
        return None
    identity = (
        key[0], key[1], key[2], case.get("event_id"), object_digest(case),
    )
    if (
        classification == "unresolved"
        and tuple(problems) == (
            "score_cutoff_tie_is_semantically_ambiguous",
        )
        and _bowl_tie_case_shape(case)
    ):
        return "card_reward_bowl_dominates_skip_v1"
    fixture_id = _case_fixture_id_from_binding(
        key[0],
        key[1],
        key[2],
        case.get("event_id"),
        object_digest(case),
        classification,
        problems,
    )
    if (
        identity in _EXACT_RESOURCE_CASE_IDENTITIES
        and not _exact_resource_case_shape(case, fixture_id)
    ):
        return None
    if (
        identity in _EXACT_LEGACY_SHOP_PARENT_CASE_IDENTITIES
        and not _legacy_shop_parent_case_shape(case)
    ):
        return None
    return fixture_id


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def case_key(value):
    if not isinstance(value, dict):
        return None
    attempt_id = value.get("attempt_id")
    before_seq = value.get("before_seq")
    phase = value.get("phase")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return None
    if not isinstance(before_seq, int) or isinstance(before_seq, bool):
        return None
    if not isinstance(phase, str) or not phase.strip():
        return None
    return attempt_id, before_seq, phase


def problem_kinds(result):
    if not isinstance(result, dict):
        return []
    return sorted(set(
        str(value)
        for field in ("issues", "unknowns")
        for value in (result.get(field) or [])
        if isinstance(value, str) and value
    ))


def load_json_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResolutionError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ResolutionError(f"{label} is not an object")
    return value


def validate_fixture_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ResolutionError("resolution fixture catalog schema mismatch")
    rows = catalog.get("fixtures")
    if not isinstance(rows, list):
        raise ResolutionError("resolution fixture catalog is missing rows")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ResolutionError("resolution fixture row is not an object")
        fixture_id = row.get("fixture_id")
        if fixture_id in by_id:
            raise ResolutionError("duplicate resolution fixture id")
        by_id[fixture_id] = row
    if set(by_id) != set(_EXPECTED_FIXTURE_ROWS):
        raise ResolutionError("resolution fixture catalog coverage mismatch")
    for fixture_id, expected in _EXPECTED_FIXTURE_ROWS.items():
        if by_id[fixture_id] != expected:
            raise ResolutionError(
                f"resolution fixture definition mismatch: {fixture_id}"
            )
    return by_id


def _consequence():
    return {
        "schema_version": 1,
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": 0,
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


def _legacy_probe(option_count=2):
    letters = [chr(ord("a") + index) for index in range(option_count)]
    options = [
        {
            "option_id": f"option:{letter}",
            "choice_index": index,
            "label": letter.upper(),
            "target": {"kind": "event_option", "id": letter},
        }
        for index, letter in enumerate(letters)
    ]
    candidates = [
        {
            "choice_id": f"option:{letter}",
            "choice_index": index,
            "action": "choose",
            "semantic_id": f"event_option:{letter}",
            "score": float(index + 1),
            "consequences": _consequence(),
        }
        for index, letter in enumerate(letters)
    ]
    selected = f"option:{letters[-1]}"
    return {
        "case_schema_version": 1,
        "attempt_id": "resolution-fixture-attempt",
        "run_id": "IRONCLAD:0:resolution-fixture",
        "decision_hash": "resolution-fixture-hash",
        "before_seq": 1,
        "phase": "EVENT",
        "action": "choose",
        "chosen": {
            "requested_target_id": selected,
            "resolved_target_id": selected,
        },
        "available_options": options,
        "candidates": candidates,
        "model_advice": {"final_choice_ids": [selected]},
    }


def _permutation_variants(case):
    variants = []
    for reverse_options, reverse_candidates in (
        (True, False), (False, True), (True, True),
    ):
        value = copy.deepcopy(case)
        if reverse_options:
            value["available_options"].reverse()
        if reverse_candidates:
            value["candidates"].reverse()
        variants.append(value)
    return variants


def _permutation_specs(candidate_count, protocol_count):
    """Return only permutations that change a real production container."""

    if (
        type(candidate_count) is not int
        or candidate_count < 0
        or type(protocol_count) is not int
        or protocol_count < 0
    ):
        raise ResolutionError("permutation container cardinality is invalid")
    candidate_applicable = candidate_count >= 2
    protocol_applicable = protocol_count >= 2
    specs = []
    if candidate_applicable:
        specs.append(("candidate", True, False))
    if protocol_applicable:
        specs.append(("protocol", False, True))
    if candidate_applicable and protocol_applicable:
        specs.append(("candidate+protocol", True, True))
    return specs


def _permutation_axis_evidence(count):
    if count >= 2:
        return {
            "status": "clear",
            "authority": PERMUTATION_CLEAR_AUTHORITY,
            "reason": PERMUTATION_CLEAR_REASON,
        }
    return {
        "status": "not_applicable",
        "authority": PERMUTATION_NOT_APPLICABLE_AUTHORITY,
        "reason": PERMUTATION_NOT_APPLICABLE_REASON,
    }


def _negative_fixture(invariant_id):
    if invariant_id == "legacy_bijection_unbound_candidate":
        value = _legacy_probe(3)
        value["candidates"] = value["candidates"][:2]
        value["candidates"][1]["choice_id"] = "candidate:ghost"
        value["candidates"][1]["semantic_id"] = "ghost"
        value["candidates"][1]["choice_index"] = 99
        return value
    value = _legacy_probe(2)
    if invariant_id == "legacy_bijection_missing_option":
        value["candidates"].pop()
    elif invariant_id == "legacy_binding_ambiguous":
        for option in value["available_options"]:
            option["label"] = "Shared"
        for index, candidate in enumerate(value["candidates"]):
            candidate["choice_id"] = f"candidate:shared:{index}"
            candidate["semantic_id"] = "shared"
            candidate["label"] = "Shared"
    elif invariant_id == "legacy_binding_missing":
        value["candidates"][1]["choice_id"] = "candidate:ghost"
        value["candidates"][1]["semantic_id"] = "ghost"
        value["candidates"][1]["choice_index"] = 99
    elif invariant_id == "legacy_candidates_missing":
        value["candidates"] = []
    elif invariant_id == "legacy_final_choice_binding":
        value["model_advice"]["final_choice_ids"] = ["option:ghost"]
    elif invariant_id == "legacy_score_tie":
        for candidate in value["candidates"]:
            candidate["score"] = 1.0
    elif invariant_id == "legacy_below_argmax":
        value["candidates"][0]["score"] = 5.0
    else:
        raise ResolutionError(f"unknown invariant: {invariant_id}")
    return value


def _load_production_probe_modules():
    root = Path(__file__).resolve().parent
    spirecomm_root = root / "src" / "spirecomm-master"
    if str(spirecomm_root) not in sys.path:
        sys.path.insert(0, str(spirecomm_root))
    import autoplay
    import bridge
    import decision_cases
    from test_live_macro_decisions import (
        LiveDecisionGame,
        event_screen,
        grid_screen,
        make_card,
        make_map,
        neow_reward_contract,
        strike,
        defend,
        typed_neow_screen,
    )
    from spirecomm.ai.agent import SimpleAgent
    from spirecomm.spire.card import CardType
    from spirecomm.spire.character import PlayerClass
    from spirecomm.spire.map import Node
    from spirecomm.spire.potion import Potion
    from spirecomm.spire.relic import Relic
    from spirecomm.spire.screen import (
        BossRewardScreen,
        CardRewardScreen,
        ChestScreen,
        ChestType,
        CombatReward,
        CombatRewardScreen,
        EventOption,
        EventScreen,
        HandSelectScreen,
        MapScreen,
        RewardType,
        RestOption,
        RestScreen,
        ShopRoomScreen,
        ShopScreen,
    )
    from unittest.mock import patch
    return locals()


def _card_json(card, *, include_price=False):
    value = {
        "id": getattr(card, "card_id", None),
        "name": getattr(card, "name", None),
        "uuid": getattr(card, "uuid", None),
        "type": getattr(getattr(card, "type", None), "name", None),
        "rarity": getattr(getattr(card, "rarity", None), "name", None),
        "upgrades": int(getattr(card, "upgrades", 0) or 0),
        "has_target": bool(getattr(card, "has_target", False)),
        "cost": int(getattr(card, "cost", 0) or 0),
        "is_playable": bool(getattr(card, "is_playable", False)),
        "exhausts": bool(getattr(card, "exhausts", False)),
        "damage": int(getattr(card, "damage", 0) or 0),
        "base_damage": getattr(card, "base_damage", None),
        "block": int(getattr(card, "block", 0) or 0),
        "base_block": getattr(card, "base_block", None),
        "magic_number": int(getattr(card, "magic_number", 0) or 0),
        "in_bottle_flame": bool(
            getattr(card, "in_bottle_flame", False)
        ),
        "in_bottle_lightning": bool(
            getattr(card, "in_bottle_lightning", False)
        ),
        "in_bottle_tornado": bool(
            getattr(card, "in_bottle_tornado", False)
        ),
    }
    if include_price:
        value["price"] = int(getattr(card, "price", 0) or 0)
    return value


def _relic_json(relic):
    return {
        "id": getattr(relic, "relic_id", None),
        "name": getattr(relic, "name", None),
        "counter": int(getattr(relic, "counter", 0) or 0),
        "price": int(getattr(relic, "price", 0) or 0),
    }


def _potion_json(potion):
    return {
        "id": getattr(potion, "potion_id", None),
        "name": getattr(potion, "name", None),
        "price": int(getattr(potion, "price", 0) or 0),
        "can_use": bool(getattr(potion, "can_use", False)),
        "can_discard": bool(getattr(potion, "can_discard", False)),
        "requires_target": bool(getattr(potion, "requires_target", False)),
    }


def _production_probe(probe_id, *, production_variant=None):
    """Run one real SimpleAgent phase producer on a deterministic surface."""

    m = _load_production_probe_modules()
    SimpleAgent = m["SimpleAgent"]
    PlayerClass = m["PlayerClass"]
    LiveDecisionGame = m["LiveDecisionGame"]
    patch = m["patch"]
    screen_state = {}
    choice_list = []
    available_commands = ["choose", "state"]
    raw_screen_type = None
    game_potions = []

    if probe_id == "duplicate_uuid_grid":
        first = m["make_card"]("Twin", uuid="grid-twin-a")
        second = m["make_card"]("Twin", uuid="grid-twin-b")
        game = LiveDecisionGame(
            m["grid_screen"]([first, second], for_upgrade=True),
            deck=[first, second],
        )
        game.current_action = "Smith"
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        with patch.object(
            agent, "_upgrade_score",
            side_effect=lambda card: 11 if card.uuid == "grid-twin-a" else 10,
        ):
            action = agent.get_next_action_in_game(game)
        phase = "GRID"
        cards = [first, second]
        screen_state = {
            "cards": [_card_json(card) for card in cards],
            "selected_cards": [], "num_cards": 1,
            "any_number": False, "confirm_up": False,
            "for_upgrade": True, "for_transform": False,
            "for_purge": False,
        }
        choice_list = [card.name for card in cards]
        selected_index = next(
            index for index, card in enumerate(cards)
            if action.cards[0].uuid == card.uuid
        )
    elif probe_id in {"map_veto", "map_choices", "map_maw_bank"}:
        current = m["Node"](0, 5, "M")
        if probe_id == "map_veto":
            nodes = [m["Node"](0, 6, "E"), m["Node"](1, 6, "M")]
            act, floor = 3, 40
        else:
            nodes = [m["Node"](0, 6, "M"), m["Node"](1, 6, "?")]
            act, floor = 2, 20
        current.children = list(nodes)
        game = LiveDecisionGame(
            m["MapScreen"](current, nodes, boss_available=False),
            dungeon_map=m["make_map"](current, *nodes),
            hp=80, max_hp=80, act=act, floor=floor,
        )
        if probe_id == "map_veto":
            game.key_system_unlocked = True
            game.has_emerald_key = True
        elif probe_id == "map_maw_bank":
            # Maw Bank grants exactly 12 gold when a room is entered until it
            # is used up.  Exercise that authoritative map-entry effect, not
            # a generic zero-delta map choice.
            game.relics = [m["Relic"]("MawBank", "Maw Bank", counter=0)]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "MAP"
        screen_state = {
            "boss_available": False,
            "next_nodes": [
                {"x": node.x, "y": node.y, "symbol": node.symbol}
                for node in nodes
            ],
        }
        choice_list = [node.symbol for node in nodes]
        selected_index = next(
            index for index, node in enumerate(nodes) if action.node is node
        )
    elif probe_id in {"sensory_stone", "match_keep"}:
        if probe_id == "sensory_stone":
            labels = [
                "Add 1 Colorless card",
                "Add 2 Colorless cards. Lose 5 HP",
                "Add 3 Colorless cards. Lose 10 HP",
            ]
            screen = m["event_screen"]("SensoryStone", labels)
            game = LiveDecisionGame(
                screen, deck=[m["strike"](), m["defend"]()],
                hp=76, max_hp=80, act=3, floor=38,
            )
            game.has_ruby_key = True
            game.has_sapphire_key = True
            agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
            with patch.object(
                agent, "_deck_readiness", return_value={"score": 0.904}
            ):
                action = agent.get_next_action_in_game(game)
        else:
            labels = ["Twin", "Twin", "Twin", "Twin"]
            screen = m["EventScreen"](
                "Match and Keep", "Match and Keep", ""
            )
            screen.options = [
                m["EventOption"](
                    label, label, False, index,
                    original_button_index=index,
                )
                for index, label in enumerate(labels)
            ]
            game = LiveDecisionGame(screen, hp=70, max_hp=70)
            agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
            action = agent.get_next_action_in_game(game)
        phase = "EVENT"
        screen_state = {
            "event_id": screen.event_id,
            "event_name": screen.event_name,
            "body_text": "",
            "options": [
                {
                    "label": option.label, "text": option.text,
                    "disabled": bool(option.disabled),
                    "choice_index": option.choice_index,
                    "original_button_index": option.original_button_index,
                }
                for option in screen.options
            ],
        }
        choice_list = list(labels)
        selected_index = action.choice_index
    elif probe_id == "shop_visible":
        card = m["make_card"]("ShopTwin", uuid="shop-card-a")
        card.price = 40
        relic = m["Relic"]("DataDisk", "Data Disk", price=50)
        potion = m["Potion"](
            "SpeedPotion", "Speed Potion", True, True, False, price=45
        )
        game = LiveDecisionGame(
            m["ShopScreen"]([card], [relic], [potion], True, 75),
            deck=[m["strike"](), m["defend"]()], gold=200,
        )
        # The real bridge binds the visible purge service at index 0 and
        # annotates each listing with its protocol index before the agent
        # produces candidates.  This probe must exercise that same binding;
        # otherwise the producer falls back to local container order and the
        # canonical card/relic/potion rows shift left of the raw purge row.
        setattr(game.screen, "protocol_purge_choice_index", 0)
        setattr(card, "protocol_choice_index", 1)
        setattr(relic, "protocol_choice_index", 2)
        setattr(potion, "protocol_choice_index", 3)
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "SHOP_SCREEN"
        screen_state = {
            "cards": [_card_json(card, include_price=True)],
            "relics": [_relic_json(relic)],
            "potions": [_potion_json(potion)],
            "purge_available": True, "purge_cost": 75,
        }
        choice_list = ["purge", card.name, relic.name, potion.name]
        available_commands.append("cancel")
        if hasattr(action, "card"):
            selected_index = 1
        elif hasattr(action, "relic"):
            selected_index = 2
        elif hasattr(action, "potion"):
            selected_index = 3
        else:
            selected_index = 0
    elif probe_id == "shop_resource_preparation":
        # Run the full-belt producer afresh for every permutation.  The
        # selected duplicate-id instance remains in the middle slot while
        # both the held-potion container and the multi-potion parent screen
        # are independently reversed.
        variant = production_variant or "baseline"
        if variant not in {
            "baseline", "candidate", "protocol", "candidate+protocol",
        }:
            raise ResolutionError(
                f"unknown shop resource-preparation variant: {variant}"
            )
        left = m["Potion"](
            "SpeedPotion", "Speed Potion", False, True, False, price=0
        )
        selected_held = m["Potion"](
            "SpeedPotion", "Speed Potion", False, True, False, price=0
        )
        right = m["Potion"](
            "FearPotion", "Fear Potion", False, True, True, price=0
        )
        # Deliberately expose two same-id/name listings.  Only raw option id,
        # index and price distinguish the parent actually selected by the
        # producer, so an id-only parent join cannot pass this fixture.
        decoy = m["Potion"](
            "LiquidMemories", "Liquid Memories", True, True, False,
            price=65,
        )
        replacement = m["Potion"](
            "LiquidMemories", "Liquid Memories", True, True, False,
            price=45,
        )
        held = [left, selected_held, right]
        listings = [decoy, replacement]
        if variant in {"candidate", "candidate+protocol"}:
            held.reverse()
        if variant in {"protocol", "candidate+protocol"}:
            listings.reverse()
        game = LiveDecisionGame(
            m["ShopScreen"]([], [], listings, False, 75),
            deck=[m["strike"](), m["defend"]()], gold=200,
        )
        game.potion_available = True
        game.potions = list(held)
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(held)
        phase = "SHOP_SCREEN"
        screen_state = {
            "cards": [], "relics": [],
            "potions": [_potion_json(potion) for potion in listings],
            "purge_available": False, "purge_cost": 75,
        }
        choice_list = [potion.name for potion in listings]
        available_commands.extend(["cancel", "potion"])
        raw_override = {
            "available_commands": list(available_commands),
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "class": "IRONCLAD", "current_hp": game.current_hp,
                "max_hp": game.max_hp, "floor": game.floor,
                "act": game.act, "gold": game.gold, "seed": 424242,
                "ascension_level": 0, "is_standard_run": True,
                "relics": [], "deck": [], "map": [],
                "potions": [_potion_json(potion) for potion in held],
                "screen_type": phase, "screen_state": screen_state,
                "choice_list": choice_list,
                "current_action": getattr(game, "current_action", None),
                "room_phase": "INCOMPLETE", "room_type": "ShopRoom",
                "is_screen_up": True,
                "has_ruby_key": False, "has_emerald_key": False,
                "has_sapphire_key": False,
            },
        }
        # Match the live controller ordering: enrich the authoritative raw
        # surface and bind its exact option ids/indexes onto parsed listings
        # before the policy produces any resource-preparation candidate.
        bound_state = m["bridge"].enrich_state(
            copy.deepcopy(raw_override), 100
        )
        m["autoplay"].bind_shop_protocol_surface(game, bound_state)
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        keep_values = {
            id(left): 20.0,
            id(selected_held): 10.0,
            id(right): 30.0,
            id(decoy): 25.0,
            id(replacement): 40.0,
        }
        with patch.object(
            agent, "_potion_keep_score",
            side_effect=lambda potion: keep_values[id(potion)],
        ), patch.object(
            agent, "_shop_potion_value",
            side_effect=lambda potion: (
                30.0 if potion is replacement else 15.0
            ),
        ):
            action = agent.get_next_action_in_game(game)
        selected_slots = [
            index for index, potion in enumerate(held)
            if potion is getattr(action, "potion", None)
        ]
        selected_slot = (
            selected_slots[0] if len(selected_slots) == 1 else None
        )
        if (
            getattr(action, "command", None) != "potion"
            or getattr(action, "use", None) is not False
            or getattr(action, "potion", None) is not selected_held
            or selected_slot != 1
        ):
            raise ResolutionError(
                "shop resource-preparation probe did not discard its bound slot"
            )
        selected_index = listings.index(replacement)
        game_potions = list(held)
        selected_payload = {
            "action": "potion",
            "operation": "discard",
            "potion_instance_id": agent._audit_potion_instance_id(
                selected_held, selected_slot
            ),
        }
        decision_surface_kind = "resource_preparation"
        producer_execution_variant = variant
        producer_execution_nonce = object_digest([
            "fresh-live-producer-execution-v1", probe_id, variant,
        ])
        producer_execution_digest = object_digest({
            "schema_version": 1,
            "execution_nonce": producer_execution_nonce,
            "held_potion_order": [potion.potion_id for potion in held],
            "parent_shop_potion_order": [
                potion.potion_id for potion in listings
            ],
            "selected_payload": selected_payload,
            "producer_decision": agent.last_noncombat_decision,
        })
    elif probe_id == "combat_reward_resource_preparation":
        # Each ordering variant constructs a fresh game, screen, agent, held
        # objects and action.  The selected low-value potion remains in the
        # middle slot when the held container is reversed, so its immutable
        # bridge identity and slot are directly comparable across independent
        # producer executions.
        variant = production_variant or "baseline"
        if variant not in {
            "baseline", "candidate", "protocol", "candidate+protocol",
        }:
            raise ResolutionError(
                f"unknown combat-reward production variant: {variant}"
            )
        left = m["Potion"](
            "EntropicBrew", "Entropic Brew",
            True, True, False, price=0,
        )
        selected_held = m["Potion"](
            "WeakPotion", "Weak Potion", False, True, False, price=0
        )
        right = m["Potion"](
            "SpeedPotion", "Speed Potion", False, True, False, price=0
        )
        replacement = m["Potion"](
            "LiquidMemories", "Liquid Memories", True, True, False,
            price=0,
        )
        potion_reward = m["CombatReward"](
            m["RewardType"].POTION, potion=replacement
        )
        gold_reward = m["CombatReward"](
            m["RewardType"].GOLD, gold=1
        )
        rewards = [potion_reward, gold_reward]
        held = [left, selected_held, right]
        if variant in {"candidate", "candidate+protocol"}:
            held.reverse()
        if variant in {"protocol", "candidate+protocol"}:
            rewards.reverse()
        game = LiveDecisionGame(
            m["CombatRewardScreen"](rewards),
            deck=[m["strike"](), m["defend"]()], act=1, floor=5,
        )
        game.potion_available = True
        game.potions = list(held)
        game.are_potions_full = lambda: True
        game.get_real_potions = lambda: list(held)
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        keep_values = {
            "EntropicBrew": 30.0,
            "WeakPotion": 10.0,
            "SpeedPotion": 20.0,
            "LiquidMemories": 40.0,
        }
        with patch.object(
            agent, "_potion_keep_score",
            side_effect=lambda potion: keep_values[potion.potion_id],
        ):
            action = agent.get_next_action_in_game(game)
        selected_slots = [
            index for index, potion in enumerate(held)
            if potion is getattr(action, "potion", None)
        ]
        selected_slot = (
            selected_slots[0] if len(selected_slots) == 1 else None
        )
        if (
            getattr(action, "command", None) != "potion"
            or getattr(action, "use", None) is not False
            or getattr(action, "potion", None) is not selected_held
            or selected_slot != 1
        ):
            raise ResolutionError(
                "combat-reward resource-preparation probe did not discard "
                "its exact bound held-potion instance"
            )
        phase = "COMBAT_REWARD"
        reward_json = {
            id(potion_reward): {
                "reward_type": "POTION",
                "potion": _potion_json(replacement),
            },
            id(gold_reward): {"reward_type": "GOLD", "gold": 1},
        }
        reward_labels = {
            id(potion_reward): replacement.name,
            id(gold_reward): "gold",
        }
        screen_state = {
            "rewards": [copy.deepcopy(reward_json[id(item)]) for item in rewards],
        }
        choice_list = [reward_labels[id(item)] for item in rewards]
        available_commands.extend(["proceed", "potion"])
        selected_index = rewards.index(potion_reward)
        game_potions = list(held)
        selected_payload = {
            "action": "potion",
            "operation": "discard",
            "potion_instance_id": agent._audit_potion_instance_id(
                selected_held, selected_slot
            ),
        }
        decision_surface_kind = "resource_preparation"
        producer_execution_variant = variant
        producer_execution_nonce = object_digest([
            "fresh-live-producer-execution-v1",
            probe_id,
            variant,
        ])
        producer_execution_digest = object_digest({
            "schema_version": 1,
            "execution_nonce": producer_execution_nonce,
            "held_potion_order": [
                potion.potion_id for potion in held
            ],
            "parent_reward_order": [
                str(
                    getattr(
                        getattr(reward, "reward_type", None),
                        "name", getattr(reward, "reward_type", ""),
                    )
                )
                for reward in rewards
            ],
            "selected_payload": selected_payload,
            "producer_decision": agent.last_noncombat_decision,
        })
    elif probe_id in {
        "boss_relic", "boss_relic_empty_cage",
        "boss_relic_starter_replacement",
    }:
        first = m["Relic"](
            "Empty Cage" if probe_id == "boss_relic_empty_cage"
            else "Black Blood"
            if probe_id == "boss_relic_starter_replacement"
            else "CoffeeDripper",
            "Empty Cage" if probe_id == "boss_relic_empty_cage"
            else "Black Blood"
            if probe_id == "boss_relic_starter_replacement"
            else "Coffee Dripper",
        )
        second = m["Relic"]("BlackStar", "Black Star")
        relics = [first, second]
        game = LiveDecisionGame(
            m["BossRewardScreen"](relics),
            deck=[m["strike"](), m["defend"]()], act=1, floor=16,
        )
        if probe_id == "boss_relic_starter_replacement":
            game.relics = [m["Relic"]("Burning Blood", "Burning Blood")]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        with patch.object(
            agent, "_boss_relic_score",
            side_effect=lambda relic: 20 if relic is first else 10,
        ):
            action = agent.get_next_action_in_game(game)
        phase = "BOSS_REWARD"
        screen_state = {"relics": [_relic_json(relic) for relic in relics]}
        choice_list = [relic.name for relic in relics]
        selected_index = next(
            index for index, relic in enumerate(relics)
            if action.relic is relic
        )
    elif probe_id == "sapphire_link":
        linked = m["Relic"]("DataDisk", "Data Disk")
        key_reward = m["CombatReward"](m["RewardType"].SAPPHIRE_KEY, link=linked)
        relic_reward = m["CombatReward"](m["RewardType"].RELIC, relic=linked)
        rewards = [key_reward, relic_reward]
        game = LiveDecisionGame(
            m["CombatRewardScreen"](rewards),
            deck=[m["strike"](), m["defend"]()], act=1, floor=8,
        )
        game.has_sapphire_key = False
        agent = SimpleAgent(PlayerClass.DEFECT, goal_mode="HEART")
        with patch.object(agent, "_relic_acquisition_score", return_value=25):
            action = agent.get_next_action_in_game(game)
        if getattr(action, "combat_reward", None) is not relic_reward:
            raise ResolutionError(
                "sapphire-link probe did not choose the exact linked relic"
            )
        # Re-emit the choice through the real combat-reward producer with the
        # deferred key explicitly vetoed.  The generic audit surface must
        # retain the same Act-1 opportunity value used by the live heart plan;
        # a stale fixed 1000 score would falsely report every intentional
        # deferral as a strategy disagreement.
        agent._record_combat_reward_choice(
            "heart_plan_sapphire_defer_for_linked_relic",
            relic_reward,
            candidate_vetoes={
                id(key_reward): (
                    "heart_plan_sapphire_deferred_for_linked_relic"
                ),
            },
        )
        key_candidate = next(
            row for row in agent.last_noncombat_decision["candidates"]
            if (row.get("consequences") or {}).get("operation")
            == "gain_sapphire_key"
        )
        if (
            key_candidate.get("score") != 4.0
            or key_candidate.get("selection_eligible") is not False
        ):
            raise ResolutionError(
                "sapphire-link probe did not retain its dynamic ineligible key"
            )
        # Protocol v2 deliberately classifies any combat reward surface that
        # contains SAPPHIRE_KEY as SAPPHIRE_KEY.  The legacy cases below were
        # recorded as COMBAT_REWARD; keep that migration explicit in the
        # fixture registry instead of pretending the current phase is the
        # old one.
        phase = "SAPPHIRE_KEY"
        available_commands.append("confirm")
        screen_state = {
            "rewards": [
                {
                    "reward_type": "SAPPHIRE_KEY",
                    "link": _relic_json(linked),
                },
                {
                    "reward_type": "RELIC",
                    "relic": _relic_json(linked),
                },
            ],
        }
        choice_list = ["Sapphire Key", linked.name]
        selected_index = next(
            index for index, reward in enumerate(rewards)
            if action.combat_reward is reward
        )
    elif probe_id == "sapphire_bloody_idol_gold":
        gold_reward = m["CombatReward"](m["RewardType"].GOLD, gold=80)
        linked = m["Relic"]("FrozenEgg2", "Frozen Egg")
        relic_reward = m["CombatReward"](
            m["RewardType"].RELIC, relic=linked
        )
        key_reward = m["CombatReward"](
            m["RewardType"].SAPPHIRE_KEY, link=linked
        )
        rewards = [gold_reward, relic_reward, key_reward]
        game = LiveDecisionGame(
            m["CombatRewardScreen"](rewards),
            deck=[m["strike"](), m["defend"]()],
            act=2, floor=26, hp=40, max_hp=80,
        )
        game.has_sapphire_key = False
        game.relics = [m["Relic"]("BloodyIdol", "Bloody Idol")]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        with patch.object(agent, "_relic_acquisition_score", return_value=35):
            action = agent.get_next_action_in_game(game)
        if getattr(action, "combat_reward", None) is not gold_reward:
            raise ResolutionError(
                "bloody-idol sapphire probe did not choose the gold reward"
            )
        phase = "SAPPHIRE_KEY"
        available_commands.append("confirm")
        screen_state = {
            "rewards": [
                {"reward_type": "GOLD", "gold": 80},
                {
                    "reward_type": "RELIC",
                    "relic": _relic_json(linked),
                },
                {
                    "reward_type": "SAPPHIRE_KEY",
                    "link": _relic_json(linked),
                },
            ],
        }
        choice_list = ["80 Gold", linked.name, "Sapphire Key"]
        selected_index = 0
        combat_reward_gold_settlement = {"gold_delta": 80, "hp_delta": 5}
    elif probe_id == "card_reward_bowl_dominates_skip":
        cards = [
            m["make_card"]("Fixture Marginal A", uuid="bowl-marginal-a"),
            m["make_card"]("Fixture Marginal B", uuid="bowl-marginal-b"),
        ]
        game = LiveDecisionGame(
            m["CardRewardScreen"](cards, can_bowl=True, can_skip=True),
            deck=[m["strike"](), m["defend"]()], act=2, floor=22,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        with patch.object(
            agent, "_permanent_card_pick_hurdle", return_value=12.0,
        ):
            action = agent.get_next_action_in_game(game)
        if getattr(action, "bowl", None) is not True:
            raise ResolutionError("bowl dominance probe did not select Bowl")
        phase = "CARD_REWARD"
        screen_state = {
            "cards": [_card_json(card) for card in cards],
            "bowl_available": True, "skip_available": True,
        }
        choice_list = [card.name for card in cards] + ["Singing Bowl"]
        available_commands.append("return")
        selected_index = len(cards)
    elif probe_id == "card_reward":
        cards = [
            m["make_card"]("Pommel Strike", uuid="reward-pommel"),
            m["make_card"]("Shrug It Off", uuid="reward-shrug"),
        ]
        game = LiveDecisionGame(
            m["CardRewardScreen"](cards, can_bowl=False, can_skip=True),
            deck=[m["strike"](), m["defend"]()], act=1, floor=4,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        if not hasattr(action, "card"):
            raise ResolutionError("card reward probe unexpectedly skipped")
        phase = "CARD_REWARD"
        screen_state = {
            "cards": [_card_json(card) for card in cards],
            "bowl_available": False, "skip_available": True,
        }
        choice_list = [card.name for card in cards]
        available_commands.append("return")
        selected_index = next(
            index for index, card in enumerate(cards) if action.card is card
        )
    elif probe_id == "hand_select":
        first = m["make_card"](
            "Noxious Fumes",
            card_type=m["CardType"].POWER,
            uuid="hand-select-power",
        )
        second = m["defend"]()
        second.uuid = "hand-select-defend"
        cards = [first, second]
        game = LiveDecisionGame(
            m["HandSelectScreen"](cards, [], 1, False),
            deck=cards, act=2, floor=22,
        )
        game.current_action = "DiscardAction"
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        if not getattr(action, "cards", None):
            raise ResolutionError("hand-select probe did not select a card")
        phase = "HAND_SELECT"
        screen_state = {
            "hand": [_card_json(card) for card in cards],
            "selected_cards": [],
            "max_cards": 1,
            "can_pick_zero": False,
        }
        choice_list = [card.name for card in cards]
        selected_index = next(
            index for index, card in enumerate(cards)
            if action.cards[0].uuid == card.uuid
        )
    elif probe_id == "hand_select_plan_protection":
        # This is the actual regression shape: a mandatory combat overlay
        # must remove one card, while the immediately preceding combat plan
        # needs a particular future card.  Give that protected card a higher
        # standalone removal score so the invariant proves that the bound
        # veto, rather than an accidental score ordering, preserves it.
        planned = m["defend"]()
        planned.uuid = "hand-plan-protected-defend"
        spare = m["strike"]()
        spare.uuid = "hand-plan-spare-strike"
        cards = [planned, spare]
        game = LiveDecisionGame(
            m["HandSelectScreen"](cards, [], 1, False),
            deck=cards, act=2, floor=22,
        )
        game.in_combat = True
        game.turn = 1
        game.current_action = "ExhaustAction"
        agent = SimpleAgent(PlayerClass.THE_SILENT, goal_mode="HEART")
        agent.game = game
        agent._combat_epoch = 1
        agent.combat_planner.last_decision = {
            "planned_sequence": [{
                "card_id": "Defend_G",
                "card_uuid": planned.uuid,
            }],
            "_combat_context": agent._combat_plan_context(),
        }
        with patch.object(
            agent,
            "_hand_card_keep_score_parts",
            side_effect=lambda card: {
                "fixture_keep_value": -10.0 if card is planned else 0.0,
            },
        ):
            action = agent.handle_screen()
        if not getattr(action, "cards", None) or action.cards[0] is not spare:
            raise ResolutionError(
                "hand-plan probe did not remove its unprotected card"
            )
        protection = (agent.last_noncombat_decision or {}).get(
            "combat_plan_protection"
        )
        if not isinstance(protection, dict) or protection.get(
            "protected_card_instance_ids"
        ) != [planned.uuid]:
            raise ResolutionError("hand-plan probe did not emit bound protection")
        phase = "HAND_SELECT"
        screen_state = {
            "hand": [_card_json(card) for card in cards],
            "selected_cards": [],
            "max_cards": 1,
            "can_pick_zero": False,
        }
        choice_list = [card.name for card in cards]
        selected_index = 1
    elif probe_id == "rest":
        options = [m["RestOption"].REST, m["RestOption"].SMITH]
        game = LiveDecisionGame(
            m["RestScreen"](False, options),
            deck=[m["strike"](), m["defend"]()],
            hp=28, max_hp=70, act=2, floor=24,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "REST"
        screen_state = {
            "has_rested": False,
            "rest_options": [option.name for option in options],
        }
        choice_list = [option.name for option in options]
        selected_index = options.index(action.rest_option)
    elif probe_id == "chest":
        screen = m["ChestScreen"](m["ChestType"].MEDIUM, False)
        game = LiveDecisionGame(
            screen, deck=[m["strike"](), m["defend"]()], act=2, floor=17,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "CHEST"
        screen_state = {"chest_type": "MediumChest", "chest_open": False}
        choice_list = ["Open"]
        available_commands.append("proceed")
        selected_index = 0
    elif probe_id == "shop_room":
        game = LiveDecisionGame(
            m["ShopRoomScreen"](),
            deck=[m["strike"](), m["defend"]()], act=2, floor=20,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "SHOP_ROOM"
        screen_state = {}
        choice_list = ["Enter Shop"]
        available_commands.append("proceed")
        selected_index = 0
    elif probe_id in {"combat_reward", "combat_reward_skip"}:
        potion = m["Potion"](
            "SpeedPotion", "Speed Potion", True, True, False, price=0
        )
        reward = m["CombatReward"](m["RewardType"].POTION, potion=potion)
        rewards = [reward]
        game = LiveDecisionGame(
            m["CombatRewardScreen"](rewards),
            deck=[m["strike"](), m["defend"]()], act=1, floor=5,
        )
        if probe_id == "combat_reward_skip":
            held = m["Potion"](
                "FairyPotion", "Fairy in a Bottle",
                False, False, False, price=0,
            )
            game.are_potions_full = lambda: True
            game.get_real_potions = lambda: [held]
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "COMBAT_REWARD"
        screen_state = {
            "rewards": [{
                "reward_type": "POTION", "potion": _potion_json(potion),
            }],
        }
        choice_list = [potion.name]
        available_commands.append("proceed")
        selected_index = 0
        selected_action = (
            "proceed" if probe_id == "combat_reward_skip" else None
        )
    elif probe_id == "event_leave":
        labels = ["Gain a relic", "Leave"]
        screen = m["event_screen"]("Modded Crossroads", labels)
        game = LiveDecisionGame(
            screen, deck=[m["strike"](), m["defend"]()], hp=30, max_hp=70,
        )
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "EVENT"
        screen_state = {
            "event_id": screen.event_id, "event_name": screen.event_name,
            "body_text": "",
            "options": [
                {
                    "label": option.label, "text": option.text,
                    "disabled": bool(option.disabled),
                    "choice_index": option.choice_index,
                }
                for option in screen.options
            ],
        }
        choice_list = labels
        selected_index = action.choice_index
    elif probe_id == "neow":
        labels = ["Gain 100 gold", "Gain max HP"]
        contracts = [
            m["neow_reward_contract"]("HUNDRED_GOLD", hp_bonus=7),
            m["neow_reward_contract"]("TEN_PERCENT_HP_BONUS", hp_bonus=7),
        ]
        screen = m["typed_neow_screen"](labels, contracts)
        game = LiveDecisionGame(screen, hp=70, max_hp=70)
        agent = SimpleAgent(PlayerClass.IRONCLAD, goal_mode="HEART")
        action = agent.get_next_action_in_game(game)
        phase = "NEOW"
        raw_screen_type = "EVENT"
        screen_state = {
            "event_id": screen.event_id, "event_name": screen.event_name,
            "body_text": "",
            "options": [
                {
                    "label": option.label, "text": option.text,
                    "disabled": bool(option.disabled),
                    "choice_index": option.choice_index,
                    "original_button_index": getattr(
                        option, "original_button_index", option.choice_index
                    ),
                    "neow_contract": option.neow_contract,
                }
                for option in screen.options
            ],
        }
        choice_list = labels
        selected_index = action.choice_index
    else:
        raise ResolutionError(f"unknown production probe: {probe_id}")

    raw = locals().get("raw_override") or {
        "available_commands": available_commands,
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "class": "IRONCLAD", "current_hp": game.current_hp,
            "max_hp": game.max_hp, "floor": game.floor, "act": game.act,
            "gold": game.gold, "seed": 424242, "ascension_level": 0,
            "is_standard_run": True,
            "relics": [_relic_json(relic) for relic in game.relics],
            "deck": [],
            "map": [],
            "potions": [_potion_json(potion) for potion in game_potions],
            "screen_type": raw_screen_type or phase,
            "screen_state": screen_state, "choice_list": choice_list,
            "current_action": getattr(game, "current_action", None),
            "room_phase": "INCOMPLETE", "room_type": "EventRoom",
            "is_screen_up": True,
            "has_ruby_key": bool(getattr(game, "has_ruby_key", False)),
            "has_emerald_key": bool(getattr(game, "has_emerald_key", False)),
            "has_sapphire_key": bool(getattr(game, "has_sapphire_key", False)),
        },
    }
    return {
        "phase": phase,
        "raw": raw,
        "decision": copy.deepcopy(agent.last_noncombat_decision),
        "selected_index": selected_index,
        "selected_action": locals().get("selected_action"),
        "selected_payload": locals().get("selected_payload"),
        "decision_surface_kind": locals().get("decision_surface_kind"),
        "producer_execution_variant": locals().get(
            "producer_execution_variant"
        ),
        "producer_execution_nonce": locals().get(
            "producer_execution_nonce"
        ),
        "producer_execution_digest": locals().get(
            "producer_execution_digest"
        ),
        "map_entry_gold_delta": 12 if probe_id == "map_maw_bank" else None,
        "combat_reward_gold_settlement": locals().get(
            "combat_reward_gold_settlement"
        ),
    }


def _build_probe_case(
    probe, *, reverse_candidates=False, reverse_protocol_options=False,
):
    m = _load_production_probe_modules()
    bridge = m["bridge"]
    autoplay = m["autoplay"]
    decision_cases = m["decision_cases"]
    state = bridge.enrich_state(copy.deepcopy(probe["raw"]), 101)
    if reverse_protocol_options:
        # Reorder the bound protocol option container before the production
        # canonicalizer runs.  ``choice_index`` and semantic targets remain
        # unchanged, so only accidental container-order dependence can move
        # the final semantic choice.
        state["options"] = list(reversed(state.get("options") or []))
    state.update({
        "state_seq": 101,
        "protocol_version": 2,
        "attempt_id": "decision-case-production-probe",
        "run_id": "IRONCLAD:0:decision-case-production-probe",
        "seed": 424242,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "policy_version": "fast-policy-v5",
        "decision_hash": "decision-case-production-probe-hash",
        "controller_hash": "decision-case-production-probe-controller",
        "selection_id": "decision-case-production-probe-selection",
        "selection_digest": "d" * 64,
    })
    options = state.get("options") or []
    selected_index = probe["selected_index"]
    selected_action = probe.get("selected_action")
    selected_payload = probe.get("selected_payload")
    selected = None
    resource_options = []
    if isinstance(selected_payload, dict):
        payload = copy.deepcopy(selected_payload)
        resource_options = autoplay._resource_preparation_options(
            state, payload
        )
        selected = next((
            row for row in resource_options
            if str((row.get("target") or {}).get("potion_instance_id"))
            == str(payload.get("potion_instance_id"))
            and str((row.get("target") or {}).get("operation"))
            == str(payload.get("operation"))
        ), None)
        if selected is None:
            raise ResolutionError(
                "production resource-preparation target is absent"
            )
    elif selected_action is None:
        selected = next(
            (row for row in options if row.get("choice_index") == selected_index),
            None,
        )
        if not isinstance(selected, dict):
            raise ResolutionError("production probe selected option is absent")
        payload = {"action": "choose", "option_id": selected["option_id"]}
    else:
        payload = {"action": selected_action}
    decision = copy.deepcopy(probe["decision"])
    if reverse_candidates:
        decision["candidates"] = list(reversed(decision.get("candidates") or []))
    decision["outcome_facts"] = {
        "scope": "deterministic_candidate_production_probe",
        "selected_choice_index": selected_index,
    }
    canonical = autoplay.canonical_legal_choices(state, payload, decision)
    selected_ids = [
        row["choice_id"] for row in canonical if row.get("selected") is True
    ]
    if len(selected_ids) != 1:
        raise ResolutionError("production probe final choice is not unique")
    if selected is None:
        selected = next(
            row for row in canonical if row.get("choice_id") == selected_ids[0]
        )
    observable_delta = {
        "current_hp_delta": 0, "hp_delta": 0, "max_hp_delta": 0,
        "gold_delta": 0, "block_delta": 0,
        "deck": {"added": [], "removed": [], "changed": []},
        "relics": {"added": [], "removed": [], "changed": []},
        "potions": {"added": [], "removed": [], "changed": []},
        "keys_before": {
            "ruby": bool((state.get("game_state") or {}).get("has_ruby_key")),
            "emerald": bool((state.get("game_state") or {}).get("has_emerald_key")),
            "sapphire": bool((state.get("game_state") or {}).get("has_sapphire_key")),
        },
        "keys_after": {
            "ruby": bool((state.get("game_state") or {}).get("has_ruby_key")),
            "emerald": bool((state.get("game_state") or {}).get("has_emerald_key")),
            "sapphire": bool((state.get("game_state") or {}).get("has_sapphire_key")),
        },
    }
    after = copy.deepcopy(state)
    # The shop probe is a real producer + settlement exercise, not merely a
    # listing snapshot.  Materialize the selected purchase in the synthetic
    # authoritative after-frame so the independent acquired_benefit claim is
    # checked against an observable inventory/gold delta rather than being
    # left as an unprovable producer assertion.
    if probe.get("decision_surface_kind") == "resource_preparation":
        selected_target = (
            selected.get("target") if isinstance(selected, dict) else {}
        )
        selected_target = (
            selected_target if isinstance(selected_target, dict) else {}
        )
        removed_id = str(selected_target.get("potion_instance_id") or "")
        game_after = after.setdefault("game_state", {})
        held = list(game_after.get("potions") or [])
        removed = [
            copy.deepcopy(potion) for potion in held
            if str(potion.get("potion_instance_id") or "") == removed_id
        ]
        if len(removed) != 1:
            raise ResolutionError(
                "resource-preparation probe removal is not uniquely bound"
            )
        game_after["potions"] = [
            potion for potion in held
            if str(potion.get("potion_instance_id") or "") != removed_id
        ]
        observable_delta["potions"]["removed"] = removed
    elif probe.get("combat_reward_gold_settlement") is not None:
        settlement = probe["combat_reward_gold_settlement"]
        gold_delta = int(settlement["gold_delta"])
        hp_delta = int(settlement["hp_delta"])
        game_after = after.setdefault("game_state", {})
        game_after["gold"] = int(game_after.get("gold") or 0) + gold_delta
        game_after["current_hp"] = min(
            int(game_after.get("max_hp") or 0),
            int(game_after.get("current_hp") or 0) + hp_delta,
        )
        observable_delta["gold_delta"] = gold_delta
        observable_delta["current_hp_delta"] = hp_delta
        observable_delta["hp_delta"] = hp_delta
    elif (
        probe["phase"] == "CARD_REWARD"
        and str((selected.get("target") or {}).get("kind") or "").casefold()
        == "bowl"
    ):
        game_after = after.setdefault("game_state", {})
        game_after["current_hp"] = int(game_after.get("current_hp") or 0) + 2
        game_after["max_hp"] = int(game_after.get("max_hp") or 0) + 2
        observable_delta["current_hp_delta"] = 2
        observable_delta["hp_delta"] = 2
        observable_delta["max_hp_delta"] = 2
    elif probe["phase"] == "SHOP_SCREEN":
        selected_target = selected.get("target") if isinstance(selected, dict) else {}
        selected_target = selected_target if isinstance(selected_target, dict) else {}
        selected_kind = str(selected_target.get("kind") or "").casefold()
        selected_item = selected_target.get("item")
        selected_item = selected_item if isinstance(selected_item, dict) else {}
        if selected_kind in {"card", "relic", "potion"} and selected_item:
            game_after = after.setdefault("game_state", {})
            price = int(selected_item.get("price") or 0)
            game_after["gold"] = int(game_after.get("gold") or 0) - price
            collection = {
                "card": "deck", "relic": "relics", "potion": "potions",
            }[selected_kind]
            game_after.setdefault(collection, []).append(copy.deepcopy(selected_item))
            observable_delta["gold_delta"] = -price
            observable_delta[collection]["added"] = [copy.deepcopy(selected_item)]
    elif probe.get("map_entry_gold_delta") is not None:
        gold_delta = int(probe["map_entry_gold_delta"])
        game_after = after.setdefault("game_state", {})
        game_after["gold"] = int(game_after.get("gold") or 0) + gold_delta
        observable_delta["gold_delta"] = gold_delta
    after["state_seq"] = 102
    record_action = (
        payload.get("action")
        if probe.get("decision_surface_kind") == "resource_preparation"
        else {
            "BOSS_REWARD": "boss_reward",
            "COMBAT_REWARD": "reward",
            "SHOP_SCREEN": "buy",
        }.get(probe["phase"], "choose")
    )
    record = {
        "trace_schema_version": 5,
        "record_type": "decision",
        "attempt_id": state["attempt_id"], "run_id": state["run_id"],
        "seed": state["seed"], "character": state["character"],
        "ascension_level": 0, "run_type": "standard",
        "policy_version": state["policy_version"],
        "decision_hash": state["decision_hash"],
        "controller_hash": state["controller_hash"],
        "selection_id": state["selection_id"],
        "selection_digest": state["selection_digest"],
        "before_seq": 101, "after_seq": 102,
        "phase": probe["phase"], "action": record_action,
        "act": (state.get("game_state") or {}).get("act"),
        "floor": (state.get("game_state") or {}).get("floor"),
        "hp_before": (state.get("game_state") or {}).get("current_hp"),
        "decision": decision,
        "available_options_before": copy.deepcopy(options),
        "available_commands_before": list(state.get("available_commands") or []),
        "legal_choices_before": canonical,
        "selected_choice_ids": selected_ids,
        "final_choice_ids": list(selected_ids),
        "requested_target_id": (
            (selected.get("target") or {}).get("potion_instance_id")
            if probe.get("decision_surface_kind") == "resource_preparation"
            else selected_ids[0]
        ),
        "resolved_target_id": (
            (selected.get("target") or {}).get("potion_instance_id")
            if probe.get("decision_surface_kind") == "resource_preparation"
            else selected_ids[0]
        ),
        "chosen_option_before": copy.deepcopy(selected),
        "decision_context": {
            "scope": "production_candidate_regression",
            "phase": probe["phase"],
            **(
                {
                    "producer_execution_variant": probe[
                        "producer_execution_variant"
                    ],
                    "producer_execution_nonce": probe[
                        "producer_execution_nonce"
                    ],
                    "producer_execution_digest": probe[
                        "producer_execution_digest"
                    ],
                }
                if probe.get("producer_execution_variant") is not None
                else {}
            ),
        },
        "decision_outcome": observable_delta,
        "authoritative_state_before": copy.deepcopy(state),
        "authoritative_state_after": after,
        "authoritative_choice_settlement": {
            "status": "observed",
            "authority": "protocol_state_delta",
            "fully_observable": True,
            "choice_id": selected_ids[0],
            "before_seq": 101, "after_seq": 102,
            "observed_outcome": observable_delta,
        },
    }
    if probe.get("decision_surface_kind") == "resource_preparation":
        record.update({
            "decision_surface_kind": "resource_preparation",
            "parent_choice_surface_pending": True,
            "potion_operation": (selected.get("target") or {}).get(
                "operation"
            ),
            "resource_preparation_options_before": copy.deepcopy(
                resource_options
            ),
        })
        record["authoritative_choice_settlement"] = (
            autoplay.authoritative_choice_settlement(record)
        )
    case = decision_cases.build_decision_case(record)
    if not isinstance(case, dict):
        raise ResolutionError("production DecisionCase builder rejected probe")
    return case


def _mutated_probe_case(probe, mutation):
    value = copy.deepcopy(probe)
    candidates = value["decision"].get("candidates") or []
    if mutation == "delete_candidate":
        value["decision"]["candidates"] = candidates[:-1]
        return _build_probe_case(value)
    if mutation == "delete_all_candidates":
        value["decision"]["candidates"] = []
        return _build_probe_case(value)
    if mutation == "typed_binding_missing":
        candidates[0].pop("choice_index", None)
        return _build_probe_case(value)
    if mutation == "typed_binding_ambiguous":
        candidates.append(copy.deepcopy(candidates[0]))
        return _build_probe_case(value)
    if mutation == "swap_uuid_index":
        candidates[0]["choice_index"], candidates[1]["choice_index"] = (
            candidates[1]["choice_index"], candidates[0]["choice_index"]
        )
        return _build_probe_case(value)
    case = _build_probe_case(value)
    if mutation in {"score_tie", "resource_score_tie"}:
        for candidate in case["candidates"]:
            candidate["score"] = candidate["local_score"] = 1.0
        for candidate in case["producer_candidates"]:
            candidate["score"] = 1.0
        case["v2_contract"]["missing_value_fields"] = [
            field for field in case["v2_contract"]["required_fields"]
            if case.get(field) in (None, "", [], {})
        ]
    elif mutation == "below_argmax":
        selected = set(case["selected_choice_ids"])
        for candidate in case["candidates"]:
            candidate["score"] = candidate["local_score"] = (
                -100.0 if candidate["choice_id"] in selected else 100.0
            )
        for candidate in case["producer_candidates"]:
            candidate["score"] = (
                -100.0 if candidate.get("choice_index")
                == case["chosen"]["choice_index"] else 100.0
            )
    elif mutation == "final_binding":
        case["final_choice_ids"] = ["option:forged"]
        case["model_advice"]["final_choice_ids"] = ["option:forged"]
    elif mutation == "consequence_tamper":
        case["producer_candidates"][0].setdefault(
            "consequences", {}
        )["gold_delta"] = 987654
    elif mutation == "plan_protection_removed":
        # Mutate every mirrored surface together.  The negative must prove
        # the whole preservation contract is required for score reselection,
        # not merely trip a shallow producer/canonical binding mismatch.  A
        # protected producer now also reflects the bound plan in its numeric
        # score, so the negative must remove that term together with the hard
        # eligibility veto; otherwise it has not actually removed protection.
        def remove_plan_protection(row):
            row["selection_eligible"] = True
            row["veto_reason"] = None
            inputs = row.get("score_inputs")
            if isinstance(inputs, dict):
                inputs.pop("bound_plan_preservation_value", None)
            components = row.get("score_components")
            if isinstance(components, list):
                row["score_components"] = [
                    component for component in components
                    if not isinstance(component, dict)
                    or component.get("name")
                    != "bound_plan_preservation_value"
                ]
                recomputed = sum(
                    float(component.get("value", 0.0) or 0.0)
                    for component in row["score_components"]
                    if isinstance(component, dict)
                )
                if isinstance(row.get("score"), (int, float)):
                    row["score"] = recomputed
                if isinstance(row.get("local_score"), (int, float)):
                    row["local_score"] = recomputed
            raw = row.get("producer_candidate_raw")
            if isinstance(raw, dict) and raw is not row:
                remove_plan_protection(raw)

        surfaces = (
            case.get("canonical_choices") or [],
            case.get("candidates") or [],
            case.get("producer_candidates") or [],
        )
        changed = 0
        for rows in surfaces:
            for row in rows:
                if row.get("veto_reason") != (
                    "exact_combat_plan_card_preservation"
                ):
                    continue
                remove_plan_protection(row)
                changed += 1
        # The projection intentionally reuses the exact raw producer object
        # in a few places, so deepcopy preserves object sharing here.  Count
        # mutations only as a sanity check; validate the postcondition across
        # all three surfaces rather than assuming three distinct objects.
        if not changed or any(
            row.get("veto_reason")
            == "exact_combat_plan_card_preservation"
            for rows in surfaces for row in rows
        ):
            raise ResolutionError(
                "hand-plan protection mutation did not clear all v2 surfaces"
            )
    elif mutation == "settlement_unobserved":
        settlement = case.get("authoritative_choice_settlement")
        if not isinstance(settlement, dict):
            raise ResolutionError(
                "settlement mutation requires an authoritative envelope"
            )
        settlement["status"] = "pending"
        settlement["fully_observable"] = False
    else:
        raise ResolutionError(f"unknown production mutation: {mutation}")
    return case


def _resource_preparation_selection_binding(case):
    """Return the exact immutable selection made by a resource producer."""

    selected_ids = case.get("selected_choice_ids") or []
    if len(selected_ids) != 1:
        raise ResolutionError(
            "resource-preparation producer did not select exactly one choice"
        )
    selected_id = selected_ids[0]
    choices = [
        row for row in case.get("canonical_choices") or []
        if row.get("choice_id") == selected_id
    ]
    if len(choices) != 1:
        raise ResolutionError(
            "resource-preparation selected canonical choice is not unique"
        )
    target = choices[0].get("target")
    target = target if isinstance(target, dict) else {}
    instance_id = str(target.get("potion_instance_id") or "")
    operation = str(target.get("operation") or "")
    slot = target.get("slot")
    producers = [
        row for row in case.get("producer_candidates") or []
        if (row.get("consequences") or {}).get("potion_instance_id")
        == instance_id
        and str(row.get("operation") or "") == operation
    ]
    if (
        not instance_id
        or type(slot) is not int
        or operation not in {"discard", "use"}
        or len(producers) != 1
    ):
        raise ResolutionError(
            "resource-preparation producer selection binding is incomplete"
        )
    consequence = producers[0].get("consequences") or {}
    held = (
        case.get("authoritative_state_before", {})
        .get("game_state", {}).get("potions") or []
    )
    held_matches = [
        (index, row) for index, row in enumerate(held)
        if row.get("potion_instance_id") == instance_id
    ]
    reward_options = [
        row for row in case.get("available_options") or []
        if (
            ((row.get("target") or {}).get("reward") or {}).get(
                "potion"
            ) or {}
        ).get("id") == consequence.get("bound_new_potion_id")
    ]
    if len(reward_options) != 1:
        raise ResolutionError(
            "resource-preparation bound reward is not protocol-unique"
        )
    bound_reward_index = reward_options[0].get("choice_index")
    rewards = (
        case.get("authoritative_state_before", {}).get("game_state", {})
        .get("screen_state", {}).get("rewards") or []
    )
    binding = {
        "choice_id": selected_id,
        "operation": operation,
        "potion_id": str(target.get("potion_id") or ""),
        "potion_instance_id": instance_id,
        "potion_slot": slot,
        "bound_new_potion_id": str(
            consequence.get("bound_new_potion_id") or ""
        ),
        "bound_reward_choice_index": bound_reward_index,
    }
    if (
        not binding["potion_id"]
        or not binding["bound_new_potion_id"]
        or len(held_matches) != 1
        or held_matches[0][0] != slot
        or held_matches[0][1].get("slot") != slot
        or consequence.get("potion_slot") != slot
        or type(bound_reward_index) is not int
        or bound_reward_index < 0
        or bound_reward_index >= len(rewards)
        or str(rewards[bound_reward_index].get("reward_type") or "").casefold()
        != "potion"
        or ((rewards[bound_reward_index].get("potion") or {}).get("id"))
        != binding["bound_new_potion_id"]
        or case.get("final_choice_ids") != [selected_id]
        or case.get("requested_target_id") != instance_id
        or case.get("resolved_target_id") != instance_id
        or (case.get("chosen") or {}).get("requested_target_id")
        != instance_id
        or (case.get("chosen") or {}).get("resolved_target_id")
        != instance_id
        or (case.get("chosen") or {}).get("choice_index") != slot
        or case.get("authoritative_choice_settlement", {}).get(
            "choice_id"
        ) != selected_id
    ):
        raise ResolutionError(
            "resource-preparation final selection binding is inconsistent"
        )
    return binding


def _resource_preparation_surface_order(case):
    """Describe independently rebuilt held and parent protocol containers."""

    game = case.get("authoritative_state_before", {}).get("game_state", {})

    canonical_meta = {}
    for row in case.get("canonical_choices") or []:
        target = row.get("target") or {}
        key = (
            str(target.get("potion_instance_id") or ""),
            str(target.get("operation") or "").casefold(),
        )
        canonical_meta[key] = (
            row.get("selection_eligible"), row.get("veto_reason")
        )

    def fingerprint(potion_id, operation, eligible, veto):
        return "|".join((
            str(potion_id or ""), str(operation or "").casefold(),
            "eligible" if eligible is True else "vetoed",
            str(veto or ""),
        ))

    def target_children(rows):
        result = []
        for row in rows:
            target = row.get("target") or {}
            key = (
                str(target.get("potion_instance_id") or ""),
                str(target.get("operation") or "").casefold(),
            )
            eligible, veto = canonical_meta.get(key, (None, None))
            result.append(fingerprint(
                target.get("potion_id"), key[1], eligible, veto
            ))
        return result

    def producer_children(rows):
        return [
            fingerprint(
                (row.get("consequences") or {}).get("potion_id"),
                row.get("operation"), row.get("selection_eligible"),
                row.get("veto_reason"),
            )
            for row in rows
        ]

    held_groups = []
    for potion in game.get("potions") or []:
        instance_id = str(potion.get("potion_instance_id") or "")
        operations = []
        if potion.get("can_discard") is True:
            operations.append("discard")
        if (
            potion.get("can_use") is True
            and potion.get("requires_target") is False
        ):
            operations.append("use")
        group = []
        for operation in operations:
            eligible, veto = canonical_meta.get(
                (instance_id, operation), (None, None)
            )
            group.append(fingerprint(
                potion.get("id"), operation, eligible, veto
            ))
        held_groups.append(group)

    def reward_types(rows):
        return [
            str(row.get("reward_type") or "").casefold()
            for row in rows
            if isinstance(row, dict)
        ]

    def protocol_reward_types(rows):
        return [
            str(
                ((row.get("target") or {}).get("reward") or {}).get(
                    "reward_type"
                ) or ""
            ).casefold()
            for row in rows
        ]

    result = {
        "held": [item for group in held_groups for item in group],
        "held_groups": held_groups,
        "authoritative_held_instance_ids": [
            str(row.get("potion_instance_id") or "")
            for row in game.get("potions") or []
        ],
        "resource": target_children(
            case.get("resource_preparation_options") or []
        ),
        "canonical": target_children(case.get("canonical_choices") or []),
        "candidate": target_children(case.get("candidates") or []),
        "producer": producer_children(
            case.get("producer_candidates") or []
        ),
        "parent_rewards": reward_types(
            (game.get("screen_state") or {}).get("rewards") or []
        ),
        "protocol_options": protocol_reward_types(
            case.get("available_options") or []
        ),
    }
    if any(not values or any(not value for value in values) for values in result.values()):
        raise ResolutionError(
            "resource-preparation production surface order is incomplete"
        )
    return result


def _shop_resource_preparation_selection_binding(case):
    """Bind a shop preparation child to one visible typed parent listing."""

    selected_ids = case.get("selected_choice_ids") or []
    if len(selected_ids) != 1:
        raise ResolutionError("shop preparation selection is not unique")
    selected_id = selected_ids[0]
    choices = [
        row for row in case.get("canonical_choices") or []
        if row.get("choice_id") == selected_id
    ]
    if len(choices) != 1:
        raise ResolutionError("shop preparation canonical child is not unique")
    target = choices[0].get("target") or {}
    instance_id = str(target.get("potion_instance_id") or "")
    potion_id = str(target.get("potion_id") or "")
    operation = str(target.get("operation") or "").casefold()
    slot = target.get("slot")
    producers = [
        row for row in case.get("producer_candidates") or []
        if (row.get("consequences") or {}).get("potion_instance_id")
        == instance_id
        and str(row.get("operation") or "").casefold() == operation
    ]
    if (
        not instance_id or not potion_id or operation not in {"discard", "use"}
        or type(slot) is not int or len(producers) != 1
    ):
        raise ResolutionError("shop preparation selected child is incomplete")
    consequence = producers[0].get("consequences") or {}
    bound_id = str(consequence.get("bound_new_potion_id") or "")
    parent_listing_id = consequence.get("bound_purchase_listing_id")
    parent_choice_index = consequence.get("bound_purchase_choice_index")
    parent_item_id = consequence.get("bound_purchase_item_id")
    parent_price = consequence.get("bound_purchase_price")
    parent_options = [
        row for row in case.get("available_options") or []
        if row.get("option_id") == parent_listing_id
        and row.get("choice_index") == parent_choice_index
        and str((row.get("target") or {}).get("kind") or "").casefold()
        == "potion"
        and ((row.get("target") or {}).get("item") or {}).get("id")
        == parent_item_id
        and ((row.get("target") or {}).get("item") or {}).get("price")
        == parent_price
    ]
    if len(parent_options) != 1:
        raise ResolutionError("shop bound parent listing is not protocol-unique")
    parent_index = parent_options[0].get("choice_index")
    game = case.get("authoritative_state_before", {}).get("game_state", {})
    held = game.get("potions") or []
    held_matches = [
        (index, row) for index, row in enumerate(held)
        if row.get("potion_instance_id") == instance_id
    ]
    listings = (game.get("screen_state") or {}).get("potions") or []
    binding = {
        "choice_id": selected_id,
        "operation": operation,
        "potion_id": potion_id,
        "potion_instance_id": instance_id,
        "potion_slot": slot,
        "bound_new_potion_id": bound_id,
        "bound_parent_choice_index": parent_index,
    }
    if (
        len(held_matches) != 1
        or held_matches[0][0] != slot
        or held_matches[0][1].get("slot") != slot
        or consequence.get("potion_slot") != slot
        or bound_id != parent_item_id
        or type(parent_index) is not int
        or parent_index < 0 or parent_index >= len(listings)
        or str((listings[parent_index] or {}).get("id") or "") != bound_id
        or case.get("final_choice_ids") != [selected_id]
        or case.get("requested_target_id") != instance_id
        or case.get("resolved_target_id") != instance_id
        or (case.get("chosen") or {}).get("requested_target_id") != instance_id
        or (case.get("chosen") or {}).get("resolved_target_id") != instance_id
        or (case.get("chosen") or {}).get("choice_index") != slot
        or case.get("authoritative_choice_settlement", {}).get("choice_id")
        != selected_id
    ):
        raise ResolutionError("shop preparation selection binding is inconsistent")
    return binding


def _shop_resource_preparation_surface_order(case):
    """Describe the held child rows and typed shop parent listings."""

    game = case.get("authoritative_state_before", {}).get("game_state", {})
    canonical_meta = {}
    for row in case.get("canonical_choices") or []:
        target = row.get("target") or {}
        canonical_meta[(
            str(target.get("potion_instance_id") or ""),
            str(target.get("operation") or "").casefold(),
        )] = (row.get("selection_eligible"), row.get("veto_reason"))

    def fingerprint(potion_id, operation, eligible, veto):
        return "|".join((
            str(potion_id or ""), str(operation or "").casefold(),
            "eligible" if eligible is True else "vetoed", str(veto or ""),
        ))

    def target_children(rows):
        result = []
        for row in rows:
            target = row.get("target") or {}
            key = (
                str(target.get("potion_instance_id") or ""),
                str(target.get("operation") or "").casefold(),
            )
            eligible, veto = canonical_meta.get(key, (None, None))
            result.append(fingerprint(
                target.get("potion_id"), key[1], eligible, veto
            ))
        return result

    held_groups = []
    for potion in game.get("potions") or []:
        instance = str(potion.get("potion_instance_id") or "")
        operations = []
        if potion.get("can_discard") is True:
            operations.append("discard")
        if potion.get("can_use") is True and potion.get("requires_target") is False:
            operations.append("use")
        group = []
        for operation in operations:
            eligible, veto = canonical_meta.get((instance, operation), (None, None))
            group.append(fingerprint(potion.get("id"), operation, eligible, veto))
        held_groups.append(group)

    def producer_children(rows):
        return [
            fingerprint(
                (row.get("consequences") or {}).get("potion_id"),
                row.get("operation"), row.get("selection_eligible"),
                row.get("veto_reason"),
            )
            for row in rows
        ]

    result = {
        "held": [child for group in held_groups for child in group],
        "held_groups": held_groups,
        "authoritative_held_instance_ids": [
            str(row.get("potion_instance_id") or "")
            for row in game.get("potions") or []
        ],
        "resource": target_children(case.get("resource_preparation_options") or []),
        "canonical": target_children(case.get("canonical_choices") or []),
        "candidate": target_children(case.get("candidates") or []),
        "producer": producer_children(case.get("producer_candidates") or []),
        "parent_potions": [
            str(row.get("id") or "")
            for row in (game.get("screen_state") or {}).get("potions") or []
        ],
        "protocol_options": [
            str(((row.get("target") or {}).get("item") or {}).get("id") or "")
            for row in case.get("available_options") or []
            if str((row.get("target") or {}).get("kind") or "").casefold()
            == "potion"
        ],
    }
    if any(not values or any(not value for value in values) for values in result.values()):
        raise ResolutionError("shop resource-preparation surface is incomplete")
    return result


def run_fixture_invariant(fixture_row, audit_one):
    """Execute a real producer→canonicalizer→DecisionCase regression."""

    fixture_id = fixture_row.get("fixture_id")
    expected = _EXPECTED_FIXTURE_ROWS.get(fixture_id)
    if fixture_row != expected:
        raise ResolutionError("fixture row does not match the built-in invariant")
    fresh_resource_producers = fixture_id in {
        "shop-resource-preparation-score-v2",
        "combat-reward-resource-preparation-score-v2",
    }
    probe = _production_probe(
        expected["producer_probe"],
        **(
            {"production_variant": "baseline"}
            if fresh_resource_producers else {}
        ),
    )
    if probe.get("phase") != expected.get("current_phase"):
        raise ResolutionError(
            f"production probe phase mismatch: {fixture_id}: "
            f"{probe.get('phase')} != {expected.get('current_phase')}"
        )
    positive = _build_probe_case(probe)
    candidate_surface = positive.get("producer_candidates")
    protocol_surface = positive.get("available_options")
    if not isinstance(candidate_surface, list) or not isinstance(
        protocol_surface, list
    ):
        raise ResolutionError(
            f"production permutation surface is malformed: {fixture_id}"
        )
    candidate_count = len(candidate_surface)
    protocol_count = len(protocol_surface)
    permutation_specs = _permutation_specs(
        candidate_count, protocol_count
    )
    permutations = []
    for kind, reverse_candidates, reverse_protocol_options in permutation_specs:
        if fresh_resource_producers:
            variant_probe = _production_probe(
                expected["producer_probe"], production_variant=kind
            )
            if variant_probe.get("phase") != expected.get("current_phase"):
                raise ResolutionError(
                    f"production variant phase mismatch: {fixture_id}:{kind}"
                )
            value = _build_probe_case(variant_probe)
        else:
            value = _build_probe_case(
                probe,
                reverse_candidates=reverse_candidates,
                reverse_protocol_options=reverse_protocol_options,
            )
        permutations.append((
            kind,
            value,
        ))
    positive_results = [
        audit_one(positive, historical=False),
        *(
            audit_one(value, historical=False)
            for _kind, value in permutations
        ),
    ]
    if any(
        result.get("classification") != "audited"
        or result.get("issues") or result.get("unknowns")
        for result in positive_results
    ):
        raise ResolutionError(
            f"positive production invariant failed: {fixture_id}: "
            f"{[problem_kinds(row) for row in positive_results]}"
        )
    if any(
        positive.get("selected_choice_ids") != value.get(
            "selected_choice_ids"
        )
        for _kind, value in permutations
    ):
        raise ResolutionError(f"producer container order changed choice: {fixture_id}")

    sapphire_link_contract = None
    bloody_idol_gold_contract = None
    if fixture_id == "sapphire-consequence-v2":
        key_rows = [
            row for row in positive.get("producer_candidates") or []
            if (row.get("consequences") or {}).get("operation")
            == "gain_sapphire_key"
        ]
        linked_rows = [
            row for row in positive.get("producer_candidates") or []
            if (row.get("consequences") or {}).get("operation")
            == "gain_linked_relic"
        ]
        selected_ids = set(positive.get("selected_choice_ids") or [])
        selected_choices = [
            row for row in positive.get("canonical_choices") or []
            if row.get("choice_id") in selected_ids
        ]
        if (
            len(key_rows) != 1
            or key_rows[0].get("score") != 4.0
            or key_rows[0].get("selection_eligible") is not False
            or len(linked_rows) != 1
            or (linked_rows[0].get("consequences") or {}).get("relic_id")
            != "DataDisk"
            or len(selected_choices) != 1
            or (selected_choices[0].get("consequences") or {}).get(
                "operation"
            ) != "gain_linked_relic"
        ):
            raise ResolutionError(
                "sapphire linked-relic eligibility contract is not exact"
            )
        sapphire_link_contract = {
            "status": "clear",
            "authority": "production_sapphire_link_eligibility_v2",
            "ineligible_key_score": 4.0,
            "key_operation": "gain_sapphire_key",
            "linked_operation": "gain_linked_relic",
            "linked_relic_id": "DataDisk",
            "selected_choice_ids": list(positive["selected_choice_ids"]),
        }
    elif fixture_id == "sapphire-bloody-idol-gold-consequence-v1":
        selected_ids = set(positive.get("selected_choice_ids") or [])
        selected_choices = [
            row for row in positive.get("canonical_choices") or []
            if row.get("choice_id") in selected_ids
        ]
        bound_candidate_ids = {
            candidate_id
            for row in selected_choices
            for candidate_id in (row.get("candidate_ids") or [])
        }
        producer_rows = [
            row for row in positive.get("producer_candidates") or []
            if row.get("choice_id") in bound_candidate_ids
        ]
        consequence = (
            producer_rows[0].get("consequences") or {}
            if len(producer_rows) == 1 else {}
        )
        observed = (
            positive.get("authoritative_choice_settlement") or {}
        ).get("observed_outcome") or {}
        before_relic_ids = {
            str(row.get("id") or "")
            for row in (
                positive.get("authoritative_state_before", {})
                .get("game_state", {}).get("relics") or []
            )
        }
        if (
            len(selected_choices) != 1
            or len(producer_rows) != 1
            or consequence.get("operation") != "collect_independent_reward"
            or consequence.get("reward_type") != "gold"
            or consequence.get("gold_delta") != 80
            or consequence.get("hp_delta") != 5
            or observed.get("gold_delta") != 80
            or observed.get("hp_delta") != 5
            or "BloodyIdol" not in before_relic_ids
        ):
            raise ResolutionError(
                "bloody-idol gold reward consequence contract is not exact"
            )
        bloody_idol_gold_contract = {
            "status": "clear",
            "authority": "production_bloody_idol_gold_reward_v1",
            "operation": "collect_independent_reward",
            "reward_type": "gold",
            "gold_delta": 80,
            "hp_delta": 5,
            "relic_id": "BloodyIdol",
            "selected_choice_ids": list(positive["selected_choice_ids"]),
        }

    resource_permutation_contract = None
    shop_resource_permutation_contract = None
    bowl_dominance_contract = None
    if fixture_id == "combat-reward-resource-preparation-score-v2":
        baseline_binding = _resource_preparation_selection_binding(positive)
        baseline_surface = _resource_preparation_surface_order(positive)
        baseline_context = positive.get("decision_context") or {}
        baseline_execution = {
            "variant": baseline_context.get("producer_execution_variant"),
            "nonce": baseline_context.get("producer_execution_nonce"),
            "digest": baseline_context.get("producer_execution_digest"),
        }
        producer_rows = positive.get("producer_candidates") or []
        selected_producer = next(
            (
                row for row in producer_rows
                if (row.get("consequences") or {}).get(
                    "potion_instance_id"
                ) == baseline_binding["potion_instance_id"]
            ),
            None,
        )
        before_game = (
            positive.get("authoritative_state_before", {}).get(
                "game_state", {}
            )
        )
        after_game = (
            positive.get("authoritative_state_after", {}).get(
                "game_state", {}
            )
        )
        before_rewards = (before_game.get("screen_state") or {}).get(
            "rewards"
        )
        after_rewards = (after_game.get("screen_state") or {}).get(
            "rewards"
        )
        removed_potions = (
            positive.get("decision_outcome", {}).get("potions", {}).get(
                "removed"
            ) or []
        )
        if (
            positive.get("decision_surface_kind") != "resource_preparation"
            or positive.get("parent_choice_surface_pending") is not True
            or baseline_binding != {
                "choice_id": "potion:587459eeceee48c06335:discard",
                "operation": "discard",
                "potion_id": "WeakPotion",
                "potion_instance_id": "potion:587459eeceee48c06335",
                "potion_slot": 1,
                "bound_new_potion_id": "LiquidMemories",
                "bound_reward_choice_index": 0,
            }
            or baseline_execution.get("variant") != "baseline"
            or any(
                not isinstance(baseline_execution.get(field), str)
                or len(baseline_execution[field]) != 64
                for field in ("nonce", "digest")
            )
            or baseline_surface["authoritative_held_instance_ids"].count(
                baseline_binding["potion_instance_id"]
            ) != 1
            or baseline_surface["authoritative_held_instance_ids"].index(
                baseline_binding["potion_instance_id"]
            ) != baseline_binding["potion_slot"]
            or not isinstance(selected_producer, dict)
            or selected_producer.get("score") != 30.0
            or (selected_producer.get("score_inputs") or {}).get(
                "held_keep_value"
            ) != 10.0
            or sorted(
                (row.get("score_inputs") or {}).get("held_keep_value")
                for row in producer_rows
            ) != [10.0, 20.0, 30.0, 30.0]
            or any(
                (row.get("consequences") or {}).get(
                    "bound_new_potion_id"
                ) != "LiquidMemories"
                for row in producer_rows
            )
            or before_rewards != after_rewards
            or not isinstance(before_rewards, list)
            or not any(
                str(reward.get("reward_type") or "").casefold() == "potion"
                and ((reward.get("potion") or {}).get("id"))
                == "LiquidMemories"
                for reward in before_rewards
                if isinstance(reward, dict)
            )
            or len(removed_potions) != 1
            or removed_potions[0].get("potion_instance_id")
            != baseline_binding["potion_instance_id"]
        ):
            raise ResolutionError(
                "combat-reward resource-preparation baseline is not exact"
            )

        variant_contracts = []
        execution_nonces = [baseline_execution["nonce"]]
        execution_digests = [baseline_execution["digest"]]
        held_axes = {"candidate", "candidate+protocol"}
        protocol_axes = {"protocol", "candidate+protocol"}
        stable_binding_fields = (
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "bound_new_potion_id",
        )
        for kind, value in permutations:
            context = value.get("decision_context") or {}
            binding = _resource_preparation_selection_binding(value)
            surface = _resource_preparation_surface_order(value)
            execution = {
                "variant": context.get("producer_execution_variant"),
                "nonce": context.get("producer_execution_nonce"),
                "digest": context.get("producer_execution_digest"),
            }
            expected_held_order = (
                [
                    child
                    for group in reversed(
                        baseline_surface["held_groups"]
                    )
                    for child in group
                ]
                if kind in held_axes else baseline_surface["held"]
            )
            expected_reward_order = (
                list(reversed(baseline_surface["parent_rewards"]))
                if kind in protocol_axes
                else baseline_surface["parent_rewards"]
            )
            held_surface_names = (
                "held", "resource", "canonical", "candidate", "producer",
            )
            if (
                context.get("producer_execution_variant") != kind
                or execution.get("variant") != kind
                or any(
                    not isinstance(execution.get(field), str)
                    or len(execution[field]) != 64
                    for field in ("nonce", "digest")
                )
                or any(
                    binding[field] != baseline_binding[field]
                    for field in stable_binding_fields
                )
                or binding["bound_reward_choice_index"]
                != (1 if kind in protocol_axes else 0)
                or surface["authoritative_held_instance_ids"].count(
                    binding["potion_instance_id"]
                ) != 1
                or surface["authoritative_held_instance_ids"].index(
                    binding["potion_instance_id"]
                ) != binding["potion_slot"]
                or binding["potion_slot"] != 1
                or any(
                    surface[name] != expected_held_order
                    for name in held_surface_names
                )
                or surface["parent_rewards"] != expected_reward_order
                or surface["protocol_options"] != expected_reward_order
                or value.get("parent_choice_surface_pending") is not True
                or (
                    value.get("authoritative_state_before", {})
                    .get("game_state", {}).get("screen_state", {}).get(
                        "rewards"
                    )
                    != value.get("authoritative_state_after", {})
                    .get("game_state", {}).get("screen_state", {}).get(
                        "rewards"
                    )
                )
            ):
                raise ResolutionError(
                    "fresh production variant changed resource selection: "
                    f"{fixture_id}:{kind}"
                )
            variant_contracts.append({
                "kind": kind,
                "producer_execution_variant": context[
                    "producer_execution_variant"
                ],
                "producer_execution_nonce": execution["nonce"],
                "producer_execution_digest": execution["digest"],
                "selection_binding": binding,
                "surface_order": surface,
                "case_sha256": object_digest(value),
            })
            execution_nonces.append(execution["nonce"])
            execution_digests.append(execution["digest"])
        if (
            len(set(execution_nonces)) != 4
            or len(set(execution_digests)) != 4
        ):
            raise ResolutionError(
                "fresh production executions are not uniquely bound"
            )
        resource_permutation_contract = {
            "status": "clear",
            "authority": (
                "fresh_live_producer_per_variant_v1"
            ),
            "producer_execution_count": 4,
            "producer_execution_variants": [
                "baseline", "candidate", "protocol", "candidate+protocol",
            ],
            "producer_execution_nonces": execution_nonces,
            "producer_execution_digests": execution_digests,
            "baseline_producer_execution": baseline_execution,
            "baseline_selection_binding": baseline_binding,
            "baseline_surface_order": baseline_surface,
            "production_variants": variant_contracts,
            "selected_potion_instance_id": baseline_binding[
                "potion_instance_id"
            ],
            "selected_potion_slot": baseline_binding["potion_slot"],
            "selected_held_keep_value": 10.0,
            "bound_reward_potion_id": "LiquidMemories",
            "parent_choice_surface_pending": True,
            "parent_reward_consumed": False,
        }

    if fixture_id == "shop-resource-preparation-score-v2":
        baseline_binding = _shop_resource_preparation_selection_binding(positive)
        baseline_surface = _shop_resource_preparation_surface_order(positive)
        baseline_context = positive.get("decision_context") or {}
        baseline_execution = {
            "variant": baseline_context.get("producer_execution_variant"),
            "nonce": baseline_context.get("producer_execution_nonce"),
            "digest": baseline_context.get("producer_execution_digest"),
        }
        producer_rows = positive.get("producer_candidates") or []
        selected_producer = next((
            row for row in producer_rows
            if (row.get("consequences") or {}).get("potion_instance_id")
            == baseline_binding["potion_instance_id"]
            and str(row.get("operation") or "").casefold()
            == baseline_binding["operation"]
        ), None)
        removed = (
            positive.get("decision_outcome", {}).get("potions", {}).get("removed")
            or []
        )
        if (
            positive.get("decision_surface_kind") != "resource_preparation"
            or positive.get("parent_choice_surface_pending") is not True
            or baseline_binding["operation"] != "discard"
            or baseline_binding["potion_id"] != "SpeedPotion"
            or baseline_binding["potion_slot"] != 1
            or baseline_binding["bound_new_potion_id"] != "LiquidMemories"
            or baseline_binding["bound_parent_choice_index"] != 1
            or baseline_execution.get("variant") != "baseline"
            or any(
                not isinstance(baseline_execution.get(field), str)
                or len(baseline_execution[field]) != 64
                for field in ("nonce", "digest")
            )
            or baseline_surface["parent_potions"]
            != ["LiquidMemories", "LiquidMemories"]
            or baseline_surface["protocol_options"]
            != baseline_surface["parent_potions"]
            or not isinstance(selected_producer, dict)
            or selected_producer.get("score") != 30.0
            or sorted(
                (row.get("score_inputs") or {}).get("held_keep_value")
                for row in producer_rows
            ) != [10.0, 20.0, 30.0]
            or any(
                (row.get("consequences") or {}).get("bound_new_potion_id")
                != "LiquidMemories"
                for row in producer_rows
            )
            or len(removed) != 1
            or removed[0].get("potion_instance_id")
            != baseline_binding["potion_instance_id"]
        ):
            raise ResolutionError("shop resource-preparation baseline is not exact")

        variant_contracts = []
        execution_nonces = [baseline_execution["nonce"]]
        execution_digests = [baseline_execution["digest"]]
        held_axes = {"candidate", "candidate+protocol"}
        protocol_axes = {"protocol", "candidate+protocol"}
        stable_binding_fields = (
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "bound_new_potion_id",
        )
        for kind, value in permutations:
            context = value.get("decision_context") or {}
            binding = _shop_resource_preparation_selection_binding(value)
            surface = _shop_resource_preparation_surface_order(value)
            execution = {
                "variant": context.get("producer_execution_variant"),
                "nonce": context.get("producer_execution_nonce"),
                "digest": context.get("producer_execution_digest"),
            }
            expected_held = (
                [child for group in reversed(baseline_surface["held_groups"])
                 for child in group]
                if kind in held_axes else baseline_surface["held"]
            )
            expected_parent = (
                list(reversed(baseline_surface["parent_potions"]))
                if kind in protocol_axes else baseline_surface["parent_potions"]
            )
            if (
                execution.get("variant") != kind
                or any(
                    not isinstance(execution.get(field), str)
                    or len(execution[field]) != 64
                    for field in ("nonce", "digest")
                )
                or any(
                    binding[field] != baseline_binding[field]
                    for field in stable_binding_fields
                )
                or binding["potion_slot"] != 1
                or binding["bound_parent_choice_index"]
                != (0 if kind in protocol_axes else 1)
                or any(
                    surface[name] != expected_held
                    for name in ("held", "resource", "canonical", "candidate", "producer")
                )
                or surface["parent_potions"] != expected_parent
                or surface["protocol_options"] != expected_parent
                or value.get("parent_choice_surface_pending") is not True
            ):
                raise ResolutionError(
                    "fresh shop producer changed resource selection: "
                    f"{fixture_id}:{kind}"
                )
            variant_contracts.append({
                "kind": kind,
                "producer_execution_variant": execution["variant"],
                "producer_execution_nonce": execution["nonce"],
                "producer_execution_digest": execution["digest"],
                "selection_binding": binding,
                "surface_order": surface,
                "case_sha256": object_digest(value),
            })
            execution_nonces.append(execution["nonce"])
            execution_digests.append(execution["digest"])
        if len(set(execution_nonces)) != 4 or len(set(execution_digests)) != 4:
            raise ResolutionError("fresh shop executions are not uniquely bound")

        bound_choice = next(
            row for row in positive.get("available_options") or []
            if row.get("option_id")
            == (positive.get("resource_parent_listing") or {}).get(
                "listing_id"
            )
        )
        parent_negative_controls = []
        for kind in ("missing_bound_parent", "ambiguous_bound_parent"):
            negative_parent = copy.deepcopy(positive)
            if kind == "missing_bound_parent":
                negative_parent["available_options"] = [
                    row for row in negative_parent.get("available_options") or []
                    if row.get("option_id") != bound_choice.get("option_id")
                ]
            else:
                duplicate = copy.deepcopy(bound_choice)
                negative_parent["available_options"].append(duplicate)
            result = audit_one(negative_parent, historical=False)
            problems = problem_kinds(result)
            if (
                result.get("classification") not in BLOCKING_CLASSIFICATIONS
                or "v2_producer_bound_new_potion_id_not_protocol_visible"
                not in problems
            ):
                raise ResolutionError(
                    f"shop typed-parent negative control did not block: {kind}"
                )
            parent_negative_controls.append({
                "kind": kind,
                "case_sha256": object_digest(negative_parent),
                "classification": result.get("classification"),
                "problem_kinds": problems,
            })

        shop_resource_permutation_contract = {
            "status": "clear",
            "authority": "fresh_live_shop_producer_per_variant_v1",
            "producer_execution_count": 4,
            "producer_execution_variants": [
                "baseline", "candidate", "protocol", "candidate+protocol",
            ],
            "producer_execution_nonces": execution_nonces,
            "producer_execution_digests": execution_digests,
            "baseline_producer_execution": baseline_execution,
            "baseline_selection_binding": baseline_binding,
            "baseline_surface_order": baseline_surface,
            "production_variants": variant_contracts,
            "selected_potion_instance_id": baseline_binding["potion_instance_id"],
            "selected_potion_slot": baseline_binding["potion_slot"],
            "bound_parent_potion_id": baseline_binding["bound_new_potion_id"],
            "parent_choice_surface_pending": True,
            "typed_parent_negative_controls": parent_negative_controls,
        }

    if fixture_id == "card_reward_bowl_dominates_skip_v1":
        selected_ids = positive.get("selected_choice_ids") or []
        selected_rows = [
            row for row in positive.get("canonical_choices") or []
            if row.get("choice_id") in selected_ids
        ]
        bowl_rows = [
            row for row in positive.get("candidates") or []
            if str((row.get("target") or {}).get("kind") or "").casefold()
            == "bowl"
        ]
        return_rows = [
            row for row in positive.get("candidates") or []
            if row.get("choice_id") == "action:return"
        ]
        if len(bowl_rows) != 1 or len(return_rows) != 1:
            raise ResolutionError("bowl dominance candidates are not unique")
        bowl = bowl_rows[0]
        returned = return_rows[0]
        other_eligible_scores = [
            float(row.get("score"))
            for row in positive.get("candidates") or []
            if row not in (bowl, returned)
            and row.get("selection_eligible") is not False
        ]
        observed = (
            positive.get("authoritative_choice_settlement", {}).get(
                "observed_outcome"
            ) or {}
        )
        bowl_effect = bowl.get("consequences") or {}
        return_effect = returned.get("consequences") or {}
        if (
            len(selected_ids) != 1 or len(selected_rows) != 1
            or str((selected_rows[0].get("target") or {}).get("kind") or "").casefold()
            != "bowl"
            or positive.get("final_choice_ids") != selected_ids
            or positive.get("authoritative_choice_settlement", {}).get("choice_id")
            != selected_ids[0]
            or bowl.get("score") != returned.get("score")
            or not isinstance(bowl.get("score"), (int, float))
            or any(score >= float(bowl["score"]) for score in other_eligible_scores)
            or bowl_effect.get("hp_delta") != 2
            or bowl_effect.get("max_hp_delta") != 2
            or bowl_effect.get("operation") != "singing_bowl"
            or return_effect.get("max_hp_delta") != 0
            or return_effect.get("operation") != "return"
            or any(
                observed.get(field) != 2
                for field in ("current_hp_delta", "hp_delta", "max_hp_delta")
            )
        ):
            raise ResolutionError("Bowl does not strictly dominate return")
        bowl_dominance_contract = {
            "status": "clear",
            "authority": "typed_bowl_resource_dominance_v1",
            "selected_choice_id": selected_ids[0],
            "bowl_score": float(bowl["score"]),
            "return_score": float(returned["score"]),
            "bowl_max_hp_delta": 2,
            "return_max_hp_delta": 0,
            "observed_current_hp_delta": 2,
            "observed_max_hp_delta": 2,
            "other_eligible_scores": sorted(other_eligible_scores),
            "all_candidate_score_tie_remains_blocking": True,
        }

    for kind, value in permutations:
        if fresh_resource_producers:
            continue
        expected_candidates = (
            list(reversed(candidate_surface))
            if kind in {"candidate", "candidate+protocol"}
            else candidate_surface
        )
        expected_options = (
            list(reversed(protocol_surface))
            if kind in {"protocol", "candidate+protocol"}
            else protocol_surface
        )
        if (
            value.get("producer_candidates") != expected_candidates
            or value.get("available_options") != expected_options
        ):
            raise ResolutionError(
                f"production permutation did not reorder its declared axes: "
                f"{fixture_id}:{kind}"
            )

    positive_sha256 = object_digest(positive)
    permutation_sha256s = [
        object_digest(value) for _kind, value in permutations
    ]
    if (
        len(set(permutation_sha256s)) != len(permutation_sha256s)
        or positive_sha256 in set(permutation_sha256s)
    ):
        raise ResolutionError(
            f"production permutations are not distinct: {fixture_id}"
        )

    negative_case = _mutated_probe_case(probe, expected["mutation"])
    negative = audit_one(negative_case, historical=False)
    negative_problems = problem_kinds(negative)
    # A resolution is evidence only when its injected negative control remains
    # machine-blocking.  Agreement from a reviewer cannot turn a negative
    # mutation into a passing control.
    if (
        negative.get("classification")
        != expected["expected_negative_classification"]
        or negative_problems
        != expected["expected_negative_problem_kinds"]
    ):
        raise ResolutionError(
            f"production mutation contract mismatch: {fixture_id}: "
            f"{negative.get('classification')}/{negative_problems}"
        )

    root = Path(__file__).resolve().parent
    source_paths = (
        root / "src" / "spirecomm-master" / "spirecomm" / "ai" / "agent.py",
        root / "autoplay.py", root / "bridge.py", root / "decision_cases.py",
        root / "decision_case_replay.py", root / "test_live_macro_decisions.py",
    )
    invariant = {
        "fixture_id": fixture_id,
        "fixture_sha256": object_digest(fixture_row),
        "producer_probe": expected["producer_probe"],
        "resolution_class": expected["resolution_class"],
        "positive_case_sha256": positive_sha256,
        "candidate_count": candidate_count,
        "protocol_count": protocol_count,
        "permutation_axes": {
            "candidate": _permutation_axis_evidence(candidate_count),
            "protocol": _permutation_axis_evidence(protocol_count),
        },
        "reordered_case_sha256": (
            permutation_sha256s[0] if permutation_sha256s else None
        ),
        "positive_permutation_count": len(permutations),
        "positive_permutation_kinds": [
            kind for kind, _value in permutations
        ],
        "positive_permutation_sha256s": permutation_sha256s,
        "positive_permutation_selected_choice_ids": [
            list(value["selected_choice_ids"])
            for _kind, value in permutations
        ],
        "selected_choice_ids": list(positive["selected_choice_ids"]),
        "negative_case_sha256": object_digest(negative_case),
        "negative_classification": negative.get("classification"),
        "negative_problem_kinds": negative_problems,
        "source_sha256": {
            str(path.relative_to(root)).replace("\\", "/"): file_digest(path)
            for path in source_paths
        },
        "status": "clear",
    }
    if sapphire_link_contract is not None:
        invariant["sapphire_link_contract"] = sapphire_link_contract
    if bloody_idol_gold_contract is not None:
        invariant["bloody_idol_gold_contract"] = bloody_idol_gold_contract
    if resource_permutation_contract is not None:
        invariant["resource_preparation_permutation_contract"] = (
            resource_permutation_contract
        )
    if shop_resource_permutation_contract is not None:
        invariant["shop_resource_preparation_permutation_contract"] = (
            shop_resource_permutation_contract
        )
    if bowl_dominance_contract is not None:
        invariant["bowl_dominance_contract"] = bowl_dominance_contract
    return invariant


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _blocked_case_bindings(cases, case_results, *, allow_empty=False):
    """Return the exact independently recomputed blocking-case bindings."""

    cases = list(cases)
    case_results = list(case_results)
    if len(cases) != len(case_results):
        raise ResolutionError("case/result cardinality mismatch")
    targets = {}
    for index, (case, result) in enumerate(zip(cases, case_results)):
        if result.get("classification") not in BLOCKING_CLASSIFICATIONS:
            continue
        key = case_key(case)
        if key is None:
            raise ResolutionError(f"blocked case {index} has no exact binding")
        if key in targets:
            raise ResolutionError(f"duplicate blocked case binding: {key}")
        targets[key] = {
            "case_index": index,
            "original_case_sha256": object_digest(case),
            "source_decision_hash": case.get("decision_hash"),
            "original_classification": result.get("classification"),
            "original_problem_kinds": problem_kinds(result),
        }
    if not targets and not allow_empty:
        raise ResolutionError("no blocked cases require trace extraction")
    return targets


def extract_trace_evidence(trace_path, cases, case_results):
    """Scan ``trace_path`` once and retain only exact blocked case records."""

    trace_path = Path(trace_path)
    cases = list(cases)
    case_results = list(case_results)
    targets = _blocked_case_bindings(cases, case_results)

    attempt_tokens = {
        attempt_id.encode("utf-8") for attempt_id, _seq, _phase in targets
    }
    digest = hashlib.sha256()
    found = {}
    byte_offset = 0
    with trace_path.open("rb") as source:
        for line_number, raw_line in enumerate(source, start=1):
            line_offset = byte_offset
            byte_offset += len(raw_line)
            digest.update(raw_line)
            if not any(token in raw_line for token in attempt_tokens):
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            key = case_key(record)
            if key not in targets:
                continue
            if key in found:
                raise ResolutionError(f"duplicate trace record binding: {key}")
            found[key] = {
                **targets[key],
                "attempt_id": key[0],
                "before_seq": key[1],
                "phase": key[2],
                "trace_line_number": line_number,
                "trace_byte_offset": line_offset,
                "trace_line_byte_length": len(raw_line),
                "trace_line_sha256": hashlib.sha256(raw_line).hexdigest(),
                "trace_record_sha256": object_digest(record),
                "record": record,
            }
    ordered = [
        found[key] for key in sorted(
            found, key=lambda value: (value[0], value[1], value[2])
        )
    ]
    missing = [
        {
            "attempt_id": key[0], "before_seq": key[1], "phase": key[2],
            **targets[key],
        }
        for key in sorted(
            set(targets) - set(found),
            key=lambda value: (value[0], value[1], value[2]),
        )
    ]
    return {
        "schema_version": LEGACY_TRACE_EVIDENCE_SCHEMA_VERSION,
        "source_trace": str(trace_path),
        "scan_byte_length": byte_offset,
        "source_trace_prefix_sha256": digest.hexdigest(),
        "target_case_count": len(targets),
        "found_case_count": len(found),
        "missing_case_count": len(missing),
        "records": ordered,
        "missing": missing,
    }


def _validate_legacy_trace_evidence(
    trace_path, evidence, cases, case_results, *, verify_trace_prefix=True,
):
    """Validate an immutable trace prefix while allowing later appends."""

    if not isinstance(evidence, dict) or evidence.get("schema_version") not in {
        LEGACY_TRACE_EVIDENCE_SCHEMA_VERSION,
        TRACE_EVIDENCE_SCHEMA_VERSION,
    }:
        raise ResolutionError("trace evidence schema mismatch")
    cases = list(cases)
    case_results = list(case_results)
    if len(cases) != len(case_results):
        raise ResolutionError("case/result cardinality mismatch")
    expected = {}
    for index, (case, result) in enumerate(zip(cases, case_results)):
        if result.get("classification") not in BLOCKING_CLASSIFICATIONS:
            continue
        key = case_key(case)
        if key is None or key in expected:
            raise ResolutionError("blocked case binding is missing or duplicate")
        expected[key] = {
            "case_index": index,
            "original_case_sha256": object_digest(case),
            "source_decision_hash": case.get("decision_hash"),
            "original_classification": result.get("classification"),
            "original_problem_kinds": problem_kinds(result),
        }
    rows = evidence.get("records")
    if not isinstance(rows, list):
        raise ResolutionError("trace evidence records are missing")
    if (
        evidence.get("target_case_count") != len(expected)
        or evidence.get("found_case_count") != len(expected)
        or evidence.get("missing_case_count") != 0
        or evidence.get("missing") != []
        or len(rows) != len(expected)
    ):
        raise ResolutionError("trace evidence coverage is incomplete")

    scan_length = evidence.get("scan_byte_length")
    prefix_digest = evidence.get("source_trace_prefix_sha256")
    if (
        not isinstance(scan_length, int) or isinstance(scan_length, bool)
        or scan_length <= 0
        or not isinstance(prefix_digest, str) or len(prefix_digest) != 64
    ):
        raise ResolutionError("trace prefix binding is invalid")
    compact = evidence.get("schema_version") == TRACE_EVIDENCE_SCHEMA_VERSION
    if compact:
        migration = evidence.get("migration_authority")
        if (
            evidence.get("evidence_mode") != COMPACT_TRACE_EVIDENCE_MODE
            or not isinstance(migration, dict)
            or migration.get("verified_historical_prefix") is not True
            or not isinstance(migration.get("compacted_at"), (int, float))
            or isinstance(migration.get("compacted_at"), bool)
            or not math.isfinite(float(migration["compacted_at"]))
            or migration["compacted_at"] <= 0
            or not all(
                isinstance(migration.get(field), str)
                and len(migration[field]) == 64
                for field in (
                    "legacy_trace_evidence_sha256",
                    "freeze_manifest_sha256",
                )
            )
            or type(migration.get("original_trace_size")) is not int
            or migration["original_trace_size"] < scan_length
        ):
            raise ResolutionError("compact trace migration authority is invalid")
    else:
        if trace_path is None:
            raise ResolutionError("legacy trace evidence requires the source trace")
        trace_path = Path(trace_path)
    if compact:
        trace_path = None
    else:
        trace_path = Path(trace_path)
    if not compact:
        try:
            if trace_path.stat().st_size < scan_length:
                raise ResolutionError("historical trace prefix was truncated")
            if verify_trace_prefix:
                digest = hashlib.sha256()
                remaining = scan_length
                with trace_path.open("rb") as source:
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ResolutionError(
                                "historical trace prefix was truncated"
                            )
                        digest.update(block)
                        remaining -= len(block)
                if digest.hexdigest() != prefix_digest:
                    raise ResolutionError("historical trace prefix digest mismatch")
        except OSError as exc:
            raise ResolutionError("historical trace is unreadable") from exc

    observed = {}
    intervals = []
    try:
        source_context = (
            trace_path.open("rb") if not compact else _NullBinaryReader()
        )
        with source_context as source:
            for row in rows:
                key = case_key(row)
                if key not in expected or key in observed:
                    raise ResolutionError("trace evidence row binding is invalid")
                wanted = expected[key]
                for field, value in wanted.items():
                    if row.get(field) != value:
                        raise ResolutionError(
                            f"trace evidence {field} mismatch: {key}"
                        )
                offset = row.get("trace_byte_offset")
                length = row.get("trace_line_byte_length")
                if (
                    not isinstance(offset, int) or isinstance(offset, bool)
                    or not isinstance(length, int) or isinstance(length, bool)
                    or offset < 0 or length <= 0
                    or offset + length > scan_length
                ):
                    raise ResolutionError("trace evidence byte range is invalid")
                intervals.append((offset, offset + length))
                if compact:
                    encoded = row.get("raw_line_base64")
                    if not isinstance(encoded, str) or not encoded:
                        raise ResolutionError(
                            "compact trace evidence raw line is missing"
                        )
                    try:
                        raw_line = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise ResolutionError(
                            "compact trace evidence raw line is malformed"
                        ) from exc
                else:
                    source.seek(offset)
                    raw_line = source.read(length)
                if len(raw_line) != length:
                    raise ResolutionError("trace evidence line length mismatch")
                if hashlib.sha256(raw_line).hexdigest() != row.get(
                    "trace_line_sha256"
                ):
                    raise ResolutionError("trace evidence line digest mismatch")
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ResolutionError("trace evidence line is malformed") from exc
                if (
                    object_digest(record) != row.get("trace_record_sha256")
                    or object_digest(row.get("record"))
                    != row.get("trace_record_sha256")
                    or record != row.get("record")
                    or case_key(record) != key
                    or record.get("decision_hash") != wanted[
                        "source_decision_hash"
                    ]
                ):
                    raise ResolutionError("trace evidence record digest mismatch")
                observed[key] = row
    except OSError as exc:
        raise ResolutionError("historical trace lines are unreadable") from exc
    if set(observed) != set(expected):
        raise ResolutionError("trace evidence does not cover every blocked case")
    for left, right in zip(sorted(intervals), sorted(intervals)[1:]):
        if left[1] > right[0]:
            raise ResolutionError("trace evidence byte ranges overlap")
    return observed


class _NullBinaryReader:
    """Tiny context manager used by self-contained evidence validation."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_archived_schema2_evidence(evidence, cases):
    """Verify the complete released schema-2 chain without reclassifying it.

    Three formerly blocking historical rows can legitimately become
    authoritative N/A after the independent oracle improves.  The archived
    evidence must nevertheless remain byte-for-byte verifiable.  Recreate its
    original result envelope from its own immutable rows, while binding every
    row back to the append-only case at its original index.
    """

    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != TRACE_EVIDENCE_SCHEMA_VERSION
        or evidence.get("evidence_mode") != COMPACT_TRACE_EVIDENCE_MODE
    ):
        raise ResolutionError("archived trace evidence is not compact schema 2")
    cases = list(cases)
    rows = evidence.get("records")
    if not isinstance(rows, list):
        raise ResolutionError("archived trace evidence records are missing")
    archived_results = [
        {"classification": "audited", "issues": [], "unknowns": []}
        for _case in cases
    ]
    occupied = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ResolutionError("archived trace evidence row is invalid")
        index = row.get("case_index")
        if (
            not isinstance(index, int) or isinstance(index, bool)
            or index < 0 or index >= len(cases) or index in occupied
        ):
            raise ResolutionError("archived trace evidence case index is invalid")
        occupied.add(index)
        if (
            case_key(cases[index]) != case_key(row)
            or object_digest(cases[index]) != row.get("original_case_sha256")
            or cases[index].get("decision_hash")
            != row.get("source_decision_hash")
        ):
            raise ResolutionError("archived trace evidence case binding changed")
        classification = row.get("original_classification")
        problems = row.get("original_problem_kinds")
        if (
            classification not in BLOCKING_CLASSIFICATIONS
            or not isinstance(problems, list)
            or problems != sorted(set(problems))
            or any(not isinstance(value, str) or not value for value in problems)
        ):
            raise ResolutionError(
                "archived trace evidence original problems are invalid"
            )
        archived_results[index] = {
            "classification": classification,
            "issues": list(problems),
            "unknowns": [],
        }
    return _validate_legacy_trace_evidence(
        None,
        evidence,
        cases,
        archived_results,
        verify_trace_prefix=False,
    )


def _compressed_source_identity(source):
    return {
        "schema_version": source.get("schema_version"),
        "evidence_mode": source.get("evidence_mode"),
        "source_trace": source.get("source_trace"),
        "source_attempt_id": source.get("source_attempt_id"),
        "scan_byte_length": source.get("scan_byte_length"),
        "source_trace_sha256": source.get("source_trace_sha256"),
        "source_digest_authority": source.get("source_digest_authority"),
        "source_scan_pass_count": source.get("source_scan_pass_count"),
        "records_sha256": source.get("records_sha256"),
        "payload_compressed_byte_length": source.get(
            "payload_compressed_byte_length"
        ),
        "payload_compressed_sha256": source.get("payload_compressed_sha256"),
        "payload_uncompressed_byte_length": source.get(
            "payload_uncompressed_byte_length"
        ),
        "payload_uncompressed_sha256": source.get(
            "payload_uncompressed_sha256"
        ),
    }


def _compressed_source_id(source):
    return "attempt-trace:" + object_digest(
        _compressed_source_identity(source)
    )[:32]


def _archived_source_id(evidence_sha256):
    return "archived-schema2:" + evidence_sha256[:32]


def _source_manifest_payload(archive_summary, sources):
    return {
        "archived_schema2_summary": archive_summary,
        "compressed_sources": [
            {
                "source_id": source.get("source_id"),
                **_compressed_source_identity(source),
            }
            for source in sources
        ],
    }


def trace_evidence_source_digest(evidence):
    """Return the exact source-set digest for every supported schema."""

    if not isinstance(evidence, dict):
        return None
    if evidence.get("schema_version") == MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION:
        return evidence.get("source_manifest_sha256")
    return evidence.get("source_trace_prefix_sha256")


def trace_evidence_scan_byte_length(evidence):
    if not isinstance(evidence, dict):
        return None
    return evidence.get("scan_byte_length")


def _validate_attempt_trace_label(label, attempt_id):
    if not isinstance(label, str) or not label or "\\" in label:
        raise ResolutionError("compressed trace source path is not canonical")
    path = Path(label)
    if path.is_absolute() or path.as_posix() != label:
        raise ResolutionError("compressed trace source path is not canonical")
    parts = path.parts
    if (
        len(parts) != 4
        or parts[0] != "logs"
        or parts[1] != "attempts"
        or parts[2] != attempt_id
        or parts[3] != "autoplay.log"
    ):
        raise ResolutionError(
            "compressed trace source is not an exact attempt autoplay log"
        )


def _attempt_id_from_trace_label(label):
    """Return the exact attempt encoded by a canonical bounded-trace label."""

    if not isinstance(label, str) or not label or "\\" in label:
        raise ResolutionError("compressed trace source path is not canonical")
    path = Path(label)
    parts = path.parts
    if len(parts) != 4:
        raise ResolutionError(
            "compressed trace source is not an exact attempt autoplay log"
        )
    attempt_id = parts[2]
    _validate_attempt_trace_label(label, attempt_id)
    return attempt_id


_COMPRESSED_SOURCE_FIELDS = {
    "schema_version", "evidence_mode", "source_id", "source_trace",
    "source_attempt_id", "scan_byte_length", "source_trace_sha256",
    "source_digest_authority", "source_scan_pass_count",
    "target_case_count", "found_case_count", "missing_case_count", "missing",
    "records", "records_sha256", "payload_encoding", "payload_base64",
    "payload_compressed_byte_length", "payload_compressed_sha256",
    "payload_uncompressed_byte_length", "payload_uncompressed_sha256",
}
_COMPRESSED_RECORD_FIELDS = {
    "case_index", "original_case_sha256", "source_decision_hash",
    "original_classification", "original_problem_kinds", "attempt_id",
    "before_seq", "phase", "trace_line_number", "trace_byte_offset",
    "trace_line_byte_length", "trace_line_sha256", "trace_record_sha256",
    "payload_byte_offset", "payload_line_byte_length",
}
_ACTIVE_RECORD_REFERENCE_FIELDS = {
    "source_id", "attempt_id", "before_seq", "phase",
}
_ARCHIVE_SUMMARY_FIELDS = {
    "schema_version", "evidence_mode", "source_id", "evidence_sha256",
    "source_trace", "scan_byte_length", "source_trace_prefix_sha256",
    "record_count", "active_record_count",
}
_MULTI_SOURCE_EVIDENCE_FIELDS = {
    "schema_version", "evidence_mode", "archived_schema2_evidence",
    "archived_schema2_evidence_sha256", "archived_schema2_summary", "sources",
    "records", "source_manifest_sha256", "scan_byte_length",
    "target_case_count", "found_case_count", "missing_case_count", "missing",
}


def _validate_compressed_trace_source(source, cases):
    if not isinstance(source, dict) or set(source) != _COMPRESSED_SOURCE_FIELDS:
        raise ResolutionError("compressed trace source envelope is invalid")
    if (
        source.get("schema_version") != COMPRESSED_TRACE_SOURCE_SCHEMA_VERSION
        or source.get("evidence_mode") != COMPRESSED_TRACE_SOURCE_MODE
        or source.get("payload_encoding") != "gzip+base64;mtime=0"
        or source.get("source_digest_authority")
        != "sha256_full_single_stream_v1"
        or source.get("source_scan_pass_count") != 1
    ):
        raise ResolutionError("compressed trace source schema is invalid")
    attempt_id = source.get("source_attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ResolutionError("compressed trace source attempt is missing")
    _validate_attempt_trace_label(source.get("source_trace"), attempt_id)
    if source.get("source_id") != _compressed_source_id(source):
        raise ResolutionError("compressed trace source identity mismatch")
    scan_length = source.get("scan_byte_length")
    if (
        not isinstance(scan_length, int) or isinstance(scan_length, bool)
        or scan_length <= 0
        or not _valid_sha256(source.get("source_trace_sha256"))
    ):
        raise ResolutionError("compressed trace source digest is invalid")
    rows = source.get("records")
    if (
        not isinstance(rows, list)
        or source.get("target_case_count") != len(rows)
        or source.get("found_case_count") != len(rows)
        or source.get("missing_case_count") != 0
        or source.get("missing") != []
        or object_digest(rows) != source.get("records_sha256")
    ):
        raise ResolutionError("compressed trace source coverage is incomplete")
    encoded = source.get("payload_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ResolutionError("compressed trace source payload is missing")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ResolutionError("compressed trace source payload is malformed") from exc
    if (
        len(compressed) != source.get("payload_compressed_byte_length")
        or hashlib.sha256(compressed).hexdigest()
        != source.get("payload_compressed_sha256")
    ):
        raise ResolutionError("compressed trace payload digest mismatch")
    try:
        payload = gzip.decompress(compressed)
    except (OSError, EOFError) as exc:
        raise ResolutionError("compressed trace payload cannot be decoded") from exc
    if (
        len(payload) != source.get("payload_uncompressed_byte_length")
        or hashlib.sha256(payload).hexdigest()
        != source.get("payload_uncompressed_sha256")
    ):
        raise ResolutionError("uncompressed trace payload digest mismatch")

    cases = list(cases)
    observed = {}
    payload_cursor = 0
    source_intervals = []
    previous_source_position = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != _COMPRESSED_RECORD_FIELDS:
            raise ResolutionError("compressed trace record envelope is invalid")
        key = case_key(row)
        index = row.get("case_index")
        if (
            key is None or key in observed or key[0] != attempt_id
            or not isinstance(index, int) or isinstance(index, bool)
            or index < 0 or index >= len(cases)
            or case_key(cases[index]) != key
            or object_digest(cases[index]) != row.get("original_case_sha256")
            or cases[index].get("decision_hash")
            != row.get("source_decision_hash")
        ):
            raise ResolutionError("compressed trace record case binding is invalid")
        if (
            row.get("original_classification") not in BLOCKING_CLASSIFICATIONS
            or not isinstance(row.get("original_problem_kinds"), list)
            or row["original_problem_kinds"]
            != sorted(set(row["original_problem_kinds"]))
        ):
            raise ResolutionError("compressed trace record problems are invalid")
        line_number = row.get("trace_line_number")
        source_offset = row.get("trace_byte_offset")
        source_length = row.get("trace_line_byte_length")
        payload_offset = row.get("payload_byte_offset")
        payload_length = row.get("payload_line_byte_length")
        if (
            not isinstance(line_number, int) or isinstance(line_number, bool)
            or line_number <= 0
            or not isinstance(source_offset, int) or isinstance(source_offset, bool)
            or source_offset < 0
            or not isinstance(source_length, int) or isinstance(source_length, bool)
            or source_length <= 0 or source_offset + source_length > scan_length
            or payload_offset != payload_cursor
            or payload_length != source_length
        ):
            raise ResolutionError("compressed trace record offset is invalid")
        source_position = (source_offset, line_number)
        if (
            previous_source_position is not None
            and source_position <= previous_source_position
        ):
            raise ResolutionError("compressed trace records are not in source order")
        previous_source_position = source_position
        raw_line = payload[payload_offset:payload_offset + payload_length]
        if (
            len(raw_line) != payload_length
            or hashlib.sha256(raw_line).hexdigest()
            != row.get("trace_line_sha256")
        ):
            raise ResolutionError("compressed trace record line digest mismatch")
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResolutionError("compressed trace record is malformed") from exc
        if (
            object_digest(record) != row.get("trace_record_sha256")
            or case_key(record) != key
            or record.get("decision_hash") != row.get("source_decision_hash")
        ):
            raise ResolutionError("compressed trace record digest mismatch")
        observed[key] = row
        payload_cursor += payload_length
        source_intervals.append((source_offset, source_offset + source_length))
    if payload_cursor != len(payload):
        raise ResolutionError("compressed trace payload has unbound bytes")
    for left, right in zip(source_intervals, source_intervals[1:]):
        if left[1] > right[0]:
            raise ResolutionError("compressed trace source ranges overlap")
    return observed


def _load_multi_source_trace_evidence(evidence, cases):
    """Validate immutable schema-3 sources without trusting old classifications.

    Classification/problem fields are derived from the current replay code and
    can legitimately become stricter after an auditor fix.  The raw line,
    append-only case, full attempt-trace digest, offsets, and compressed payload
    remain immutable.  Keep those two concerns separate so a later attempt can
    extend schema 3 without reopening an older attempt trace.
    """

    if not isinstance(evidence, dict) or set(evidence) != _MULTI_SOURCE_EVIDENCE_FIELDS:
        raise ResolutionError("multi-source trace evidence envelope is invalid")
    if (
        evidence.get("schema_version")
        != MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION
        or evidence.get("evidence_mode") != MULTI_SOURCE_TRACE_EVIDENCE_MODE
    ):
        raise ResolutionError("multi-source trace evidence schema mismatch")
    cases = list(cases)
    archive = evidence.get("archived_schema2_evidence")
    archive_sha256 = object_digest(archive)
    if archive_sha256 != evidence.get("archived_schema2_evidence_sha256"):
        raise ResolutionError("archived schema-2 evidence digest mismatch")
    archived_rows = _validate_archived_schema2_evidence(archive, cases)
    archive_id = _archived_source_id(archive_sha256)
    references = evidence.get("records")
    if not isinstance(references, list):
        raise ResolutionError("multi-source active record references are missing")
    active_archive_count = sum(
        1 for reference in references
        if isinstance(reference, dict) and reference.get("source_id") == archive_id
    )
    expected_summary = {
        "schema_version": TRACE_EVIDENCE_SCHEMA_VERSION,
        "evidence_mode": COMPACT_TRACE_EVIDENCE_MODE,
        "source_id": archive_id,
        "evidence_sha256": archive_sha256,
        "source_trace": archive.get("source_trace"),
        "scan_byte_length": archive.get("scan_byte_length"),
        "source_trace_prefix_sha256": archive.get(
            "source_trace_prefix_sha256"
        ),
        "record_count": len(archived_rows),
        "active_record_count": active_archive_count,
    }
    summary = evidence.get("archived_schema2_summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != _ARCHIVE_SUMMARY_FIELDS
        or summary != expected_summary
    ):
        raise ResolutionError("archived schema-2 evidence summary mismatch")

    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ResolutionError("multi-source compressed sources are missing")
    source_rows = {archive_id: archived_rows}
    source_ids = {archive_id}
    for source in sources:
        source_id = source.get("source_id") if isinstance(source, dict) else None
        if source_id in source_ids:
            raise ResolutionError("duplicate multi-source trace identity")
        source_rows[source_id] = _validate_compressed_trace_source(source, cases)
        source_ids.add(source_id)
    manifest = _source_manifest_payload(summary, sources)
    if object_digest(manifest) != evidence.get("source_manifest_sha256"):
        raise ResolutionError("multi-source trace manifest digest mismatch")
    total_scan_length = archive.get("scan_byte_length") + sum(
        source["scan_byte_length"] for source in sources
    )
    if evidence.get("scan_byte_length") != total_scan_length:
        raise ResolutionError("multi-source trace scan length mismatch")

    observed = {}
    normalized_references = []
    for reference in references:
        if (
            not isinstance(reference, dict)
            or set(reference) != _ACTIVE_RECORD_REFERENCE_FIELDS
        ):
            raise ResolutionError("multi-source active record reference is invalid")
        source_id = reference.get("source_id")
        key = case_key(reference)
        if (
            source_id not in source_rows or key not in source_rows[source_id]
            or key in observed
        ):
            raise ResolutionError("multi-source active record binding is invalid")
        row = source_rows[source_id][key]
        observed[key] = row
        normalized_references.append(reference)
    if normalized_references != sorted(
        normalized_references,
        key=lambda row: (row["attempt_id"], row["before_seq"], row["phase"]),
    ):
        raise ResolutionError("multi-source active records are not canonical")
    if (
        evidence.get("target_case_count") != len(observed)
        or evidence.get("found_case_count") != len(observed)
        or evidence.get("missing_case_count") != 0
        or evidence.get("missing") != []
        or len(references) != len(observed)
    ):
        raise ResolutionError("multi-source trace evidence coverage is incomplete")
    return {
        "archive_id": archive_id,
        "archived_rows": archived_rows,
        "source_rows": source_rows,
        "observed": observed,
    }


def _validate_multi_source_trace_evidence(evidence, cases, case_results):
    cases = list(cases)
    case_results = list(case_results)
    expected = _blocked_case_bindings(cases, case_results, allow_empty=True)
    loaded = _load_multi_source_trace_evidence(evidence, cases)
    observed = loaded["observed"]
    for key, row in observed.items():
        if key not in expected:
            raise ResolutionError(
                f"multi-source trace contains a nonblocking active row: {key}"
            )
        for field, wanted in expected[key].items():
            if row.get(field) != wanted:
                raise ResolutionError(
                    f"multi-source trace {field} mismatch: {key}"
                )
    if set(observed) != set(expected):
        raise ResolutionError("multi-source trace evidence coverage is incomplete")
    return observed


def validate_trace_evidence(
    trace_path, evidence, cases, case_results, *, verify_trace_prefix=True,
):
    """Validate legacy, compact, or self-contained multi-source evidence."""

    if (
        isinstance(evidence, dict)
        and evidence.get("schema_version")
        == MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION
    ):
        # Schema 3 was fully digested during its single extraction pass.  It
        # deliberately never reopens either the retired root trace or an
        # attempt trace during replay/freeze revalidation.
        return _validate_multi_source_trace_evidence(
            evidence, cases, case_results
        )
    return _validate_legacy_trace_evidence(
        trace_path,
        evidence,
        cases,
        case_results,
        verify_trace_prefix=verify_trace_prefix,
    )


def _build_compressed_trace_source(
    trace_path, remaining, *, source_trace_label,
):
    """Read one exact attempt trace once and embed only requested raw lines."""

    if not remaining:
        raise ResolutionError("no blocked cases require an attempt trace source")
    attempt_ids = {key[0] for key in remaining}
    if len(attempt_ids) != 1:
        raise ResolutionError(
            "remaining blocked cases do not belong to one exact attempt"
        )
    source_attempt_id = next(iter(attempt_ids))
    _validate_attempt_trace_label(source_trace_label, source_attempt_id)

    attempt_token = source_attempt_id.encode("utf-8")
    digest = hashlib.sha256()
    found = {}
    found_raw = {}
    byte_offset = 0
    trace_path = Path(trace_path)
    try:
        with trace_path.open("rb") as source:
            for line_number, raw_line in enumerate(source, start=1):
                line_offset = byte_offset
                byte_offset += len(raw_line)
                digest.update(raw_line)
                if attempt_token not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                key = case_key(record)
                if key not in remaining:
                    continue
                if key in found:
                    raise ResolutionError(
                        f"duplicate attempt trace record binding: {key}"
                    )
                found[key] = {
                    **remaining[key],
                    "attempt_id": key[0],
                    "before_seq": key[1],
                    "phase": key[2],
                    "trace_line_number": line_number,
                    "trace_byte_offset": line_offset,
                    "trace_line_byte_length": len(raw_line),
                    "trace_line_sha256": hashlib.sha256(raw_line).hexdigest(),
                    "trace_record_sha256": object_digest(record),
                }
                found_raw[key] = raw_line
    except OSError as exc:
        raise ResolutionError("attempt trace is unreadable") from exc
    missing_keys = set(remaining) - set(found)
    if missing_keys:
        missing = sorted(
            missing_keys, key=lambda key: (key[0], key[1], key[2])
        )
        raise ResolutionError(
            "attempt trace does not cover remaining blocked cases: "
            + ",".join(f"{key[0]}:{key[1]}:{key[2]}" for key in missing)
        )

    ordered_keys = sorted(
        found,
        key=lambda key: (
            found[key]["trace_byte_offset"],
            found[key]["trace_line_number"],
        ),
    )
    payload_parts = []
    payload_offset = 0
    source_rows = []
    for key in ordered_keys:
        raw_line = found_raw[key]
        row = {
            **found[key],
            "payload_byte_offset": payload_offset,
            "payload_line_byte_length": len(raw_line),
        }
        source_rows.append(row)
        payload_parts.append(raw_line)
        payload_offset += len(raw_line)
    payload = b"".join(payload_parts)
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    source = {
        "schema_version": COMPRESSED_TRACE_SOURCE_SCHEMA_VERSION,
        "evidence_mode": COMPRESSED_TRACE_SOURCE_MODE,
        "source_trace": source_trace_label,
        "source_attempt_id": source_attempt_id,
        "scan_byte_length": byte_offset,
        "source_trace_sha256": digest.hexdigest(),
        "source_digest_authority": "sha256_full_single_stream_v1",
        "source_scan_pass_count": 1,
        "target_case_count": len(remaining),
        "found_case_count": len(found),
        "missing_case_count": 0,
        "missing": [],
        "records": source_rows,
        "records_sha256": object_digest(source_rows),
        "payload_encoding": "gzip+base64;mtime=0",
        "payload_base64": base64.b64encode(compressed).decode("ascii"),
        "payload_compressed_byte_length": len(compressed),
        "payload_compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "payload_uncompressed_byte_length": len(payload),
        "payload_uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
    }
    source["source_id"] = _compressed_source_id(source)
    return source, found


def _build_compressed_trace_sources(
    trace_sources, remaining, *, existing_sources=(),
):
    """Embed several exact attempt traces, opening each source exactly once."""

    trace_sources = list(trace_sources)
    if not trace_sources:
        raise ResolutionError("no attempt trace sources were supplied")
    remaining = dict(remaining)
    existing_labels = {
        source.get("source_trace") for source in existing_sources
        if isinstance(source, dict)
    }
    existing_attempt_ids = {
        source.get("source_attempt_id") for source in existing_sources
        if isinstance(source, dict)
    }
    normalized = []
    supplied_labels = set()
    supplied_attempt_ids = set()
    for trace_path, source_trace_label in trace_sources:
        source_attempt_id = _attempt_id_from_trace_label(source_trace_label)
        if (
            source_trace_label in existing_labels
            or source_attempt_id in existing_attempt_ids
            or source_trace_label in supplied_labels
            or source_attempt_id in supplied_attempt_ids
        ):
            raise ResolutionError("attempt trace source is already embedded")
        supplied_labels.add(source_trace_label)
        supplied_attempt_ids.add(source_attempt_id)
        normalized.append((
            source_attempt_id, Path(trace_path), source_trace_label,
        ))

    expected_attempt_ids = {key[0] for key in remaining}
    if supplied_attempt_ids != expected_attempt_ids:
        missing = sorted(expected_attempt_ids - supplied_attempt_ids)
        extra = sorted(supplied_attempt_ids - expected_attempt_ids)
        raise ResolutionError(
            "attempt trace source set does not exactly cover blocker attempts"
            f"; missing={missing}; extra={extra}"
        )

    built_sources = []
    found_rows = {}
    for source_attempt_id, trace_path, source_trace_label in sorted(
        normalized, key=lambda row: row[0]
    ):
        requested = {
            key: value for key, value in remaining.items()
            if key[0] == source_attempt_id
        }
        if not requested:
            raise ResolutionError(
                "no blocked cases require attempt trace source: "
                + source_attempt_id
            )
        source, found = _build_compressed_trace_source(
            trace_path,
            requested,
            source_trace_label=source_trace_label,
        )
        built_sources.append(source)
        found_rows.update(found)
        for key in found:
            remaining.pop(key, None)
    return built_sources, found_rows, remaining


def _extend_multi_source_trace_evidence_many(
    base_evidence,
    trace_sources,
    cases,
    case_results,
    *,
    allow_refresh_only=False,
):
    """Refresh derived classifications and append bounded trace sources.

    Existing raw lines and full source-trace digests are independently
    revalidated from the self-contained schema-3 bundle.  Only replay-derived
    case metadata is refreshed.  Therefore an auditor taxonomy change does not
    force a scan of an already archived attempt log.
    """

    cases = list(cases)
    case_results = list(case_results)
    expected = _blocked_case_bindings(cases, case_results, allow_empty=True)
    loaded = _load_multi_source_trace_evidence(base_evidence, cases)
    archive = copy.deepcopy(base_evidence["archived_schema2_evidence"])
    archive_sha256 = object_digest(archive)
    archive_id = _archived_source_id(archive_sha256)

    active_archived = {}
    for key, row in loaded["archived_rows"].items():
        wanted = expected.get(key)
        if wanted is not None and all(
            row.get(field) == value for field, value in wanted.items()
        ):
            active_archived[key] = row

    refreshed_sources = []
    active_source_ids = {}
    for original_source in base_evidence["sources"]:
        source = copy.deepcopy(original_source)
        for row in source["records"]:
            key = case_key(row)
            wanted = expected.get(key)
            if wanted is not None:
                for field, value in wanted.items():
                    row[field] = copy.deepcopy(value)
        source["records_sha256"] = object_digest(source["records"])
        source["source_id"] = _compressed_source_id(source)
        refreshed_sources.append(source)
        for row in source["records"]:
            key = case_key(row)
            if key in expected:
                active_source_ids[key] = source["source_id"]

    remaining = {
        key: value for key, value in expected.items()
        if key not in active_archived and key not in active_source_ids
    }
    if not remaining and not allow_refresh_only:
        raise ResolutionError(
            "no new blocked cases require an attempt trace source"
        )
    if remaining:
        # A later auditor can expose a blocker in a trace whose earlier
        # blocker subset is already embedded.  Re-scan that same immutable
        # source exactly once and replace its compressed subset with the
        # union of old and newly required raw lines.  The full-source digest
        # and byte length must remain identical, so this cannot substitute a
        # different or edited trace under an existing attempt identity.
        remaining_trace_sources = []
        supplied_attempt_ids = set()
        for trace_path, source_trace_label in trace_sources:
            source_attempt_id = _attempt_id_from_trace_label(
                source_trace_label
            )
            if source_attempt_id in supplied_attempt_ids:
                raise ResolutionError("attempt trace source was supplied twice")
            supplied_attempt_ids.add(source_attempt_id)
            existing_index = next((
                index for index, source in enumerate(refreshed_sources)
                if source.get("source_attempt_id") == source_attempt_id
            ), None)
            if existing_index is None:
                remaining_trace_sources.append((
                    trace_path, source_trace_label,
                ))
                continue
            missing_for_attempt = {
                key: value for key, value in remaining.items()
                if key[0] == source_attempt_id
            }
            if not missing_for_attempt:
                raise ResolutionError(
                    "embedded attempt trace has no newly required blockers"
                )
            original_source = refreshed_sources[existing_index]
            requested = {
                case_key(row): copy.deepcopy(row)
                for row in original_source.get("records") or []
            }
            requested.update(missing_for_attempt)
            rebuilt, found = _build_compressed_trace_source(
                trace_path,
                requested,
                source_trace_label=source_trace_label,
            )
            if (
                rebuilt.get("scan_byte_length")
                != original_source.get("scan_byte_length")
                or rebuilt.get("source_trace_sha256")
                != original_source.get("source_trace_sha256")
            ):
                raise ResolutionError(
                    "embedded attempt trace changed during coverage extension"
                )
            refreshed_sources[existing_index] = rebuilt
            for key in list(active_source_ids):
                if key[0] == source_attempt_id:
                    active_source_ids.pop(key, None)
            for key in found:
                if key in expected:
                    active_source_ids[key] = rebuilt["source_id"]
                remaining.pop(key, None)

        expected_remaining_attempts = {key[0] for key in remaining}
        supplied_remaining_attempts = {
            _attempt_id_from_trace_label(label)
            for _path, label in remaining_trace_sources
        }
        if supplied_remaining_attempts != expected_remaining_attempts:
            raise ResolutionError(
                "attempt trace source set does not exactly cover remaining "
                "blocker attempts"
            )
        if remaining:
            new_sources, _found, remaining = _build_compressed_trace_sources(
                remaining_trace_sources,
                remaining,
                existing_sources=refreshed_sources,
            )
            refreshed_sources.extend(new_sources)
            for source in new_sources:
                for row in source["records"]:
                    active_source_ids[case_key(row)] = source["source_id"]

    unresolved_archive = set(expected) - (
        set(active_archived) | set(active_source_ids)
    )
    if unresolved_archive:
        raise ResolutionError(
            "current blockers cannot be rebound without an exact trace source: "
            + ",".join(
                f"{key[0]}:{key[1]}:{key[2]}"
                for key in sorted(unresolved_archive)
            )
        )

    references = [
        {
            "source_id": archive_id,
            "attempt_id": key[0],
            "before_seq": key[1],
            "phase": key[2],
        }
        for key in active_archived
    ] + [
        {
            "source_id": source_id,
            "attempt_id": key[0],
            "before_seq": key[1],
            "phase": key[2],
        }
        for key, source_id in active_source_ids.items()
    ]
    references.sort(
        key=lambda row: (row["attempt_id"], row["before_seq"], row["phase"])
    )
    archive_summary = {
        "schema_version": TRACE_EVIDENCE_SCHEMA_VERSION,
        "evidence_mode": COMPACT_TRACE_EVIDENCE_MODE,
        "source_id": archive_id,
        "evidence_sha256": archive_sha256,
        "source_trace": archive.get("source_trace"),
        "scan_byte_length": archive.get("scan_byte_length"),
        "source_trace_prefix_sha256": archive.get(
            "source_trace_prefix_sha256"
        ),
        "record_count": len(loaded["archived_rows"]),
        "active_record_count": len(active_archived),
    }
    evidence = {
        "schema_version": MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION,
        "evidence_mode": MULTI_SOURCE_TRACE_EVIDENCE_MODE,
        "archived_schema2_evidence": archive,
        "archived_schema2_evidence_sha256": archive_sha256,
        "archived_schema2_summary": archive_summary,
        "sources": refreshed_sources,
        "records": references,
        "source_manifest_sha256": object_digest(
            _source_manifest_payload(archive_summary, refreshed_sources)
        ),
        "scan_byte_length": archive["scan_byte_length"] + sum(
            source["scan_byte_length"] for source in refreshed_sources
        ),
        "target_case_count": len(expected),
        "found_case_count": len(expected),
        "missing_case_count": 0,
        "missing": [],
    }
    validate_trace_evidence(None, evidence, cases, case_results)
    return evidence


def refresh_compact_trace_evidence(
    archived_evidence,
    cases,
    case_results,
):
    """Refresh replay-derived schema-3 metadata without rescanning traces.

    The embedded raw lines, their digests, and the complete source-trace
    digests stay immutable.  Only classifications/problem kinds produced by
    the current independent auditor and the derived source IDs/manifest are
    regenerated.  This is required when an auditor improvement changes the
    problem taxonomy for an already archived case but introduces no new
    blocked case that would otherwise drive an ``extend`` operation.
    """

    if (
        not isinstance(archived_evidence, dict)
        or archived_evidence.get("schema_version")
        != MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION
    ):
        raise ResolutionError(
            "refresh requires self-contained schema-3 trace evidence"
        )
    return _extend_multi_source_trace_evidence_many(
        archived_evidence,
        [],
        cases,
        case_results,
        allow_refresh_only=True,
    )


def _extend_multi_source_trace_evidence(
    base_evidence,
    trace_path,
    cases,
    case_results,
    *,
    source_trace_label,
):
    return _extend_multi_source_trace_evidence_many(
        base_evidence,
        [(trace_path, source_trace_label)],
        cases,
        case_results,
    )


def extend_compact_trace_evidence_many(
    archived_evidence,
    trace_sources,
    cases,
    case_results,
):
    """Build schema 3 from released evidence plus bounded attempt traces.

    Every attempt trace is opened exactly once and consumed sequentially to
    EOF.  All supplied sources must collectively cover every current blocker;
    no incomplete intermediate evidence object is returned or written.
    """

    cases = list(cases)
    case_results = list(case_results)
    trace_sources = list(trace_sources)
    if (
        isinstance(archived_evidence, dict)
        and archived_evidence.get("schema_version")
        == MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION
    ):
        return _extend_multi_source_trace_evidence_many(
            archived_evidence,
            trace_sources,
            cases,
            case_results,
        )
    expected = _blocked_case_bindings(cases, case_results)
    archived_rows = _validate_archived_schema2_evidence(
        archived_evidence, cases
    )
    archived_sha256 = object_digest(archived_evidence)
    archive_id = _archived_source_id(archived_sha256)

    active_archived = {}
    for key, row in archived_rows.items():
        wanted = expected.get(key)
        if wanted is None:
            continue
        if all(row.get(field) == value for field, value in wanted.items()):
            active_archived[key] = row
    remaining = {
        key: value for key, value in expected.items()
        if key not in active_archived
    }
    if not remaining:
        raise ResolutionError(
            "no new blocked cases require an attempt trace source"
        )
    sources, found, remaining = _build_compressed_trace_sources(
        trace_sources,
        remaining,
    )
    if remaining:
        raise ResolutionError(
            "current blockers cannot be rebound without an exact trace source: "
            + ",".join(
                f"{key[0]}:{key[1]}:{key[2]}"
                for key in sorted(remaining)
            )
        )

    references = [
        {
            "source_id": archive_id,
            "attempt_id": key[0],
            "before_seq": key[1],
            "phase": key[2],
        }
        for key in active_archived
    ] + [
        {
            "source_id": next(
                source["source_id"] for source in sources
                if source["source_attempt_id"] == key[0]
            ),
            "attempt_id": key[0],
            "before_seq": key[1],
            "phase": key[2],
        }
        for key in found
    ]
    references.sort(
        key=lambda row: (row["attempt_id"], row["before_seq"], row["phase"])
    )
    archive_summary = {
        "schema_version": TRACE_EVIDENCE_SCHEMA_VERSION,
        "evidence_mode": COMPACT_TRACE_EVIDENCE_MODE,
        "source_id": archive_id,
        "evidence_sha256": archived_sha256,
        "source_trace": archived_evidence.get("source_trace"),
        "scan_byte_length": archived_evidence.get("scan_byte_length"),
        "source_trace_prefix_sha256": archived_evidence.get(
            "source_trace_prefix_sha256"
        ),
        "record_count": len(archived_rows),
        "active_record_count": len(active_archived),
    }
    evidence = {
        "schema_version": MULTI_SOURCE_TRACE_EVIDENCE_SCHEMA_VERSION,
        "evidence_mode": MULTI_SOURCE_TRACE_EVIDENCE_MODE,
        "archived_schema2_evidence": copy.deepcopy(archived_evidence),
        "archived_schema2_evidence_sha256": archived_sha256,
        "archived_schema2_summary": archive_summary,
        "sources": sources,
        "records": references,
        "source_manifest_sha256": object_digest(
            _source_manifest_payload(archive_summary, sources)
        ),
        "scan_byte_length": (
            archived_evidence["scan_byte_length"]
            + sum(source["scan_byte_length"] for source in sources)
        ),
        "target_case_count": len(expected),
        "found_case_count": len(expected),
        "missing_case_count": 0,
        "missing": [],
    }
    validate_trace_evidence(None, evidence, cases, case_results)
    return evidence


def extend_compact_trace_evidence(
    archived_evidence,
    trace_path,
    cases,
    case_results,
    *,
    source_trace_label,
):
    """Compatibility wrapper for extending with one exact attempt trace."""

    return extend_compact_trace_evidence_many(
        archived_evidence,
        [(trace_path, source_trace_label)],
        cases,
        case_results,
    )


def compact_trace_evidence(
    trace_path,
    evidence,
    cases,
    case_results,
    *,
    freeze_manifest,
    legacy_evidence_sha256,
    freeze_manifest_sha256,
):
    """Embed the exact bound lines after a prior freeze verified the 1GB prefix.

    This migration intentionally performs only 45 bounded seeks.  It never
    re-hashes the historical trace: the supplied freeze manifest must already
    bind the legacy evidence and prove that the full prefix was verified.
    """

    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != LEGACY_TRACE_EVIDENCE_SCHEMA_VERSION
    ):
        raise ResolutionError("only legacy trace evidence can be compacted")
    revalidation = (
        freeze_manifest.get("decision_case_replay_revalidation")
        if isinstance(freeze_manifest, dict) else None
    )
    artifacts = (
        revalidation.get("artifact_sha256")
        if isinstance(revalidation, dict) else None
    )
    if (
        freeze_manifest.get("release_gate_passed") is not True
        or not isinstance(revalidation, dict)
        or revalidation.get("trace_prefix_verified") is not True
        or not isinstance(artifacts, dict)
        or artifacts.get("decision-case-trace-evidence.json")
        != legacy_evidence_sha256
        or not all(
            isinstance(value, str) and len(value) == 64
            for value in (legacy_evidence_sha256, freeze_manifest_sha256)
        )
    ):
        raise ResolutionError("freeze manifest did not verify legacy trace evidence")

    trace_path = Path(trace_path)
    observed = validate_trace_evidence(
        trace_path,
        evidence,
        cases,
        case_results,
        verify_trace_prefix=False,
    )
    compact = copy.deepcopy(evidence)
    compact["schema_version"] = TRACE_EVIDENCE_SCHEMA_VERSION
    compact["evidence_mode"] = COMPACT_TRACE_EVIDENCE_MODE
    compact["source_trace"] = "archived:autoplay.log"
    compact["migration_authority"] = {
        "authority": "freeze_manifest_verified_prefix_v1",
        "verified_historical_prefix": True,
        "legacy_trace_evidence_sha256": legacy_evidence_sha256,
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "original_trace_size": trace_path.stat().st_size,
        "compacted_at": time.time(),
    }
    with trace_path.open("rb") as source:
        for row in compact["records"]:
            key = case_key(row)
            verified = observed[key]
            source.seek(verified["trace_byte_offset"])
            raw_line = source.read(verified["trace_line_byte_length"])
            if (
                len(raw_line) != verified["trace_line_byte_length"]
                or hashlib.sha256(raw_line).hexdigest()
                != verified["trace_line_sha256"]
            ):
                raise ResolutionError("historical trace changed during compaction")
            row["raw_line_base64"] = base64.b64encode(raw_line).decode("ascii")
    validate_trace_evidence(
        None,
        compact,
        cases,
        case_results,
        verify_trace_prefix=False,
    )
    return compact


def generate_resolution_document(
    cases,
    case_results,
    *,
    fixture_catalog,
    trace_evidence,
    trace_path,
    audit_one,
):
    """Generate only bindings; validation still re-executes every invariant."""

    cases = list(cases)
    case_results = list(case_results)
    fixture_by_id = validate_fixture_catalog(fixture_catalog)
    trace_by_key = validate_trace_evidence(
        trace_path, trace_evidence, cases, case_results
    )
    invariant_results = {}
    resolutions = []
    for index, (case, result) in enumerate(zip(cases, case_results)):
        classification = result.get("classification")
        if classification not in BLOCKING_CLASSIFICATIONS:
            continue
        if result.get("historical") is not True:
            raise ResolutionError(
                f"current-hash case cannot use a legacy resolution: {index}"
            )
        problems = tuple(problem_kinds(result))
        fixture_id = _case_fixture_id(case, classification, problems)
        if fixture_id is None:
            raise ResolutionError(
                f"no executable invariant for blocked case {index}: "
                f"{classification}/{problems}"
            )
        fixture = fixture_by_id[fixture_id]
        if fixture_id not in invariant_results:
            invariant_results[fixture_id] = run_fixture_invariant(
                fixture, audit_one
            )
        key = case_key(case)
        trace_row = trace_by_key[key]
        resolutions.append({
            "authority_version": AUTHORITY_VERSION,
            "resolution_kind": "resolved_by_current_regression",
            "attempt_id": key[0],
            "before_seq": key[1],
            "phase": key[2],
            "source_decision_hash": case.get("decision_hash"),
            "original_case_sha256": object_digest(case),
            "original_classification": classification,
            "original_problem_kinds": list(problems),
            "trace_record_sha256": trace_row["trace_record_sha256"],
            "trace_line_sha256": trace_row["trace_line_sha256"],
            "trace_byte_offset": trace_row["trace_byte_offset"],
            "fixture_id": fixture_id,
            "fixture_sha256": object_digest(fixture),
        })
    resolutions.sort(
        key=lambda row: (row["attempt_id"], row["before_seq"], row["phase"])
    )
    return {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "authority_version": AUTHORITY_VERSION,
        "trace_evidence_sha256": object_digest(trace_evidence),
        "fixture_catalog_sha256": object_digest(fixture_catalog),
        "resolution_count": len(resolutions),
        "resolutions": resolutions,
    }


def apply_resolution_document(
    cases,
    case_results,
    *,
    target_decision_hash,
    resolution_document,
    fixture_catalog,
    trace_evidence,
    trace_path,
    audit_one,
    verify_trace_prefix=True,
):
    """Validate and apply regression resolutions to independently found gaps."""

    cases = list(cases)
    results = [dict(row) for row in case_results]
    failures = []
    resolved = []
    runtime = {
        "authority_version": AUTHORITY_VERSION,
        "target_decision_hash": target_decision_hash,
        "source_case_corpus_sha256": object_digest(cases),
        "fixture_catalog_sha256": object_digest(fixture_catalog),
        "trace_evidence_sha256": object_digest(trace_evidence),
        "resolution_document_sha256": object_digest(resolution_document),
        "trace_prefix_sha256": trace_evidence_source_digest(trace_evidence),
        "trace_scan_byte_length": trace_evidence_scan_byte_length(
            trace_evidence
        ),
    }
    try:
        if (
            not isinstance(resolution_document, dict)
            or resolution_document.get("schema_version")
            != RESOLUTION_SCHEMA_VERSION
            or resolution_document.get("authority_version")
            != AUTHORITY_VERSION
            or resolution_document.get("trace_evidence_sha256")
            != object_digest(trace_evidence)
            or resolution_document.get("fixture_catalog_sha256")
            != object_digest(fixture_catalog)
        ):
            raise ResolutionError("resolution document envelope mismatch")
        fixture_by_id = validate_fixture_catalog(fixture_catalog)
        trace_by_key = validate_trace_evidence(
            trace_path,
            trace_evidence,
            cases,
            results,
            verify_trace_prefix=verify_trace_prefix,
        )
        entries = resolution_document.get("resolutions")
        if (
            not isinstance(entries, list)
            or resolution_document.get("resolution_count") != len(entries)
        ):
            raise ResolutionError("resolution document count mismatch")
        by_key = {}
        for entry in entries:
            key = case_key(entry)
            if not isinstance(entry, dict) or key is None or key in by_key:
                raise ResolutionError("resolution entry binding is invalid")
            by_key[key] = entry
        blocked_keys = {
            case_key(case) for case, result in zip(cases, results)
            if result.get("classification") in BLOCKING_CLASSIFICATIONS
        }
        if any(
            result.get("historical") is not True
            for result in results
            if result.get("classification") in BLOCKING_CLASSIFICATIONS
        ):
            raise ResolutionError(
                "current-hash case cannot use a legacy resolution"
            )
        if set(by_key) != blocked_keys:
            raise ResolutionError(
                "resolution document does not exactly cover blocked cases"
            )

        invariant_cache = {}
        for index, (case, result) in enumerate(zip(cases, results)):
            if result.get("classification") not in BLOCKING_CLASSIFICATIONS:
                continue
            key = case_key(case)
            entry = by_key[key]
            classification = result.get("classification")
            problems = tuple(problem_kinds(result))
            fixture_id = _case_fixture_id(case, classification, problems)
            fixture = fixture_by_id.get(fixture_id)
            trace_row = trace_by_key.get(key)
            expected = {
                "authority_version": AUTHORITY_VERSION,
                "resolution_kind": "resolved_by_current_regression",
                "attempt_id": key[0],
                "before_seq": key[1],
                "phase": key[2],
                "source_decision_hash": case.get("decision_hash"),
                "original_case_sha256": object_digest(case),
                "original_classification": classification,
                "original_problem_kinds": list(problems),
                "trace_record_sha256": trace_row.get("trace_record_sha256"),
                "trace_line_sha256": trace_row.get("trace_line_sha256"),
                "trace_byte_offset": trace_row.get("trace_byte_offset"),
                "fixture_id": fixture_id,
                "fixture_sha256": object_digest(fixture),
            }
            if entry != expected:
                raise ResolutionError(f"resolution entry mismatch: {key}")
            if fixture_id not in invariant_cache:
                invariant_cache[fixture_id] = run_fixture_invariant(
                    fixture, audit_one
                )
            invariant = invariant_cache[fixture_id]
            original = {
                "classification": classification,
                "issues": list(result.get("issues") or []),
                "unknowns": list(result.get("unknowns") or []),
                "problem_kinds": list(problems),
            }
            result.update({
                "classification": "resolved",
                "authority": AUTHORITY_VERSION,
                "reason": (
                    "resolved_by_current_regression:"
                    + invariant["fixture_id"]
                ),
                "issues": [],
                "unknowns": [],
                "not_applicable_checks": [],
                "resolution": {
                    **expected,
                    "original": original,
                    "invariant": invariant,
                    "target_decision_hash": target_decision_hash,
                },
            })
            resolved.append(index)
    except (ResolutionError, TypeError, AttributeError) as exc:
        failures.append({
            "kind": "decision_case_resolution_invalid",
            "error": str(exc),
        })
    runtime.update({
        "resolved_count": len(resolved),
        "resolution_failure_count": len(failures),
        "resolved_case_indexes": resolved,
        "failures": failures,
        "status": "clear" if not failures else "issues",
    })
    return results, runtime


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--trace", type=Path, required=True)
    extract.add_argument("--cases", type=Path, required=True)
    extract.add_argument("--decision-hash", required=True)
    extract.add_argument("--write", type=Path, required=True)
    extend = subparsers.add_parser("extend")
    extend.add_argument("--trace", type=Path, required=True)
    extend.add_argument("--source-trace-label", required=True)
    extend.add_argument("--cases", type=Path, required=True)
    extend.add_argument("--decision-hash", required=True)
    extend.add_argument("--base-evidence", type=Path, required=True)
    extend.add_argument("--write", type=Path, required=True)
    extend_many = subparsers.add_parser("extend-many")
    extend_many.add_argument("--trace", type=Path, action="append", required=True)
    extend_many.add_argument(
        "--source-trace-label", action="append", required=True
    )
    extend_many.add_argument("--cases", type=Path, required=True)
    extend_many.add_argument("--decision-hash", required=True)
    extend_many.add_argument("--base-evidence", type=Path, required=True)
    extend_many.add_argument("--write", type=Path, required=True)
    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--cases", type=Path, required=True)
    refresh.add_argument("--decision-hash", required=True)
    refresh.add_argument("--base-evidence", type=Path, required=True)
    refresh.add_argument("--write", type=Path, required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--trace", type=Path)
    generate.add_argument("--cases", type=Path, required=True)
    generate.add_argument("--decision-hash", required=True)
    generate.add_argument("--trace-evidence", type=Path, required=True)
    generate.add_argument("--fixtures", type=Path, required=True)
    generate.add_argument("--write", type=Path, required=True)
    compact = subparsers.add_parser("compact")
    compact.add_argument("--trace", type=Path, required=True)
    compact.add_argument("--cases", type=Path, required=True)
    compact.add_argument("--decision-hash", required=True)
    compact.add_argument("--trace-evidence", type=Path, required=True)
    compact.add_argument("--freeze-manifest", type=Path, required=True)
    compact.add_argument("--write", type=Path, required=True)
    for command_parser in (
        extract, extend, extend_many, refresh, generate, compact,
    ):
        command_parser.add_argument(
            "--case-sources", type=Path,
            default=Path(decision_case_corpus.DEFAULT_MANIFEST_NAME),
        )
    args = parser.parse_args(argv)
    if args.command == "extract":
        import decision_case_replay

        cases = decision_case_replay.load_case_corpus(
            args.cases, args.case_sources,
        )
        results = [
            decision_case_replay._audit_one(
                case,
                historical=case.get("decision_hash") != args.decision_hash,
            )
            for case in cases
        ]
        evidence = extract_trace_evidence(args.trace, cases, results)
        _write_json(args.write, evidence)
        print(json.dumps({
            key: evidence[key] for key in (
                "source_trace_prefix_sha256", "scan_byte_length",
                "target_case_count",
                "found_case_count", "missing_case_count",
            )
        }, ensure_ascii=False))
        return 0 if evidence["missing_case_count"] == 0 else 1
    if args.command in {"extend", "extend-many"}:
        import decision_case_replay

        cases = decision_case_replay.load_case_corpus(
            args.cases, args.case_sources,
        )
        results = [
            decision_case_replay._audit_one(
                case,
                historical=case.get("decision_hash") != args.decision_hash,
            )
            for case in cases
        ]
        archived = load_json_object(
            args.base_evidence, "archived DecisionCase trace evidence"
        )
        if args.command == "extend-many":
            if len(args.trace) != len(args.source_trace_label):
                raise ResolutionError(
                    "trace paths and source trace labels must have equal counts"
                )
            old_source_count = (
                len(archived.get("sources", []))
                if isinstance(archived, dict) else 0
            )
            evidence = extend_compact_trace_evidence_many(
                archived,
                list(zip(args.trace, args.source_trace_label)),
                cases,
                results,
            )
            new_sources = evidence["sources"][old_source_count:]
        else:
            evidence = extend_compact_trace_evidence(
                archived,
                args.trace,
                cases,
                results,
                source_trace_label=args.source_trace_label,
            )
            extended_attempt_id = _attempt_id_from_trace_label(
                args.source_trace_label
            )
            new_sources = [
                source for source in evidence["sources"]
                if source.get("source_attempt_id") == extended_attempt_id
            ]
            if len(new_sources) != 1:
                raise ResolutionError(
                    "extended attempt trace source is not uniquely bound"
                )
        _write_json(args.write, evidence)
        print(json.dumps({
            "schema_version": evidence["schema_version"],
            "evidence_mode": evidence["evidence_mode"],
            "target_case_count": evidence["target_case_count"],
            "archived_active_record_count": evidence[
                "archived_schema2_summary"
            ]["active_record_count"],
            "new_source_count": len(new_sources),
            "new_source_record_count": sum(
                source["found_case_count"] for source in new_sources
            ),
            "new_source_scan_byte_length": sum(
                source["scan_byte_length"] for source in new_sources
            ),
            "new_source_trace_sha256": [
                source["source_trace_sha256"] for source in new_sources
            ],
            "compressed_payload_bytes": sum(
                source["payload_compressed_byte_length"]
                for source in new_sources
            ),
        }, ensure_ascii=False))
        return 0
    if args.command == "refresh":
        import decision_case_replay

        cases = decision_case_replay.load_case_corpus(
            args.cases, args.case_sources,
        )
        results = [
            decision_case_replay._audit_one(
                case,
                historical=case.get("decision_hash") != args.decision_hash,
            )
            for case in cases
        ]
        archived = load_json_object(
            args.base_evidence, "archived DecisionCase trace evidence"
        )
        evidence = refresh_compact_trace_evidence(
            archived, cases, results
        )
        _write_json(args.write, evidence)
        print(json.dumps({
            "schema_version": evidence["schema_version"],
            "evidence_mode": evidence["evidence_mode"],
            "target_case_count": evidence["target_case_count"],
            "source_count": len(evidence["sources"]),
            "source_manifest_sha256": evidence[
                "source_manifest_sha256"
            ],
        }, ensure_ascii=False))
        return 0
    if args.command == "generate":
        import decision_case_replay

        cases = decision_case_replay.load_case_corpus(
            args.cases, args.case_sources,
        )
        results = [
            decision_case_replay._audit_one(
                case,
                historical=case.get("decision_hash") != args.decision_hash,
            )
            for case in cases
        ]
        evidence = load_json_object(
            args.trace_evidence, "DecisionCase trace evidence"
        )
        fixtures = load_json_object(
            args.fixtures, "DecisionCase resolution fixtures"
        )
        document = generate_resolution_document(
            cases,
            results,
            fixture_catalog=fixtures,
            trace_evidence=evidence,
            trace_path=args.trace,
            audit_one=decision_case_replay._audit_one,
        )
        _write_json(args.write, document)
        print(json.dumps({
            "resolution_count": document["resolution_count"],
            "trace_evidence_sha256": document["trace_evidence_sha256"],
            "fixture_catalog_sha256": document["fixture_catalog_sha256"],
        }, ensure_ascii=False))
        return 0
    if args.command == "compact":
        import decision_case_replay

        cases = decision_case_replay.load_case_corpus(
            args.cases, args.case_sources,
        )
        results = [
            decision_case_replay._audit_one(
                case,
                historical=case.get("decision_hash") != args.decision_hash,
            )
            for case in cases
        ]
        evidence = load_json_object(
            args.trace_evidence, "DecisionCase trace evidence"
        )
        freeze = load_json_object(args.freeze_manifest, "freeze manifest")
        compacted = compact_trace_evidence(
            args.trace,
            evidence,
            cases,
            results,
            freeze_manifest=freeze,
            legacy_evidence_sha256=file_digest(args.trace_evidence),
            freeze_manifest_sha256=file_digest(args.freeze_manifest),
        )
        _write_json(args.write, compacted)
        print(json.dumps({
            "schema_version": compacted["schema_version"],
            "evidence_mode": compacted["evidence_mode"],
            "record_count": len(compacted["records"]),
            "embedded_bytes": sum(
                row["trace_line_byte_length"] for row in compacted["records"]
            ),
            "historical_prefix_bytes_released": compacted["scan_byte_length"],
        }, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
