"""Independent, order-invariant validation for persisted DecisionCases.

The replay deliberately does not import the production agent or ranker.  A
case is evidence, not an oracle: producer scores, selected flags, and blobs
labelled ``independent_blind_review`` are claims that must be checked from the
persisted semantic surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path

import decision_case_corpus
import independent_oracle as _independent_oracle


REQUIRED_FIXTURE_PHASES = {
    "BOSS_REWARD",
    "CARD_REWARD",
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

_PERMUTATION_CLEAR_AUTHORITY = "production_container_permutation_v1"
_PERMUTATION_CLEAR_REASON = (
    "container_has_at_least_two_items_and_reorder_preserved_semantic_selection"
)
_PERMUTATION_NOT_APPLICABLE_AUTHORITY = (
    "production_container_cardinality_v1"
)
_PERMUTATION_NOT_APPLICABLE_REASON = (
    "container_has_fewer_than_two_items"
)

HISTORY_COVERAGE_VERSION = 2
_SEMANTIC_FIELDS = ("choice_index", "action", "semantic_id")
_V2_SEMANTIC_FIELDS = (
    "choice_index", "action", "operation", "semantic_id"
)
_CANONICAL_CHOICE_FIELDS = (
    "choice_schema_version", "choice_id", "choice_index", "action", "operation",
    "legal", "visible", "selection_eligible", "label", "raw_text",
    "semantic_id", "target", "consequences", "probability_outcomes",
    "local_score", "local_reason", "reason_codes", "model_score",
    "model_confidence", "final_source",
    "model_evidence_status", "model_evidence_reason", "override",
    "uncertainty", "selected", "candidate_ids", "candidate_binding",
    "veto_reason", "score_rule_id", "score_formula", "score_inputs",
    "score_components", "producer_candidate_raw",
    "producer_consequence_raw", "producer_consequence_claim",
    "producer_scoring_facts", "unclassified_producer_fields",
)
_LEGACY_CANONICAL_CHOICE_FIELDS = (
    "choice_schema_version", "choice_id", "choice_index", "action",
    "legal", "visible", "selection_eligible", "label", "raw_text",
    "semantic_id", "target", "consequences", "probability_outcomes",
    "local_score", "model_score", "model_confidence", "final_source",
    "override", "uncertainty", "selected",
)
_V2_REQUIRED_FIELDS = (
    "case_schema_version", "trace_schema_version", "attempt_id", "run_id",
    "seed", "character", "ascension_level", "run_type",
    "policy_version", "decision_hash", "controller_hash", "selection_id",
    "before_seq", "after_seq", "phase", "action", "reason", "goal_mode",
    "candidates", "producer_candidates", "candidate_contract",
    "candidate_join", "model_advice", "decision_context", "outcome_facts",
    "available_options", "available_commands", "canonical_choices",
    "selected_choice_ids", "final_choice_ids", "requested_target_id",
    "resolved_target_id", "authoritative_state_before",
    "authoritative_state_after", "decision_outcome",
    "authoritative_choice_settlement", "decision_surface_kind",
    "parent_choice_surface_pending", "resource_preparation_options",
)
_STRUCTURED_CONSEQUENCE_FIELDS = (
    "schema_version", "hp_delta", "max_hp_delta", "gold_delta",
    "card_changes", "relic_changes", "potion_changes", "curse",
    "probabilistic_outcomes", "current_cost", "future_costs",
    "raw_effect_text", "uncertainty",
)
_RESOURCE_FIELDS = ("hp_delta", "max_hp_delta", "gold_delta")
_COST_FIELDS = ("gold", "hp", "max_hp")
_NONCHOICE_ACTIONS = {"proceed"}

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
    "item_id", "relic_id", "reward_type", "card_id", "potion_id",
    "card_instance_id", "potion_instance_id", "upgrades", "rest_option",
    "combat_delta", "acquired_benefit",
}
_PRODUCER_SCORING_FIELDS_V1 = {
    "path_survival_risk", "route_summary", "score_components", "utility",
    "value", "quality", "priority", "risk", "penalty", "bonus",
    "reason", "reason_codes", "uncertainty", "strategic_value",
    # Route-scoring context emitted with high-gold shop lookahead.  This is a
    # threshold used to qualify the bonus, not an immediate state effect.
    "shop_arrival_survival_floor",
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
    # Route-scoring evidence mirrored from the DecisionCase builder. Keep it
    # in the scoring partition so replay still checks the canonical
    # consequence projection without misclassifying this as a state effect.
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
    "nominal_hp_loss",
}
_PRODUCER_SCORING_FIELDS = (
    _PRODUCER_SCORING_FIELDS_V1 | _PRODUCER_SCORING_FIELDS_V2
)
_INDEPENDENTLY_AUDITABLE_UNCLASSIFIED_PRODUCER_FIELDS = {
    "uncertainty_classification",
}
_PRODUCER_TARGET_IDENTITY_FIELDS = {
    "item_id", "relic_id", "reward_type", "card_id", "potion_id",
    "card_instance_id", "potion_instance_id", "upgrades",
    "rest_option",
    "bound_new_potion_id", "bound_purchase_potion_id",
    "bound_purchase_listing_id", "bound_purchase_choice_index",
    "bound_purchase_item_id", "bound_purchase_price",
    "bound_reward_potion_id",
}


class ReplayError(ValueError):
    pass


def load_cases(path):
    path = Path(path)
    if not path.exists():
        return []
    cases = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"case line {line_number} is malformed") from exc
            if not isinstance(value, dict):
                raise ReplayError(f"case line {line_number} is not an object")
            cases.append(value)
    return cases


def _case_corpus_digest(cases):
    """Digest the canonical JSON corpus, independent of JSONL formatting."""

    return decision_case_corpus.case_corpus_digest(cases)


def load_case_corpus(path, source_manifest_path, *, with_evidence=False):
    try:
        return decision_case_corpus.load_case_corpus(
            path, source_manifest_path, with_evidence=with_evidence,
        )
    except decision_case_corpus.CaseCorpusError as exc:
        raise ReplayError(str(exc)) from exc


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ReplayError("DecisionCase artifact is unreadable") from exc
    return digest.hexdigest()


def _load_case_snapshot(path, count, expected_digest=None):
    """Load the frozen prefix of an append-only JSONL case corpus.

    A live game appends new decision rows after a freeze.  Revalidating the
    frozen report against those later rows would make a completed six-run
    cohort impossible to continue and would repeatedly rescan unrelated data.
    The report's canonical corpus digest and row count define the immutable
    snapshot; any mutation within that prefix still fails closed.
    """

    if type(count) is not int or count <= 0:
        raise ReplayError("frozen case snapshot count is invalid")
    cases = []
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if len(cases) >= count:
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ReplayError(
                        f"case line {line_number} is malformed"
                    ) from exc
                if not isinstance(value, dict):
                    raise ReplayError(
                        f"case line {line_number} is not an object"
                    )
                cases.append(value)
    except OSError as exc:
        raise ReplayError("DecisionCase corpus is unreadable") from exc
    if len(cases) != count:
        raise ReplayError("DecisionCase corpus is shorter than frozen snapshot")
    if expected_digest and _case_corpus_digest(cases) != expected_digest:
        raise ReplayError("DecisionCase frozen snapshot digest mismatch")
    return cases


def _identity(choice):
    if not isinstance(choice, dict):
        return None
    value = choice.get("choice_id")
    return value if isinstance(value, str) and value.strip() else None


def _candidate_identity(candidate):
    if not isinstance(candidate, dict):
        return None
    value = candidate.get(
        "choice_id", candidate.get("candidate_id", candidate.get("id"))
    )
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    value = str(value).strip()
    return value or None


def _numeric(value):
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _normalized_operation(row):
    value = row.get("operation") if isinstance(row, dict) else None
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized or None


def _resource_parent_key(target):
    if not isinstance(target, dict):
        return None
    instance_id = target.get("potion_instance_id")
    potion_id = target.get("potion_id")
    slot = target.get("slot")
    if (
        not isinstance(instance_id, str) or not instance_id
        or not isinstance(potion_id, str) or not potion_id
        or type(slot) is not int or slot < 0
    ):
        return None
    return instance_id, slot, potion_id


def _resource_child_key(row, *, producer=False):
    if not isinstance(row, dict):
        return None
    if producer:
        consequence = row.get("consequences")
        consequence = consequence if isinstance(consequence, dict) else {}
        parent = _resource_parent_key({
            "potion_instance_id": consequence.get("potion_instance_id"),
            "potion_id": consequence.get("potion_id"),
            "slot": consequence.get("potion_slot"),
        })
        operation = _normalized_operation(row)
    else:
        target = row.get("target")
        if not isinstance(target, dict) or target.get("kind") != (
            "potion_resource"
        ):
            return None
        parent = _resource_parent_key(target)
        operation = str(
            row.get("operation", target.get("operation")) or ""
        ).strip().casefold() or None
    if parent is None or operation not in {"use", "discard"}:
        return None
    return (*parent, operation)


def _authoritative_resource_children(case):
    game = (
        case.get("authoritative_state_before", {}).get("game_state", {})
        if isinstance(case, dict) else {}
    )
    children = []
    parents = {}
    for potion in game.get("potions") or []:
        if not isinstance(potion, dict) or potion.get("id") == "Potion Slot":
            continue
        parent = _resource_parent_key({
            "potion_instance_id": potion.get("potion_instance_id"),
            "potion_id": potion.get("id"),
            "slot": potion.get("slot"),
        })
        if (
            parent is None
            or parent[0] in parents
            or any(
                not isinstance(potion.get(field), bool)
                for field in ("can_use", "can_discard", "requires_target")
            )
        ):
            return None, None
        parents[parent[0]] = potion
        if potion["can_discard"]:
            children.append((*parent, "discard"))
        if potion["can_use"] and not potion["requires_target"]:
            children.append((*parent, "use"))
    if not children:
        return None, None
    return children, parents


def _resource_target_matches_authoritative(row, parents):
    child = _resource_child_key(row)
    if child is None or not isinstance(parents, dict):
        return False
    target = row.get("target")
    nested = target.get("potion") if isinstance(target, dict) else None
    parent = parents.get(child[0])
    if not isinstance(nested, dict) or not isinstance(parent, dict):
        return False
    return all(
        nested.get(field) == parent.get(field)
        for field in (
            "id", "potion_instance_id", "slot", "can_use",
            "can_discard", "requires_target",
        )
    )


def _independent_producer_partition(producer):
    """Repartition the raw producer object inside the independent replay."""

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
            "present": isinstance(raw, dict), "value": raw,
        },
        "producer_consequence_claim": claim,
        "producer_scoring_facts": scoring,
        "unclassified_producer_fields": sorted(set(unclassified)),
    }


def _case_oracle_record(case):
    """Expose persisted case fields under the oracle's trace field names."""

    record = dict(case) if isinstance(case, dict) else {}
    record["available_options_before"] = case.get("available_options") or []
    record["available_commands_before"] = case.get("available_commands") or []
    record["legal_choices_before"] = case.get("canonical_choices") or []
    record["resource_preparation_options_before"] = (
        case.get("resource_preparation_options") or []
    )
    return record


def _raw_choice_target_binding_problems(case, choice):
    """Bind canonical targets back to the untouched protocol surface."""

    problems = []
    if not isinstance(choice, dict):
        return ["canonical_choice_not_object"]
    action = str(choice.get("action") or "").strip().casefold()
    if action == "potion":
        source = case.get("resource_preparation_options") or []
    elif action == "choose":
        source = case.get("available_options") or []
    else:
        source = []
    if action in {"choose", "potion"}:
        matches = [
            option for option in source
            if isinstance(option, dict)
            and option.get("choice_index") == choice.get("choice_index")
            and (
                action != "potion"
                or str(
                    ((option.get("target") or {}).get("operation")) or ""
                ).strip().casefold() == _normalized_operation(choice)
            )
        ]
        if len(matches) != 1:
            return ["raw_choice_index_binding_not_unique"]
        option = matches[0]
        if str(option.get("option_id") or "") != str(
            choice.get("choice_id") or ""
        ):
            problems.append("raw_choice_id_binding")
        if _independent_oracle._freeze(
            _normalized_protocol_target(option.get("target") or {})
        ) != (
            _independent_oracle._freeze(
                _normalized_protocol_target(choice.get("target") or {})
            )
        ):
            problems.append("raw_choice_target_binding")
        return problems

    expected_target = {"kind": "protocol_action", "action": action}
    if _independent_oracle._freeze(choice.get("target") or {}) != (
        _independent_oracle._freeze(expected_target)
    ):
        problems.append("raw_protocol_action_target_binding")
    if choice.get("choice_id") != f"action:{action}":
        problems.append("raw_protocol_action_choice_id_binding")
    command_aliases = {
        "proceed": {"confirm", "proceed"},
        "return": {"cancel", "skip", "leave", "return"},
    }
    available = {
        str(value or "").strip().casefold()
        for value in case.get("available_commands") or []
    }
    wanted_commands = command_aliases.get(action, {action})
    if not (available & wanted_commands):
        problems.append("raw_protocol_action_command_missing")
    return problems


def _normalized_protocol_target(value):
    """Drop only bridge aliases proven redundant with stable instance IDs."""

    if isinstance(value, list):
        return [_normalized_protocol_target(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _normalized_protocol_target(item)
        for key, item in value.items()
    }
    # Event targets retain the raw button presentation on the protocol
    # surface, while the canonical choice deliberately keeps only the typed
    # binding (event id/original index/contract/card identity).  Labels and
    # text are not identity and may be localized or omitted by the
    # canonicalizer; comparing them here would reject an otherwise exact
    # raw->canonical binding.  Do not drop any typed fields.
    # Labels/text are presentation aliases for every protocol target (not
    # just EVENT buttons); chest/shop-room fallback descriptors may carry a
    # label on the raw surface while their canonical target is typed-only.
    # Keep all identity/mechanics fields intact.
    result.pop("label", None)
    result.pop("text", None)
    # Audit projection provenance is deliberately not base-game target
    # identity.  Production fixtures may hand the canonicalizer an in-memory
    # raw option before the compact trace copy receives this marker.
    result.pop("audit_projection_version", None)
    if (
        result.get("uuid") is not None
        and result.get("card_instance_id") is not None
        and str(result["uuid"]) == str(result["card_instance_id"])
    ):
        result.pop("uuid", None)
    return result


def _expected_operation(case, choice):
    target = choice.get("target") if isinstance(choice, dict) else None
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "").strip().casefold()
    action = str(choice.get("action") or "").strip().casefold()
    phase = str(case.get("phase") or "").upper()
    if kind == "protocol_action":
        return target.get("action")
    if kind == "potion_resource":
        return str(target.get("operation") or "").casefold() or None
    if kind == "sapphire_key":
        return "gain_sapphire_key"
    if kind == "reward":
        reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
        reward_type = str(reward.get("reward_type") or "").casefold()
        if reward_type == "relic":
            return "gain_linked_relic" if phase == "SAPPHIRE_KEY" else "collect_combat_reward"
        return (
            "collect_independent_reward"
            if phase == "SAPPHIRE_KEY" else "collect_combat_reward"
        )
    if kind in {"card", "relic", "potion"} and isinstance(
        target.get("item"), dict
    ):
        return f"buy_{kind}_then_rerank_shop"
    if kind == "relic":
        return "gain_boss_relic" if phase == "BOSS_REWARD" else None
    if kind == "purge":
        return "open_card_purge_grid"
    if kind == "rest":
        option = str(target.get("rest_option") or "").upper()
        return {
            "REST": "campfire_rest", "SMITH": "campfire_smith",
            "TOKE": "campfire_toke", "RECALL": "campfire_recall",
        }.get(option)
    if kind == "chest":
        return "open_chest"
    if kind == "shop_room":
        return "enter_shop"
    if kind == "event_option":
        contract = target.get("neow_contract")
        if isinstance(contract, dict):
            return (
                "neow_dialog_advance"
                if contract.get("contract_kind") == "NEOW_DIALOG_ADVANCE"
                else "neow_reward"
            )
        event_id = "".join(
            character.lower()
            for character in str(target.get("event_id") or "")
            if character.isalnum()
        )
        if event_id in {"matchandkeep", "matchkeep", "gremlinmatchgame", "matchgame"}:
            return "flip_match_position"
    if phase == "HAND_SELECT" and kind == "card":
        before = case.get("authoritative_state_before") or {}
        game = before.get("game_state") if isinstance(before, dict) else {}
        screen = game.get("screen_state") if isinstance(game, dict) else {}
        card = target.get("card") if isinstance(target.get("card"), dict) else {}
        card_instance_id = target.get("card_instance_id")
        visible = [
            row for row in (screen.get("hand") or [])
            if isinstance(row, dict)
            and row.get("card_instance_id") == card_instance_id
        ] if isinstance(screen, dict) else []
        if (
            isinstance(game.get("current_action"), str)
            and game.get("current_action")
            and type(screen.get("max_cards")) is int
            and screen.get("max_cards") >= 0
            and type(screen.get("can_pick_zero")) is bool
            and isinstance(card_instance_id, str)
            and card_instance_id
            and card.get("card_instance_id") == card_instance_id
            and len(visible) == 1
        ):
            return "hand_select_card"
    if phase == "GRID" and kind == "card":
        before = case.get("authoritative_state_before") or {}
        game = before.get("game_state") if isinstance(before, dict) else {}
        screen = game.get("screen_state") if isinstance(game, dict) else {}
        if isinstance(screen, dict):
            if screen.get("for_upgrade") is True:
                return "grid_upgrade"
            if screen.get("for_transform") is True:
                return "grid_transform"
            if screen.get("for_purge") is True:
                return "grid_purge"
    return action if action in {"proceed", "return"} else None


