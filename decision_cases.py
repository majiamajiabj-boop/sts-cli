"""Compact replay cases for high-impact autonomous decisions.

The production trace is intentionally rich and append-only, which makes it
excellent evidence but a poor regression corpus once it approaches a
gigabyte.  This module extracts one small, self-contained record for every
high-impact non-combat choice.  The cases are evidence for independent audit
and metamorphic tests; they never participate in the live choice itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


CASE_SCHEMA_VERSION = 2
LEGACY_CASE_SCHEMA_VERSIONS = {1}
SUPPORTED_CASE_SCHEMA_VERSIONS = LEGACY_CASE_SCHEMA_VERSIONS | {
    CASE_SCHEMA_VERSION
}
# The historical/root corpus is a frozen input to replay.  Live attempts must
# never append to it: a single long run could otherwise consume the global
# budget and kill the controller while it is trying to persist a supplementary
# audit case.
DECISION_CASES_MAX_BYTES = 128 * 1024 * 1024
# A long but valid attempt can approach the independently bounded 128 MiB
# authoritative trace while still producing replay candidates.  Keep the
# attempt-local case shard on the same independent budget so evidence capture
# does not become a protocol stop before the authoritative trace limit does.
ATTEMPT_DECISION_CASES_MAX_BYTES = 128 * 1024 * 1024


class DecisionCaseCorpusLimitError(RuntimeError):
    """Raised before the append-only replay corpus can grow without bound."""

V2_CANONICAL_FIELDS = (
    "case_schema_version",
    "trace_schema_version",
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "policy_version",
    "decision_hash",
    "controller_hash",
    "selection_id",
    "before_seq",
    "after_seq",
    "phase",
    "action",
    "reason",
    "goal_mode",
    "candidates",
    "producer_candidates",
    "candidate_contract",
    "candidate_join",
    "model_advice",
    "decision_context",
    "outcome_facts",
    "available_options",
    "available_commands",
    "canonical_choices",
    "selected_choice_ids",
    "final_choice_ids",
    "requested_target_id",
    "resolved_target_id",
    "authoritative_state_before",
    "authoritative_state_after",
    "decision_outcome",
    "authoritative_choice_settlement",
    "decision_surface_kind",
    "parent_choice_surface_pending",
    "resource_preparation_options",
)
HIGH_IMPACT_PHASES = {
    "BOSS_REWARD",
    "CARD_REWARD",
    "CHEST",
    "COMBAT_REWARD",
    "EVENT",
    "GRID",
    "HAND_SELECT",
    "MAP",
    "NEOW",
    "REST",
    "SAPPHIRE_KEY",
    "SHOP_ROOM",
    "SHOP_SCREEN",
}

# Repartition the producer's raw consequence object at the DecisionCase
# boundary.  The canonical choice keeps the raw production evidence, but its
# producer-authored classification is not authority.  Every raw key must be
# assigned exactly once here; unknown keys keep the case fail-closed.
_PRODUCER_EFFECT_FIELDS = {
    "hp_delta", "current_hp_delta", "max_hp_delta", "gold_delta",
    "card_changes", "relic_changes", "potion_changes", "curse",
    "probabilistic_outcomes", "probability_outcomes", "current_cost",
    "future_costs", "route", "event_id", "campfire_option",
    "selected_card", "key_changes", "operation", "deck_size_delta",
    "max_hp_gain", "post_purchase_gold", "price", "leave",
    "potion_id", "potion_slot", "bound_new_potion_id",
    "bound_purchase_potion_id",
    "bound_purchase_listing_id", "bound_purchase_choice_index",
    "bound_purchase_item_id", "bound_purchase_price",
    "bound_reward_potion_id", "mechanism_id", "neow_contract",
    "reward_kind", "drawback_kind", "parameters", "random_effects",
    "acquired_benefit",
    "original_button_index",
    # Exact visible-target identity claims.  They are checked against the raw
    # protocol target, never accepted merely because the producer supplied
    # them.
    "item_id", "relic_id", "reward_type", "card_id", "rest_option",
    "card_instance_id", "potion_instance_id", "upgrades",
    # A legacy producer claim retained as an effect so replay must explicitly
    # classify it rather than silently treating it as a scoring feature.
    "combat_delta",
}
_PRODUCER_SCORING_FIELDS_V1 = {
    "path_survival_risk", "route_summary", "score_components", "utility",
    "value", "quality", "priority", "risk", "penalty", "bonus",
    "reason", "reason_codes", "uncertainty", "strategic_value",
    # The route planner publishes this threshold beside path risk so replay
    # can verify that a shop bonus was only applied when the projected arrival
    # HP clears the survival floor.  It is scoring context, not an immediate
    # state transition.
    "shop_arrival_survival_floor",
    # Match-and-Keep state and Sensory Stone value-model inputs describe why
    # a candidate was scored; they are not assertions of an immediate state
    # transition.
    "card_gain_requires_successful_pair", "known_curse",
    "known_pair_count", "match_stage", "revealed_label", "unknown_card",
    "card_change_kind", "card_delta", "card_delta_max", "card_delta_min",
    "card_gain", "card_loss", "card_option_value_model",
    "card_reward_candidates_per_screen", "card_reward_pool",
    "emerald_route_pressure", "event_outcome_id",
    "expected_card_option_value", "hp_change_kind", "lost_card",
    "marginal_card_option_values", "optional_card_reward_count",
    "selection_optional",
}
_PRODUCER_SCORING_FIELDS_V2 = {
    "first_shop_arrival_hp",
    # Auditable decomposition of the projected value assigned to a future
    # question-mark room. It explains route utility and does not claim an
    # immediate protocol state transition.
    "question_room_value_components",
    "relic_delta", "relic_value", "potion_delta", "card_delta",
    "upgrade_delta", "upgrade_value", "best_removal_value",
    "empty_slots_filled", "replacement_choices", "replacement_option_value",
    "legal", "sozu_blocks", "hp_loss_type", "lethal", "curse_delta",
    "raw_curse_delta", "effective_curse_delta", "curse_probability",
    "expected_effective_curse_delta", "expected_omamori_charge_use",
    "omamori_charges_before", "omamori_charges_after_if_triggered",
    "expected_omamori_charges_after", "omamori_prevented_curse_delta",
    "curse_change_kind", "curse_id", "curse_trade_profile",
    "potion_loss", "potion_gain", "potion_change_kind", "lost_potion",
    "hp_gain_before_damage", "damage_amount", "upgraded_card_delta",
    "healing_locked", "rare_relic_delta", "combat_rewards",
    "future_combat_gold",
    "nominal_hp_loss", "reward_value",
}
_PRODUCER_SCORING_FIELDS = (
    _PRODUCER_SCORING_FIELDS_V1 | _PRODUCER_SCORING_FIELDS_V2
)
# This producer-authored metadata is not an effect claim and remains outside
# both consequence partitions.  Unlike an arbitrary unknown key, replay can
# independently verify its semantic status against the mechanics projection,
# so it need not make the strict identity join discard the whole surface.
_INDEPENDENTLY_AUDITABLE_UNCLASSIFIED_PRODUCER_FIELDS = {
    "uncertainty_classification",
}


def _json_object(value):
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value):
    return list(value) if isinstance(value, list) else []


def _normalized_operation(row):
    value = row.get("operation") if isinstance(row, dict) else None
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized or None


def _normalized_game_id(value):
    return "".join(
        character for character in str(value or "").casefold()
        if character.isalnum()
    )


def _independent_producer_partition(producer):
    """Partition raw producer evidence without trusting canonical labels."""

    raw = producer.get("consequences") if isinstance(producer, dict) else None
    claim = {}
    scoring = {}
    unclassified = []
    scoring_fields = (
        _PRODUCER_SCORING_FIELDS
        if isinstance(producer, dict)
        and producer.get("producer_partition_version") == 2
        else _PRODUCER_SCORING_FIELDS_V1
    )
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in _PRODUCER_EFFECT_FIELDS:
                claim[key] = value
            elif key in scoring_fields:
                scoring[key] = value
            else:
                unclassified.append(str(key))
    elif raw is not None:
        unclassified.append("<non_object_consequences>")
    return {
        "producer_candidate_raw": producer if isinstance(producer, dict) else None,
        "producer_consequence_raw": {
            "present": isinstance(raw, dict),
            "value": raw,
        },
        "producer_consequence_claim": claim,
        "producer_scoring_facts": scoring,
        "unclassified_producer_fields": sorted(set(unclassified)),
    }


def _score_evidence_shape_complete(row):
    if not isinstance(row, dict):
        return False
    rule_id = row.get("score_rule_id")
    formula = row.get("score_formula")
    inputs = row.get("score_inputs")
    components = row.get("score_components")
    return bool(
        isinstance(rule_id, str)
        and rule_id.strip()
        and rule_id.strip().casefold() != "unclassified"
        and isinstance(formula, dict)
        and formula
        and isinstance(inputs, dict)
        and inputs
        and isinstance(components, list)
        and components
        and all(
            isinstance(component, dict)
            and isinstance(component.get("name"), str)
            and component["name"].strip()
            and isinstance(component.get("input"), str)
            and component.get("input") in inputs
            and isinstance(component.get("coefficient"), (int, float))
            and not isinstance(component.get("coefficient"), bool)
            and isinstance(component.get("value"), (int, float))
            and not isinstance(component.get("value"), bool)
            for component in components
        )
    )


def _v2_value_missing(field, value, surface_kind):
    if field == "resource_preparation_options" and surface_kind != (
        "resource_preparation"
    ):
        return not isinstance(value, list)
    return value in (None, "", [], {})


def _choice_aliases(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return set()
    raw = str(value).strip().casefold()
    if not raw:
        return set()
    aliases = {raw}
    token = "".join(character for character in raw if character.isalnum())
    if token:
        aliases.add(token)
    if ":" in raw:
        parts = raw.split(":")
        suffix = parts[-1]
        if suffix and not suffix.isdigit():
            aliases.update(_choice_aliases(suffix))
        if parts[0] == "shop" and len(parts) >= 3:
            aliases.update(_choice_aliases(parts[-2]))
    if "@" in raw:
        aliases.update(_choice_aliases(raw.rsplit("@", 1)[-1]))
    return aliases


def _candidate_aliases(candidate):
    aliases = set()
    if not isinstance(candidate, dict):
        return aliases
    for key in (
        "id", "candidate_id", "choice_id", "semantic_id", "action",
        "label",
    ):
        aliases.update(_choice_aliases(candidate.get(key)))
    return aliases


def _option_aliases(option):
    aliases = set()
    if not isinstance(option, dict):
        return aliases
    for key in (
        "option_id", "choice_id", "choice_index", "semantic_id",
        "action", "label",
    ):
        aliases.update(_choice_aliases(option.get(key)))
    index = option.get("choice_index")
    if index is not None:
        aliases.update(_choice_aliases(f"event:{index}"))
        aliases.update(_choice_aliases(f"option:{index}"))
    target = _json_object(option.get("target"))
    for key in (
        "id", "label", "card_instance_id", "potion_instance_id",
        "relic_id",
    ):
        aliases.update(_choice_aliases(target.get(key)))
    for nested_key in ("card", "relic", "item", "reward"):
        nested = _json_object(target.get(nested_key))
        for key in (
            "id", "name", "label", "item_id", "card_instance_id",
            "potion_instance_id", "relic_id", "reward_type",
        ):
            aliases.update(_choice_aliases(nested.get(key)))
    if all(target.get(key) is not None for key in ("symbol", "x", "y")):
        aliases.update(_choice_aliases(
            f"{target['symbol']}@{target['x']},{target['y']}"
        ))
    return aliases


def _stable_candidate_aliases(candidate):
    """Return producer-owned semantic identities, excluding display text."""

    aliases = set()
    if not isinstance(candidate, dict):
        return aliases
    for key in ("choice_id", "candidate_id", "id", "semantic_id"):
        aliases.update(_choice_aliases(candidate.get(key)))
    index = candidate.get("choice_index")
    if isinstance(index, int) and not isinstance(index, bool):
        aliases.update(_choice_aliases(index))
        aliases.update(_choice_aliases(f"event:{index}"))
        aliases.update(_choice_aliases(f"match:{index}"))
    facts = _json_object(candidate.get("facts"))
    for key in (
        "choice_id", "card_instance_id", "potion_instance_id",
        "relic_id", "item_id",
    ):
        aliases.update(_choice_aliases(facts.get(key)))
    return aliases


def _stable_option_aliases(option):
    """Recompute protocol semantics without using localized display labels."""

    aliases = set()
    if not isinstance(option, dict):
        return aliases
    for key in ("choice_id", "option_id", "semantic_id", "action"):
        aliases.update(_choice_aliases(option.get(key)))
    index = option.get("choice_index")
    if isinstance(index, int) and not isinstance(index, bool):
        aliases.update(_choice_aliases(index))
        aliases.update(_choice_aliases(f"event:{index}"))
        aliases.update(_choice_aliases(f"match:{index}"))
    target = _json_object(option.get("target"))
    kind = str(target.get("kind") or "").strip().casefold()
    for key in (
        "id", "card_instance_id", "potion_instance_id", "relic_id",
    ):
        aliases.update(_choice_aliases(target.get(key)))
    if kind:
        aliases.update(_choice_aliases(kind))
    if all(target.get(key) is not None for key in ("x", "y")):
        aliases.update(_choice_aliases(
            f"map:{target['x']},{target['y']}"
        ))
        if target.get("symbol") is not None:
            aliases.update(_choice_aliases(
                f"{target['symbol']}@{target['x']},{target['y']}"
            ))
    if kind == "event_option" and isinstance(index, int):
        aliases.update(_choice_aliases(f"event:{index}"))
        event_id = str(target.get("event_id") or "").casefold()
        if "match" in event_id:
            aliases.update(_choice_aliases(f"match:{index}"))
    for nested_key in ("card", "relic", "potion", "item", "reward"):
        nested = _json_object(target.get(nested_key))
        for key in (
            "id", "item_id", "card_instance_id", "potion_instance_id",
            "relic_id", "reward_type",
        ):
            value = nested.get(key)
            aliases.update(_choice_aliases(value))
            if value is not None and kind in {"card", "relic", "potion"}:
                aliases.update(_choice_aliases(f"{kind}:{value}"))
        for child_key in ("card", "relic", "potion", "item", "link"):
            child = _json_object(nested.get(child_key))
            for key in (
                "id", "item_id", "card_instance_id",
                "potion_instance_id", "relic_id",
            ):
                aliases.update(_choice_aliases(child.get(key)))
    if kind == "purge":
        aliases.update(_choice_aliases("purge"))
    return aliases


def _visible_candidate_coverage(candidates, options):
    """Return a conservative one-to-one binding of candidates to options."""

    candidate_aliases = [
        _candidate_aliases(candidate) for candidate in candidates
    ]
    option_matches = [
        [
            index for index, aliases in enumerate(candidate_aliases)
            if aliases & _option_aliases(option)
        ]
        for option in options
    ]
    candidate_to_option = {}

    def bind(option_index, seen):
        for candidate_index in option_matches[option_index]:
            if candidate_index in seen:
                continue
            seen.add(candidate_index)
            previous = candidate_to_option.get(candidate_index)
            if previous is None or bind(previous, seen):
                candidate_to_option[candidate_index] = option_index
                return True
        return False

    matched = {
        option_index
        for option_index in range(len(options))
        if bind(option_index, set())
    }
    return matched


def _validated_candidate_contract(decision, available_options):
    """Downgrade producer claims that the standalone case cannot prove."""

    contract = _json_object(decision.get("candidate_contract"))
    candidates = _json_list(decision.get("candidates"))
    options = _json_list(available_options)
    if not options:
        return contract
    matched = _visible_candidate_coverage(candidates, options)
    option_aliases = [_option_aliases(option) for option in options]
    unmatched_candidates = [
        index
        for index, candidate in enumerate(candidates)
        if not any(
            _candidate_aliases(candidate) & aliases
            for aliases in option_aliases
        )
    ]
    identities = [
        str(
            candidate.get(
                "choice_id",
                candidate.get("id", candidate.get("candidate_id")),
            )
        )
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    duplicate_identities = sorted({
        identity for identity in identities
        if identities.count(identity) > 1
    })
    complete = bool(
        len(matched) == len(options)
        and not unmatched_candidates
        and len(candidates) == len(options)
        and not duplicate_identities
    )
    result = dict(contract)
    result.update({
        "case_visible_option_coverage_complete": complete,
        "case_visible_option_count": len(options),
        "case_bound_visible_option_count": len(matched),
        "case_candidate_count": len(candidates),
        "case_unmatched_candidate_count": len(unmatched_candidates),
        "case_duplicate_candidate_ids": duplicate_identities,
    })
    if not complete:
        if result.get("strategy_quality_auditable") is True:
            result["producer_strategy_quality_auditable"] = True
        result["strategy_quality_auditable"] = False
        result["case_validation"] = (
            "visible_candidate_coverage_incomplete"
        )
        result["unmatched_visible_option_ids"] = [
            option.get(
                "option_id", option.get("choice_id", option.get("choice_index"))
            )
            for index, option in enumerate(options)
            if index not in matched and isinstance(option, dict)
        ]
        result["unmatched_candidate_ids"] = [
            candidates[index].get(
                "choice_id",
                candidates[index].get(
                    "id", candidates[index].get("candidate_id")
                ),
            )
            for index in unmatched_candidates
            if isinstance(candidates[index], dict)
        ]
    return result


def _strict_canonical_candidate_projection(decision, canonical_choices):
    """Join every raw producer candidate to one canonical protocol choice.

    Canonical choices are protocol-derived, but they are not permission to
    invent a missing producer candidate.  Projection is available only after
    a strict two-way unique identity join; otherwise the raw evidence and the
    exact join failure remain in the case and strategy audit is fail-closed.
    """

    producer_candidates = _json_list(decision.get("candidates"))
    choices = _json_list(canonical_choices)
    choice_matches = []
    candidate_matches = [[] for _candidate in producer_candidates]
    for choice_index, choice in enumerate(choices):
        choice_key = (
            choice.get("choice_index"),
            choice.get("action"),
            _normalized_operation(choice),
        )
        matches = [
            candidate_index
            for candidate_index, candidate in enumerate(producer_candidates)
            if (
                candidate.get("choice_index"),
                candidate.get("action"),
                _normalized_operation(candidate),
            ) == choice_key
        ]
        choice_matches.append(matches)
        for candidate_index in matches:
            candidate_matches[candidate_index].append(choice_index)

    missing_choices = [
        index for index, matches in enumerate(choice_matches) if not matches
    ]
    ambiguous_choices = [
        index for index, matches in enumerate(choice_matches)
        if len(matches) > 1
    ]
    unmatched_candidates = [
        index for index, matches in enumerate(candidate_matches) if not matches
    ]
    ambiguous_candidates = [
        index for index, matches in enumerate(candidate_matches)
        if len(matches) > 1
    ]
    semantic_mismatches = []
    consequence_target_mismatches = []
    producer_raw_binding_mismatches = []
    unclassified_producer_choice_indexes = []
    operation_missing_choices = [
        index for index, choice in enumerate(choices)
        if not isinstance(choice, dict) or "operation" not in choice
    ]
    operation_missing_candidates = [
        index for index, candidate in enumerate(producer_candidates)
        if not isinstance(candidate, dict) or "operation" not in candidate
    ]
    for choice_index, matches in enumerate(choice_matches):
        if len(matches) != 1:
            continue
        producer = producer_candidates[matches[0]]
        choice = choices[choice_index]
        if producer.get("semantic_id") != choice.get("semantic_id"):
            semantic_mismatches.append(choice_index)
        if not _producer_consequence_target_matches(choice, producer):
            consequence_target_mismatches.append(choice_index)
        partition = _independent_producer_partition(producer)
        if (
            choice.get("producer_candidate_raw") != producer
            or choice.get("producer_consequence_raw")
            != partition["producer_consequence_raw"]
        ):
            producer_raw_binding_mismatches.append(choice_index)
        unsupported_unclassified = set(
            partition["unclassified_producer_fields"]
        ) - _INDEPENDENTLY_AUDITABLE_UNCLASSIFIED_PRODUCER_FIELDS
        if unsupported_unclassified:
            unclassified_producer_choice_indexes.append(choice_index)
    complete = bool(
        choices
        and producer_candidates
        and len(choices) == len(producer_candidates)
        and not missing_choices
        and not ambiguous_choices
        and not unmatched_candidates
        and not ambiguous_candidates
        and not semantic_mismatches
        and not consequence_target_mismatches
        and not producer_raw_binding_mismatches
        and not unclassified_producer_choice_indexes
        and not operation_missing_choices
        and not operation_missing_candidates
        and all(
            isinstance(choice, dict)
            and choice.get("candidate_binding") == "unique"
            for choice in choices
        )
    )
    evidence = {
        "join_schema_version": 2,
        "status": "clear" if complete else "incomplete",
        "canonical_choice_count": len(choices),
        "producer_candidate_count": len(producer_candidates),
        "missing_choice_indexes": missing_choices,
        "ambiguous_choice_indexes": ambiguous_choices,
        "unmatched_candidate_indexes": unmatched_candidates,
        "ambiguous_candidate_indexes": ambiguous_candidates,
        "semantic_mismatch_choice_indexes": semantic_mismatches,
        "consequence_target_mismatch_choice_indexes": (
            consequence_target_mismatches
        ),
        "producer_raw_binding_mismatch_choice_indexes": (
            producer_raw_binding_mismatches
        ),
        "unclassified_producer_choice_indexes": (
            unclassified_producer_choice_indexes
        ),
        "operation_missing_choice_indexes": operation_missing_choices,
        "operation_missing_candidate_indexes": operation_missing_candidates,
        "choice_candidate_matches": [list(matches) for matches in choice_matches],
        "candidate_choice_matches": [list(matches) for matches in candidate_matches],
    }
    if not complete:
        return [], producer_candidates, evidence

    projected = []
    for choice_index, choice in enumerate(choices):
        producer_index = choice_matches[choice_index][0]
        producer = producer_candidates[producer_index]
        producer_partition = _independent_producer_partition(producer)
        score = producer.get("score", producer.get("local_score"))
        projected.append({
            "choice_id": choice.get("choice_id"),
            "choice_index": choice.get("choice_index"),
            "action": choice.get("action"),
            "operation": _normalized_operation(choice),
            "legal": choice.get("legal"),
            "visible": choice.get("visible"),
            "selection_eligible": choice.get("selection_eligible"),
            "veto_reason": choice.get("veto_reason"),
            "label": choice.get("label"),
            "raw_text": choice.get("raw_text"),
            "semantic_id": choice.get("semantic_id"),
            "target": _json_object(choice.get("target")),
            "local_score": score,
            "score": score,
            "local_reason": choice.get("local_reason"),
            "reason_codes": _json_list(choice.get("reason_codes")),
            "score_rule_id": choice.get("score_rule_id"),
            "score_formula": _json_object(choice.get("score_formula")),
            "score_inputs": _json_object(choice.get("score_inputs")),
            "score_components": _json_list(choice.get("score_components")),
            "model_score": choice.get("model_score"),
            "model_confidence": choice.get("model_confidence"),
            "model_evidence_status": choice.get("model_evidence_status"),
            "model_evidence_reason": choice.get("model_evidence_reason"),
            "final_source": choice.get("final_source"),
            "override": _json_object(choice.get("override")),
            "consequences": _json_object(choice.get("consequences")),
            "probability_outcomes": _json_list(
                choice.get("probability_outcomes")
            ),
            "uncertainty": choice.get("uncertainty"),
            **producer_partition,
            "producer_candidate_index": producer_index,
            "producer_candidate_id": (
                producer.get(
                    "choice_id",
                    producer.get("candidate_id", producer.get("id")),
                )
            ),
        })
    return projected, producer_candidates, evidence


def _canonical_goop_event_binding(target):
    """Return the exact Goop mechanism bound by a canonical bridge target."""

    contract = target.get("event_contract")
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "original_button_index", "option_kind", "parameters",
    }
    original_index = target.get("original_button_index")
    if (
        not isinstance(contract, dict)
        or set(contract) != required
        or contract.get("contract_version") != 1
        or contract.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or contract.get("event_id") != "World of Goop"
        or contract.get("event_class") != (
            "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
        )
        or type(original_index) is not int
        or contract.get("original_button_index") != original_index
        or not isinstance(contract.get("parameters"), dict)
    ):
        return None
    option_kind = contract.get("option_kind")
    parameters = contract["parameters"]
    expected_fields = {
        (0, "GATHER"): {"gold_gain", "hp_damage"},
        (1, "LEAVE"): {"gold_loss"},
        (0, "CONTINUE"): set(),
    }.get((original_index, option_kind))
    if (
        expected_fields is None
        or set(parameters) != expected_fields
        or any(
            type(item) is not int or item < 0
            for item in parameters.values()
        )
        or (
            option_kind == "GATHER"
            and parameters != {"gold_gain": 75, "hp_damage": 11}
        )
    ):
        return None
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:20]
    mechanism_id = f"event-mechanism:{digest}"
    if target.get("mechanism_id") != mechanism_id:
        return None
    return mechanism_id


def _canonical_staged_event_binding(target):
    """Bind staged event producer evidence to the exact bridge contract."""

    contract = target.get("event_contract")
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "event_stage", "original_button_index", "option_kind",
        "instance_parameters", "parameters",
    }
    original_index = target.get("original_button_index")
    if (
        not isinstance(contract, dict)
        or set(contract) != required
        or contract.get("contract_version") != 1
        or contract.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or type(contract.get("event_stage")) is not str
        or type(contract.get("option_kind")) is not str
        or not isinstance(contract.get("instance_parameters"), dict)
        or not isinstance(contract.get("parameters"), dict)
        or type(original_index) is not int
        or contract.get("original_button_index") != original_index
        or _normalized_game_id(contract.get("event_id"))
        != _normalized_game_id(target.get("event_id"))
    ):
        return None
    identities = {
        "thecleric": "com.megacrit.cardcrawl.events.exordium.Cleric",
        "designer": "com.megacrit.cardcrawl.events.shrines.Designer",
        "cursedtome": "com.megacrit.cardcrawl.events.city.CursedTome",
    }
    event_id = _normalized_game_id(contract.get("event_id"))
    if contract.get("event_class") != identities.get(event_id):
        return None
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:20]
    mechanism_id = f"event-mechanism:{digest}"
    return (
        mechanism_id
        if target.get("mechanism_id") == mechanism_id else None
    )


def _producer_consequence_target_matches(choice, producer):
    """Verify target identity without treating producer effects as truth."""

    if not isinstance(choice, dict) or not isinstance(producer, dict):
        return False
    consequence = producer.get("consequences")
    target = choice.get("target")
    if not isinstance(consequence, dict) or not consequence:
        return False
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "").casefold()
    if kind == "map_node":
        route = consequence.get("route")
        return isinstance(route, dict) and all(
            route.get(field) == target.get(field)
            for field in ("symbol", "x", "y")
        )
    if kind in {"card", "relic", "potion"} and isinstance(
        target.get("item"), dict
    ):
        item = target["item"]
        change_field = {
            "card": "card_changes",
            "relic": "relic_changes",
            "potion": "potion_changes",
        }[kind]
        changes = consequence.get(change_field)
        gains = changes.get("gain") if isinstance(changes, dict) else None
        return bool(
            isinstance(gains, list) and len(gains) == 1
            and gains[0] == item
        )
    if kind == "card":
        card = target.get("card")
        card = card if isinstance(card, dict) else {}
        wanted = target.get("card_instance_id") or card.get(
            "card_instance_id"
        )
        selected = consequence.get("selected_card")
        if isinstance(selected, dict):
            return bool(wanted and selected == card)
        changes = consequence.get("card_changes")
        if not isinstance(changes, dict):
            return False
        exact_targets = []
        for field in ("gain", "remove", "upgrade", "transform"):
            values = changes.get(field)
            if isinstance(values, list):
                exact_targets.extend(values)
        matches = [
            value for value in exact_targets
            if isinstance(value, dict)
            and (value.get("card_instance_id") or value.get("uuid")) == wanted
            and (
                not card.get("id")
                or not value.get("id")
                or value.get("id") == card.get("id")
            )
        ]
        return bool(wanted and len(matches) == 1)
    if kind == "relic":
        relic = target.get("relic")
        wanted = relic.get("id") if isinstance(relic, dict) else None
        return wanted is not None and str(consequence.get("relic_id")) == str(wanted)
    if kind == "reward":
        reward = target.get("reward")
        wanted = str(
            reward.get("reward_type") if isinstance(reward, dict) else ""
        ).casefold()
        if not (
            wanted
            and str(consequence.get("reward_type") or "").casefold()
            == wanted
        ):
            return False
        # A linked relic is a distinct visible reward listing.  Reward type
        # alone cannot bind two RELIC rows, so retain the exact protocol relic
        # identity in the producer/canonical join.
        if wanted == "relic" and isinstance(reward, dict):
            relic = reward.get("relic")
            wanted_relic = (
                relic.get("id") if isinstance(relic, dict) else None
            )
            return bool(wanted_relic) and str(
                consequence.get("relic_id") or ""
            ) == str(wanted_relic)
        return True
    if kind == "sapphire_key":
        reward = target.get("reward")
        return bool(
            isinstance(reward, dict)
            and str(reward.get("reward_type") or "").casefold()
            == "sapphire_key"
            and str(consequence.get("reward_type") or "").casefold()
            == "sapphire_key"
            and consequence.get("operation") == "gain_sapphire_key"
        )
    if kind == "purge":
        return consequence.get("operation") == "open_card_purge_grid"
    if kind == "protocol_action":
        return consequence.get("operation") == target.get("action")
    if kind == "potion_resource":
        operation = _normalized_operation(choice)
        claimed_operation = str(
            consequence.get("operation") or ""
        ).strip().casefold()
        return bool(
            operation in {"use", "discard"}
            and claimed_operation.startswith(operation + "_held_potion_for_")
            and consequence.get("potion_instance_id")
            == target.get("potion_instance_id")
            and consequence.get("potion_slot") == target.get("slot")
        )
    if kind == "rest":
        option = str(target.get("rest_option") or "").upper()
        expected = {
            "REST": "campfire_rest", "SMITH": "campfire_smith",
            "TOKE": "campfire_toke", "RECALL": "campfire_recall",
        }.get(option)
        return bool(
            expected
            and str(consequence.get("rest_option") or "").upper() == option
            and consequence.get("operation") == expected
        )
    if kind == "chest":
        return consequence.get("operation") == "open_chest"
    if kind == "shop_room":
        return consequence.get("operation") == "enter_shop"
    # Event option identity is independently fixed by the exact typed key and
    # semantic id; its visible target contains no separate stable effect id.
    if kind == "event_option":
        contract = target.get("neow_contract")
        if isinstance(contract, dict):
            expected = (
                "neow_dialog_advance"
                if contract.get("contract_kind") == "NEOW_DIALOG_ADVANCE"
                else "neow_reward"
            )
            return bool(
                consequence.get("mechanism_id") == target.get("mechanism_id")
                and consequence.get("operation") == expected
            )
        mechanism_id = str(consequence.get("mechanism_id") or "")
        typed_event_mechanisms = {
            "themausoleum": "base_game_the_mausoleum_v1",
        }
        target_event_id = _normalized_game_id(target.get("event_id"))
        if target_event_id == "worldofgoop":
            goop_mechanism = _canonical_goop_event_binding(target)
            return bool(
                goop_mechanism
                and _normalized_game_id(consequence.get("event_id"))
                == target_event_id
                and consequence.get("mechanism_id") == goop_mechanism
                and consequence.get("original_button_index")
                == target.get("original_button_index")
            )
        if target_event_id in {"thecleric", "designer", "cursedtome"}:
            staged_mechanism = _canonical_staged_event_binding(target)
            return bool(
                staged_mechanism
                and _normalized_game_id(consequence.get("event_id"))
                == target_event_id
                and consequence.get("mechanism_id") == staged_mechanism
                and consequence.get("original_button_index")
                == target.get("original_button_index")
            )
        if (
            target_event_id in typed_event_mechanisms
            or mechanism_id in set(typed_event_mechanisms.values())
        ):
            return bool(
                _normalized_game_id(consequence.get("event_id"))
                == target_event_id
                and mechanism_id == typed_event_mechanisms.get(
                    target_event_id
                )
            )
        return True
    if kind in {"map_boss", "bowl"}:
        return True
    return False


def _compact_model_advice(value):
    advice = _json_object(value)
    if not advice:
        return None
    result = {
        key: advice.get(key)
        for key in (
            "status",
            "advice_id",
            "decision_type",
            "rule_choice_id",
            "model_choice_id",
            "final_choice_ids",
            "confidence",
            "applied",
            "reason",
            "consultation_outcome",
            "semantic_choice_change",
            "protocol_confirmation",
            "local_confirmation",
            "override_gate_passed",
            "gate_passed",
            "error_class",
            "reason_codes",
            "rationale",
            "latency_ms",
            "local_cache_hit",
            "transport",
        )
        if advice.get(key) is not None
    }
    fusion = _json_object(advice.get("fusion"))
    if fusion:
        result["fusion"] = {
            key: fusion.get(key)
            for key in (
                "model_margin",
                "model_vs_rule_gap",
                "model_span",
                "local_regret",
                "candidates",
            )
            if fusion.get(key) is not None
        }
    return result


def is_replay_candidate(record):
    """Return whether a confirmed trace record merits a replay case."""

    if not isinstance(record, dict) or record.get("record_type") != "decision":
        return False
    if str(record.get("phase") or "") not in HIGH_IMPACT_PHASES:
        return False
    action = record.get("action")
    if action == "potion":
        return record.get("decision_surface_kind") == "resource_preparation"
    return action in {
        "boss_reward",
        "buy",
        "cancel",
        "choose",
        "return",
        "skip",
        "leave",
        "rest",
        "reward",
    }


def build_decision_case(record):
    """Build a bounded case without copying the full macro prompt snapshot."""

    if not is_replay_candidate(record):
        return None
    decision = _json_object(record.get("decision"))
    chosen_option = _json_object(record.get("chosen_option_before"))
    chosen_target = _json_object(chosen_option.get("target"))
    model_advice = _compact_model_advice(decision.get("model_advice"))
    available_options = _json_list(record.get("available_options_before"))
    canonical_choices = _json_list(record.get("legal_choices_before"))
    selected_canonical = [
        choice for choice in canonical_choices
        if isinstance(choice, dict) and choice.get("selected") is True
    ]
    if not isinstance(model_advice, dict) or not model_advice:
        model_advice = {
            "status": "not_consulted",
            "applied": False,
            "confidence": None,
            "reason": "producer_recorded_no_remote_macro_advice",
        }
    producer_model_final_ids = _json_list(
        model_advice.get("final_choice_ids")
    )
    model_advice["producer_final_choice_ids"] = producer_model_final_ids
    model_advice["final_choice_ids"] = _json_list(
        record.get("final_choice_ids")
    )
    model_advice["final_source"] = (
        selected_canonical[0].get("final_source")
        if len(selected_canonical) == 1 else "mixed_or_unbound"
    )
    model_advice["override"] = (
        _json_object(selected_canonical[0].get("override"))
        if len(selected_canonical) == 1 else {}
    )
    validation_surface = canonical_choices or available_options
    canonical_candidates, producer_candidates, candidate_join = (
        _strict_canonical_candidate_projection(decision, canonical_choices)
    )
    candidate_contract = _validated_candidate_contract(
        decision, validation_surface
    )
    producer_auditable = _json_object(
        decision.get("candidate_contract")
    ).get("strategy_quality_auditable") is True
    independent_candidate_evidence_complete = bool(
        producer_candidates
        and all(
            isinstance(candidate, dict)
            and isinstance(
                candidate.get("score", candidate.get("local_score")),
                (int, float),
            )
            and not isinstance(
                candidate.get("score", candidate.get("local_score")), bool
            )
            and isinstance(candidate.get("consequences"), dict)
            and bool(candidate.get("consequences"))
            and _score_evidence_shape_complete(candidate)
            for candidate in producer_candidates
        )
    )
    candidate_contract.update({
        "producer_strategy_quality_auditable": producer_auditable,
        "strict_identity_join_complete": candidate_join["status"] == "clear",
        "independent_candidate_evidence_complete": (
            independent_candidate_evidence_complete
        ),
        "strategy_quality_auditable": bool(
            candidate_join["status"] == "clear"
            and independent_candidate_evidence_complete
        ),
    })
    if candidate_join["status"] != "clear":
        candidate_contract["case_validation"] = (
            "strict_canonical_candidate_join_incomplete"
        )
    case = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "trace_schema_version": record.get("trace_schema_version"),
        "time": record.get("time"),
        "attempt_id": record.get("attempt_id"),
        "run_id": record.get("run_id"),
        "seed": record.get("seed"),
        "character": record.get("character"),
        "ascension_level": record.get("ascension_level"),
        "run_type": record.get("run_type"),
        "policy_version": record.get("policy_version"),
        "decision_hash": record.get("decision_hash"),
        "controller_hash": record.get("controller_hash"),
        "selection_id": record.get("selection_id"),
        "selection_digest": record.get("selection_digest"),
        "before_seq": record.get("before_seq"),
        "after_seq": record.get("after_seq"),
        "phase": record.get("phase"),
        "action": record.get("action"),
        "act": record.get("act"),
        "floor": record.get("floor"),
        "hp_before": record.get("hp_before"),
        "reason": decision.get("reason"),
        "resource_parent_listing": (
            {
                "listing_id": decision.get("bound_purchase_listing_id"),
                "choice_index": decision.get("bound_purchase_choice_index"),
                "item_id": decision.get("bound_purchase_item_id"),
                "price": decision.get("bound_purchase_price"),
            }
            if decision.get("reason")
            == "shop_potion_replacement_resource_preparation"
            else {}
        ),
        "goal_mode": decision.get("goal_mode"),
        "event_id": decision.get("event_id") or chosen_target.get("event_id"),
        "chosen": {
            "requested_target_id": record.get("requested_target_id"),
            "resolved_target_id": record.get("resolved_target_id"),
            "choice_index": chosen_option.get("choice_index"),
            "label": chosen_option.get("label"),
            "semantic_id": (
                decision.get("chosen_id")
                or decision.get("chosen")
                or decision.get("chosen_index")
            ),
        },
        "candidates": canonical_candidates,
        "producer_candidates": producer_candidates,
        "candidate_contract": candidate_contract,
        "candidate_join": candidate_join,
        "outcome_facts": (
            _json_object(decision.get("outcome_facts"))
            or _json_object(record.get("decision_outcome"))
        ),
        "available_options": available_options,
        "available_commands": _json_list(
            record.get("available_commands_before")
        ),
        "canonical_choices": canonical_choices,
        "decision_context": _json_object(record.get("decision_context")),
        "decision_outcome": _json_object(record.get("decision_outcome")),
        "selected_choice_ids": _json_list(
            record.get("selected_choice_ids")
        ),
        "final_choice_ids": _json_list(record.get("final_choice_ids")),
        "requested_target_id": record.get("requested_target_id"),
        "resolved_target_id": record.get("resolved_target_id"),
        "authoritative_state_before": _json_object(
            record.get("authoritative_state_before")
        ),
        "authoritative_state_after": _json_object(
            record.get("authoritative_state_after")
        ),
        "authoritative_choice_settlement": _json_object(
            record.get("authoritative_choice_settlement")
        ),
        "decision_surface_kind": (
            record.get("decision_surface_kind") or "strategic_choice"
        ),
        "parent_choice_surface_pending": bool(
            record.get("parent_choice_surface_pending", False)
        ),
        "resource_preparation_options": _json_list(
            record.get("resource_preparation_options_before")
        ),
        "model_advice": model_advice,
    }
    # Drop empty optional fields so the replay corpus stays substantially
    # smaller than the authoritative trace.
    compact = {
        key: value
        for key, value in case.items()
        if key in V2_CANONICAL_FIELDS or value not in (None, {}, [])
    }
    compact["v2_contract"] = {
        "required_fields": list(V2_CANONICAL_FIELDS),
        "present_fields": [
            field for field in V2_CANONICAL_FIELDS if field in compact
        ],
        "missing_value_fields": [
            field for field in V2_CANONICAL_FIELDS
            if _v2_value_missing(
                field,
                compact.get(field),
                compact.get("decision_surface_kind"),
            )
        ],
    }
    return compact


def append_decision_case(
    path, record, *, max_bytes=None
):
    """Append one confirmed case; return False for non-candidate records."""

    if max_bytes is None:
        max_bytes = DECISION_CASES_MAX_BYTES
    case = build_decision_case(record)
    if case is None:
        return False
    path = Path(path)
    encoded = (
        json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    current_size = path.stat().st_size if path.exists() else 0
    if current_size + len(encoded) > max_bytes:
        raise DecisionCaseCorpusLimitError(
            "DecisionCase corpus reached its fail-closed size limit"
        )
    with path.open("ab") as handle:
        handle.write(encoded)
    return True


def load_cases(path, *, attempt_id=None, decision_hash=None):
    """Load valid cases with optional cohort filters, skipping bad lines."""

    path = Path(path)
    if not path.exists():
        return []
    cases = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(case, dict):
                continue
            if case.get("case_schema_version") not in (
                SUPPORTED_CASE_SCHEMA_VERSIONS
            ):
                continue
            if attempt_id is not None and case.get("attempt_id") != attempt_id:
                continue
            if decision_hash is not None and case.get("decision_hash") != decision_hash:
                continue
            cases.append(case)
    return cases


def export_trace_cases(
    trace_path,
    output_path,
    *,
    attempt_id=None,
    decision_hash=None,
    append=False,
):
    """Extract replay cases from an existing authoritative JSONL trace."""

    trace_path = Path(trace_path)
    output_path = Path(output_path)
    mode = "a" if append else "w"
    exported = 0
    with trace_path.open("r", encoding="utf-8") as source, output_path.open(
        mode, encoding="utf-8"
    ) as destination:
        for line in source:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if attempt_id is not None and record.get("attempt_id") != attempt_id:
                continue
            if decision_hash is not None and record.get("decision_hash") != decision_hash:
                continue
            case = build_decision_case(record)
            if case is None:
                continue
            destination.write(
                json.dumps(case, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            exported += 1
    return exported


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract compact high-impact decision replay cases"
    )
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument("--decision-hash")
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)
    exported = export_trace_cases(
        args.trace,
        args.output,
        attempt_id=args.attempt_id,
        decision_hash=args.decision_hash,
        append=args.append,
    )
    print(json.dumps({"exported_cases": exported}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