def _typed_parent_potion_for_claim(case, field, claimed):
    """Join a resource-preparation claim to one typed parent option.

    A full belt can be prepared for one of several visible shop/reward
    potions.  The old audit required the *whole* parent surface to contain a
    single potion, which made every otherwise exact multi-potion surface
    unresolved.  Bind the producer's claimed potion id to exactly one typed
    parent row instead; the fresh producer invariant independently proves
    which parent the producer intended.
    """

    if (
        case.get("decision_surface_kind") != "resource_preparation"
        or case.get("parent_choice_surface_pending") is not True
        or not isinstance(claimed, str)
        or not claimed
    ):
        return None
    phase = str(case.get("phase") or "").upper()
    required_phase = {
        "bound_purchase_potion_id": "SHOP_SCREEN",
        "bound_reward_potion_id": "COMBAT_REWARD",
    }.get(field)
    if (
        phase not in {"SHOP_SCREEN", "COMBAT_REWARD"}
        or (required_phase is not None and phase != required_phase)
    ):
        return None

    matches = []
    for option in case.get("available_options") or []:
        if not isinstance(option, dict):
            continue
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        potion_id = None
        typed = False
        if phase == "SHOP_SCREEN":
            item = target.get("item")
            item = item if isinstance(item, dict) else {}
            potion_id = (
                item.get("id")
                if str(target.get("kind") or "").casefold() == "potion"
                else None
            )
            typed = bool(potion_id and type(item.get("price")) is int)
        else:
            reward = target.get("reward")
            reward = reward if isinstance(reward, dict) else {}
            potion = reward.get("potion")
            potion = potion if isinstance(potion, dict) else {}
            potion_id = potion.get("id")
            typed = bool(
                potion_id
                and str(reward.get("reward_type") or "").casefold()
                == "potion"
            )
        if (
            typed
            and type(option.get("choice_index")) is int
            and isinstance(option.get("option_id"), str)
            and option.get("option_id")
            and str(potion_id).casefold() == claimed.casefold()
        ):
            matches.append(str(potion_id))
    return matches[0] if len(matches) == 1 else None


def _typed_shop_parent_listing_for_claim(case, claim):
    """Resolve the producer's exact shop listing tuple against raw options."""

    claim = claim if isinstance(claim, dict) else {}
    if (
        str(case.get("phase") or "").upper() != "SHOP_SCREEN"
        or case.get("decision_surface_kind") != "resource_preparation"
        or case.get("parent_choice_surface_pending") is not True
        or case.get("reason")
        != "shop_potion_replacement_resource_preparation"
    ):
        return None
    listing_id = claim.get("bound_purchase_listing_id")
    choice_index = claim.get("bound_purchase_choice_index")
    item_id = claim.get("bound_purchase_item_id")
    price = claim.get("bound_purchase_price")
    recorded = case.get("resource_parent_listing")
    recorded = recorded if isinstance(recorded, dict) else {}
    if (
        not isinstance(listing_id, str) or not listing_id
        or type(choice_index) is not int
        or not isinstance(item_id, str) or not item_id
        or type(price) is not int or price < 0
        or recorded != {
            "listing_id": listing_id,
            "choice_index": choice_index,
            "item_id": item_id,
            "price": price,
        }
    ):
        return None
    matches = []
    for option in case.get("available_options") or []:
        if (
            not isinstance(option, dict)
            or option.get("option_id") != listing_id
            or option.get("choice_index") != choice_index
        ):
            continue
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        item = target.get("item")
        item = item if isinstance(item, dict) else {}
        if (
            str(target.get("kind") or "").casefold() == "potion"
            and item.get("id") == item_id
            and item.get("price") == price
            and isinstance(item.get("name"), str)
            and item.get("name")
            and all(
                type(item.get(field)) is bool
                for field in ("can_use", "can_discard", "requires_target")
            )
        ):
            matches.append(option)
    if len(matches) != 1:
        return None
    return {
        "bound_purchase_listing_id": listing_id,
        "bound_purchase_choice_index": choice_index,
        "bound_purchase_item_id": item_id,
        "bound_purchase_price": price,
    }


def _target_identity_values(case, choice, claim=None):
    target = choice.get("target") if isinstance(choice, dict) else None
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "").casefold()
    card = target.get("card") if isinstance(target.get("card"), dict) else {}
    relic = target.get("relic") if isinstance(target.get("relic"), dict) else {}
    item = target.get("item") if isinstance(target.get("item"), dict) else {}
    reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
    reward_relic = reward.get("relic") if isinstance(reward.get("relic"), dict) else {}
    target_potion = (
        target.get("potion") if isinstance(target.get("potion"), dict) else {}
    )
    claim = claim if isinstance(claim, dict) else {}
    shop_parent = _typed_shop_parent_listing_for_claim(case, claim)
    shop_parent = shop_parent if isinstance(shop_parent, dict) else {}
    values = {
        "item_id": (
            item.get("card_instance_id")
            or item.get("potion_instance_id")
            or item.get("relic_id")
            or item.get("id")
        ),
        "card_id": card.get("id") or (item.get("id") if kind == "card" else None),
        "card_instance_id": (
            target.get("card_instance_id") or card.get("card_instance_id")
            or card.get("uuid")
        ),
        "potion_instance_id": (
            target.get("potion_instance_id") or item.get("potion_instance_id")
        ),
        "potion_id": (
            target.get("potion_id")
            or target_potion.get("id")
            or (
                (reward.get("potion") or {}).get("id")
                if isinstance(reward.get("potion"), dict) else None
            )
        ),
        "relic_id": (
            relic.get("id") or reward_relic.get("id")
            or (item.get("id") if kind == "relic" else None)
        ),
        "reward_type": reward.get("reward_type"),
        "upgrades": card.get("upgrades"),
        "rest_option": target.get("rest_option"),
        "bound_new_potion_id": (
            shop_parent.get("bound_purchase_item_id")
            if str(case.get("phase") or "").upper() == "SHOP_SCREEN"
            else _typed_parent_potion_for_claim(
                case, "bound_new_potion_id", claim.get("bound_new_potion_id")
            )
        ),
        "bound_purchase_potion_id": _typed_parent_potion_for_claim(
            case, "bound_purchase_potion_id",
            claim.get("bound_purchase_potion_id"),
        ),
        "bound_reward_potion_id": _typed_parent_potion_for_claim(
            case, "bound_reward_potion_id",
            claim.get("bound_reward_potion_id"),
        ),
        "bound_purchase_listing_id": shop_parent.get(
            "bound_purchase_listing_id"
        ),
        "bound_purchase_choice_index": shop_parent.get(
            "bound_purchase_choice_index"
        ),
        "bound_purchase_item_id": shop_parent.get(
            "bound_purchase_item_id"
        ),
        "bound_purchase_price": shop_parent.get("bound_purchase_price"),
    }
    return values


def _producer_effect_claim_problems(case, choice, producer, expected):
    """Validate claims without importing producer score/reason authority."""

    issues = []
    unknowns = []
    partition = _independent_producer_partition(producer)
    claim = partition["producer_consequence_claim"]
    if not isinstance(producer.get("consequences"), dict) or not claim:
        unknowns.append("producer_effect_claim_empty")
        return issues, unknowns, partition
    for field in partition["unclassified_producer_fields"]:
        if field not in (
            _INDEPENDENTLY_AUDITABLE_UNCLASSIFIED_PRODUCER_FIELDS
        ):
            unknowns.append(f"producer_effect_field_unclassified:{field}")
            continue
        supplied = (producer.get("consequences") or {}).get(field)
        wanted = expected.get(field)
        supplied_status = (
            supplied.get("status") if isinstance(supplied, dict) else None
        )
        wanted_status = (
            wanted.get("status") if isinstance(wanted, dict) else None
        )
        if supplied_status is None or wanted_status is None:
            unknowns.append(
                "producer_uncertainty_classification_not_independently_bound"
            )
        elif supplied_status != wanted_status:
            issues.append("producer_uncertainty_classification_contradicted")

    target_values = _target_identity_values(case, choice, claim)
    for field in _PRODUCER_TARGET_IDENTITY_FIELDS:
        if field not in claim:
            continue
        wanted = target_values.get(field)
        if wanted is None:
            if claim[field] is not None:
                unknowns.append(f"producer_{field}_not_protocol_visible")
        elif str(claim[field]).casefold() != str(wanted).casefold():
            issues.append(f"producer_{field}_target_mismatch")

    if "operation" in claim:
        wanted = _expected_operation(case, choice)
        observed = str(claim.get("operation") or "").strip().casefold()
        if wanted is None:
            unknowns.append("producer_operation_not_independently_classified")
        else:
            wanted = str(wanted).strip().casefold()
            if str((choice.get("target") or {}).get("kind") or "").casefold() == "potion_resource":
                if observed.startswith("use_"):
                    observed = "use"
                elif observed.startswith("discard_"):
                    observed = "discard"
            if observed != wanted:
                issues.append("producer_operation_contradicted")

    if "combat_delta" in claim:
        unknowns.append("producer_combat_delta_not_independently_classified")

    locally_validated = _PRODUCER_TARGET_IDENTITY_FIELDS | {
        "operation", "combat_delta",
    }
    if "current_cost" in claim:
        wanted_cost = expected.get("current_cost")
        knowledge = (expected.get("field_knowledge") or {}).get(
            "current_cost"
        )
        supplied = claim.get("current_cost")
        if (
            not isinstance(knowledge, dict)
            or knowledge.get("status") not in {
                "known", "known_domain", "not_applicable",
            }
            or not isinstance(wanted_cost, dict)
        ):
            unknowns.append("producer_current_cost_not_independently_classified")
        elif not isinstance(supplied, dict) or not supplied:
            issues.append("producer_current_cost_invalid")
        else:
            for field, value in supplied.items():
                if field not in {"gold", "hp", "max_hp"}:
                    unknowns.append(
                        f"producer_current_cost_field_unclassified:{field}"
                    )
                elif _independent_oracle._freeze(value) != (
                    _independent_oracle._freeze(wanted_cost.get(field))
                ):
                    issues.append(
                        f"producer_current_cost_{field}_contradicted"
                    )
        locally_validated.add("current_cost")
    independent_claim = {
        key: value for key, value in claim.items()
        if key not in locally_validated
    }
    if independent_claim:
        oracle_row = {
            "producer_consequence_raw": {
                "present": True, "value": independent_claim,
            },
            "producer_consequence_claim": independent_claim,
            "producer_scoring_facts": {},
            "unclassified_producer_fields": [],
        }
        selected = choice.get("selected") is True
        settlement = None
        if selected:
            settlement = _independent_oracle._authoritative_settlement_observation(
                _case_oracle_record(case), choice.get("choice_id"), expected
            )
        contradictions, unresolved = (
            _independent_oracle._review_producer_consequence_claim(
                _case_oracle_record(case), choice, oracle_row,
                expected, settlement,
            )
        )
        issues.extend(
            "producer_effect_claim_contradicted:"
            + str(item.get("field") or "unknown")
            for item in contradictions
        )
        unknowns.extend(
            "producer_effect_claim_unresolved:" + str(value)
            for value in unresolved
        )
    return sorted(set(issues)), sorted(set(unknowns)), partition


def _observable_state_delta(case):
    before = _independent_oracle._observable_from_authoritative_state(
        case.get("authoritative_state_before")
    )
    after = _independent_oracle._observable_from_authoritative_state(
        case.get("authoritative_state_after")
    )
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    result = {}
    for field in ("current_hp", "max_hp", "gold", "block"):
        left = before.get(field)
        right = after.get(field)
        if not _numeric(left) or not _numeric(right):
            return None
        name = "current_hp_delta" if field == "current_hp" else f"{field}_delta"
        result[name] = right - left
    result["hp_delta"] = result["current_hp_delta"]
    for field in ("deck", "relics", "potions"):
        delta = _independent_oracle._observable_collection_delta(
            before.get(field), after.get(field)
        )
        if not isinstance(delta, dict):
            return None
        result[field] = delta
    result["keys_before"] = before.get("keys")
    result["keys_after"] = after.get("keys")
    return result


def _settlement_contract_problems(case):
    """Cross-check state delta, decision outcome and settlement envelope."""

    issues = []
    unknowns = []
    observed_delta = _observable_state_delta(case)
    if observed_delta is None:
        unknowns.append("authoritative_state_delta_unavailable")
        return issues, unknowns
    selected = case.get("selected_choice_ids")
    if not isinstance(selected, list) or len(selected) != 1:
        issues.append("settlement_selected_choice_binding")
        return issues, unknowns
    settlement = case.get("authoritative_choice_settlement")
    outcome = case.get("decision_outcome")
    if not isinstance(settlement, dict) or not isinstance(outcome, dict):
        unknowns.append("settlement_or_decision_outcome_missing")
        return issues, unknowns
    fixture_scope = (
        (case.get("decision_context") or {}).get("scope")
        == "production_candidate_regression"
    )
    allowed = (
        {
            ("fixture_observed", "deterministic_production_probe"),
            ("observed", "protocol_state_delta"),
        }
        if fixture_scope
        else {
            ("observed", "protocol_state_delta"),
            ("resolved", "protocol_state_delta"),
            ("clear", "protocol_state_delta"),
        }
    )
    if (settlement.get("status"), settlement.get("authority")) not in allowed:
        issues.append("settlement_authority_binding")
    if settlement.get("fully_observable") is not True:
        issues.append("settlement_not_fully_observable")
    if str(settlement.get("choice_id") or "") != str(selected[0]):
        issues.append("settlement_choice_id_binding")
    if (
        settlement.get("before_seq") != case.get("before_seq")
        or settlement.get("after_seq") != case.get("after_seq")
    ):
        issues.append("settlement_sequence_binding")
    observed = settlement.get("observed_outcome")
    if not isinstance(observed, dict):
        unknowns.append("settlement_observed_outcome_missing")
        return issues, unknowns
    required = {
        "current_hp_delta", "max_hp_delta", "gold_delta", "block_delta",
        "deck", "relics", "potions", "keys_before", "keys_after",
    }
    for field in required:
        if field not in observed:
            unknowns.append(f"settlement_observed_{field}_missing")
            continue
        if _independent_oracle._freeze(observed[field]) != (
            _independent_oracle._freeze(observed_delta[field])
        ):
            issues.append(f"settlement_{field}_state_delta_mismatch")
        if field not in outcome:
            unknowns.append(f"decision_outcome_{field}_missing")
        elif _independent_oracle._freeze(outcome[field]) != (
            _independent_oracle._freeze(observed_delta[field])
        ):
            issues.append(f"decision_outcome_{field}_state_delta_mismatch")
    return sorted(set(issues)), sorted(set(unknowns))


def _unproven_consequence_payload_mismatches(actual, expected):
    """Reject payload injection into independently unobservable fields.

    ``_consequence_matches_visible`` proves fields whose mechanics are known.
    For a protocol-hidden or otherwise unobservable field, its neutral value
    is an envelope marker, not permission to replace it with an arbitrary
    asserted outcome.  Keep that marker and its knowledge status bound to the
    independent oracle so synchronized producer/canonical tampering cannot
    turn an unknown into fabricated evidence.
    """

    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return ["consequence_envelope"]
    actual_knowledge = actual.get("field_knowledge")
    expected_knowledge = expected.get("field_knowledge")
    actual_knowledge = (
        actual_knowledge if isinstance(actual_knowledge, dict) else {}
    )
    expected_knowledge = (
        expected_knowledge if isinstance(expected_knowledge, dict) else {}
    )
    mismatches = []
    for field in (
        "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
        "relic_changes", "potion_changes", "curse",
        "probabilistic_outcomes", "current_cost", "future_costs",
    ):
        expected_status = expected_knowledge.get(field)
        expected_status = (
            expected_status.get("status")
            if isinstance(expected_status, dict) else None
        )
        if expected_status in {"known", "known_domain", "not_applicable"}:
            continue
        actual_status = actual_knowledge.get(field)
        actual_status = (
            actual_status.get("status")
            if isinstance(actual_status, dict) else None
        )
        if actual_status != expected_status:
            mismatches.append(f"{field}:knowledge_status")
        if _freeze(actual.get(field)) != _freeze(expected.get(field)):
            mismatches.append(f"{field}:unproven_payload")
    return sorted(set(mismatches))


def _recompute_local_score(row, prefix="candidate"):
    """Validate a producer score from structured inputs, never from selection."""

    issues = []
    unknowns = []
    rule_id = row.get("score_rule_id") if isinstance(row, dict) else None
    formula = row.get("score_formula") if isinstance(row, dict) else None
    inputs = row.get("score_inputs") if isinstance(row, dict) else None
    components = row.get("score_components") if isinstance(row, dict) else None
    if not isinstance(rule_id, str) or not rule_id.strip():
        unknowns.append(f"{prefix}_score_rule_id_missing")
        return issues, unknowns
    if rule_id.strip().casefold() == "unclassified":
        unknowns.append(f"{prefix}_score_rule_unclassified")
        return issues, unknowns
    if not isinstance(formula, dict) or not formula:
        unknowns.append(f"{prefix}_score_formula_missing")
        return issues, unknowns
    if not isinstance(inputs, dict) or not inputs:
        unknowns.append(f"{prefix}_score_inputs_missing")
        return issues, unknowns
    if not isinstance(components, list) or not components:
        unknowns.append(f"{prefix}_score_components_missing")
        return issues, unknowns
    if formula.get("kind") != "sum_components_v1":
        unknowns.append(f"{prefix}_score_formula_unsupported")
        return issues, unknowns

    component_names = []
    recomputed_values = []
    for component in components:
        if not isinstance(component, dict):
            issues.append(f"{prefix}_score_component_invalid")
            continue
        name = component.get("name")
        input_name = component.get("input")
        coefficient = component.get("coefficient")
        claimed_value = component.get("value")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"{prefix}_score_component_name_invalid")
            continue
        component_names.append(name)
        if not isinstance(input_name, str) or input_name not in inputs:
            issues.append(f"{prefix}_score_component_input_binding")
            continue
        input_value = inputs.get(input_name)
        if not _numeric(input_value) or not _numeric(coefficient) or not _numeric(
            claimed_value
        ):
            unknowns.append(f"{prefix}_score_component_numeric_unknown")
            continue
        expected = float(input_value) * float(coefficient)
        if not math.isclose(
            expected, float(claimed_value), rel_tol=0.0, abs_tol=1e-9
        ):
            issues.append(f"{prefix}_score_component_value_mismatch")
        recomputed_values.append(expected)
    if len(component_names) != len(set(component_names)):
        issues.append(f"{prefix}_score_component_name_duplicate")
    local_score = row.get("local_score", row.get("score"))
    if not _numeric(local_score):
        unknowns.append(f"{prefix}_local_score_missing")
    elif len(recomputed_values) == len(components) and not math.isclose(
        sum(recomputed_values), float(local_score), rel_tol=0.0, abs_tol=1e-9
    ):
        issues.append(f"{prefix}_score_formula_result_mismatch")
    return issues, unknowns


def _v2_value_missing(field, value, surface_kind):
    if field == "resource_preparation_options" and surface_kind != (
        "resource_preparation"
    ):
        return not isinstance(value, list)
    return value in (None, "", [], {})


def _vetoed_resource_use_numeric_is_not_applicable(case, row, value):
    """Recognize one fail-closed potion-use row whose resources are unknown.

    A resource-preparation producer deliberately exposes an unusable ``use``
    sibling so the protocol surface stays complete.  Its HP/max-HP/gold
    effects are not mechanics claims and must remain ``None``.  Keep this
    exception tied to the full typed veto and sibling-discard contract; an
    eligible, selected, weakly-vetoed, or score-dependent row still fails the
    ordinary numeric consequence checks below.
    """

    if not (
        isinstance(case, dict)
        and isinstance(row, dict)
        and isinstance(value, dict)
        and case.get("decision_surface_kind") == "resource_preparation"
        and case.get("parent_choice_surface_pending") is True
        and str(case.get("phase") or "").upper()
        in {"COMBAT_REWARD", "SHOP_SCREEN"}
        and str(case.get("action") or "").casefold() == "potion"
        and row.get("selection_eligible") is False
        and row.get("veto_reason")
        == "use_has_no_verified_preparation_value"
        and row.get("local_reason")
        == "use_has_no_verified_preparation_value"
        and row.get("reason_codes")
        == ["use_has_no_verified_preparation_value"]
        and row.get("final_source") == "not_selected"
        and row.get("score_rule_id")
        == "potion_resource_preparation_net_v1"
        and row.get("score_formula") == {"kind": "sum_components_v1"}
        and value.get("scope") == "immediate_protocol_transition"
        and all(value.get(field) is None for field in _RESOURCE_FIELDS)
    ):
        return False

    choice_id = _identity(row)
    selected_ids = case.get("selected_choice_ids") or []
    target = row.get("target")
    if (
        not choice_id
        or choice_id in selected_ids
        or not isinstance(target, dict)
        or target.get("kind") != "potion_resource"
        or target.get("operation") != "use"
        or str(row.get("operation") or "").casefold() != "use"
    ):
        return False
    instance_id = str(target.get("potion_instance_id") or "")
    potion_id = str(target.get("potion_id") or "")
    slot = target.get("slot")
    if (
        not instance_id
        or not potion_id
        or type(slot) is not int
        or choice_id != f"{instance_id}:use"
    ):
        return False

    expected_knowledge = {
        "status": "unknown",
        "authority": "protocol_surface",
        "reason": "mechanism_not_classified",
    }
    knowledge = value.get("field_knowledge")
    if not isinstance(knowledge, dict) or any(
        knowledge.get(field) != expected_knowledge
        for field in _RESOURCE_FIELDS
    ):
        return False

    inputs = row.get("score_inputs")
    components = row.get("score_components")
    expected_inputs = {
        "new_keep_value", "held_keep_value", "verified_use_bonus",
    }
    if (
        not isinstance(inputs, dict)
        or set(inputs) != expected_inputs
        or not all(_numeric(inputs.get(field)) for field in expected_inputs)
        or float(inputs["verified_use_bonus"]) != 0.0
        or not isinstance(components, list)
        or len(components) != 3
    ):
        return False
    component_bindings = {
        (component.get("name"), component.get("input"))
        for component in components
        if isinstance(component, dict)
    }
    if component_bindings != {
        ("new_keep_value", "new_keep_value"),
        ("held_keep_cost", "held_keep_value"),
        ("verified_use_bonus", "verified_use_bonus"),
    }:
        return False

    producer = row.get("producer_candidate_raw")
    claim = row.get("producer_consequence_claim")
    raw_claim = (
        producer.get("consequences")
        if isinstance(producer, dict) else None
    )
    if not (
        isinstance(producer, dict)
        and producer.get("selection_eligible") is False
        and producer.get("veto_reason")
        == "use_has_no_verified_preparation_value"
        and producer.get("reason")
        == "use_has_no_verified_preparation_value"
        and isinstance(raw_claim, dict)
        and raw_claim.get("operation")
        == "use_held_potion_for_reward_slot"
        and raw_claim.get("potion_instance_id") == instance_id
        and raw_claim.get("potion_id") == potion_id
        and raw_claim.get("potion_slot") == slot
        and isinstance(raw_claim.get("bound_new_potion_id"), str)
        and raw_claim.get("bound_new_potion_id")
        and isinstance(claim, dict)
        and claim.get("operation") == "use_held_potion_for_reward_slot"
        and claim.get("potion_id") == potion_id
        and claim.get("potion_slot") == slot
        and claim.get("potion_instance_id", instance_id) == instance_id
        and claim.get(
            "bound_new_potion_id", raw_claim["bound_new_potion_id"]
        ) == raw_claim["bound_new_potion_id"]
    ):
        return False

    # Both canonical surfaces must carry this exact vetoed use row and one
    # eligible discard sibling for the same immutable held-potion instance.
    for surface_name in ("canonical_choices", "candidates"):
        surface = case.get(surface_name)
        if not isinstance(surface, list):
            return False
        uses = [item for item in surface if _identity(item) == choice_id]
        siblings = [
            item for item in surface
            if isinstance(item, dict)
            and str((item.get("target") or {}).get("potion_instance_id") or "")
            == instance_id
            and str(item.get("operation") or "").casefold() == "discard"
        ]
        if (
            len(uses) != 1
            or uses[0].get("selection_eligible") is not False
            or uses[0].get("veto_reason")
            != "use_has_no_verified_preparation_value"
            or len(siblings) != 1
            or siblings[0].get("selection_eligible") is not True
            or _identity(siblings[0]) != f"{instance_id}:discard"
        ):
            return False
    return True


def _structured_consequence_unknowns(prefix, value, *, case=None, row=None):
    if not isinstance(value, dict):
        return []
    unknowns = [
        f"{prefix}_{field}_missing"
        for field in _STRUCTURED_CONSEQUENCE_FIELDS
        if field not in value
    ]
    numeric_not_applicable = _vetoed_resource_use_numeric_is_not_applicable(
        case, row, value
    )
    for field in ("hp_delta", "max_hp_delta", "gold_delta"):
        if (
            field in value
            and not _numeric(value[field])
            and not numeric_not_applicable
        ):
            unknowns.append(f"{prefix}_{field}_invalid")
    for field in (
        "card_changes", "relic_changes", "potion_changes", "curse",
        "current_cost",
    ):
        if field in value and not isinstance(value[field], dict):
            unknowns.append(f"{prefix}_{field}_invalid")
    for field in ("probabilistic_outcomes", "future_costs", "uncertainty"):
        if field in value and not isinstance(value[field], list):
            unknowns.append(f"{prefix}_{field}_invalid")
    if "raw_effect_text" in value and not isinstance(value["raw_effect_text"], str):
        unknowns.append(f"{prefix}_raw_effect_text_invalid")
    current_cost = value.get("current_cost")
    if isinstance(current_cost, dict):
        for field in _COST_FIELDS:
            if field not in current_cost or not _numeric(current_cost.get(field)):
                unknowns.append(f"{prefix}_current_cost_{field}_invalid")
    return unknowns


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def _aliases(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return set()
    raw = str(value).strip().casefold()
    if not raw:
        return set()
    values = {raw}
    token = "".join(character for character in raw if character.isalnum())
    if token:
        values.add(token)
    if ":" in raw:
        parts = raw.split(":")
        suffix = parts[-1]
        if suffix:
            values.update(_aliases(suffix))
        if parts[0] in {"event", "option"} and suffix:
            values.update(_aliases(suffix))
        if parts[0] == "shop" and len(parts) >= 3:
            values.update(_aliases(parts[-2]))
    if "@" in raw:
        values.update(_aliases(raw.rsplit("@", 1)[-1]))
    return values


def _option_identity(option):
    if not isinstance(option, dict):
        return None
    option_id = option.get("option_id", option.get("choice_id"))
    if isinstance(option_id, str) and option_id.strip():
        return option_id.strip()
    index = option.get("choice_index")
    if isinstance(index, int) and not isinstance(index, bool):
        return f"choice_index:{index}"
    return None


def _option_aliases(option):
    if not isinstance(option, dict):
        return set()
    result = set()
    for key in (
        "option_id", "choice_id", "choice_index", "semantic_id", "action",
        "label",
    ):
        result.update(_aliases(option.get(key)))
    index = option.get("choice_index")
    if index is not None:
        result.update(_aliases(f"event:{index}"))
        result.update(_aliases(f"option:{index}"))
    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    for key in (
        "id", "label", "card_instance_id", "potion_instance_id", "relic_id",
        "x", "y",
    ):
        result.update(_aliases(target.get(key)))
    for nested_name in ("card", "relic", "item", "reward"):
        nested = target.get(nested_name)
        nested = nested if isinstance(nested, dict) else {}
        for key in (
            "id", "name", "label", "item_id", "card_instance_id",
            "potion_instance_id", "relic_id", "reward_type",
        ):
            result.update(_aliases(nested.get(key)))
    if all(target.get(key) is not None for key in ("symbol", "x", "y")):
        result.update(_aliases(
            f"{target['symbol']}@{target['x']},{target['y']}"
        ))
    return result


def _candidate_aliases(candidate):
    if not isinstance(candidate, dict):
        return set()
    result = set()
    for key in (
        "choice_id", "candidate_id", "id", "choice_index", "semantic_id",
        "action", "label",
    ):
        result.update(_aliases(candidate.get(key)))
    facts = candidate.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    for key in (
        "choice_id", "choice_index", "card_id", "card_instance_id", "item_id",
        "relic_id", "potion_id",
    ):
        result.update(_aliases(facts.get(key)))
    return result


def _legacy_common_problems(case):
    issues = []
    if case.get("case_schema_version") != 1:
        issues.append("case_schema_version")
    for field in ("attempt_id", "run_id", "decision_hash", "phase"):
        if not isinstance(case.get(field), str) or not case[field].strip():
            issues.append(f"binding_{field}")
    if type(case.get("before_seq")) is not int:
        issues.append("binding_before_seq")
    return issues


def _missing_contract_value(value):
    return value is None or value == "" or value == [] or value == {}


def _producer_identity(candidate):
    if not isinstance(candidate, dict):
        return None
    value = candidate.get(
        "choice_id", candidate.get("candidate_id", candidate.get("id"))
    )
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    value = str(value).strip()
    return value or None


def _producer_consequence_target_matches(choice, producer):
    """Independently bind producer claims to the protocol target identity."""

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
            and _freeze(gains[0]) == _freeze(item)
        )
    if kind == "card":
        card = target.get("card")
        card = card if isinstance(card, dict) else {}
        wanted = target.get("card_instance_id") or card.get(
            "card_instance_id"
        )
        selected = consequence.get("selected_card")
        if isinstance(selected, dict):
            return bool(wanted and _freeze(selected) == _freeze(card))
        changes = consequence.get("card_changes")
        if not isinstance(changes, dict):
            return False
        exact_targets = []
        for field in ("gain", "remove", "upgrade", "transform"):
            values = changes.get(field)
            if isinstance(values, list):
                exact_targets.extend(value for value in values)
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
        return bool(consequence.get("rest_option"))
    if kind == "chest":
        return consequence.get("operation") == "open_chest"
    if kind == "shop_room":
        return consequence.get("operation") == "enter_shop"
    return kind in {"event_option", "map_boss", "bowl"}


def _v2_contract_problems(case):
    """Independently recompute the schema-v2 producer/canonical join."""

    issues = []
    unknowns = []
    if case.get("case_schema_version") != 2:
        issues.append("case_schema_version")
        return issues, unknowns
    for field in (
        "attempt_id", "run_id", "character", "run_type",
        "policy_version", "decision_hash", "controller_hash",
        "selection_id", "phase", "action", "reason",
        "goal_mode",
        "requested_target_id", "resolved_target_id",
    ):
        if not isinstance(case.get(field), str) or not case[field].strip():
            issues.append(f"v2_binding_{field}")
    if "selection_digest" in case:
        digest = case.get("selection_digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            issues.append("v2_binding_selection_digest")
    for field in (
        "trace_schema_version", "seed", "ascension_level",
        "before_seq", "after_seq",
    ):
        if not isinstance(case.get(field), int) or isinstance(case.get(field), bool):
            issues.append(f"v2_binding_{field}")
    if case.get("trace_schema_version") != 5:
        issues.append("v2_trace_schema_version")
    if case.get("run_type") != "standard":
        issues.append("v2_run_type")
    if case.get("goal_mode") != "HEART":
        issues.append("v2_goal_mode")
    if case.get("requested_target_id") != case.get("resolved_target_id"):
        issues.append("v2_target_binding")
    for field in (
        "candidate_contract", "candidate_join", "model_advice",
        "decision_context", "outcome_facts", "authoritative_state_before",
        "authoritative_state_after", "decision_outcome",
        "authoritative_choice_settlement",
    ):
        if not isinstance(case.get(field), dict) or not case.get(field):
            unknowns.append(f"v2_{field}_missing")
    model_advice = case.get("model_advice")
    if isinstance(model_advice, dict):
        if not isinstance(model_advice.get("status"), str):
            unknowns.append("v2_model_advice_status_missing")
        if not isinstance(model_advice.get("applied"), bool):
            unknowns.append("v2_model_advice_applied_invalid")
        if (
            model_advice.get("confidence") is not None
            and not _numeric(model_advice.get("confidence"))
        ):
            unknowns.append("v2_model_advice_confidence_invalid")
        if model_advice.get("final_choice_ids") != case.get(
            "final_choice_ids"
        ):
            issues.append("v2_model_final_choice_binding")
        if not isinstance(model_advice.get("final_source"), str):
            unknowns.append("v2_model_final_source_missing")
        if not isinstance(model_advice.get("override"), dict):
            unknowns.append("v2_model_override_missing")

    contract = case.get("v2_contract")
    if not isinstance(contract, dict):
        issues.append("v2_contract_missing")
    else:
        if contract.get("required_fields") != list(_V2_REQUIRED_FIELDS):
            issues.append("v2_required_field_contract")
        expected_present = [
            field for field in _V2_REQUIRED_FIELDS if field in case
        ]
        expected_missing = [
            field for field in _V2_REQUIRED_FIELDS
            if _v2_value_missing(
                field,
                case.get(field),
                case.get("decision_surface_kind"),
            )
        ]
        if contract.get("present_fields") != expected_present:
            issues.append("v2_present_field_contract")
        if contract.get("missing_value_fields") != expected_missing:
            issues.append("v2_missing_value_contract")
        unknowns.extend(
            f"v2_required_{field}_missing_value"
            for field in expected_missing
        )

    choices = case.get("canonical_choices")
    projected = case.get("candidates")
    producers = case.get("producer_candidates")
    surface_kind = case.get("decision_surface_kind")
    resource_options = case.get("resource_preparation_options")
    if surface_kind == "resource_preparation":
        if (
            case.get("action") != "potion"
            or case.get("parent_choice_surface_pending") is not True
            or not isinstance(resource_options, list)
            or not resource_options
        ):
            issues.append("v2_resource_preparation_envelope")
    elif (
        surface_kind != "strategic_choice"
        or case.get("parent_choice_surface_pending") is not False
        or resource_options != []
        or case.get("action") == "potion"
    ):
        issues.append("v2_strategic_surface_envelope")
    if not isinstance(choices, list) or not choices:
        unknowns.append("v2_canonical_choices_missing")
        return issues, unknowns
    if not isinstance(projected, list) or not projected:
        unknowns.append("v2_projected_candidates_missing")
        return issues, unknowns
    if not isinstance(producers, list) or not producers:
        unknowns.append("v2_producer_candidates_missing")
        return issues, unknowns

    if surface_kind == "resource_preparation":
        choice_keys = [_resource_child_key(choice) for choice in choices]
        projected_keys = [
            _resource_child_key(candidate) for candidate in projected
        ]
        producer_keys = [
            _resource_child_key(candidate, producer=True)
            for candidate in producers
        ]
    else:
        choice_keys = [
            (
                choice.get("choice_index"), choice.get("action"),
                _normalized_operation(choice),
            )
            if isinstance(choice, dict) else None
            for choice in choices
        ]
        projected_keys = []
        producer_keys = [
            (
                candidate.get("choice_index"), candidate.get("action"),
                _normalized_operation(candidate),
            )
            if isinstance(candidate, dict) else None
            for candidate in producers
        ]
    if surface_kind == "resource_preparation":
        resource_keys = [
            _resource_child_key(option) for option in resource_options
        ]
        expected_keys, resource_parents = _authoritative_resource_children(
            case
        )
        if expected_keys is None or resource_parents is None:
            issues.append("v2_resource_preparation_parent_binding")
        elif any(
            Counter(keys) != Counter(expected_keys)
            for keys in (
                resource_keys, choice_keys, projected_keys, producer_keys,
            )
        ):
            issues.append("v2_resource_preparation_choice_bijection")
        if any(
            not _resource_target_matches_authoritative(row, resource_parents)
            for row in [*resource_options, *choices, *projected]
        ):
            issues.append("v2_resource_preparation_parent_binding")
    if None in choice_keys or (
        surface_kind != "resource_preparation"
        and any(
            not isinstance(key[1], str) or not key[1]
            for key in choice_keys if key is not None
        )
    ):
        issues.append("v2_choice_typed_key_invalid")
    if None in producer_keys or (
        surface_kind != "resource_preparation"
        and any(
            not isinstance(key[1], str) or not key[1]
            for key in producer_keys if key is not None
        )
    ):
        issues.append("v2_producer_typed_key_invalid")
    if any(count != 1 for count in Counter(choice_keys).values()):
        issues.append("v2_choice_typed_key_duplicate")
    if any(count != 1 for count in Counter(producer_keys).values()):
        issues.append("v2_producer_typed_key_duplicate")
    if Counter(choice_keys) != Counter(producer_keys):
        issues.append("v2_producer_choice_typed_bijection")
    for row, key in zip(choices, choice_keys):
        if key is None:
            continue
        operation = _normalized_operation(row)
        if (
            "operation" not in row
            or (
                surface_kind == "resource_preparation"
                and (
                    row.get("action") != "potion"
                    or operation not in {"use", "discard"}
                )
            )
            or (
                surface_kind != "resource_preparation"
                and key[1] == "potion"
                and operation not in {"use", "discard"}
            )
            or (
                surface_kind != "resource_preparation"
                and key[1] != "potion"
                and operation is not None
            )
            or row.get("operation") != operation
        ):
            issues.append("v2_choice_operation_binding")
    for row, key in zip(producers, producer_keys):
        if key is None:
            continue
        operation = _normalized_operation(row)
        if (
            "operation" not in row
            or (
                surface_kind == "resource_preparation"
                and (
                    row.get("action") != "potion"
                    or operation not in {"use", "discard"}
                )
            )
            or (
                surface_kind != "resource_preparation"
                and key[1] == "potion"
                and operation not in {"use", "discard"}
            )
            or (
                surface_kind != "resource_preparation"
                and key[1] != "potion"
                and operation is not None
            )
            or row.get("operation") != operation
        ):
            issues.append("v2_producer_operation_binding")

    projected_by_id = {
        _identity(candidate): candidate for candidate in projected
        if _identity(candidate) is not None
    }
    if len(projected_by_id) != len(projected):
        issues.append("v2_projected_choice_id_duplicate")
    producer_by_key = {
        key: (index, candidate)
        for index, (key, candidate) in enumerate(zip(producer_keys, producers))
    }
    copy_fields = (
        "choice_index", "action", "operation", "legal", "visible",
        "selection_eligible", "veto_reason", "label", "raw_text",
        "semantic_id", "target", "consequences", "probability_outcomes",
        "model_score", "model_confidence", "model_evidence_status",
        "model_evidence_reason", "final_source", "override", "uncertainty",
        "local_reason", "reason_codes", "score_rule_id", "score_formula",
        "score_inputs", "score_components",
    )
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        choice_id = _identity(choice)
        candidate = projected_by_id.get(choice_id)
        if candidate is None:
            issues.append("v2_projected_choice_binding")
            continue
        key = (
            _resource_child_key(choice)
            if surface_kind == "resource_preparation"
            else (
                choice.get("choice_index"), choice.get("action"),
                _normalized_operation(choice),
            )
        )
        producer_entry = producer_by_key.get(key)
        if producer_entry is None:
            issues.append("v2_producer_choice_typed_binding")
            continue
        producer_index, producer = producer_entry
        producer_id = _producer_identity(producer)
        if (
            candidate.get("producer_candidate_index") != producer_index
            or candidate.get("producer_candidate_id") != producer_id
        ):
            issues.append("v2_projected_producer_binding")
        if producer.get("semantic_id") != choice.get("semantic_id"):
            issues.append("v2_producer_semantic_binding")
        if not _producer_consequence_target_matches(choice, producer):
            issues.append("v2_producer_consequence_target_binding")
        for problem in _raw_choice_target_binding_problems(case, choice):
            issues.append(f"v2_{problem}")

        expected_consequence = (
            _independent_oracle._expected_visible_consequence(
                case.get("phase"), choice, _case_oracle_record(case)
            )
        )
        consequence_record = _case_oracle_record(case)
        consequence_record["_raw_target"] = choice.get("target")
        legacy_golden_wing_projection = (
            _independent_oracle._legacy_golden_wing_terminal_projection(
                case.get("phase"), choice, choice.get("consequences"),
                expected_consequence, consequence_record,
            )
        )
        consequence_clear, consequence_mismatches = (
            _independent_oracle._consequence_matches_visible(
                choice.get("consequences"), expected_consequence,
                consequence_record,
            )
        )
        if not consequence_clear:
            issues.extend(
                f"v2_independent_consequence_mismatch:{field}"
                for field in consequence_mismatches
            )
        issues.extend(
            f"v2_independent_consequence_mismatch:{field}"
            for field in _unproven_consequence_payload_mismatches(
                choice.get("consequences"), expected_consequence
            )
            if not legacy_golden_wing_projection
        )
        claim_issues, claim_unknowns, producer_partition = (
            _producer_effect_claim_problems(
                case, choice, producer, expected_consequence
            )
        )
        issues.extend(f"v2_{value}" for value in claim_issues)
        unknowns.extend(f"v2_{value}" for value in claim_unknowns)
        for field in copy_fields:
            if candidate.get(field) != choice.get(field):
                issues.append(f"v2_projected_{field}_binding")
        for field, wanted in producer_partition.items():
            if candidate.get(field) != wanted:
                issues.append(f"v2_projected_{field}_recompute")
        producer_score = producer.get("score", producer.get("local_score"))
        if not _numeric(producer_score):
            unknowns.append("v2_producer_local_score_missing")
        elif not _numeric(candidate.get("local_score")) or not math.isclose(
            float(candidate["local_score"]), float(producer_score),
            rel_tol=0.0, abs_tol=1e-9,
        ):
            issues.append("v2_projected_local_score_binding")
        score_issues, score_unknowns = _recompute_local_score(
            candidate, prefix="v2_projected_candidate"
        )
        issues.extend(score_issues)
        unknowns.extend(score_unknowns)

    join = case.get("candidate_join")
    if not isinstance(join, dict) or join.get("status") != "clear":
        issues.append("v2_candidate_join_not_clear")
    else:
        if (
            join.get("canonical_choice_count") != len(choices)
            or join.get("producer_candidate_count") != len(producers)
            or any(join.get(field) not in ([], None) for field in (
                "missing_choice_indexes", "ambiguous_choice_indexes",
                "unmatched_candidate_indexes", "ambiguous_candidate_indexes",
                "semantic_mismatch_choice_indexes",
                "consequence_target_mismatch_choice_indexes",
                "producer_raw_binding_mismatch_choice_indexes",
                "unclassified_producer_choice_indexes",
                "operation_missing_choice_indexes",
                "operation_missing_candidate_indexes",
            ))
        ):
            issues.append("v2_candidate_join_evidence_mismatch")
    candidate_contract = case.get("candidate_contract")
    if not isinstance(candidate_contract, dict):
        issues.append("v2_candidate_contract_missing")
    elif (
        candidate_contract.get("strategy_quality_auditable") is not True
        or candidate_contract.get("strict_identity_join_complete") is not True
        or candidate_contract.get("independent_candidate_evidence_complete")
        is not True
    ):
        issues.append("v2_candidate_contract_not_auditable")
    settlement_issues, settlement_unknowns = _settlement_contract_problems(
        case
    )
    issues.extend(f"v2_{value}" for value in settlement_issues)
    unknowns.extend(f"v2_{value}" for value in settlement_unknowns)
    return issues, unknowns


def _independent_blind_review(candidate_by_id, expected_ids):
    """Return a consequence-only recommendation without producer choice data."""

    review = {
        "review_version": "decision-case-independent-consequence-v1",
        "status": "unknown",
        "recommended_choice_id": None,
        "candidate_choice_ids": sorted(str(value) for value in expected_ids),
        "excluded_fields": [
            "selected", "final_choice_ids", "local_score", "score",
            "model_score", "producer independent_blind_review",
        ],
    }
    if Counter(candidate_by_id.keys()) != Counter(expected_ids):
        review["reason"] = "candidate_surface_incomplete"
        return review
    if len(expected_ids) == 1:
        review.update({
            "status": "clear",
            "recommended_choice_id": expected_ids[0],
            "reason": "only_semantic_candidate",
        })
        return review

    vectors = {}
    signatures = {}
    for choice_id in expected_ids:
        row = candidate_by_id.get(choice_id)
        consequences = row.get("consequences") if isinstance(row, dict) else None
        if not isinstance(consequences, dict) or not consequences:
            review["reason"] = "structured_consequence_missing"
            return review
        if any(consequences.get(field) for field in (
            "probabilistic_outcomes", "probability_outcomes", "uncertainty",
        )) or row.get("probability_outcomes") or row.get("uncertainty"):
            review["reason"] = "probabilistic_or_uncertain_consequence"
            return review
        numeric = [consequences.get(field) for field in _RESOURCE_FIELDS]
        current_cost = consequences.get("current_cost")
        costs = [
            current_cost.get(field) if isinstance(current_cost, dict) else None
            for field in _COST_FIELDS
        ]
        if not all(_numeric(value) for value in numeric + costs):
            review["reason"] = "deterministic_resource_vector_incomplete"
            return review
        vectors[choice_id] = tuple(
            [float(value) for value in numeric]
            + [-float(value) for value in costs]
        )
        signatures[choice_id] = _freeze({
            key: value for key, value in consequences.items()
            if key not in set(_RESOURCE_FIELDS) | {
                "schema_version", "current_cost", "raw_effect_text",
                "event_id", "producer_evidence",
            }
        })
    if len(set(signatures.values())) != 1:
        review["reason"] = "non_resource_consequences_incomparable"
        return review

    dominant = []
    for choice_id, vector in vectors.items():
        others = [
            other for other_id, other in vectors.items() if other_id != choice_id
        ]
        if all(
            all(left >= right for left, right in zip(vector, other))
            and any(left > right for left, right in zip(vector, other))
            for other in others
        ):
            dominant.append(choice_id)
    if len(dominant) == 1:
        review.update({
            "status": "clear",
            "recommended_choice_id": dominant[0],
            "reason": "unique_deterministic_resource_dominance",
        })
    else:
        review["reason"] = "no_unique_deterministic_dominant_choice"
    return review


def _strict_bowl_over_return_tie(
    candidate_by_id, tied, selection_count, *, case=None,
):
    """Resolve only the typed Singing Bowl versus plain-return boundary.

    The producer intentionally scores both at the deck-preservation hurdle,
    but Bowl also has deterministic +2 current- and max-HP consequences.  This is not a
    generic tie breaker: every identity and immediate consequence must prove
    Bowl strictly dominates the no-op return, otherwise the tie stays
    blocking.
    """

    if (
        not isinstance(case, dict)
        or str(case.get("phase") or "").upper() != "CARD_REWARD"
        or selection_count != 1
        or len(tied) != 2
    ):
        return None
    rows = [candidate_by_id.get(choice_id) for choice_id in tied]
    if any(not isinstance(row, dict) for row in rows):
        return None
    bowl = next((
        row for row in rows
        if str((row.get("target") or {}).get("kind") or "").casefold()
        == "bowl"
    ), None)
    returned = next((
        row for row in rows
        if row.get("choice_id") == "action:return"
        and str(row.get("action") or "").casefold() == "return"
        and str((row.get("target") or {}).get("kind") or "").casefold()
        == "protocol_action"
        and str((row.get("target") or {}).get("action") or "").casefold()
        == "return"
    ), None)
    if bowl is None or returned is None or bowl is returned:
        return None
    bowl_id = bowl.get("choice_id")
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
    if (
        case.get("selected_choice_ids") != [bowl_id]
        or case.get("final_choice_ids") != [bowl_id]
        or settlement.get("status") != "observed"
        or settlement.get("authority") != "protocol_state_delta"
        or settlement.get("fully_observable") is not True
        or settlement.get("choice_id") != bowl_id
        or any(
            observed.get(field) != 2
            for field in ("current_hp_delta", "hp_delta", "max_hp_delta")
        )
        or not all(
            type(game.get(field)) is int
            for game in (before_game, after_game)
            for field in ("current_hp", "max_hp")
        )
        or after_game["current_hp"] - before_game["current_hp"] != 2
        or after_game["max_hp"] - before_game["max_hp"] != 2
    ):
        return None

    def empty_changes(value):
        return (
            isinstance(value, dict)
            and bool(value)
            and all(isinstance(items, list) and not items for items in value.values())
        )

    bowl_effect = bowl.get("consequences")
    return_effect = returned.get("consequences")
    if not isinstance(bowl_effect, dict) or not isinstance(return_effect, dict):
        return None
    common_zero = ("gold_delta",)
    bowl_projection_v2 = (
        type((bowl.get("target") or {}).get("audit_projection_version"))
        is int
        and (bowl.get("target") or {}).get("audit_projection_version") >= 2
    )
    expected_bowl_claimed_hp_delta = 2 if bowl_projection_v2 else 0
    return_future = (
        return_effect.get("future_costs")
        if isinstance(return_effect, dict) else None
    )
    if (
        str(bowl.get("action") or "").casefold() != "choose"
        or bowl.get("selection_eligible") is False
        or returned.get("selection_eligible") is False
        or bowl.get("legal") is not True
        or returned.get("legal") is not True
        or bowl.get("visible") is not True
        or returned.get("visible") is not True
        or bowl_effect.get("operation") != "singing_bowl"
        or return_effect.get("operation") != "return"
        or bowl_effect.get("max_hp_delta") != 2
        or return_effect.get("max_hp_delta") != 0
        or bowl_effect.get("hp_delta") != expected_bowl_claimed_hp_delta
        or return_effect.get("hp_delta") != 0
        or any(bowl_effect.get(field) != 0 for field in common_zero)
        or any(return_effect.get(field) != 0 for field in common_zero)
        or bowl_effect.get("current_cost") != {
            "gold": 0, "hp": 0, "max_hp": 0,
        }
        or return_effect.get("current_cost") != {
            "gold": 0, "hp": 0, "max_hp": 0,
        }
        or not empty_changes(bowl_effect.get("card_changes"))
        or not empty_changes(return_effect.get("card_changes"))
        or not empty_changes(bowl_effect.get("relic_changes"))
        or not empty_changes(return_effect.get("relic_changes"))
        or not empty_changes(bowl_effect.get("potion_changes"))
        or not empty_changes(return_effect.get("potion_changes"))
        or bowl_effect.get("probabilistic_outcomes") != []
        or return_effect.get("probabilistic_outcomes") != []
        or bowl_effect.get("future_costs") != []
        or not isinstance(return_future, list)
        or len(return_future) != 1
        or not isinstance(return_future[0], dict)
        or return_future[0].get("kind") != "foregone_visible_option_ids"
        or not isinstance(return_future[0].get("choice_ids"), list)
        or bowl.get("choice_id") not in return_future[0]["choice_ids"]
        or bowl_effect.get("curse") != {
            "gain": [], "remove": [], "probability": 0.0,
            "omamori_applicable": False, "omamori_charges_consumed": 0,
        }
        or return_effect.get("curse") != bowl_effect.get("curse")
    ):
        return None
    return bowl.get("choice_id")


def _score_selection(candidate_by_id, selection_count, *, case=None):
    if not isinstance(selection_count, int) or isinstance(selection_count, bool):
        return None, "selection_count_invalid"
    eligible = {
        choice_id: candidate
        for choice_id, candidate in candidate_by_id.items()
        if candidate.get("selection_eligible") is not False
    }
    if selection_count < 1 or selection_count > len(eligible):
        return None, "selection_count_out_of_range"
    scored = []
    for choice_id, candidate in eligible.items():
        score = candidate.get("score", candidate.get("local_score"))
        if not _numeric(score):
            return None, "candidate_local_score_missing"
        scored.append((choice_id, float(score)))
    ranked = sorted(scored, key=lambda item: (-item[1], item[0]))
    cutoff = ranked[selection_count - 1][1]
    above = [choice_id for choice_id, score in ranked if score > cutoff]
    tied = [choice_id for choice_id, score in ranked if score == cutoff]
    required_from_tie = selection_count - len(above)
    if required_from_tie != len(tied):
        bowl = _strict_bowl_over_return_tie(
            candidate_by_id, tied, required_from_tie, case=case
        )
        if bowl is not None:
            return sorted(above + [bowl]), None
        return None, "score_cutoff_tie_is_semantically_ambiguous"
    return sorted(above + tied), None


def _semantic_reselection(candidate_by_id, selection_count, *, case=None):
    eligible = {
        choice_id: candidate
        for choice_id, candidate in candidate_by_id.items()
        if candidate.get("selection_eligible") is not False
    }
    expected_ids = sorted(eligible)
    blind = _independent_blind_review(eligible, expected_ids)
    if selection_count == 1 and blind.get("status") == "clear":
        return [blind["recommended_choice_id"]], None, blind
    selected, reason = _score_selection(
        eligible, selection_count, case=case
    )
    return selected, reason, blind


def _canonical_surface(choices, candidates, *, schema_v2=False, case=None):
    issues = []
    unknowns = []
    if not isinstance(choices, list) or not choices:
        return None, None, ["canonical_choices_missing"], None
    if not isinstance(candidates, list) or not candidates:
        return None, None, ["candidates_missing"], None
    choice_ids = [_identity(choice) for choice in choices]
    candidate_ids = [_identity(candidate) for candidate in candidates]
    if any(value is None for value in choice_ids):
        issues.append("choice_id_missing")
    if any(value is None for value in candidate_ids):
        issues.append("candidate_choice_id_missing")
    if any(count > 1 for count in Counter(choice_ids).values()):
        issues.append("duplicate_choice_id")
    if any(count > 1 for count in Counter(candidate_ids).values()):
        issues.append("duplicate_candidate_choice_id")
    if Counter(choice_ids) != Counter(candidate_ids):
        issues.append("candidate_choice_bijection")
    if issues:
        return None, None, issues, None

    choice_by_id = {choice_id: row for choice_id, row in zip(choice_ids, choices)}
    candidate_by_id = {
        choice_id: row for choice_id, row in zip(candidate_ids, candidates)
    }
    for choice_id in sorted(choice_by_id):
        choice = choice_by_id[choice_id]
        candidate = candidate_by_id[choice_id]
        required_choice_fields = (
            _CANONICAL_CHOICE_FIELDS
            if schema_v2 else _LEGACY_CANONICAL_CHOICE_FIELDS
        )
        for field in required_choice_fields:
            if field not in choice:
                unknowns.append(f"canonical_choice_{field}_missing")
        if (
            "choice_schema_version" in choice
            and choice.get("choice_schema_version") != 1
        ):
            issues.append("canonical_choice_schema_version")
        if choice.get("legal") is not True or choice.get("visible") is not True:
            issues.append("canonical_choice_not_protocol_legal")
        if not isinstance(choice.get("selection_eligible"), bool):
            unknowns.append("canonical_choice_selection_eligible_invalid")
        elif (
            choice.get("selection_eligible") is False
            and not isinstance(choice.get("veto_reason"), str)
        ):
            unknowns.append("canonical_choice_veto_reason_missing")
        if "selection_eligible" in candidate:
            if not isinstance(candidate.get("selection_eligible"), bool):
                unknowns.append("candidate_selection_eligible_invalid")
            elif candidate.get("selection_eligible") != choice.get(
                "selection_eligible"
            ):
                issues.append("candidate_selection_eligible_binding")
            if (
                "veto_reason" in candidate or "veto_reason" in choice
            ) and candidate.get("veto_reason") != choice.get("veto_reason"):
                issues.append("candidate_veto_reason_binding")
        elif schema_v2:
            unknowns.append("candidate_selection_eligible_invalid")
        if not isinstance(choice.get("target"), dict):
            unknowns.append("canonical_choice_target_invalid")
        if not isinstance(choice.get("probability_outcomes"), list):
            unknowns.append("canonical_choice_probability_outcomes_invalid")
        if not isinstance(choice.get("override"), dict):
            unknowns.append("canonical_choice_override_invalid")
        if not (
            isinstance(choice.get("final_source"), str)
            and choice["final_source"].strip()
        ):
            unknowns.append("canonical_choice_final_source_invalid")
        if not (
            isinstance(choice.get("raw_text"), str)
            and choice["raw_text"].strip()
        ):
            unknowns.append("canonical_choice_raw_text_invalid")
        if schema_v2 and not (
            isinstance(choice.get("local_reason"), str)
            and choice["local_reason"].strip()
            and isinstance(choice.get("reason_codes"), list)
            and choice["reason_codes"]
            and all(
                isinstance(code, str) and code.strip()
                for code in choice["reason_codes"]
            )
        ):
            unknowns.append("canonical_choice_local_reason_invalid")
        if schema_v2 and choice.get("candidate_binding") != "unique":
            issues.append("canonical_choice_candidate_binding")
        if schema_v2 and (not isinstance(choice.get("candidate_ids"), list) or not choice.get(
            "candidate_ids"
        )):
            unknowns.append("canonical_choice_candidate_ids_invalid")
        if schema_v2 and not (
            isinstance(choice.get("model_evidence_status"), str)
            and choice["model_evidence_status"].strip()
            and isinstance(choice.get("model_evidence_reason"), str)
            and choice["model_evidence_reason"].strip()
        ):
            unknowns.append("canonical_choice_model_evidence_invalid")
        if (
            choice.get("model_confidence") is not None
            and not _numeric(choice.get("model_confidence"))
        ):
            unknowns.append("canonical_choice_model_confidence_invalid")
        for field in (
            _V2_SEMANTIC_FIELDS if schema_v2 else _SEMANTIC_FIELDS
        ):
            if field not in choice or field not in candidate:
                unknowns.append(f"candidate_semantic_{field}_missing")
            elif (
                type(choice.get(field)) is not type(candidate.get(field))
                or choice.get(field) != candidate.get(field)
            ):
                issues.append(f"candidate_semantic_{field}")
        left = choice.get("consequences")
        right = candidate.get("consequences")
        if not isinstance(left, dict) or not left:
            unknowns.append("choice_consequences_missing")
        if not isinstance(right, dict) or not right:
            unknowns.append("candidate_consequences_missing")
        if isinstance(left, dict) and left and isinstance(right, dict) and right:
            unknowns.extend(_structured_consequence_unknowns(
                "choice_consequence", left, case=case, row=choice
            ))
            unknowns.extend(_structured_consequence_unknowns(
                "candidate_consequence", right, case=case, row=candidate
            ))
            if "schema_version" in left and left.get("schema_version") != 1:
                issues.append("choice_consequence_schema_version")
            if "schema_version" in right and right.get("schema_version") != 1:
                issues.append("candidate_consequence_schema_version")
            if left != right:
                issues.append("candidate_consequence_binding")
            if (
                "probabilistic_outcomes" in left
                and choice.get("probability_outcomes")
                != left.get("probabilistic_outcomes")
            ):
                issues.append("canonical_probability_binding")
            uncertainty = left.get("uncertainty")
            expected_uncertainty = (
                "; ".join(str(value) for value in uncertainty)
                if isinstance(uncertainty, list) and uncertainty else None
            )
            if (
                "uncertainty" in left
                and choice.get("uncertainty") != expected_uncertainty
            ):
                issues.append("canonical_uncertainty_binding")
        candidate_score = candidate.get("score", candidate.get("local_score"))
        if not _numeric(candidate_score):
            unknowns.append("candidate_local_score_missing")
        if not _numeric(choice.get("local_score")):
            unknowns.append("canonical_choice_local_score_missing")
        elif _numeric(candidate_score) and not math.isclose(
            float(choice["local_score"]), float(candidate_score),
            rel_tol=0.0, abs_tol=1e-9,
        ):
            issues.append("candidate_local_score_binding")
    return choice_by_id, candidate_by_id, issues, unknowns


def _resource_preparation_receipt_binds_selected_child(
    case, selected_ids, choice_by_id,
):
    """Bind a bridge potion receipt to its typed use/discard child choice."""

    if not (
        isinstance(case, dict)
        and case.get("decision_surface_kind") == "resource_preparation"
        and case.get("parent_choice_surface_pending") is True
        and str(case.get("phase") or "").upper()
        in {"COMBAT_REWARD", "SHOP_SCREEN"}
        and str(case.get("action") or "").casefold() == "potion"
        and isinstance(selected_ids, list)
        and len(selected_ids) == 1
        and isinstance(choice_by_id, dict)
    ):
        return False
    selected_id = selected_ids[0]
    selected = choice_by_id.get(selected_id)
    target = selected.get("target") if isinstance(selected, dict) else None
    if not isinstance(target, dict):
        return False
    instance_id = str(target.get("potion_instance_id") or "")
    potion_id = str(target.get("potion_id") or "")
    operation = str(target.get("operation") or "").casefold()
    slot = target.get("slot")
    if (
        target.get("kind") != "potion_resource"
        or operation not in {"use", "discard"}
        or str(selected.get("operation") or "").casefold() != operation
        or not instance_id
        or not potion_id
        or type(slot) is not int
        or selected_id != f"{instance_id}:{operation}"
    ):
        return False

    chosen = case.get("chosen")
    settlement = case.get("authoritative_choice_settlement")
    expected_semantic = f"potion:{operation}:{slot}:{potion_id}"
    if not (
        case.get("requested_target_id") == instance_id
        and case.get("resolved_target_id") == instance_id
        and isinstance(chosen, dict)
        and chosen.get("requested_target_id") == instance_id
        and chosen.get("resolved_target_id") == instance_id
        and chosen.get("choice_index") == slot
        and chosen.get("semantic_id") == expected_semantic
        and isinstance(settlement, dict)
        and settlement.get("choice_id") == selected_id
        and case.get("final_choice_ids") == [selected_id]
    ):
        return False

    resource_matches = [
        row for row in case.get("resource_preparation_options") or []
        if str(row.get("option_id") or row.get("choice_id") or "")
        == selected_id
    ]
    if len(resource_matches) != 1:
        return False
    resource_target = resource_matches[0].get("target")
    return bool(
        isinstance(resource_target, dict)
        and resource_target.get("kind") == "potion_resource"
        and resource_target.get("potion_instance_id") == instance_id
        and resource_target.get("potion_id") == potion_id
        and resource_target.get("operation") == operation
        and resource_target.get("slot") == slot
    )


def _canonical_case(case, *, schema_v2=False):
    if schema_v2:
        issues, unknowns = _v2_contract_problems(case)
        nonchoice = _v2_forced_singleton_event_nonchoice_classification(
            case,
            case.get("available_options")
            if isinstance(case.get("available_options"), list) else [],
            case.get("candidates")
            if isinstance(case.get("candidates"), list) else [],
        ) or _legacy_nonchoice_classification(
            case,
            case.get("available_options")
            if isinstance(case.get("available_options"), list) else [],
            case.get("producer_candidates")
            if isinstance(case.get("producer_candidates"), list) else [],
        )
        if nonchoice is not None:
            # A DecisionCase proves ranking and container-order behavior.  A
            # protocol transition with no ranking surface remains covered by
            # the attempt trace/oracle, but must not be forced through the
            # strategic candidate contract.  Preserve every envelope/binding
            # failure; only discard fields that are inapplicable to ranking.
            envelope_issues = []
            for issue in issues:
                if issue.startswith("v2_binding_") and issue not in {
                    "v2_binding_reason", "v2_binding_goal_mode",
                }:
                    envelope_issues.append(issue)
                elif issue in {
                    "v2_trace_schema_version", "v2_run_type",
                    "v2_target_binding", "v2_contract_missing",
                    "v2_required_field_contract",
                    "v2_present_field_contract",
                    "v2_missing_value_contract",
                }:
                    envelope_issues.append(issue)
            authority = nonchoice["authority"]
            reason = nonchoice["reason"]
            return envelope_issues, [], None, {
                **nonchoice,
                "classification": "not_applicable",
                "not_applicable_checks": [
                    {
                        "check": "candidate_ranking",
                        "authority": authority,
                        "reason": reason,
                    },
                    {
                        "check": "container_order_reselection",
                        "authority": authority,
                        "reason": reason,
                    },
                ],
            }
    else:
        issues = _legacy_common_problems(case)
        unknowns = []
        nonchoice = _legacy_nonchoice_classification(
            case,
            case.get("available_options")
            if isinstance(case.get("available_options"), list) else [],
            case.get("candidates")
            if isinstance(case.get("candidates"), list) else [],
        )
        if nonchoice is not None:
            authority = nonchoice["authority"]
            reason = nonchoice["reason"]
            return issues, [], None, {
                **nonchoice,
                "classification": "not_applicable",
                "not_applicable_checks": [
                    {
                        "check": "candidate_ranking",
                        "authority": authority,
                        "reason": reason,
                    },
                    {
                        "check": "container_order_reselection",
                        "authority": authority,
                        "reason": reason,
                    },
                ],
            }
    choice_by_id, candidate_by_id, surface_issues, surface_unknowns = (
        _canonical_surface(
            case.get("canonical_choices"), case.get("candidates"),
            schema_v2=schema_v2, case=case,
        )
    )
    issues.extend(surface_issues or [])
    unknowns.extend(surface_unknowns or [])
    if choice_by_id is None or candidate_by_id is None:
        return issues, unknowns, None, {
            "authority": "canonical_choice_schema_v1",
            "reason": "canonical_surface_unavailable",
        }

    selected_ids = sorted(
        choice_id for choice_id, row in choice_by_id.items()
        if row.get("selected") is True
    )
    if not selected_ids:
        issues.append("selected_choice_cardinality")
    elif any(
        choice_by_id[choice_id].get("selection_eligible") is not True
        for choice_id in selected_ids
    ):
        issues.append("selected_choice_ineligible")
    final_ids = case.get("final_choice_ids")
    if final_ids is None:
        chosen = case.get("chosen")
        chosen = chosen if isinstance(chosen, dict) else {}
        final_ids = [chosen.get("requested_target_id")]
    if not isinstance(final_ids, list) or not final_ids:
        issues.append("final_choice_cardinality")
    elif Counter(str(value) for value in final_ids) != Counter(selected_ids):
        issues.append("final_choice_binding")
    chosen = case.get("chosen")
    if not isinstance(chosen, dict):
        issues.append("chosen_binding_missing")
    elif chosen.get("requested_target_id") != chosen.get("resolved_target_id"):
        issues.append("chosen_target_mismatch")
    elif (
        len(selected_ids) == 1
        and chosen.get("requested_target_id") != selected_ids[0]
        and not _resource_preparation_receipt_binds_selected_child(
            case, selected_ids, choice_by_id
        )
    ):
        issues.append("chosen_selected_binding")
    if (
        case.get("decision_surface_kind") == "resource_preparation"
        and len(selected_ids) == 1
        and not _resource_preparation_receipt_binds_selected_child(
            case, selected_ids, choice_by_id
        )
    ):
        issues.append("resource_preparation_receipt_binding")

    semantic_ids = None
    if selected_ids and not surface_unknowns:
        semantic_ids, reason, blind = _semantic_reselection(
            candidate_by_id, len(selected_ids), case=case
        )
        if semantic_ids is None:
            unknowns.append(reason or "semantic_reselection_unresolved")
        elif semantic_ids != selected_ids:
            local_ids, _ = _score_selection(
                candidate_by_id, len(selected_ids), case=case
            )
            if blind.get("status") == "clear":
                issues.append("independent_blind_review_disagreement")
            elif local_ids != selected_ids:
                unknowns.append("below_argmax_without_independent_review")
            else:
                issues.append("independent_reselection_disagreement")

        variants = (
            (list(reversed(list(choice_by_id.values()))), list(candidate_by_id.values())),
            (list(choice_by_id.values()), list(reversed(list(candidate_by_id.values())))),
            (
                list(reversed(list(choice_by_id.values()))),
                list(reversed(list(candidate_by_id.values()))),
            ),
        )
        for choices, candidates in variants:
            _, reordered_candidates, variant_issues, variant_unknowns = (
                _canonical_surface(
                    choices, candidates, schema_v2=schema_v2, case=case
                )
            )
            if variant_issues:
                issues.append("container_order_invariant")
                break
            if variant_unknowns or reordered_candidates is None:
                unknowns.append("container_order_invariant_unproven")
                break
            reordered, reordered_reason, _ = _semantic_reselection(
                reordered_candidates, len(selected_ids), case=case
            )
            if reordered is None:
                unknowns.append(
                    reordered_reason or "container_order_invariant_unproven"
                )
                break
            if semantic_ids is None or reordered != semantic_ids:
                issues.append("container_order_invariant")
                break
    return issues, unknowns, selected_ids or None, {
        "authority": "canonical_choice_schema_v1",
        "reason": "independent semantic bijection and score reselection",
        "producer_blind_review_ignored": "independent_blind_review" in case,
    }


def _chosen_option_identity(case, options):
    chosen = case.get("chosen")
    if not isinstance(chosen, dict):
        return None, "chosen_binding_missing"
    requested = chosen.get("requested_target_id")
    resolved = chosen.get("resolved_target_id")
    if requested != resolved:
        return None, "chosen_target_mismatch"
    matches = [
        _option_identity(option) for option in options
        if _aliases(requested) & _option_aliases(option)
    ]
    matches = [value for value in matches if value is not None]
    if len(matches) != 1:
        return None, "chosen_option_binding_unproven"
    return matches[0], None


def _legacy_nonchoice_classification(case, options, candidates):
    action = str(case.get("action") or "").casefold()
    chosen = case.get("chosen")
    chosen = chosen if isinstance(chosen, dict) else {}
    requested = chosen.get("requested_target_id")
    if (
        not options and not candidates and action in _NONCHOICE_ACTIONS
        and requested == chosen.get("resolved_target_id")
        and requested == f"action:{action}"
    ):
        return {
            "authority": "persisted action and requested/resolved target binding",
            "reason": "non_choice_protocol_transition",
        }
    if isinstance(options, list) and len(options) == 1 and not candidates:
        chosen_id, problem = _chosen_option_identity(case, options)
        if problem is None and chosen_id is not None:
            return {
                "authority": "single raw visible protocol option plus exact target binding",
                "reason": "single_protocol_visible_choice",
            }
    if (
        str(case.get("phase") or "").upper() == "COMBAT_REWARD"
        and isinstance(options, list) and options
        and all(
            isinstance(option, dict)
            and isinstance(option.get("target"), dict)
            and option["target"].get("kind") == "reward"
            and str(option["target"].get("reward_type") or "").upper()
            != "SAPPHIRE_KEY"
            for option in options
        )
    ):
        identities = [_option_identity(option) for option in options]
        chosen_id, problem = _chosen_option_identity(case, options)
        if (
            problem is None and chosen_id is not None
            and None not in identities
            and len(set(identities)) == len(identities)
        ):
            return {
                "authority": "raw COMBAT_REWARD target.kind=reward surface and exact target binding",
                "reason": "independent_collectible_rewards_not_mutually_exclusive",
            }
    return None


def _v2_forced_singleton_event_nonchoice_classification(
    case, options, candidates
):
    """Classify the one observed forced Wheel transition as non-ranking.

    This deliberately does *not* classify the event consequence as known.  It
    only says that a record with exactly one protocol-visible legal event
    target has no alternative for DecisionCase's candidate-ranking replay.
    The narrow event-id check keeps other one-button event screens fail-closed
    until they have their own typed mechanism contract.
    """

    if (
        str(case.get("phase") or "").upper() != "EVENT"
        or str(case.get("action") or "").casefold() != "choose"
        or not isinstance(options, list)
        or not isinstance(candidates, list)
        or len(options) != 1
        or len(candidates) != 1
    ):
        return None
    choices = case.get("canonical_choices")
    producers = case.get("producer_candidates")
    if (
        not isinstance(choices, list)
        or not isinstance(producers, list)
        or len(choices) != 1
        or len(producers) != 1
        or case.get("decision_surface_kind") != "strategic_choice"
        or case.get("parent_choice_surface_pending") is not False
        or case.get("resource_preparation_options") != []
    ):
        return None

    option = options[0]
    choice = choices[0]
    candidate = candidates[0]
    producer = producers[0]
    if not all(isinstance(value, dict) for value in (
        option, choice, candidate, producer,
    )):
        return None
    target = option.get("target")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "event_option"
        or target.get("event_id") != "Wheel of Change"
    ):
        return None

    option_id = _option_identity(option)
    choice_id = _identity(choice)
    producer_id = _producer_identity(producer)
    chosen = case.get("chosen")
    if (
        option_id is None
        or choice_id != option_id
        or producer_id is None
        or not isinstance(chosen, dict)
        or case.get("requested_target_id") != option_id
        or case.get("resolved_target_id") != option_id
        or chosen.get("requested_target_id") != option_id
        or chosen.get("resolved_target_id") != option_id
        or case.get("final_choice_ids") != [option_id]
        or choice.get("selected") is not True
        or choice.get("candidate_binding") != "unique"
        or choice.get("candidate_ids") != [producer_id]
        or candidate.get("choice_id") != option_id
        or candidate.get("producer_candidate_index") != 0
        or candidate.get("producer_candidate_id") != producer_id
        or choice.get("producer_candidate_raw") != producer
        or candidate.get("producer_candidate_raw") != producer
    ):
        return None

    expected_index = option.get("choice_index")
    if (
        type(expected_index) is not int
        or chosen.get("choice_index") != expected_index
        or any(row.get("choice_index") != expected_index for row in (
            choice, candidate, producer,
        ))
        or any(row.get("action") != "choose" for row in (
            choice, candidate, producer,
        ))
        or any(row.get("operation") is not None for row in (
            choice, candidate, producer,
        ))
        or choice.get("target") != target
        or candidate.get("target") != target
        or choice.get("semantic_id") != producer.get("semantic_id")
        or candidate.get("semantic_id") != choice.get("semantic_id")
        or any(row.get("legal") is not True for row in (
            choice, candidate, producer,
        ))
        or any(row.get("visible") is not True for row in (
            choice, candidate, producer,
        ))
        or any(row.get("selection_eligible") is not True for row in (
            choice, candidate, producer,
        ))
    ):
        return None
    return {
        "authority": (
            "Wheel of Change singleton raw event option plus exact "
            "v2 producer/canonical/requested/resolved binding"
        ),
        "reason": "forced_singleton_wheel_of_change_transition",
    }


def _legacy_surface(options, candidates):
    issues = []
    unknowns = []
    if not isinstance(options, list) or not options:
        return None, issues, ["legacy_raw_options_missing"]
    if not isinstance(candidates, list) or not candidates:
        return None, issues, ["legacy_candidates_missing"]
    option_ids = [_option_identity(option) for option in options]
    if any(value is None for value in option_ids):
        unknowns.append("legacy_option_stable_identity_missing")
    if any(count > 1 for count in Counter(option_ids).values()):
        issues.append("legacy_duplicate_option_identity")
    if len(candidates) != len(options):
        issues.append("legacy_candidate_choice_bijection")

    matches = {}
    for index, candidate in enumerate(candidates):
        candidate_id = _candidate_identity(candidate)
        if candidate_id is None:
            unknowns.append("legacy_candidate_identity_missing")
            continue
        matching = [
            option_id for option_id, option in zip(option_ids, options)
            if option_id is not None
            and _candidate_aliases(candidate) & _option_aliases(option)
        ]
        if not matching:
            unknowns.append("legacy_candidate_option_binding_missing")
        elif len(matching) > 1:
            unknowns.append("legacy_candidate_option_binding_ambiguous")
        else:
            matches.setdefault(matching[0], []).append((index, candidate))
    if any(len(rows) > 1 for rows in matches.values()):
        issues.append("legacy_duplicate_candidate_binding")
    if set(matches) != set(value for value in option_ids if value is not None):
        unknowns.append("legacy_visible_option_coverage_unproven")
    if issues or unknowns:
        return None, issues, unknowns
    candidate_by_id = {
        option_id: {
            **candidate,
            "choice_id": option_id,
        }
        for option_id, rows in matches.items()
        for _index, candidate in rows
    }
    for candidate in candidate_by_id.values():
        if not _numeric(candidate.get("score", candidate.get("local_score"))):
            unknowns.append("candidate_local_score_missing")
    return candidate_by_id, issues, unknowns


def _legacy_case(case):
    issues = _legacy_common_problems(case)
    unknowns = []
    options = case.get("available_options")
    candidates = case.get("candidates")
    classification = _legacy_nonchoice_classification(
        case,
        options if isinstance(options, list) else [],
        candidates if isinstance(candidates, list) else [],
    )
    if classification is not None:
        authority = classification["authority"]
        reason = classification["reason"]
        return issues, unknowns, None, {
            **classification,
            "classification": "not_applicable",
            "not_applicable_checks": [
                {
                    "check": "candidate_ranking",
                    "authority": authority,
                    "reason": reason,
                },
                {
                    "check": "container_order_reselection",
                    "authority": authority,
                    "reason": reason,
                },
            ],
        }

    candidate_by_id, surface_issues, surface_unknowns = _legacy_surface(
        options, candidates
    )
    issues.extend(surface_issues)
    unknowns.extend(surface_unknowns)
    if candidate_by_id is None:
        return issues, unknowns, None, {
            "authority": "precanonical case raw available_options/candidates",
            "reason": "legacy semantic surface could not be independently bound",
            "classification": "unresolved",
        }

    selected_id, selected_problem = _chosen_option_identity(case, options)
    if selected_problem == "chosen_target_mismatch":
        issues.append(selected_problem)
    elif selected_problem is not None:
        unknowns.append(selected_problem)
    semantic_ids, selection_reason, blind = _semantic_reselection(
        candidate_by_id, 1, case=case
    )
    if semantic_ids is None:
        unknowns.append(selection_reason or "semantic_reselection_unresolved")
    elif selected_id is not None and semantic_ids != [selected_id]:
        local_ids, _ = _score_selection(candidate_by_id, 1, case=case)
        if blind.get("status") == "clear":
            issues.append("independent_blind_review_disagreement")
        elif local_ids != [selected_id]:
            unknowns.append("below_argmax_without_independent_review")
        else:
            issues.append("independent_reselection_disagreement")

    model = case.get("model_advice")
    final_ids = model.get("final_choice_ids") if isinstance(model, dict) else None
    if isinstance(final_ids, list) and final_ids:
        bound = []
        for final_id in final_ids:
            matches = [
                _option_identity(option) for option in options
                if _aliases(final_id) & _option_aliases(option)
            ]
            matches = [value for value in matches if value is not None]
            if len(matches) != 1:
                unknowns.append("legacy_final_choice_binding_unproven")
                bound = []
                break
            bound.append(matches[0])
        if bound and (len(bound) != 1 or bound[0] != selected_id):
            issues.append("final_choice_binding")

    baseline = semantic_ids
    for reordered_options, reordered_candidates in (
        (list(reversed(options)), list(candidates)),
        (list(options), list(reversed(candidates))),
        (list(reversed(options)), list(reversed(candidates))),
    ):
        reordered_by_id, variant_issues, variant_unknowns = _legacy_surface(
            reordered_options, reordered_candidates
        )
        if variant_issues:
            issues.append("container_order_invariant")
            break
        if variant_unknowns or reordered_by_id is None:
            unknowns.append("container_order_invariant_unproven")
            break
        reordered, reason, _ = _semantic_reselection(
            reordered_by_id, 1, case=case
        )
        if reordered is None:
            unknowns.append(reason or "container_order_invariant_unproven")
            break
        if baseline is None or reordered != baseline:
            issues.append("container_order_invariant")
            break

    consequence_complete = all(
        isinstance(row.get("consequences"), dict) and row["consequences"]
        for row in candidate_by_id.values()
    )
    return issues, unknowns, [selected_id] if selected_id else None, {
        "authority": "precanonical case raw available_options/candidates",
        "reason": "independent legacy bijection and score reselection",
        "classification": "audited",
        "not_applicable_checks": (
            [] if consequence_complete else [{
                "check": "structured_consequence_replay",
                "authority": "case_schema_version=1 field inventory",
                "reason": "precanonical structured consequences were not recorded",
            }]
        ),
        "producer_blind_review_ignored": "independent_blind_review" in case,
    }


def _audit_one(case, *, historical):
    schema_version = case.get("case_schema_version")
    if schema_version == 2:
        issues, unknowns, selected, evidence = _canonical_case(
            case, schema_v2=True
        )
        mode = "canonical_v2"
    elif isinstance(case.get("canonical_choices"), list) and case.get(
        "canonical_choices"
    ):
        issues, unknowns, selected, evidence = _canonical_case(
            case, schema_v2=False
        )
        mode = "canonical_legacy_v1"
    else:
        issues, unknowns, selected, evidence = _legacy_case(case)
        mode = "precanonical_legacy"
    explicit = evidence.get("classification") if isinstance(evidence, dict) else None
    if issues:
        classification = "audited_issues"
    elif unknowns:
        classification = "unresolved"
    elif explicit == "not_applicable":
        classification = "not_applicable"
    else:
        classification = "audited"
    return {
        "mode": mode,
        "historical": bool(historical),
        "source_decision_hash": case.get("decision_hash"),
        "classification": classification,
        "selected_choice_ids": selected,
        "authority": (evidence or {}).get("authority"),
        "reason": (evidence or {}).get("reason"),
        "not_applicable_checks": (evidence or {}).get(
            "not_applicable_checks", []
        ),
        "producer_blind_review_ignored": bool(
            (evidence or {}).get("producer_blind_review_ignored")
        ),
        "issues": issues,
        "unknowns": unknowns,
    }


def _self_contained_trace_evidence(evidence):
    if not isinstance(evidence, dict):
        return False
    return (
        evidence.get("schema_version") == 2
        and evidence.get("evidence_mode") == "embedded_raw_lines_v1"
    ) or (
        evidence.get("schema_version") == 3
        and evidence.get("evidence_mode")
        == "multi_source_embedded_gzip_v1"
    )


def audit_cases(
    cases,
    target_hash,
    *,
    fixture_cases,
    resolution_document=None,
    resolution_fixture_catalog=None,
    trace_evidence=None,
    trace_path=None,
    verify_trace_prefix=True,
    case_corpus_evidence=None,
):
    if not isinstance(target_hash, str) or not target_hash.strip():
        raise ReplayError("target_hash is missing")
    cases = list(cases or [])
    corpus_digest = _case_corpus_digest(cases)
    if case_corpus_evidence is None:
        case_corpus_evidence = {
            "mode": "in_memory_cases_v1",
            "manifest_sha256": None,
            "source_manifest_sha256": None,
            "source_count": 1,
            "case_count": len(cases),
            "case_corpus_sha256": corpus_digest,
        }
    if (
        not isinstance(case_corpus_evidence, dict)
        or case_corpus_evidence.get("case_count") != len(cases)
        or case_corpus_evidence.get("case_corpus_sha256") != corpus_digest
        or type(case_corpus_evidence.get("source_count")) is not int
        or case_corpus_evidence["source_count"] <= 0
    ):
        raise ReplayError("DecisionCase corpus evidence is invalid")
    fixture_cases = list(fixture_cases or [])
    issues = []
    unknowns = []
    phase_counts = Counter()
    case_results = []
    current_count = 0
    historical_by_hash = Counter()
    for index, case in enumerate(cases):
        historical = case.get("decision_hash") != target_hash
        if historical:
            historical_by_hash[str(case.get("decision_hash") or "missing_hash")] += 1
        else:
            current_count += 1
        result = _audit_one(case, historical=historical)
        result.update({
            "case": index,
            "attempt_id": case.get("attempt_id"),
            "before_seq": case.get("before_seq"),
            "phase": case.get("phase"),
        })
        case_results.append(result)

    resolution_evidence = {
        "authority_version": None,
        "target_decision_hash": target_hash,
        "status": "not_applicable",
        "resolved_count": 0,
        "resolution_failure_count": 0,
        "failures": [],
    }
    compact_trace_evidence = _self_contained_trace_evidence(trace_evidence)
    resolution_inputs = (
        resolution_document,
        resolution_fixture_catalog,
        trace_evidence,
    )
    resolution_requested = any(value is not None for value in (
        *resolution_inputs, trace_path,
    ))
    if resolution_requested:
        if any(value is None for value in resolution_inputs) or (
            trace_path is None and not compact_trace_evidence
        ):
            resolution_evidence.update({
                "status": "issues",
                "resolution_failure_count": 1,
                "failures": [{
                    "kind": "decision_case_resolution_inputs_incomplete",
                }],
            })
        else:
            import decision_case_resolution

            case_results, resolution_evidence = (
                decision_case_resolution.apply_resolution_document(
                    cases,
                    case_results,
                    target_decision_hash=target_hash,
                    resolution_document=resolution_document,
                    fixture_catalog=resolution_fixture_catalog,
                    trace_evidence=trace_evidence,
                    trace_path=trace_path,
                    audit_one=_audit_one,
                    verify_trace_prefix=verify_trace_prefix,
                )
            )

    for index, (case, result) in enumerate(zip(cases, case_results)):
        issues.extend({
            "case": index,
            "historical": result["historical"],
            "source_decision_hash": case.get("decision_hash"),
            "kind": kind,
        } for kind in result["issues"])
        unknowns.extend({
            "case": index,
            "historical": result["historical"],
            "source_decision_hash": case.get("decision_hash"),
            "kind": kind,
        } for kind in result["unknowns"])
    issues.extend({
        **failure,
        "severity": "P1",
    } for failure in resolution_evidence.get("failures", []))

    fixture_results = []
    for index, case in enumerate(fixture_cases):
        phase = str(case.get("phase") or "").upper()
        phase_counts[phase] += 1
        result = _audit_one(case, historical=False)
        result.update({"fixture": index, "phase": phase})
        fixture_results.append(result)
        issues.extend({
            "fixture": index, "kind": kind
        } for kind in result["issues"])
        unknowns.extend({
            "fixture": index, "kind": kind
        } for kind in result["unknowns"])
    missing_phases = sorted(REQUIRED_FIXTURE_PHASES - set(phase_counts))
    if missing_phases:
        unknowns.append({
            "kind": "fixture_phase_coverage_missing",
            "phases": missing_phases,
        })
    if not cases:
        unknowns.append({
            "kind": "historical_case_corpus_empty",
            "reason": "fixtures cannot self-prove replay of persisted cases",
        })

    historical_results = [row for row in case_results if row["historical"]]
    source_counts = Counter(row["classification"] for row in case_results)
    historical_counts = Counter(
        row["classification"] for row in historical_results
    )
    not_applicable_classification_counts = Counter(
        row["reason"] for row in case_results
        if row["classification"] == "not_applicable"
    )
    historical_not_applicable_classification_counts = Counter(
        row["reason"] for row in historical_results
        if row["classification"] == "not_applicable"
    )
    not_applicable_check_counts = Counter()
    historical_not_applicable_check_counts = Counter()
    for row in case_results:
        for check in row.get("not_applicable_checks", []):
            if isinstance(check, dict) and isinstance(check.get("check"), str):
                not_applicable_check_counts[check["check"]] += 1
                if row["historical"]:
                    historical_not_applicable_check_counts[check["check"]] += 1
    unresolved_case_count = source_counts["unresolved"]
    historical_unresolved_count = historical_counts["unresolved"]
    status = "clear" if not issues and not unknowns else (
        "issues" if issues else "inconclusive"
    )
    return {
        "schema_version": 1,
        "history_coverage_version": HISTORY_COVERAGE_VERSION,
        "generated_at": time.time(),
        "target_decision_hash": target_hash,
        "status": status,
        "release_gate_passed": status == "clear",
        "issue_count": len(issues),
        "eligible_unknown_count": len(unknowns),
        "source_case_count": len(cases),
        "source_case_corpus_sha256": corpus_digest,
        "current_case_count": current_count,
        "historical_case_count": len(historical_results),
        "audited_case_count": source_counts["audited"],
        "classified_not_applicable_count": source_counts["not_applicable"],
        "resolved_case_count": source_counts["resolved"],
        "unresolved_case_count": unresolved_case_count,
        "issue_case_count": source_counts["audited_issues"],
        "historical_audited_count": historical_counts["audited"],
        "historical_classified_not_applicable_count": historical_counts[
            "not_applicable"
        ],
        "historical_resolved_count": historical_counts["resolved"],
        "historical_unresolved_count": historical_unresolved_count,
        "historical_issue_case_count": historical_counts["audited_issues"],
        "not_applicable_classification_counts": dict(sorted(
            not_applicable_classification_counts.items()
        )),
        "historical_not_applicable_classification_counts": dict(sorted(
            historical_not_applicable_classification_counts.items()
        )),
        "not_applicable_check_counts": dict(sorted(
            not_applicable_check_counts.items()
        )),
        "historical_not_applicable_check_counts": dict(sorted(
            historical_not_applicable_check_counts.items()
        )),
        "fixture_case_count": len(fixture_cases),
        "fixture_audited_count": sum(
            row["classification"] == "audited" for row in fixture_results
        ),
        "fixture_phase_counts": dict(sorted(phase_counts.items())),
        "missing_fixture_phases": missing_phases,
        "resolution_evidence": resolution_evidence,
        "legacy_cases": {
            "classification": "independent_per_case_replay_or_explicit_classification",
            "count": len(historical_results),
            "audited_count": historical_counts["audited"],
            "classified_not_applicable_count": historical_counts[
                "not_applicable"
            ],
            "resolved_count": historical_counts["resolved"],
            "unresolved_count": historical_unresolved_count,
            "issue_count": historical_counts["audited_issues"],
            "by_decision_hash": dict(sorted(historical_by_hash.items())),
        },
        "case_results": case_results,
        "fixture_results": fixture_results,
        "issues": issues,
        "eligible_unknowns": unknowns,
    }


def replay_contract_payload(report):
    """Return the exact replay contract, excluding only wall-clock metadata."""

    if not isinstance(report, dict):
        raise ReplayError("DecisionCase replay report is not an object")
    generated_at = report.get("generated_at")
    if (
        not isinstance(generated_at, (int, float))
        or isinstance(generated_at, bool)
        or not math.isfinite(float(generated_at))
        or generated_at <= 0
    ):
        raise ReplayError("DecisionCase replay generated_at is invalid")
    payload = dict(report)
    payload.pop("generated_at", None)
    return payload


def replay_contract_sha256(report):
    payload = replay_contract_payload(report)
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolution_permutation_invariant_is_valid(invariant):
    """Validate the axis-aware production permutation evidence envelope."""

    if not isinstance(invariant, dict):
        return False

    def valid_sha256(value):
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    candidate_count = invariant.get("candidate_count")
    protocol_count = invariant.get("protocol_count")
    if (
        type(candidate_count) is not int
        or candidate_count < 0
        or type(protocol_count) is not int
        or protocol_count < 0
    ):
        return False

    axes = invariant.get("permutation_axes")
    if not isinstance(axes, dict) or set(axes) != {"candidate", "protocol"}:
        return False
    for axis, count in (
        ("candidate", candidate_count),
        ("protocol", protocol_count),
    ):
        evidence = axes.get(axis)
        if not isinstance(evidence, dict) or set(evidence) != {
            "status", "authority", "reason",
        }:
            return False
        if count >= 2:
            expected = {
                "status": "clear",
                "authority": _PERMUTATION_CLEAR_AUTHORITY,
                "reason": _PERMUTATION_CLEAR_REASON,
            }
        else:
            expected = {
                "status": "not_applicable",
                "authority": _PERMUTATION_NOT_APPLICABLE_AUTHORITY,
                "reason": _PERMUTATION_NOT_APPLICABLE_REASON,
            }
        if evidence != expected:
            return False

    expected_kinds = []
    if candidate_count >= 2:
        expected_kinds.append("candidate")
    if protocol_count >= 2:
        expected_kinds.append("protocol")
    if candidate_count >= 2 and protocol_count >= 2:
        expected_kinds.append("candidate+protocol")

    kinds = invariant.get("positive_permutation_kinds")
    digests = invariant.get("positive_permutation_sha256s")
    selections = invariant.get(
        "positive_permutation_selected_choice_ids"
    )
    selected_ids = invariant.get("selected_choice_ids")
    if (
        invariant.get("positive_permutation_count") != len(expected_kinds)
        or kinds != expected_kinds
        or not isinstance(digests, list)
        or len(digests) != len(expected_kinds)
        or any(not valid_sha256(value) for value in digests)
        or len(set(digests)) != len(digests)
        or not valid_sha256(invariant.get("positive_case_sha256"))
        or invariant.get("positive_case_sha256") in set(digests)
        or not isinstance(selected_ids, list)
        or not selected_ids
        or any(
            not isinstance(value, str) or not value.strip()
            for value in selected_ids
        )
        or len(set(selected_ids)) != len(selected_ids)
        or not isinstance(selections, list)
        or len(selections) != len(expected_kinds)
        or any(value != selected_ids for value in selections)
    ):
        return False
    if invariant.get("fixture_id") == (
        "combat-reward-resource-preparation-score-v2"
    ):
        contract = invariant.get(
            "resource_preparation_permutation_contract"
        )
        if (
            not isinstance(contract, dict)
            or contract.get("status") != "clear"
            or contract.get("authority")
            != "fresh_live_producer_per_variant_v1"
            or contract.get("producer_execution_count") != 4
            or contract.get("producer_execution_variants") != [
                "baseline", "candidate", "protocol",
                "candidate+protocol",
            ]
        ):
            return False
        baseline = contract.get("baseline_selection_binding")
        baseline_surface = contract.get("baseline_surface_order")
        baseline_execution = contract.get("baseline_producer_execution")
        execution_nonces = contract.get("producer_execution_nonces")
        execution_digests = contract.get("producer_execution_digests")
        variants = contract.get("production_variants")
        binding_fields = {
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "potion_slot", "bound_new_potion_id",
            "bound_reward_choice_index",
        }
        surface_fields = {
            "held", "held_groups", "resource", "canonical", "candidate", "producer",
            "parent_rewards", "protocol_options",
            "authoritative_held_instance_ids",
        }
        if (
            not isinstance(baseline, dict)
            or set(baseline) != binding_fields
            or baseline.get("choice_id") not in selected_ids
            or any(
                not isinstance(baseline.get(field), str)
                or not baseline.get(field)
                for field in (
                    "choice_id", "operation", "potion_id",
                    "potion_instance_id", "bound_new_potion_id",
                )
            )
            or type(baseline.get("potion_slot")) is not int
            or baseline.get("potion_slot") != 1
            or baseline.get("bound_reward_choice_index") != 0
            or not isinstance(baseline_surface, dict)
            or set(baseline_surface) != surface_fields
            or not isinstance(baseline_execution, dict)
            or set(baseline_execution) != {"variant", "nonce", "digest"}
            or baseline_execution.get("variant") != "baseline"
            or not valid_sha256(baseline_execution.get("nonce"))
            or not valid_sha256(baseline_execution.get("digest"))
            or not isinstance(execution_nonces, list)
            or len(execution_nonces) != 4
            or len(set(execution_nonces)) != 4
            or any(not valid_sha256(value) for value in execution_nonces)
            or execution_nonces[0] != baseline_execution.get("nonce")
            or not isinstance(execution_digests, list)
            or len(execution_digests) != 4
            or len(set(execution_digests)) != 4
            or any(not valid_sha256(value) for value in execution_digests)
            or execution_digests[0] != baseline_execution.get("digest")
            or not isinstance(variants, list)
            or len(variants) != 3
            or contract.get("selected_potion_instance_id")
            != baseline.get("potion_instance_id")
            or contract.get("selected_potion_slot")
            != baseline.get("potion_slot")
            or contract.get("bound_reward_potion_id")
            != baseline.get("bound_new_potion_id")
            or contract.get("parent_choice_surface_pending") is not True
            or contract.get("parent_reward_consumed") is not False
        ):
            return False
        for field in surface_fields - {"held_groups"}:
            values = baseline_surface.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str) or not value
                    for value in values
                )
            ):
                return False
        groups = baseline_surface.get("held_groups")
        if (
            not isinstance(groups, list)
            or not groups
            or any(
                not isinstance(group, list)
                or not group
                or any(
                    not isinstance(value, str) or not value
                    for value in group
                )
                for group in groups
            )
            or [value for group in groups for value in group]
            != baseline_surface["held"]
        ):
            return False
        held_surface_fields = (
            "held", "resource", "canonical", "candidate", "producer",
        )
        baseline_instances = baseline_surface[
            "authoritative_held_instance_ids"
        ]
        if any(
            baseline_surface[field] != baseline_surface["held"]
            for field in held_surface_fields
        ) or baseline_surface["protocol_options"] != baseline_surface[
            "parent_rewards"
        ] or (
            baseline_instances.count(baseline["potion_instance_id"]) != 1
            or baseline_instances.index(baseline["potion_instance_id"])
            != baseline["potion_slot"]
        ):
            return False
        stable_binding_fields = (
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "bound_new_potion_id",
        )
        held_axes = {"candidate", "candidate+protocol"}
        protocol_axes = {"protocol", "candidate+protocol"}
        for index, (kind, variant) in enumerate(zip(expected_kinds, variants)):
            if (
                not isinstance(variant, dict)
                or set(variant) != {
                    "kind", "producer_execution_variant",
                    "producer_execution_nonce", "producer_execution_digest",
                    "selection_binding", "surface_order", "case_sha256",
                }
                or variant.get("kind") != kind
                or variant.get("producer_execution_variant") != kind
                or variant.get("producer_execution_nonce")
                != execution_nonces[index + 1]
                or variant.get("producer_execution_digest")
                != execution_digests[index + 1]
                or variant.get("case_sha256") != digests[index]
            ):
                return False
            binding = variant.get("selection_binding")
            surface = variant.get("surface_order")
            if (
                not isinstance(binding, dict)
                or set(binding) != binding_fields
                or any(
                    binding.get(field) != baseline.get(field)
                    for field in stable_binding_fields
                )
                or type(binding.get("potion_slot")) is not int
                or binding.get("potion_slot") != 1
                or binding.get("bound_reward_choice_index")
                != (1 if kind in protocol_axes else 0)
                or not isinstance(surface, dict)
                or set(surface) != surface_fields
            ):
                return False
            expected_held = (
                [
                    child
                    for group in reversed(
                        baseline_surface["held_groups"]
                    )
                    for child in group
                ]
                if kind in held_axes else baseline_surface["held"]
            )
            expected_protocol = (
                list(reversed(baseline_surface["parent_rewards"]))
                if kind in protocol_axes
                else baseline_surface["parent_rewards"]
            )
            instance_ids = surface.get("authoritative_held_instance_ids")
            if (
                any(
                    surface.get(field) != expected_held
                    for field in held_surface_fields
                )
                or surface.get("parent_rewards") != expected_protocol
                or surface.get("protocol_options") != expected_protocol
                or not isinstance(surface.get("held_groups"), list)
                or [
                    child
                    for group in surface.get("held_groups")
                    for child in group
                ] != surface.get("held")
                or not isinstance(instance_ids, list)
                or instance_ids.count(binding["potion_instance_id"]) != 1
                or instance_ids.index(binding["potion_instance_id"])
                != binding["potion_slot"]
            ):
                return False
    if invariant.get("fixture_id") == "shop-resource-preparation-score-v2":
        contract = invariant.get(
            "shop_resource_preparation_permutation_contract"
        )
        binding_fields = {
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "potion_slot", "bound_new_potion_id",
            "bound_parent_choice_index",
        }
        surface_fields = {
            "held", "held_groups", "authoritative_held_instance_ids",
            "resource", "canonical", "candidate", "producer",
            "parent_potions", "protocol_options",
        }
        if (
            not isinstance(contract, dict)
            or contract.get("status") != "clear"
            or contract.get("authority")
            != "fresh_live_shop_producer_per_variant_v1"
            or contract.get("producer_execution_count") != 4
            or contract.get("producer_execution_variants") != [
                "baseline", "candidate", "protocol", "candidate+protocol",
            ]
        ):
            return False
        baseline = contract.get("baseline_selection_binding")
        surface = contract.get("baseline_surface_order")
        execution = contract.get("baseline_producer_execution")
        nonces = contract.get("producer_execution_nonces")
        executions = contract.get("producer_execution_digests")
        variants = contract.get("production_variants")
        controls = contract.get("typed_parent_negative_controls")
        if (
            not isinstance(baseline, dict) or set(baseline) != binding_fields
            or baseline.get("choice_id") not in selected_ids
            or baseline.get("operation") != "discard"
            or baseline.get("potion_id") != "SpeedPotion"
            or baseline.get("potion_slot") != 1
            or baseline.get("bound_new_potion_id") != "LiquidMemories"
            or baseline.get("bound_parent_choice_index") != 1
            or not isinstance(surface, dict) or set(surface) != surface_fields
            or surface.get("parent_potions")
            != ["LiquidMemories", "LiquidMemories"]
            or surface.get("protocol_options") != surface.get("parent_potions")
            or not isinstance(execution, dict)
            or set(execution) != {"variant", "nonce", "digest"}
            or execution.get("variant") != "baseline"
            or not valid_sha256(execution.get("nonce"))
            or not valid_sha256(execution.get("digest"))
            or not isinstance(nonces, list) or len(nonces) != 4
            or len(set(nonces)) != 4
            or any(not valid_sha256(value) for value in nonces)
            or nonces[0] != execution.get("nonce")
            or not isinstance(executions, list) or len(executions) != 4
            or len(set(executions)) != 4
            or any(not valid_sha256(value) for value in executions)
            or executions[0] != execution.get("digest")
            or not isinstance(variants, list) or len(variants) != 3
            or contract.get("selected_potion_instance_id")
            != baseline.get("potion_instance_id")
            or contract.get("selected_potion_slot") != 1
            or contract.get("bound_parent_potion_id") != "LiquidMemories"
            or contract.get("parent_choice_surface_pending") is not True
            or not isinstance(controls, list) or len(controls) != 2
        ):
            return False
        held_fields = ("held", "resource", "canonical", "candidate", "producer")
        groups = surface.get("held_groups")
        instances = surface.get("authoritative_held_instance_ids")
        if (
            any(
                not isinstance(surface.get(field), list) or not surface[field]
                for field in surface_fields - {"held_groups"}
            )
            or not isinstance(groups, list) or not groups
            or [child for group in groups for child in group] != surface.get("held")
            or any(surface[field] != surface["held"] for field in held_fields)
            or not isinstance(instances, list)
            or instances.count(baseline.get("potion_instance_id")) != 1
            or instances.index(baseline.get("potion_instance_id")) != 1
        ):
            return False
        held_axes = {"candidate", "candidate+protocol"}
        protocol_axes = {"protocol", "candidate+protocol"}
        stable_fields = (
            "choice_id", "operation", "potion_id", "potion_instance_id",
            "bound_new_potion_id",
        )
        for index, (kind, variant) in enumerate(zip(expected_kinds, variants)):
            if (
                not isinstance(variant, dict)
                or set(variant) != {
                    "kind", "producer_execution_variant",
                    "producer_execution_nonce", "producer_execution_digest",
                    "selection_binding", "surface_order", "case_sha256",
                }
                or variant.get("kind") != kind
                or variant.get("producer_execution_variant") != kind
                or variant.get("producer_execution_nonce") != nonces[index + 1]
                or variant.get("producer_execution_digest")
                != executions[index + 1]
                or variant.get("case_sha256") != digests[index]
            ):
                return False
            other_binding = variant.get("selection_binding")
            other_surface = variant.get("surface_order")
            expected_held = (
                [child for group in reversed(groups) for child in group]
                if kind in held_axes else surface["held"]
            )
            expected_parent = (
                list(reversed(surface["parent_potions"]))
                if kind in protocol_axes else surface["parent_potions"]
            )
            if (
                not isinstance(other_binding, dict)
                or set(other_binding) != binding_fields
                or any(
                    other_binding.get(field) != baseline.get(field)
                    for field in stable_fields
                )
                or other_binding.get("potion_slot") != 1
                or other_binding.get("bound_parent_choice_index")
                != (0 if kind in protocol_axes else 1)
                or not isinstance(other_surface, dict)
                or set(other_surface) != surface_fields
                or any(other_surface.get(field) != expected_held for field in held_fields)
                or other_surface.get("parent_potions") != expected_parent
                or other_surface.get("protocol_options") != expected_parent
            ):
                return False
        expected_controls = {
            "missing_bound_parent", "ambiguous_bound_parent",
        }
        if {row.get("kind") for row in controls if isinstance(row, dict)} != expected_controls:
            return False
        for row in controls:
            if (
                not isinstance(row, dict)
                or set(row) != {
                    "kind", "case_sha256", "classification", "problem_kinds",
                }
                or not valid_sha256(row.get("case_sha256"))
                or row.get("classification") not in {"unresolved", "audited_issues"}
                or not isinstance(row.get("problem_kinds"), list)
                or "v2_producer_bound_new_potion_id_not_protocol_visible"
                not in row["problem_kinds"]
            ):
                return False
    elif "shop_resource_preparation_permutation_contract" in invariant:
        return False

    if invariant.get("fixture_id") == "card_reward_bowl_dominates_skip_v1":
        contract = invariant.get("bowl_dominance_contract")
        if (
            not isinstance(contract, dict)
            or set(contract) != {
                "status", "authority", "selected_choice_id", "bowl_score",
                "return_score", "bowl_max_hp_delta", "return_max_hp_delta",
                "observed_current_hp_delta", "observed_max_hp_delta",
                "other_eligible_scores", "all_candidate_score_tie_remains_blocking",
            }
            or contract.get("status") != "clear"
            or contract.get("authority") != "typed_bowl_resource_dominance_v1"
            or contract.get("selected_choice_id") not in selected_ids
            or not isinstance(contract.get("bowl_score"), (int, float))
            or isinstance(contract.get("bowl_score"), bool)
            or contract.get("bowl_score") != contract.get("return_score")
            or contract.get("bowl_max_hp_delta") != 2
            or contract.get("return_max_hp_delta") != 0
            or contract.get("observed_current_hp_delta") != 2
            or contract.get("observed_max_hp_delta") != 2
            or not isinstance(contract.get("other_eligible_scores"), list)
            or not contract["other_eligible_scores"]
            or any(
                not isinstance(score, (int, float)) or isinstance(score, bool)
                or score >= contract["bowl_score"]
                for score in contract["other_eligible_scores"]
            )
            or contract.get("all_candidate_score_tie_remains_blocking") is not True
        ):
            return False
    elif "bowl_dominance_contract" in invariant:
        return False

    if expected_kinds:
        return (
            invariant.get("reordered_case_sha256") == digests[0]
            and valid_sha256(invariant.get("reordered_case_sha256"))
        )
    return invariant.get("reordered_case_sha256") is None


def revalidate_persisted_report(
    root,
    report,
    target_hash,
    *,
    verify_trace_prefix=True,
):
    """Re-run the persisted replay inputs and exactly bind a supplied report.

    This is deliberately a build-time operation.  Resolved historical rows
    are re-derived from the case corpus, the built-in profile map, the bound
    trace records, and freshly executed positive/permutation/negative probes.
    Only ``generated_at`` is excluded from the canonical comparison.
    """

    root = Path(root).resolve()
    report_path = root / "decision-case-replay.json"
    try:
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(
            "persisted DecisionCase replay report is unreadable"
        ) from exc
    if persisted != report:
        raise ReplayError(
            "supplied DecisionCase replay differs from the persisted report"
        )

    for row in report.get("case_results") or []:
        if not isinstance(row, dict) or row.get("classification") != "resolved":
            continue
        resolution = row.get("resolution")
        invariant = (
            resolution.get("invariant")
            if isinstance(resolution, dict) else None
        )
        if not resolution_permutation_invariant_is_valid(invariant):
            raise ReplayError(
                "persisted DecisionCase permutation invariant is malformed"
            )

    cases_path = root / "decision-cases.jsonl"
    case_sources_path = root / decision_case_corpus.DEFAULT_MANIFEST_NAME
    fixtures_path = root / "test_fixtures" / "decision-cases-v2.jsonl"
    resolution_evidence = report.get("resolution_evidence")
    frozen_case_digest = report.get("source_case_corpus_sha256")
    if not isinstance(frozen_case_digest, str):
        frozen_case_digest = (
            resolution_evidence.get("source_case_corpus_sha256")
            if isinstance(resolution_evidence, dict) else None
        )
    cases, case_corpus_evidence = load_case_corpus(
        cases_path, case_sources_path, with_evidence=True,
    )
    if (
        len(cases) != report.get("source_case_count")
        or _case_corpus_digest(cases) != frozen_case_digest
    ):
        raise ReplayError("DecisionCase frozen multi-source snapshot mismatch")
    fixture_cases = load_cases(fixtures_path)

    uses_resolutions = bool(report.get("resolved_case_count")) or (
        isinstance(resolution_evidence, dict)
        and resolution_evidence.get("authority_version")
        == "decision-case-current-regression-v1"
    )
    audit_kwargs = {}
    artifact_sha256 = {
        "test_fixtures/decision-cases-v2.jsonl": _file_sha256(fixtures_path),
    }
    artifact_sha256[decision_case_corpus.DEFAULT_MANIFEST_NAME] = (
        _file_sha256(case_sources_path)
    )
    if uses_resolutions:
        import decision_case_resolution

        resolution_paths = {
            "decision-case-resolutions.json": (
                root / "decision-case-resolutions.json"
            ),
            "test_fixtures/decision-case-resolution-invariants-v1.json": (
                root
                / "test_fixtures"
                / "decision-case-resolution-invariants-v1.json"
            ),
            "decision-case-trace-evidence.json": (
                root / "decision-case-trace-evidence.json"
            ),
        }
        document = decision_case_resolution.load_json_object(
            resolution_paths["decision-case-resolutions.json"],
            "DecisionCase resolutions",
        )
        catalog = decision_case_resolution.load_json_object(
            resolution_paths[
                "test_fixtures/decision-case-resolution-invariants-v1.json"
            ],
            "DecisionCase resolution fixtures",
        )
        trace_evidence = decision_case_resolution.load_json_object(
            resolution_paths["decision-case-trace-evidence.json"],
            "DecisionCase trace evidence",
        )
        for name, path in resolution_paths.items():
            artifact_sha256[name] = _file_sha256(path)
        compact_trace_evidence = _self_contained_trace_evidence(
            trace_evidence
        )
        audit_kwargs = {
            "resolution_document": document,
            "resolution_fixture_catalog": catalog,
            "trace_evidence": trace_evidence,
            "trace_path": None if compact_trace_evidence else root / "autoplay.log",
            "verify_trace_prefix": verify_trace_prefix,
        }

    recomputed = audit_cases(
        cases,
        target_hash,
        fixture_cases=fixture_cases,
        case_corpus_evidence=case_corpus_evidence,
        **audit_kwargs,
    )
    observed_payload = replay_contract_payload(report)
    expected_payload = replay_contract_payload(recomputed)
    if observed_payload != expected_payload:
        raise ReplayError(
            "persisted DecisionCase replay differs from live recomputation"
        )
    return {
        "schema_version": 1,
        "mode": "live_recomputation",
        "target_decision_hash": target_hash,
        "report_contract_sha256": replay_contract_sha256(report),
        "persisted_report_sha256": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
        "artifact_sha256": dict(sorted(artifact_sha256.items())),
        "trace_prefix_verified": bool(uses_resolutions and verify_trace_prefix),
        "trace_evidence_mode": (
            trace_evidence.get("evidence_mode", "legacy_prefix_v1")
            if uses_resolutions else "not_applicable"
        ),
        "case_corpus_mode": case_corpus_evidence["mode"],
        "case_corpus_manifest_sha256": case_corpus_evidence[
            "manifest_sha256"
        ],
        "case_corpus_source_manifest_sha256": case_corpus_evidence[
            "source_manifest_sha256"
        ],
        "case_corpus_source_count": case_corpus_evidence["source_count"],
        "case_corpus_case_count": case_corpus_evidence["case_count"],
        "case_corpus_sha256": case_corpus_evidence["case_corpus_sha256"],
    }


def write_report(path, report):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path("decision-cases.jsonl"))
    parser.add_argument(
        "--case-sources", type=Path,
        default=Path(decision_case_corpus.DEFAULT_MANIFEST_NAME),
    )
    parser.add_argument(
        "--fixtures", type=Path,
        default=Path("test_fixtures/decision-cases-v2.jsonl"),
    )
    parser.add_argument(
        "--resolutions", type=Path,
        default=Path("decision-case-resolutions.json"),
    )
    parser.add_argument(
        "--resolution-fixtures", type=Path,
        default=Path(
            "test_fixtures/decision-case-resolution-invariants-v1.json"
        ),
    )
    parser.add_argument(
        "--trace-evidence", type=Path,
        default=Path("decision-case-trace-evidence.json"),
    )
    parser.add_argument(
        "--trace", type=Path, default=Path("autoplay.log")
    )
    parser.add_argument("--decision-hash", required=True)
    parser.add_argument("--write", type=Path, default=Path("decision-case-replay.json"))
    args = parser.parse_args()
    try:
        import decision_case_resolution

        trace_evidence = decision_case_resolution.load_json_object(
            args.trace_evidence, "DecisionCase trace evidence"
        )
        cases, case_corpus_evidence = load_case_corpus(
            args.cases, args.case_sources, with_evidence=True,
        )
        report = audit_cases(
            cases, args.decision_hash,
            fixture_cases=load_cases(args.fixtures),
            resolution_document=decision_case_resolution.load_json_object(
                args.resolutions, "DecisionCase resolutions"
            ),
            resolution_fixture_catalog=(
                decision_case_resolution.load_json_object(
                    args.resolution_fixtures,
                    "DecisionCase resolution fixtures",
                )
            ),
            trace_evidence=trace_evidence,
            trace_path=(
                None if _self_contained_trace_evidence(trace_evidence)
                else args.trace
            ),
            case_corpus_evidence=case_corpus_evidence,
        )
        write_report(args.write, report)
    except (ReplayError, decision_case_resolution.ResolutionError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["release_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
