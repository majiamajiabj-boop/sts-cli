"""Independent, fail-closed audit for protocol-v2 autoplay traces.

This module deliberately uses only the Python standard library.  In
particular it must never import the production agent, ranker, combat planner,
or combat predictor.  Its inputs are authoritative trace facts plus the
canonical choice records defined below; producer scores and reasons are
claims to validate, not facts to trust.
"""

from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math
import re


ORACLE_VERSION = "independent-oracle-v2"
ORACLE_COVERAGE_CONTRACT_VERSION = 2
CANONICAL_CHOICE_SCHEMA_VERSION = 1

# This is the integration contract for every protocol-visible legal choice.
# Nullable score/model fields are still present so absence cannot be confused
# with a producer silently dropping a field.
CANONICAL_CHOICE_FIELDS = (
    "choice_schema_version",
    "choice_id",
    "choice_index",
    "action",
    "operation",
    "legal",
    "visible",
    "selection_eligible",
    "label",
    "raw_text",
    "semantic_id",
    "target",
    "consequences",
    "probability_outcomes",
    "local_score",
    "local_reason",
    "reason_codes",
    "score_rule_id",
    "score_formula",
    "score_inputs",
    "score_components",
    "model_score",
    "model_confidence",
    "model_evidence_status",
    "model_evidence_reason",
    "final_source",
    "override",
    "uncertainty",
    "selected",
    "candidate_ids",
    "candidate_binding",
    "veto_reason",
    "producer_candidate_raw",
    "producer_consequence_raw",
    "producer_consequence_claim",
    "producer_scoring_facts",
    "unclassified_producer_fields",
)

STRUCTURED_CONSEQUENCE_FIELDS = (
    "schema_version",
    "scope",
    "hp_delta",
    "max_hp_delta",
    "gold_delta",
    "card_changes",
    "relic_changes",
    "potion_changes",
    "curse",
    "probabilistic_outcomes",
    "current_cost",
    "future_costs",
    "raw_effect_text",
    "uncertainty",
    "field_knowledge",
    "uncertainty_classification",
)

KNOWN_RECORD_TYPES = {
    "cache_warmup",
    "controller_start",
    "decision",
    "model_advice",
    "protocol_event",
    "terminal_result",
    "transition_settle",
}

STRATEGIC_PHASES = {
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

_READ_ONLY_COMMANDS = {"state", "wait"}
# These protocol verbs are not complete non-combat choices without arguments
# (or belong to another phase).  The oracle independently expands ``choose``
# from raw options and excludes the other global verbs from the screen's
# irreversible choice surface.
_PARAMETERIZED_COMMANDS = {
    "choose", "play", "potion", "key", "click", "end", "start", "resume",
}
ATTEMPT_BINDING_FIELDS = (
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "decision_hash",
    "controller_hash",
    "policy_version",
    "selection_id",
    "selection_digest",
)
_NUMERIC_DELTA_FIELDS = {
    "hp_delta": "current_hp",
    "max_hp_delta": "max_hp",
    "gold_delta": "gold",
    "block_delta": "block",
}
_COLLECTION_DELTA_FIELDS = {
    "deck_delta": "deck",
    "relic_delta": "relics",
    "potion_delta": "potions",
}
DISAGREEMENT_KINDS = {
    "candidate_choice_index_mismatch",
    "candidate_consequence_binding_mismatch",
    "candidate_duplicate",
    "candidate_extra",
    "candidate_missing",
    "candidate_not_legal_visible",
    "candidate_semantic_binding_mismatch",
    "damage_prediction_mismatch",
    "damage_observed_claim_mismatch",
    "final_choice_binding_mismatch",
    "invalid_model_override",
    "high_confidence_unresolved_strategy_disagreement",
    "independent_blind_review_disagreement",
    "local_argmax_missed",
    "observable_delta_mismatch",
    "combat_choice_transition_mismatch",
    "combat_choice_settlement_mismatch",
    "black_star_elite_reward_mismatch",
    "mausoleum_trade_settlement_mismatch",
    "busted_crown_reward_count_mismatch",
    "omamori_transition_mismatch",
    "stasis_transition_mismatch",
    "producer_score_order_contradicts_independent_consequences",
    "producer_score_evidence_contradicted",
    "producer_consequence_claim_contradicted",
    "selected_choice_binding_mismatch",
    "deferred_choice_settlement_mismatch",
    "true_combat_end_contradicted",
    "target_consequence_claim_contradicted",
}

REQUIRED_COVERAGE_KEYS = {
    "protocol_binding",
    "authoritative_state_envelopes",
    "canonical_choice_completeness",
    "candidate_consequences",
    "choice_selection",
    "strategy_conflict_review",
    "observable_state_deltas",
    "combat_choice_transitions",
    "combat_choice_settlements",
    "damage_consistency",
    "true_combat_end",
    "terminal_authority",
    "act4_authority",
    "heart_defeat_authority",
}


def canonical_choice(
    choice_id,
    *,
    choice_index=None,
    action="choose",
    operation=None,
    legal=True,
    visible=True,
    selection_eligible=True,
    label=None,
    raw_text=None,
    semantic_id=None,
    target=None,
    consequences=None,
    probability_outcomes=None,
    local_score=None,
    local_reason="test_fixture_local_reason",
    reason_codes=None,
    score_rule_id="test_fixture_rule",
    score_formula=None,
    score_inputs=None,
    score_components=None,
    model_score=None,
    model_confidence=None,
    model_evidence_status="not_consulted",
    model_evidence_reason="test_fixture_no_model",
    final_source="local",
    override=None,
    uncertainty=None,
    selected=False,
    candidate_ids=None,
    candidate_binding="unique",
    veto_reason=None,
    producer_candidate_raw=None,
    producer_consequence_raw=None,
    producer_consequence_claim=None,
    producer_scoring_facts=None,
    unclassified_producer_fields=None,
):
    """Return one canonical legal-choice row for producer integration."""

    consequence_value = dict(consequences or {})
    effect_fields = {
        "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
        "relic_changes", "potion_changes", "curse",
        "probabilistic_outcomes", "current_cost", "future_costs", "route",
        "event_id", "campfire_option", "selected_card", "key_changes",
        "operation",
    }
    if producer_consequence_claim is None:
        producer_consequence_claim = {
            key: value for key, value in consequence_value.items()
            if key in effect_fields
        }
    if producer_consequence_raw is None:
        producer_consequence_raw = {
            "present": True,
            "value": dict(producer_consequence_claim),
        }
    if producer_candidate_raw is None:
        producer_candidate_raw = {
            "candidate_id": str(choice_id),
            "choice_index": choice_index,
            "action": str(action).strip().lower(),
            "operation": (
                str(operation).strip().lower()
                if operation is not None else None
            ),
            "score": local_score,
            "local_reason": local_reason,
            "reason_codes": list(reason_codes or ["TEST_FIXTURE"]),
            "score_rule_id": score_rule_id,
            "score_formula": dict(
                score_formula or {"kind": "sum_components_v1"}
            ),
            "score_inputs": dict(
                score_inputs or {
                    "fixture": (
                        local_score
                        if _finite_number(local_score) is not None else 0
                    ),
                }
            ),
            "score_components": list(score_components or [{
                "name": "fixture", "input": "fixture", "coefficient": 1,
                "value": (
                    local_score
                    if _finite_number(local_score) is not None else 0
                ),
            }]),
            "consequences": dict(producer_consequence_claim),
        }
    return {
        "choice_schema_version": CANONICAL_CHOICE_SCHEMA_VERSION,
        "choice_id": str(choice_id),
        "choice_index": choice_index,
        "action": str(action).strip().lower(),
        "operation": (
            str(operation).strip().lower()
            if operation is not None else None
        ),
        "legal": bool(legal),
        "visible": bool(visible),
        "selection_eligible": bool(selection_eligible),
        "label": label,
        "raw_text": raw_text,
        "semantic_id": semantic_id,
        "target": dict(target or {}),
        "consequences": consequence_value,
        "probability_outcomes": list(probability_outcomes or []),
        "local_score": local_score,
        "local_reason": local_reason,
        "reason_codes": list(reason_codes or ["TEST_FIXTURE"]),
        "score_rule_id": score_rule_id,
        "score_formula": dict(
            score_formula or {"kind": "sum_components_v1"}
        ),
        "score_inputs": dict(
            score_inputs or {
                "fixture": local_score if _finite_number(local_score) is not None else 0,
            }
        ),
        "score_components": list(score_components or [{
            "name": "fixture", "input": "fixture", "coefficient": 1,
            "value": local_score if _finite_number(local_score) is not None else 0,
        }]),
        "model_score": model_score,
        "model_confidence": model_confidence,
        "model_evidence_status": model_evidence_status,
        "model_evidence_reason": model_evidence_reason,
        "final_source": final_source,
        "override": dict(override or {}),
        "uncertainty": uncertainty,
        "selected": bool(selected),
        "candidate_ids": list(candidate_ids or [str(choice_id)]),
        "candidate_binding": candidate_binding,
        "veto_reason": veto_reason,
        "producer_candidate_raw": producer_candidate_raw,
        "producer_consequence_raw": producer_consequence_raw,
        "producer_consequence_claim": dict(producer_consequence_claim or {}),
        "producer_scoring_facts": dict(producer_scoring_facts or {}),
        "unclassified_producer_fields": list(
            unclassified_producer_fields or []
        ),
    }


def _coverage_bucket():
    return {"eligible": 0, "evaluated": 0, "unknown": 0, "issues": 0}


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _numeric_domain_bounds(value):
    """Return inclusive numeric bounds in either supported wire spelling."""

    if isinstance(value, list) and len(value) == 2:
        lower = _finite_number(value[0])
        upper = _finite_number(value[1])
        if lower is not None and upper is not None and lower <= upper:
            return lower, upper
    if not isinstance(value, dict):
        return None
    for lower_key, upper_key in (("minimum", "maximum"), ("min", "max")):
        lower = _finite_number(value.get(lower_key))
        upper = _finite_number(value.get(upper_key))
        if lower is not None and upper is not None and lower <= upper:
            return lower, upper
    return None


def _review_score_evidence(row):
    """Independently recompute the canonical sum-components score receipt."""

    unresolved = []
    contradictions = []
    rule_id = row.get("score_rule_id")
    formula = row.get("score_formula")
    inputs = row.get("score_inputs")
    components = row.get("score_components")
    if not isinstance(rule_id, str) or not rule_id.strip() or (
        rule_id.strip().casefold() == "unclassified"
    ):
        unresolved.append("score_rule_id")
    if not isinstance(formula, dict) or formula.get("kind") != "sum_components_v1":
        unresolved.append("score_formula.kind")
    if not isinstance(inputs, dict) or not inputs:
        unresolved.append("score_inputs")
        inputs = {}
    else:
        for name, value in inputs.items():
            if not isinstance(name, str) or not name or _finite_number(value) is None:
                unresolved.append(f"score_inputs.{name}")
    if not isinstance(components, list) or not components:
        unresolved.append("score_components")
        components = []
    names = []
    recomputed = []
    for component_index, component in enumerate(components):
        if not isinstance(component, dict):
            unresolved.append(f"score_components.{component_index}")
            continue
        name = component.get("name")
        input_name = component.get("input")
        coefficient = _finite_number(component.get("coefficient"))
        claimed_value = _finite_number(component.get("value"))
        if not isinstance(name, str) or not name:
            unresolved.append(f"score_components.{component_index}.name")
        else:
            names.append(name)
        if not isinstance(input_name, str) or input_name not in inputs:
            unresolved.append(f"score_components.{component_index}.input")
            continue
        input_value = _finite_number(inputs.get(input_name))
        if coefficient is None or input_value is None or claimed_value is None:
            unresolved.append(f"score_components.{component_index}.numeric")
            continue
        expected_value = input_value * coefficient
        recomputed.append(expected_value)
        if abs(expected_value - claimed_value) > 1e-9:
            contradictions.append({
                "field": f"score_components.{component_index}.value",
                "claimed": claimed_value,
                "recomputed": expected_value,
            })
    if len(names) != len(set(names)):
        unresolved.append("score_components.names_not_unique")
    local_score = _finite_number(row.get("local_score"))
    if local_score is None:
        unresolved.append("local_score")
    elif len(recomputed) == len(components) and not unresolved:
        expected_score = sum(recomputed)
        # Producers intentionally round the displayed local total to three
        # decimals in several decision surfaces while retaining exact score
        # inputs/components.  Recompute from the exact receipt and allow only
        # the corresponding half-unit display tolerance; component claims
        # themselves remain exact above.
        if abs(local_score - expected_score) > 0.0005001:
            contradictions.append({
                "field": "local_score", "claimed": local_score,
                "recomputed": expected_score,
            })
    return contradictions, sorted(set(unresolved))


def _normalize_command(command):
    raw_command = command
    if isinstance(command, str):
        name = command
        choice_id = None
        label = None
    elif isinstance(command, dict):
        name = command.get("action", command.get("command", command.get("name")))
        choice_id = command.get("choice_id", command.get("target_id"))
        label = command.get("label")
    else:
        return None
    name = str(name or "").strip().lower()
    if not name:
        return None
    if name in {"skip", "cancel", "leave"}:
        name = "return"
        choice_id = "action:return"
    elif name == "confirm":
        name = "proceed"
        choice_id = "action:proceed"
    return {
        "choice_id": str(choice_id or f"action:{name}"),
        "choice_index": None,
        "action": name,
        "operation": None,
        "semantic_id": name,
        "label": label or name,
        "raw_text": str(raw_command if isinstance(raw_command, str) else name),
        "target": {"kind": "protocol_action", "action": name},
    }


def _raw_option_contract(option, phase=None):
    """Derive canonical semantics solely from one protocol option."""

    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "option").casefold()
    phase = str(phase or "").upper()
    index = option.get("choice_index")
    if phase == "NEOW":
        semantic_id = f"neow:{index}"
    elif phase == "MAP" and all(target.get(key) is not None for key in ("x", "y")):
        semantic_id = f"{target.get('symbol') or '?'}@{target['x']},{target['y']}"
    elif phase == "EVENT":
        event_id = str(target.get("event_id") or "").casefold()
        prefix = "match-position" if "match" in event_id else "event"
        semantic_id = f"{prefix}:{index}"
    elif phase in {"GRID", "HAND_SELECT"}:
        semantic_id = f"grid:{target.get('card_instance_id') or index}"
    elif phase == "SHOP_SCREEN":
        item = target.get("item") if isinstance(target.get("item"), dict) else {}
        identity = (
            item.get("card_instance_id") or item.get("relic_id")
            or item.get("potion_instance_id") or item.get("id") or index
        )
        semantic_id = f"shop:{kind}:{identity}:{index}"
    elif phase == "BOSS_REWARD":
        relic = target.get("relic") if isinstance(target.get("relic"), dict) else {}
        semantic_id = f"relic:{relic.get('id') or index}:{index}"
    elif phase == "CARD_REWARD":
        card = target.get("card") if isinstance(target.get("card"), dict) else {}
        semantic_id = f"card:{target.get('card_instance_id') or card.get('id') or index}"
    elif phase in {"COMBAT_REWARD", "SAPPHIRE_KEY"}:
        reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
        semantic_id = f"reward:{str(reward.get('reward_type') or kind).casefold()}:{index}"
    else:
        semantic_id = f"{kind}:{index}"
    raw_text = " ".join(str(value or "") for value in (
        option.get("label"), target.get("label"), target.get("text"),
    )).strip()
    return {
        "choice_id": (
            None if option.get("option_id", option.get("choice_id")) is None
            else str(option.get("option_id", option.get("choice_id")))
        ),
        "choice_index": option.get("choice_index"),
        "action": "choose",
        "operation": None,
        "semantic_id": semantic_id,
        "label": option.get("label"),
        "raw_text": raw_text,
        "target": target,
    }


def reconstruct_legal_choices(record):
    """Rebuild the legal choice surface from raw protocol fields only.

    The function intentionally does not inspect ``legal_choices_before`` or
    production candidates.  It returns ``(choices, missing_inputs)``.
    """

    missing = []
    resource_preparation = (
        record.get("decision_surface_kind") == "resource_preparation"
    )
    options = record.get(
        "resource_preparation_options_before"
        if resource_preparation else "available_options_before"
    )
    commands = record.get("available_commands_before")
    if not isinstance(options, list):
        missing.append(
            "resource_preparation_options_before"
            if resource_preparation else "available_options_before"
        )
        options = []
    if not isinstance(commands, list):
        missing.append("available_commands_before")
        commands = []

    choices = []
    for option in options:
        if not isinstance(option, dict):
            choices.append({"invalid": "option_not_object"})
            continue
        contract = _raw_option_contract(option, record.get("phase"))
        if resource_preparation:
            target = contract.get("target") or {}
            if str(target.get("kind") or "").casefold() != "potion_resource":
                choices.append({"invalid": "resource_option_not_potion_action"})
                continue
            operation = str(target.get("operation") or "").casefold()
            if operation not in {"use", "discard"}:
                choices.append({"invalid": "resource_option_operation_invalid"})
                continue
            contract["action"] = "potion"
            contract["operation"] = operation
            contract["semantic_id"] = (
                f"potion-{operation}:{target.get('potion_instance_id')}:"
                f"{contract.get('choice_index')}"
            )
        choices.append(contract)

    if resource_preparation:
        if record.get("parent_choice_surface_pending") is not True:
            missing.append("parent_choice_surface_pending")
        return choices, missing

    for raw_command in commands:
        command = _normalize_command(raw_command)
        if command is None:
            choices.append({"invalid": "command_not_parseable"})
            continue
        if command["action"] in _READ_ONLY_COMMANDS | _PARAMETERIZED_COMMANDS:
            continue
        if (
            command["action"] == "return"
            and str(record.get("phase") or "").upper()
            in {"MAP", "GRID", "HAND_SELECT"}
        ):
            continue
        if (
            str(record.get("phase") or "").upper() == "SHOP_SCREEN"
            and command["action"] == "return"
        ):
            command["semantic_id"] = "shop_leave"
        choices.append(command)
    return choices, missing


def _record_has_strategic_surface(record):
    phase = str((record or {}).get("phase") or "").upper()
    if phase not in STRATEGIC_PHASES:
        return False
    if str((record or {}).get("action") or "").strip().casefold() in {
        "wait", "state",
    }:
        return False
    if record.get("decision_surface_kind") == "resource_preparation":
        return True
    options = record.get("available_options_before")
    if not isinstance(options, list) or not options:
        return False
    return True


def _strategic_canonical_rows(record, rows):
    """Exclude protocol navigation controls from the strategy surface."""

    if not isinstance(rows, list):
        return rows
    phase = str((record or {}).get("phase") or "").upper()
    if phase not in {"MAP", "GRID", "HAND_SELECT"}:
        return rows
    return [
        row for row in rows
        if not (
            isinstance(row, dict)
            and str(row.get("action") or "").casefold()
            in {"return", "cancel"}
        )
    ]


def _canonical_advice_choice_id(value, canonical_by_id):
    """Resolve a model/local producer id through a proven unique binding."""

    raw_id = str(value or "")
    if not raw_id or not isinstance(canonical_by_id, dict):
        return raw_id
    if raw_id in canonical_by_id:
        return raw_id
    matches = []
    for choice_id, row in canonical_by_id.items():
        if not isinstance(row, dict) or row.get("candidate_binding") != "unique":
            continue
        producer = row.get("producer_candidate_raw")
        producer = producer if isinstance(producer, dict) else {}
        aliases = {
            str(alias) for alias in (
                row.get("semantic_id"),
                producer.get("id"), producer.get("candidate_id"),
                producer.get("choice_id"), producer.get("semantic_id"),
                *(row.get("candidate_ids") or []),
            )
            if alias is not None and str(alias)
        }
        if raw_id in aliases:
            matches.append(str(choice_id))
    return matches[0] if len(matches) == 1 else raw_id


def _state_value(state, name):
    if not isinstance(state, dict):
        return None
    if name in state:
        return state.get(name)
    player = state.get("player")
    if isinstance(player, dict) and name in player:
        return player.get(name)
    return None


def _collection_count(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        total = 0
        for count in value.values():
            number = _finite_number(count)
            if number is None:
                return None
            total += int(number)
        return total
    return None


def _freeze(value):
    """Return a deterministic, hashable representation without JSON."""

    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def _collection_identity(item, fallback):
    if not isinstance(item, dict):
        return ("invalid", fallback)
    return str(
        item.get("card_instance_id")
        or item.get("potion_instance_id")
        or item.get("relic_instance_id")
        or item.get("id")
        or fallback
    )


def _index_collection(value):
    if not isinstance(value, list):
        return None
    occurrences = Counter()
    indexed = {}
    for index, item in enumerate(value):
        identity = _collection_identity(item, index)
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        indexed[(identity, occurrence)] = item
    return indexed


def _observable_collection_delta(before, after):
    left = _index_collection(before)
    right = _index_collection(after)
    if left is None or right is None:
        return None
    return {
        "added": [right[key] for key in sorted(right.keys() - left.keys())],
        "removed": [left[key] for key in sorted(left.keys() - right.keys())],
        "changed": [
            {"before": left[key], "after": right[key]}
            for key in sorted(left.keys() & right.keys())
            if left[key] != right[key]
        ],
    }


def _oracle_combat_choice_background_potion_heal(
    phase, before, after, outcome, potion_delta,
):
    """Return exact Toy Ornithopter healing queued behind a combat choice."""

    if (
        str(phase or "").upper() not in _ORACLE_COMBAT_CHOICE_PHASES
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(outcome, dict)
        or not isinstance(outcome.get("combat_choice_transition"), dict)
        or not isinstance(potion_delta, dict)
    ):
        return 0
    removed = potion_delta.get("removed")
    if not isinstance(removed, list) or len(removed) != 1:
        return 0
    removed_id = _oracle_game_id(
        removed[0].get("id") or removed[0].get("name")
    ) if isinstance(removed[0], dict) else ""
    if removed_id in {"", "potionslot", "fairypotion", "fairyinabottle"}:
        return 0
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in before.get("relics") or []
        if isinstance(relic, dict)
    }
    if "toyornithopter" not in relic_ids or "markofthebloom" in relic_ids:
        return 0
    current_hp = _finite_number(_state_value(before, "current_hp"))
    max_hp = _finite_number(_state_value(before, "max_hp"))
    after_hp = _finite_number(_state_value(after, "current_hp"))
    claimed = _finite_number(
        outcome.get("current_hp_delta", outcome.get("hp_delta"))
    )
    if (
        current_hp is None or max_hp is None or after_hp is None
        or claimed is None or max_hp < current_hp
    ):
        return 0
    healing = 5
    if "magicflower" in relic_ids:
        healing = (healing * 3 + 1) // 2
    healing = min(healing, int(max_hp - current_hp))
    observed = after_hp - current_hp
    return healing if observed == healing and claimed == healing else 0


def _structured_delta_equal(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for field in ("added", "removed", "changed"):
        if not isinstance(left.get(field), list) or not isinstance(right.get(field), list):
            return False
        if Counter(_freeze(item) for item in left[field]) != Counter(
            _freeze(item) for item in right[field]
        ):
            return False
    return True


_ORACLE_CONSEQUENCE_FIELDS = (
    "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
    "relic_changes", "potion_changes", "curse", "probabilistic_outcomes",
    "current_cost", "future_costs",
)


def _oracle_empty_consequence(raw_text=""):
    value = {
        "schema_version": 1,
        "scope": "immediate_protocol_transition",
        "hp_delta": None,
        "max_hp_delta": None,
        "gold_delta": None,
        "card_changes": {"gain": [], "remove": [], "upgrade": [], "transform": []},
        "relic_changes": {"gain": [], "remove": [], "counter": []},
        "potion_changes": {"gain": [], "remove": [], "replace": []},
        "curse": {
            "gain": [], "remove": [], "probability": None,
            "omamori_applicable": None, "omamori_charges_consumed": None,
        },
        "probabilistic_outcomes": [],
        "current_cost": {"gold": None, "hp": None, "max_hp": None},
        "future_costs": [],
        "raw_effect_text": str(raw_text or ""),
        "uncertainty": [],
        "uncertainty_classification": {
            "status": "unresolved", "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        },
    }
    value["field_knowledge"] = {
        field: {
            "status": "unknown", "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        }
        for field in _ORACLE_CONSEQUENCE_FIELDS
    }
    return value


def _oracle_known(value, field, result, authority, reason):
    value[field] = result
    value["field_knowledge"][field] = {
        "status": "known", "authority": authority, "reason": reason,
    }


def _oracle_numeric(value, hp, max_hp, gold, reason):
    for field, result in (
        ("hp_delta", hp), ("max_hp_delta", max_hp), ("gold_delta", gold),
    ):
        _oracle_known(
            value, field, result, "independent_mechanics_table", reason
        )


def _oracle_empty_inventory(value, reason):
    for field, result in (
        ("card_changes", {"gain": [], "remove": [], "upgrade": [], "transform": []}),
        ("relic_changes", {"gain": [], "remove": [], "counter": []}),
        ("potion_changes", {"gain": [], "remove": [], "replace": []}),
        ("curse", {"gain": [], "remove": [], "probability": 0.0,
                   "omamori_applicable": False, "omamori_charges_consumed": 0}),
    ):
        _oracle_known(
            value, field, result, "independent_mechanics_table", reason
        )


def _oracle_costs(value, reason, gold=0, hp=0, max_hp=0):
    _oracle_known(
        value, "current_cost", {"gold": gold, "hp": hp, "max_hp": max_hp},
        "protocol_target", reason,
    )
    _oracle_known(
        value, "future_costs", [], "independent_mechanics_table", reason
    )


def _oracle_no_probability(value, reason):
    _oracle_known(
        value, "probabilistic_outcomes", [],
        "independent_mechanics_table", reason,
    )
    value["uncertainty_classification"] = {
        "status": "none", "authority": "independent_mechanics_table",
        "reason": reason,
    }


def _oracle_game_id(value):
    return "".join(
        character for character in str(value or "").casefold()
        if character.isalnum()
    )


def _oracle_card_id(value):
    """Normalize base-game card aliases without conflating arbitrary IDs."""

    normalized = _oracle_game_id(value)
    if normalized in {"apparition", "ghostly"}:
        return "ghostly"
    return normalized


def _oracle_card_gain_after_egg_relics(card, game):
    """Independently project deterministic egg upgrades on card gain."""

    gained = _oracle_clone(card)
    if not isinstance(gained, dict):
        return gained
    upgrades = gained.get("upgrades")
    if type(upgrades) is not int or upgrades != 0:
        return gained
    card_type = str(gained.get("type") or "").upper()
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    egg_ids = {
        "ATTACK": {"moltenegg", "moltenegg2"},
        "SKILL": {"toxicegg", "toxicegg2"},
        "POWER": {"frozenegg", "frozenegg2"},
    }.get(card_type, set())
    if not relic_ids.intersection(egg_ids):
        return gained
    gained["upgrades"] = 1
    name = gained.get("name")
    if isinstance(name, str) and name and not name.endswith("+"):
        gained["name"] = f"{name}+"
    return gained


_ORACLE_PASSIVE_RELIC_PICKUPS_V1 = {
    "anchor", "blackstar", "boot", "chemicalx", "coffeedripper", "datadisk",
    "bustedcrown", "clockworksouvenir", "dreamcatcher", "orichalcum",
    "questioncard", "runiccube", "runicdome", "sacredbark", "slaverscollar",
}
_ORACLE_PASSIVE_RELIC_PICKUPS_V2 = {
    "pocketwatch", "runicpyramid", "singingbowl",
    "sneckoeye", "toolbox", "toyornithopter", "whitebeaststatue",
}
_ORACLE_PASSIVE_RELIC_PICKUPS_V3 = {
    # Explicit base-game relics whose pickup changes only relic inventory.
    # Triggered combat/room effects are audited by their own runtime handlers;
    # pickup packages with HP, gold, cards, potions, slots, replacements, or a
    # follow-up GRID are deliberately excluded and handled separately.
    "akabeko", "ancientteaset", "artofwar", "bagofmarbles",
    "bagofpreparation",
    "birdfacedurn", "bloodvial", "bloodyidol", "bluecandle", "brimstone",
    "bronzescales", "calipers", "captainswheel", "ceramicfish",
    "championbelt", "charonsashes", "cloakclasp", "cursedkey",
    "darkstoneperiapt", "deadbranch", "duvudoll", "ectoplasm",
    "emotionchip", "enchiridion", "eternalfeather", "faceofcleric",
    "fossilizedhelix", "frozeneye", "gamblingchip", "ginger", "girya",
    "goldplatedcables", "goldeneye", "goldenidol", "gremlinhorn",
    "handdrill", "happyflower", "icecream", "incenseburner", "inkbottle",
    "inserter", "juzubracelet", "kunai", "lantern", "letteropener",
    "lizardtail", "magicflower", "markofpain", "markofthebloom", "mawbank",
    "mealticket", "meatonthebone", "medicalkit", "melange", "matryoshka",
    "membershipcard", "mercuryhourglass", "moltenegg2", "frozenegg2",
    "mummifiedhand",
    "mutagenicstrength", "neowsblessing", "neowslament", "ninjascroll",
    "nlothsgift", "thecourier",
    "nunchaku", "oddmushroom", "oddlysmoothstone", "omamori",
    "orangepellets", "ornamentalfan", "papercrane", "paperfrog",
    "paperphrog",
    "peacepipe", "pennib", "philosopherstone", "philosophersstone",
    "prayerwheel",
    "preservedinsect", "prismaticshard", "redmask", "redskull",
    "regalpillow", "runiccapacitor", "selfformingclay", "shovel",
    "shuriken", "sling", "slingofcourage", "smilingmask",
    "sneckoskull", "horncleat", "strangespoon", "toxicegg2",
    "stonecalendar", "strikedummy", "sundial", "symbioticvirus",
    "teardroplocket", "theabacus", "threadandneedle", "tinychest", "torii",
    "toughbandages", "tungstenrod", "turnip", "twistedfunnel",
    "unceasingtop", "vajra", "velvetchoker", "violetlotus",
    "warpedtongs", "wingboots", "wingedgreaves", "wristblade",
    # Boss relics with no immediate pickup package. Their energy/orb/potion
    # restrictions are audited when the corresponding runtime state applies.
    "fusionhammer", "hoveringkite", "nuclearbattery", "sozu",
    "pantograph",
}
_ORACLE_PASSIVE_RELIC_PICKUPS = (
    _ORACLE_PASSIVE_RELIC_PICKUPS_V1 | _ORACLE_PASSIVE_RELIC_PICKUPS_V2
    | _ORACLE_PASSIVE_RELIC_PICKUPS_V3
)
_ORACLE_IMMEDIATE_RELIC_PICKUPS = {
    "mango": {"hp_delta": 14, "max_hp_delta": 14, "gold_delta": 0},
    "oldcoin": {"hp_delta": 0, "max_hp_delta": 0, "gold_delta": 300},
    "pear": {"hp_delta": 10, "max_hp_delta": 10, "gold_delta": 0},
    "strawberry": {"hp_delta": 7, "max_hp_delta": 7, "gold_delta": 0},
}


def _oracle_projection_v2(target):
    return bool(
        isinstance(target, dict)
        and type(target.get("audit_projection_version")) is int
        and target.get("audit_projection_version") >= 2
    )


def _oracle_projection_v3(target):
    return bool(
        isinstance(target, dict)
        and type(target.get("audit_projection_version")) is int
        and target.get("audit_projection_version") >= 3
    )


def _oracle_passive_relic_pickup_known(relic_id, target):
    return bool(
        relic_id in _ORACLE_PASSIVE_RELIC_PICKUPS_V1
        or (
            _oracle_projection_v2(target)
            and (relic_id != "artofwar" or _oracle_projection_v3(target))
            and relic_id in (
                _ORACLE_PASSIVE_RELIC_PICKUPS_V2
                | _ORACLE_PASSIVE_RELIC_PICKUPS_V3
            )
        )
    )


_ORACLE_STARTER_RELIC_REPLACEMENTS = {
    "blackblood": "Burning Blood",
    "frozencore": "Cracked Core",
    "holywater": "PureWater",
    "ringoftheserpent": "Snake Ring",
}


_ORACLE_INDEPENDENT_SPECIAL_RELIC_PICKUPS = {
    "astrolabe", "tinyhouse", "leeswaffle", "waffle", "orrery",
}


def _oracle_relic_pickup_package(
    value, record, relic, relic_id, reason, *, price=None, target=None,
):
    gold_cost = price if type(price) is int else 0
    """Classify deterministic non-passive base-game relic pickup packages."""

    if relic_id in _ORACLE_STARTER_RELIC_REPLACEMENTS:
        removed_id = _ORACLE_STARTER_RELIC_REPLACEMENTS[relic_id]
        owned = [
            item for item in (_oracle_before_game(record).get("relics") or [])
            if isinstance(item, dict)
            and _oracle_game_id(item.get("id") or item.get("name"))
            == _oracle_game_id(removed_id)
        ]
        if len(owned) != 1:
            return False
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)],
                "remove": [_oracle_clone(owned[0])], "counter": [],
            }, "independent_mechanics_table", reason,
        )
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        return True
    if relic_id == "emptycage":
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)], "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason)
        _oracle_known(
            value, "future_costs", [{
                "kind": "relic_grid_selection", "relic_id": "Empty Cage",
                "domain": "current_deck", "operation": "remove",
                "select_count": 2, "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }], "independent_mechanics_table", reason,
        )
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "the two removed card UUIDs settle on the following GRID surface"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": "Empty Cage opens an exact two-card removal GRID",
        }
        return True
    if (
        relic_id == "orrery"
        and type(price) is int
        and _oracle_projection_v3(target)
    ):
        _oracle_numeric(value, 0, 0, -gold_cost, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)], "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        _oracle_known(
            value, "future_costs", [{
                "kind": "optional_card_reward_sequence",
                "relic_id": "Orrery",
                "card_pool": "CHARACTER",
                "reward_count": 5,
                "candidates_per_reward": max(
                    1,
                    3
                    + int(_oracle_has_relic(record, "QuestionCard"))
                    - 2 * int(_oracle_has_relic(record, "BustedCrown")),
                ),
                "select_count_per_reward": 1,
                "can_skip": True,
                "timing": "after_relic_purchase",
            }], "independent_mechanics_table", reason,
        )
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "the chosen card UUIDs settle across the following five "
            "CARD_REWARD surfaces"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": (
                "Orrery opens five consecutive character card reward choices"
            ),
        }
        return True
    if relic_id == "astrolabe" and _oracle_projection_v3(target):
        _oracle_numeric(value, 0, 0, -gold_cost, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)], "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        _oracle_known(
            value, "future_costs", [{
                "kind": "astrolabe_grid_selection",
                "operation": "transform_and_upgrade",
                "select_count": 3,
                "domain": "current_deck",
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }], "independent_mechanics_table", reason,
        )
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "the three transformed card UUIDs settle on the following GRID surface"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": "Astrolabe opens an exact three-card transform-and-upgrade GRID",
        }
        return True
    if relic_id == "tinyhouse" and _oracle_projection_v3(target):
        game = _oracle_before_game(record)
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        relic_ids = {
            _oracle_game_id(item.get("id") or item.get("name"))
            for item in (game.get("relics") or []) if isinstance(item, dict)
        }
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        _oracle_known_domain(
            value, "hp_delta", {
                "kind": "tiny_house_effective_heal",
                "minimum": 0,
                "maximum": min(5, max(0, max_hp + 5 - current_hp)),
            }, "tiny_house_heal_with_mark_of_the_bloom_domain",
        )
        _oracle_known(
            value, "max_hp_delta", 5,
            "independent_mechanics_table", reason,
        )
        _oracle_known(
            value, "gold_delta",
            -gold_cost,
            "independent_mechanics_table", reason,
        )
        _oracle_empty_inventory(value, reason)
        upgradeable_cards = [
            item for item in (game.get("deck") or [])
            if isinstance(item, dict)
            and int(item.get("upgrades") or 0) == 0
            and str(item.get("type") or "").upper()
            not in {"CURSE", "STATUS"}
        ]
        _oracle_known_domain(
            value, "card_changes", {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
                "random_upgrade": [{
                    "kind": "tiny_house_random_card_upgrade",
                    "domain": "current_upgradable_deck",
                    "count": min(1, len(upgradeable_cards)),
                    "selection_mode": "random",
                }],
            }, "tiny_house_random_card_upgrade_domain",
        )
        potions = game.get("potions")
        potion_slot_available = bool(
            isinstance(potions, list)
            and any(
                isinstance(item, dict)
                and _oracle_game_id(item.get("id")) == "potionslot"
                for item in potions
            )
        )
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)], "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        future_reward_surfaces = []
        if "ectoplasm" not in relic_ids:
            future_reward_surfaces.append({
                "kind": "tiny_house_gold_reward_surface",
                "gold_amount": 50,
                "timing": "after_relic_pickup",
            })
        future_reward_surfaces.append({
                "kind": "tiny_house_card_reward_surface",
                "selection_mode": "player_choice_or_skip",
                "timing": "after_relic_pickup",
        })
        if potion_slot_available and "sozu" not in relic_ids:
            future_reward_surfaces.append({
                "kind": "tiny_house_potion_reward_surface",
                "domain": "base_game_potion_pool",
                "count": 1,
                "selection_mode": "random",
                "timing": "after_relic_pickup",
            })
        _oracle_known(
            value, "future_costs", future_reward_surfaces,
            "independent_mechanics_table", reason,
        )
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "tiny_house_random_upgrade_and_potion_domains",
        )
        value["uncertainty"] = [
            "the random upgraded card settles at pickup; gold, card, and "
            "available potion rewards settle on following reward surfaces"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_random_domain",
            "authority": "independent_mechanics_table",
            "reason": "Tiny House has bounded random upgrade and potion identities",
        }
        return True
    if relic_id in {"leeswaffle", "waffle"} and _oracle_projection_v3(target):
        game = _oracle_before_game(record)
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        _oracle_known_domain(
            value, "hp_delta", {
                "kind": "lees_waffle_heal_domain",
                "minimum": 0,
                "maximum": max(0, max_hp + 7 - current_hp),
            }, "lees_waffle_heal_with_mark_of_the_bloom_domain",
        )
        _oracle_known(
            value, "max_hp_delta", 7,
            "independent_mechanics_table", reason,
        )
        _oracle_known(
            value, "gold_delta", -gold_cost,
            "independent_mechanics_table", reason,
        )
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)], "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        _oracle_no_probability(value, reason)
        return True
    if relic_id == "pandorasbox":
        if not _oracle_projection_v3(target):
            return False
        starter_ids = {
            "striker", "defendr", "strikeg", "defendg",
            "strikeb", "defendb", "strikep", "defendp",
        }
        removed = [
            _oracle_clone(card)
            for card in (_oracle_before_game(record).get("deck") or [])
            if isinstance(card, dict)
            and _oracle_game_id(card.get("id") or card.get("name"))
            in starter_ids
        ]
        if not removed:
            return False
        _oracle_numeric(value, 0, 0, -gold_cost, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "card_changes", {
                "gain": [], "remove": removed, "upgrade": [],
                "transform": [],
            }, "independent_mechanics_table", reason,
        )
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)],
                "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        _oracle_known(
            value, "future_costs", [{
                "kind": "pandoras_box_random_transforms",
                "operation": "transform",
                "source_count": len(removed),
                "result_count": len(removed),
                "result_domain": "character_cards",
                "result_selection_mode": "random",
                "timing": "following_grid_settlement",
            }], "independent_mechanics_table", reason,
        )
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "replacement card identities settle on the following GRID frame"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": "Pandora's Box transforms every basic Strike and Defend",
        }
        return True
    if relic_id == "potionbelt":
        _oracle_numeric(value, 0, 0, -gold_cost, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)],
                "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_known_domain(
            value, "potion_changes", {
                "gain": [],
                "random_gain": [{
                    "kind": "potion_slot_gain", "id": "Potion Slot",
                    "count": 2,
                    "identity_binding": "authoritative_after_inventory",
                }],
                "remove": [], "replace": [],
            }, "potion_belt_two_runtime_slots",
        )
        _oracle_costs(value, reason, gold=gold_cost)
        _oracle_no_probability(value, reason)
        return True
    if relic_id in {
        "bottledflame", "bottledlightning", "bottledtornado", "cauldron",
    }:
        _oracle_numeric(value, 0, 0, -gold_cost, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "relic_changes", {
                "gain": [_oracle_clone(relic)],
                "remove": [], "counter": [],
            }, "protocol_relic_target", reason,
        )
        _oracle_costs(value, reason, gold=gold_cost)
        bottle = {
            "bottledflame": (
                "bottled_flame_grid_selection", "bottle_attack",
                "current_deck_attacks", "Attack",
            ),
            "bottledlightning": (
                "bottled_lightning_grid_selection", "bottle_skill",
                "current_deck_skills", "Skill",
            ),
            "bottledtornado": (
                "bottled_tornado_grid_selection", "bottle_power",
                "current_deck_powers", "Power",
            ),
        }.get(relic_id)
        future = (
            [{
                "kind": bottle[0], "operation": bottle[1],
                "select_count": 1, "domain": bottle[2],
                "selection_mode": "player_choice",
                "timing": "after_relic_pickup",
            }]
            if bottle is not None else [{
                "kind": "cauldron_potion_reward_surface",
                "visible_count": 5,
                "domain": "base_game_potion_pool",
                "selection_mode": "claim_visible_rewards",
                "timing": "after_relic_pickup",
            }]
        )
        _oracle_known(
            value, "future_costs", future,
            "independent_mechanics_table", reason,
        )
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "the exact follow-up choices bind on the next protocol surface"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": (
                f"the bottled relic opens one current-deck {bottle[3]} choice"
                if bottle is not None else
                "Cauldron opens five visible potion rewards"
            ),
        }
        return True
    return False


def _oracle_apply_calling_bell_pickup(value, relic, reason):
    bell_curse = {
        "id": "CurseOfTheBell", "card_id": "CurseOfTheBell",
        "type": "CURSE", "rarity": "CURSE", "count": 1,
    }
    random_relics = [
        {
            "kind": "random_relic_gain", "domain": f"base_game_{rarity}_relic_pool",
            "rarity": rarity.upper(), "count": 1,
            "selection_mode": "random",
        }
        for rarity in ("common", "uncommon", "rare")
    ]
    _oracle_numeric(value, 0, 0, 0, reason)
    _oracle_known(
        value, "card_changes", {
            "gain": [_oracle_clone(bell_curse)], "remove": [],
            "upgrade": [], "transform": [],
        }, "independent_mechanics_table", reason,
    )
    _oracle_known_domain(
        value, "relic_changes", {
            "gain": [_oracle_clone(relic)],
            "random_gain": _oracle_clone(random_relics),
            "remove": [], "counter": [],
        }, "calling_bell_three_relic_rarity_pools",
    )
    _oracle_known(
        value, "potion_changes", {
            "gain": [], "remove": [], "replace": [],
        }, "independent_mechanics_table", reason,
    )
    _oracle_known(
        value, "curse", {
            "gain": [_oracle_clone(bell_curse)], "remove": [],
            "probability": 1.0, "omamori_applicable": False,
            "omamori_charges_consumed": 0,
        }, "independent_mechanics_table", reason,
    )
    _oracle_costs(value, reason)
    _oracle_known_domain(
        value, "probabilistic_outcomes", [],
        "calling_bell_three_relic_rarity_pools",
    )
    value["random_effects"] = _oracle_clone(random_relics)
    value["uncertainty"] = [
        "three relic identities remain inside their typed rarity pools"
    ]
    value["uncertainty_classification"] = {
        "status": "classified_random_domain",
        "authority": "independent_mechanics_table",
        "reason": reason,
    }
_ORACLE_WHETSTONE_CLASS_SHA256 = (
    "40cea3223f3ce0b4a7046867de845af61a1788958063ce24306d796529755690"
)


def _oracle_whetstone_deck(
    deck, *, eligible_type="ATTACK", effect_id="whetstone",
):
    """Return UUID-bound cards and a random-upgrade relic's exact pool."""

    prefix = str(effect_id or "random_upgrade_relic")
    if not isinstance(deck, list):
        return None, None, f"{prefix}_deck_missing"
    cards = {}
    candidates = {}
    for card in deck:
        if not isinstance(card, dict):
            return None, None, f"{prefix}_deck_card_not_object"
        uuid = _oracle_card_uuid(card)
        card_id = _oracle_game_id(card.get("id"))
        card_type = str(card.get("type") or "").upper()
        upgrades = card.get("upgrades")
        if not uuid or uuid in cards:
            return None, None, f"{prefix}_deck_uuid_missing_or_duplicate"
        if not card_id or not card_type or type(upgrades) is not int or upgrades < 0:
            return None, None, f"{prefix}_card_identity_or_upgrade_missing"
        cards[uuid] = card
        # In the base game every unupgraded Attack can upgrade, while Searing
        # Blow remains upgradable after prior upgrades.  Standard A0 excludes
        # modded cards whose canUpgrade contract is not protocol-visible.
        if card_type == eligible_type and (
            upgrades == 0
            or eligible_type == "ATTACK" and card_id == "searingblow"
        ):
            candidates[uuid] = card
    return cards, candidates, None


def _oracle_whetstone_selected(raw, record):
    choice_id = raw.get("choice_id") if isinstance(raw, dict) else None
    selected = record.get("selected_choice_ids") if isinstance(record, dict) else None
    return bool(
        choice_id is not None and isinstance(selected, list)
        and selected == [str(choice_id)]
    )


def _oracle_whetstone_settlement(
    record, relic, price, *, effect_id="whetstone", eligible_type="ATTACK",
):
    """Independently settle a two-card random-upgrade relic."""

    prefix = str(effect_id or "random_upgrade_relic")
    before_state = record.get("authoritative_state_before")
    after_state = record.get("authoritative_state_after")
    if not isinstance(before_state, dict) or not isinstance(after_state, dict):
        return "unknown", f"{prefix}_authoritative_state_pair_missing", {}
    if (
        before_state.get("state_seq") != record.get("before_seq")
        or after_state.get("state_seq") != record.get("after_seq")
    ):
        return "issues", f"{prefix}_state_sequence_mismatch", {}
    before = _game_from_state(before_state)
    after = _game_from_state(after_state)
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "unknown", f"{prefix}_game_state_pair_missing", {}
    before_cards, candidates, error = _oracle_whetstone_deck(
        before.get("deck"), eligible_type=eligible_type,
        effect_id=effect_id,
    )
    if error is not None:
        return "unknown", error, {}
    after_cards, _after_candidates, error = _oracle_whetstone_deck(
        after.get("deck"), eligible_type=eligible_type,
        effect_id=effect_id,
    )
    if error is not None:
        return "unknown", error, {}
    details = {
        "eligible_card_instance_ids": sorted(candidates),
        "eligible_count": len(candidates),
        "expected_upgrade_count": min(2, len(candidates)),
    }
    if effect_id == "whetstone":
        details["jar_class_sha256"] = _ORACLE_WHETSTONE_CLASS_SHA256
    if set(before_cards) != set(after_cards):
        details.update({
            "before_card_instance_ids": sorted(before_cards),
            "after_card_instance_ids": sorted(after_cards),
        })
        return "issues", f"{prefix}_deck_uuid_set_changed", details

    upgrades = []
    for uuid in sorted(before_cards):
        left = before_cards[uuid]
        right = after_cards[uuid]
        stable_identity = bool(
            _oracle_game_id(left.get("id")) == _oracle_game_id(right.get("id"))
            and str(left.get("type") or "").upper()
            == str(right.get("type") or "").upper()
            and str(left.get("rarity") or "").upper()
            == str(right.get("rarity") or "").upper()
            and _oracle_card_uuid(right) == uuid
        )
        if not stable_identity:
            return "issues", f"{prefix}_card_identity_changed", {
                **details, "card_instance_id": uuid,
            }
        left_upgrades = left.get("upgrades")
        right_upgrades = right.get("upgrades")
        if right_upgrades == left_upgrades:
            if _freeze(_oracle_canonical_audit_value(left)) != _freeze(
                _oracle_canonical_audit_value(right)
            ):
                return "issues", f"{prefix}_nonupgrade_card_changed", {
                    **details, "card_instance_id": uuid,
                }
            continue
        if right_upgrades != left_upgrades + 1:
            return "issues", f"{prefix}_upgrade_delta_invalid", {
                **details, "card_instance_id": uuid,
                "before_upgrades": left_upgrades,
                "after_upgrades": right_upgrades,
            }
        if uuid not in candidates:
            return "issues", f"{prefix}_upgraded_noneligible_card", {
                **details, "card_instance_id": uuid,
                "card_type": left.get("type"),
            }
        upgrades.append({
            "before": _oracle_clone(left), "after": _oracle_clone(right),
        })
    if len(upgrades) != details["expected_upgrade_count"]:
        return "issues", f"{prefix}_upgrade_count_mismatch", {
            **details, "observed_upgrade_count": len(upgrades),
        }

    numeric = {}
    for field in ("current_hp", "max_hp", "gold"):
        left = _finite_number(before.get(field))
        right = _finite_number(after.get(field))
        if left is None or right is None:
            return "unknown", f"{prefix}_resource_state_missing", details
        numeric[field] = right - left
    expected_gold = -price if type(price) is int else 0
    if numeric != {
        "current_hp": 0.0, "max_hp": 0.0, "gold": float(expected_gold),
    }:
        return "issues", f"{prefix}_unexpected_resource_delta", {
            **details, "observed_resource_delta": numeric,
            "expected_gold_delta": expected_gold,
        }
    potion_delta = _observable_collection_delta(
        before.get("potions"), after.get("potions")
    )
    relic_delta = _observable_collection_delta(
        before.get("relics"), after.get("relics")
    )
    if potion_delta is None or relic_delta is None:
        return "unknown", f"{prefix}_inventory_delta_missing", details
    if any(potion_delta.get(key) for key in ("added", "removed", "changed")):
        return "issues", f"{prefix}_unexpected_potion_delta", details
    added_relics = relic_delta.get("added") or []
    if not (
        len(added_relics) == 1
        and not relic_delta.get("removed") and not relic_delta.get("changed")
        and _oracle_game_id(
            added_relics[0].get("id") or added_relics[0].get("name")
        ) == effect_id
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == effect_id
    ):
        return "issues", f"{prefix}_relic_delta_mismatch", {
            **details, "observed_relic_delta": relic_delta,
        }
    before_keys = _observable_from_authoritative_state(before_state).get("keys")
    after_keys = _observable_from_authoritative_state(after_state).get("keys")
    if before_keys != after_keys:
        return "issues", f"{prefix}_unexpected_key_delta", details
    return "clear", f"{prefix}_settlement_clear", {
        **details,
        "upgrades": upgrades,
        "upgraded_card_instance_ids": sorted(
            _oracle_card_uuid(item["before"]) for item in upgrades
        ),
    }


def _oracle_apply_whetstone(
    value, raw, record, relic, price, reason, *, effect_id="whetstone",
    eligible_type="ATTACK",
):
    """Apply the JAR-locked random domain and, when selected, its settlement."""

    gold_delta = -price if type(price) is int else 0
    _oracle_numeric(value, 0, 0, gold_delta, reason)
    _oracle_empty_inventory(value, reason)
    _oracle_known(
        value, "relic_changes",
        {"gain": [_oracle_clone(relic)], "remove": [], "counter": []},
        "protocol_relic_target", reason,
    )
    _oracle_costs(value, reason, gold=price if type(price) is int else 0)
    _oracle_no_probability(value, reason)
    before_deck = _oracle_before_game(record).get("deck")
    _cards, candidates, pool_error = _oracle_whetstone_deck(
        before_deck, eligible_type=eligible_type, effect_id=effect_id,
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if pool_error is not None:
        value["uncertainty"].append(pool_error)
        review["unclassified"].append(
            f"{effect_id}_upgrade_candidate_pool"
        )
        return
    expected_count = min(2, len(candidates))
    random_effect = {
        "kind": f"{effect_id}_random_{eligible_type.lower()}_upgrades",
        "selection_mode": "random_without_replacement",
        "maximum_count": 2,
        "count": expected_count,
        "eligible_count": len(candidates),
        "eligible_card_instance_ids": sorted(candidates),
    }
    card_changes = {
        "gain": [], "remove": [], "upgrade": [], "transform": [],
        "random_upgrade": [_oracle_clone(random_effect)],
    }
    _oracle_known_domain(value, "card_changes", card_changes, reason)
    value["random_effects"] = [_oracle_clone(random_effect)]
    value["uncertainty"] = [
        f"{getattr(relic, 'name', None) or relic.get('name') if isinstance(relic, dict) else effect_id} "
        f"randomly upgrades at most two UUID-bound upgradable {eligible_type.title()}s"
    ]
    value["uncertainty_classification"] = {
        "status": "classified_random_domain",
        "authority": "installed_base_game_bytecode",
        "reason": (
            f"{effect_id}_at_most_two_upgradable_"
            f"{eligible_type.lower()}s_without_replacement"
        ),
    }
    if not _oracle_whetstone_selected(raw, record):
        return
    status, settlement_reason, settlement = _oracle_whetstone_settlement(
        record, relic, price, effect_id=effect_id,
        eligible_type=eligible_type,
    )
    value[f"{effect_id}_settlement"] = {
        "status": status, "reason": settlement_reason, **settlement,
    }
    if status == "unknown":
        review["unclassified"].append(
            f"{effect_id}_authoritative_settlement"
        )
    elif status == "issues":
        review["contradictions"].append({
            "field": f"{effect_id}_authoritative_settlement",
            "claimed": f"{effect_id} pickup",
            "independently_expected": random_effect,
            "reason": settlement_reason,
            "observed": settlement,
        })
    else:
        exact_changes = _oracle_clone(card_changes)
        exact_changes["upgrade"] = settlement["upgrades"]
        _oracle_known(
            value, "card_changes", exact_changes,
            "authoritative_protocol_delta", reason,
        )


def _oracle_map_entry_hp_delta(record, target):
    """Independently derive deterministic room-entry healing."""

    game = _oracle_before_game(record)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return None
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return 0
    kind = str((target or {}).get("kind") or "").casefold()
    symbol = str((target or {}).get("symbol") or "").upper()
    combat_entry = kind == "map_boss" or symbol in {"M", "E"}
    heal = 0
    if combat_entry and "bloodvial" in relic_ids:
        heal += 2
    if kind == "map_boss" and "pantograph" in relic_ids:
        heal += 25
    if symbol == "$" and "mealticket" in relic_ids:
        heal += 15
    if symbol == "R" and "eternalfeather" in relic_ids:
        heal += 3 * (len(game.get("deck") or []) // 5)
    return max(0, min(max_hp - current_hp, heal))


def _oracle_random_route_downstream_entry_hp_delta(record, target):
    """Return a proven room-entry heal outside a ``?`` route choice's scope.

    Selecting an unknown map node promises no immediate HP change.  That node
    may resolve directly into combat, where Blood Vial heals on entry before
    the next decision surface.  Stable protocol snapshots combine both
    transitions, so separate the independently proven downstream heal from the
    selected route consequence instead of blaming the map score.
    """

    if str((target or {}).get("kind") or "").casefold() != "map_node":
        return None
    if str((target or {}).get("symbol") or "").upper() != "?":
        return None
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    game = _oracle_before_game(record)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return None
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return 0
    room_phase_after = str(outcome.get("room_phase_after") or "").upper()
    room_type_after = str(outcome.get("room_type_after") or "").casefold()
    if room_phase_after == "COMBAT":
        if "bloodvial" not in relic_ids:
            return None
        return max(0, min(max_hp - current_hp, 2))
    # Some map layouts expose a shop as a late-bound ``?`` node.  Meal
    # Ticket fires when that node opens, after the route choice has already
    # settled.  Attribute the exact capped heal to this downstream room
    # transition so the selected route remains a zero-immediate-effect claim.
    if room_type_after in {"shoproom", "shop_room", "shop"}:
        if "mealticket" not in relic_ids:
            return None
        return max(0, min(max_hp - current_hp, 15))
    return None


def _oracle_event_downstream_combat_entry_hp_delta(record):
    """Return Blood Vial healing outside an event option's own effect.

    Event fight buttons can settle directly on the first combat frame.  The
    authoritative delta then contains both the selected event consequence and
    the deterministic Blood Vial room-entry heal.  Keep those scopes separate
    just as the unknown-map-node oracle does.
    """

    if str(record.get("phase") or "").upper() != "EVENT":
        return None
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    phase_after = str(outcome.get("phase_after") or "").upper()
    if (
        str(outcome.get("room_phase_before") or "").upper() != "EVENT"
        or str(outcome.get("room_phase_after") or "").upper() != "COMBAT"
        or not phase_after.startswith("COMBAT")
    ):
        return None
    game = _oracle_before_game(record)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return None
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return 0
    if "bloodvial" not in relic_ids:
        return None
    return max(0, min(max_hp - current_hp, 2))


def _oracle_map_entry_gold_delta(record, target):
    """Independently derive Maw Bank's exact room-entry gold effect."""

    game = _oracle_before_game(record)
    relics = game.get("relics")
    if not isinstance(relics, list):
        return None
    maw_banks = [
        relic for relic in relics
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "mawbank"
    ]
    if not maw_banks:
        return 0
    if len(maw_banks) != 1:
        return None
    counter = maw_banks[0].get("counter")
    if type(counter) is not int:
        return None
    kind = str((target or {}).get("kind") or "").casefold()
    if kind not in {"map_node", "map_boss"}:
        return None
    return 0 if counter == -2 else 12


def _oracle_before_game(record):
    state = record.get("authoritative_state_before") if isinstance(record, dict) else {}
    state = state if isinstance(state, dict) else {}
    game = state.get("game_state")
    return game if isinstance(game, dict) else {}


_ORACLE_GRID_OPERATION_FLAGS = {
    "for_upgrade": "grid_upgrade",
    "for_transform": "grid_transform",
    "for_purge": "grid_purge",
}

# This is an independently maintained copy of the protocol audit projection,
# not an import from the producer.  A raw bridge card retains presentation and
# transport fields such as ``uuid`` while the canonical option target does
# not.  Compare the complete canonical projection so representation-only raw
# fields cannot create a false mismatch, without weakening any audited fact.
_ORACLE_CANONICAL_AUDIT_FIELDS = {
    "kind", "id", "name", "type", "rarity", "upgrades", "price",
    "card_instance_id", "potion_instance_id", "reward_type", "gold",
    "x", "y", "symbol", "act", "event_id", "label", "text", "slot",
    "cost", "is_playable", "has_target", "damage", "base_damage",
    "block", "base_block", "magic_number", "exhausts", "action",
    "consequence_contract", "probabilistic_outcomes", "probability",
    "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
    "relic_changes", "potion_changes", "curse", "current_cost",
    "future_costs", "gain", "remove", "upgrade", "transform", "replace",
    "counter", "amount", "authority", "reason", "status",
    "omamori_applicable", "omamori_charges_consumed", "mechanism_id",
    "choice_index", "operation", "potion_id", "parent_phase",
    "can_discard", "can_use", "requires_target", "neow_contract",
    "contract_version", "contract_kind", "reward_kind", "drawback_kind",
    "parameters", "hp_bonus", "cursed", "drawback_def_kind",
    "screen_num", "event_class", "event_stage", "resource_effect",
    "parent_choice_context", "source_option_id", "source_choice_index",
    "select_count", "selection_domain", "cards", "selection_context",
    "current_action",
    "relic_id", "card_type", "in_bottle_flame", "in_bottle_lightning",
    "in_bottle_tornado",
    "max_cards", "can_pick_zero", "selected",
    "audit_projection_version",
}
_ORACLE_CANONICAL_CONTAINER_FIELDS = {
    "card", "relic", "potion", "item", "reward", "link",
}


def _oracle_canonical_audit_value(value):
    if isinstance(value, list):
        return [_oracle_canonical_audit_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _oracle_canonical_audit_value(item)
            for key, item in value.items()
            if key in _ORACLE_CANONICAL_AUDIT_FIELDS
            or key in _ORACLE_CANONICAL_CONTAINER_FIELDS
        }
    return value


def _oracle_grid_target(record, target):
    """Recover a GRID operation from authoritative state, never producer facts."""

    screen = _oracle_before_game(record).get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    if any(
        type(screen.get(flag)) is not bool
        for flag in _ORACLE_GRID_OPERATION_FLAGS
    ):
        return None, None, "grid_operation_flags_missing"
    operations = [
        operation
        for flag, operation in _ORACLE_GRID_OPERATION_FLAGS.items()
        if screen[flag]
    ]
    if len(operations) == 0:
        game = _oracle_before_game(record)
        current_action = str(game.get("current_action") or "")
        combat = game.get("combat_state")
        combat = combat if isinstance(combat, dict) else {}
        if (
            str(game.get("room_phase") or "").upper() == "COMBAT"
            and current_action == "BetterDiscardPileToHandAction"
            and isinstance(combat.get("discard_pile"), list)
            and _oracle_projection_v2(target)
        ):
            operations = ["grid_combat_discard_to_hand"]
        elif (
            str(game.get("room_phase") or "").upper() == "COMBAT"
            and current_action == "DiscardPileToTopOfDeckAction"
            and isinstance(combat.get("discard_pile"), list)
            and _oracle_projection_v2(target)
        ):
            operations = ["grid_combat_discard_to_top"]
        elif (
            str(game.get("room_phase") or "").upper() == "COMBAT"
            and current_action == "SkillFromDeckToHandAction"
            and isinstance(combat.get("draw_pile"), list)
            and _oracle_projection_v2(target)
        ):
            operations = ["grid_combat_draw_to_hand"]
        else:
            operations = []
    if len(operations) == 0:
        # CommunicationMod versions predating the parent-context projection
        # can open a Bottled Flame/Lightning/Tornado GRID with every generic
        # ``for_*`` flag false.  The transition is still fully authoritative:
        # exactly one deck card flips one bottle flag in the immediate after
        # frame.  Recover that operation from the state delta only; never use
        # the producer's strategy reason or candidate fields.
        before_deck = _oracle_before_game(record).get("deck")
        after_state = record.get("authoritative_state_after")
        after_state = after_state if isinstance(after_state, dict) else {}
        after_game = after_state.get("game_state")
        after_game = after_game if isinstance(after_game, dict) else {}
        after_deck = after_game.get("deck")
        if isinstance(before_deck, list) and isinstance(after_deck, list):
            after_by_uuid = {
                card.get("card_instance_id"): card
                for card in after_deck
                if isinstance(card, dict)
                and isinstance(card.get("card_instance_id"), str)
            }
            inferred = []
            for before_card in before_deck:
                if not isinstance(before_card, dict):
                    continue
                uuid = before_card.get("card_instance_id")
                after_card = after_by_uuid.get(uuid)
                if not isinstance(after_card, dict):
                    continue
                for flag, operation in (
                    ("in_bottle_flame", "grid_bottle_attack"),
                    ("in_bottle_lightning", "grid_bottle_skill"),
                    ("in_bottle_tornado", "grid_bottle_power"),
                ):
                    if (
                        before_card.get(flag) is False
                        and after_card.get(flag) is True
                        and all(
                            before_card.get(other) == after_card.get(other)
                            for other in (
                                "in_bottle_flame",
                                "in_bottle_lightning",
                                "in_bottle_tornado",
                            )
                            if other != flag
                        )
                    ):
                        inferred.append(operation)
            if len(inferred) == 1:
                operations = inferred
    if len(operations) == 0:
        parent = screen.get("parent_choice_context")
        parent = parent if isinstance(parent, dict) else {}
        contract = parent.get("neow_contract")
        contract = contract if isinstance(contract, dict) else {}
        future = _ORACLE_NEOW_FUTURE_REWARDS.get(
            contract.get("reward_kind")
        )
        expected_operation = (
            future.get("operation")
            if isinstance(future, dict)
            and future.get("kind") == "neow_grid_selection"
            else None
        )
        valid_neow_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "NEOW"
            and parent.get("mechanism_id")
            == _oracle_neow_mechanism_id(contract)
            and expected_operation in {"upgrade", "transform", "remove"}
            and parent.get("operation") == expected_operation
            and parent.get("select_count") == future.get("select_count")
        )
        event_contract = parent.get("event_contract")
        checked_event, event_status, _event_reason = (
            _oracle_staged_event_contract(
                event_contract, "duplicator", game
            )
        )
        valid_duplicator_parent = bool(
            event_status == "clear"
            and isinstance(checked_event, dict)
            and checked_event.get("original_button_index") == 0
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and parent.get("mechanism_id")
            == _oracle_event_mechanism_id(checked_event)
            and parent.get("operation") == "duplicate"
            and parent.get("select_count") == 1
        )
        offered_note_card = parent.get("offered_card")
        offered_note_card = (
            offered_note_card if isinstance(offered_note_card, dict) else {}
        )
        note_mechanism = {
            "event_id": "NoteForYourself",
            "event_class": (
                "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
            ),
            "original_button_index": 0,
            "operation": "note_exchange",
            "select_count": 1,
            "offered_card": offered_note_card,
        }
        valid_note_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _oracle_game_id(parent.get("event_id"))
            == "noteforyourself"
            and parent.get("event_class") == note_mechanism["event_class"]
            and parent.get("original_button_index") == 0
            and parent.get("operation") == "note_exchange"
            and parent.get("select_count") == 1
            and offered_note_card.get("id")
            and offered_note_card.get("card_instance_id")
            and parent.get("mechanism_id")
            == _oracle_stable_id("event-grid-mechanism", note_mechanism)
        )
        drug_dealer_mechanism = {
            "event_id": "Drug Dealer",
            "event_class": "com.megacrit.cardcrawl.events.city.DrugDealer",
            "original_button_index": 1,
            "operation": "transform",
            "select_count": 2,
        }
        valid_drug_dealer_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _oracle_game_id(parent.get("event_id")) == "drugdealer"
            and parent.get("event_class")
            == drug_dealer_mechanism["event_class"]
            and parent.get("original_button_index") == 1
            and parent.get("operation") == "transform"
            and parent.get("select_count") == 2
            and parent.get("mechanism_id")
            == _oracle_stable_id(
                "event-grid-mechanism", drug_dealer_mechanism
            )
        )
        library_mechanism = {
            "event_id": "The Library",
            "event_class": "com.megacrit.cardcrawl.events.city.TheLibrary",
            "original_button_index": 0,
            "operation": "gain",
            "select_count": 1,
            "selection_domain": "library_card_offering",
        }
        valid_library_parent = bool(
            parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "EVENT"
            and _oracle_game_id(parent.get("event_id")) == "thelibrary"
            and parent.get("event_class") == library_mechanism["event_class"]
            and parent.get("original_button_index") == 0
            and parent.get("operation") == "gain"
            and parent.get("select_count") == 1
            and parent.get("selection_domain") == "library_card_offering"
            and parent.get("mechanism_id")
            == _oracle_stable_id("event-grid-mechanism", library_mechanism)
        )
        bottle = {
            "bottledflame": ("bottle_attack", "ATTACK"),
            "bottledlightning": ("bottle_skill", "SKILL"),
            "bottledtornado": ("bottle_power", "POWER"),
        }.get(_oracle_game_id(parent.get("relic_id")))
        valid_bottle_parent = bool(
            bottle is not None
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "COMBAT_REWARD"
            and parent.get("operation") == bottle[0]
            and parent.get("card_type") == bottle[1]
            and parent.get("select_count") == 1
        )
        boss_relic_grid = {
            "astrolabe": ("transform", 3, "grid_transform"),
            "emptycage": ("remove", 2, "grid_remove"),
        }.get(_oracle_game_id(parent.get("relic_id")))
        valid_boss_relic_parent = bool(
            boss_relic_grid is not None
            and parent.get("authority") == "accepted_protocol_choice"
            and parent.get("parent_phase") == "BOSS_REWARD"
            and parent.get("operation") == boss_relic_grid[0]
            and parent.get("select_count") == boss_relic_grid[1]
        )
        if valid_neow_parent:
            operations = [f"grid_{expected_operation}"]
        elif valid_duplicator_parent:
            operations = ["grid_duplicate"]
        elif valid_note_parent:
            operations = ["grid_note_exchange"]
        elif valid_drug_dealer_parent:
            operations = ["grid_transform"]
        elif valid_library_parent:
            operations = ["grid_gain"]
        elif valid_bottle_parent:
            operations = [f"grid_{bottle[0]}"]
        elif valid_boss_relic_parent:
            operations = [boss_relic_grid[2]]
        else:
            return None, None, "grid_parent_choice_context_invalid"
    if len(operations) != 1:
        return None, None, "grid_operation_not_unique"
    target_card = target.get("card")
    target_card = target_card if isinstance(target_card, dict) else {}
    target_uuid = target.get("card_instance_id")
    nested_uuid = target_card.get("card_instance_id")
    if not (
        isinstance(target_uuid, str) and target_uuid
        and isinstance(nested_uuid, str) and nested_uuid == target_uuid
    ):
        return None, None, "grid_target_uuid_missing_or_mismatched"
    matches = [
        card for card in (screen.get("cards") or [])
        if isinstance(card, dict)
        and card.get("card_instance_id") == target_uuid
    ]
    if len(matches) != 1:
        return None, None, "grid_target_uuid_not_unique_on_screen"
    canonical_target = _oracle_canonical_audit_value(target_card)
    canonical_visible = _oracle_canonical_audit_value(matches[0])
    if _freeze(canonical_target) != _freeze(canonical_visible):
        return None, None, "grid_target_card_facts_mismatch"
    if operations[0] in {
        "grid_combat_discard_to_hand", "grid_combat_discard_to_top",
        "grid_combat_draw_to_hand",
    }:
        game = _oracle_before_game(record)
        source_pile = (
            "draw_pile"
            if operations[0] == "grid_combat_draw_to_hand"
            else "discard_pile"
        )
        source_matches = [
            card for card in (
                (game.get("combat_state") or {}).get(source_pile) or []
            )
            if isinstance(card, dict)
            and card.get("card_instance_id") == target_uuid
        ]
        if len(source_matches) != 1:
            return None, None, "grid_combat_source_binding_mismatch"
    if operations[0].startswith("grid_bottle_"):
        expected_type = {
            "grid_bottle_attack": "ATTACK",
            "grid_bottle_skill": "SKILL",
            "grid_bottle_power": "POWER",
        }[operations[0]]
        if str(canonical_visible.get("type") or "").upper() != expected_type:
            return None, None, "grid_bottle_card_type_mismatch"
    return operations[0], _oracle_clone(canonical_visible), None


def _oracle_hand_select_target(record, target):
    """Independently bind a transient HAND_SELECT card and action context."""

    game = _oracle_before_game(record)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    current_action = game.get("current_action")
    max_cards = screen.get("max_cards")
    can_pick_zero = screen.get("can_pick_zero")
    if not isinstance(current_action, str) or not current_action:
        return None, None, "hand_select_current_action_missing"
    if type(max_cards) is not int or max_cards < 0:
        return None, None, "hand_select_max_cards_invalid"
    if type(can_pick_zero) is not bool:
        return None, None, "hand_select_can_pick_zero_missing"
    target_card = target.get("card")
    target_card = target_card if isinstance(target_card, dict) else {}
    target_uuid = target.get("card_instance_id")
    if not (
        isinstance(target_uuid, str) and target_uuid
        and target_card.get("card_instance_id") == target_uuid
    ):
        return None, None, "hand_select_target_uuid_missing_or_mismatched"
    matches = [
        card for card in (screen.get("hand") or [])
        if isinstance(card, dict)
        and card.get("card_instance_id") == target_uuid
    ]
    if len(matches) != 1:
        return None, None, "hand_select_target_uuid_not_unique_on_screen"
    canonical_target = _oracle_canonical_audit_value(target_card)
    canonical_visible = _oracle_canonical_audit_value(matches[0])
    if _freeze(canonical_target) != _freeze(canonical_visible):
        return None, None, "hand_select_target_card_facts_mismatch"
    return _oracle_clone(canonical_visible), {
        "current_action": current_action,
        "max_cards": max_cards,
        "can_pick_zero": can_pick_zero,
    }, None


def _oracle_shop_purge(record, target):
    game = _oracle_before_game(record)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    price = target.get("price")
    screen_price = screen.get("purge_cost")
    gold = game.get("gold")
    if screen.get("purge_available") is not True:
        return None, "shop_purge_not_protocol_available"
    if (
        type(price) is not int or price < 0
        or type(screen_price) is not int or screen_price < 0
        or price != screen_price
    ):
        return None, "shop_purge_cost_binding_invalid"
    if type(gold) is not int or gold < price:
        return None, "shop_purge_affordability_not_proven"
    return price, None


def _oracle_shop_purge_followup(price=None):
    return [{
        "kind": "shop_purge_grid_selection",
        "operation": "purge",
        "select_count": 1,
        "identity_binding": "subsequent_grid_card_instance_id",
        "commit_timing": "after_grid_confirmation",
        "gold_cost": price,
    }]


def _oracle_grid_confirmation_followup(operation, selected_card):
    return [{
        "kind": "grid_confirmation_effect",
        "operation": operation,
        "selected_card": _oracle_clone(selected_card),
        "commit_timing": "after_grid_confirmation",
    }]


def _oracle_has_relic(record, relic_id):
    wanted = _oracle_game_id(relic_id)
    return any(
        _oracle_game_id(relic.get("id") or relic.get("name")) == wanted
        for relic in (_oracle_before_game(record).get("relics") or [])
        if isinstance(relic, dict)
    )


_ORACLE_NEOW_REWARD_KINDS = {
    "RANDOM_COLORLESS_2", "THREE_CARDS", "ONE_RANDOM_RARE_CARD",
    "REMOVE_CARD", "UPGRADE_CARD", "RANDOM_COLORLESS", "TRANSFORM_CARD",
    "THREE_SMALL_POTIONS", "RANDOM_COMMON_RELIC", "TEN_PERCENT_HP_BONUS",
    "HUNDRED_GOLD", "THREE_ENEMY_KILL", "REMOVE_TWO",
    "TRANSFORM_TWO_CARDS", "ONE_RARE_RELIC", "THREE_RARE_CARDS",
    "TWO_FIFTY_GOLD", "TWENTY_PERCENT_HP_BONUS", "BOSS_RELIC",
}
_ORACLE_NEOW_DRAWBACK_KINDS = {
    "NONE", "TEN_PERCENT_HP_LOSS", "NO_GOLD", "CURSE", "PERCENT_DAMAGE",
}
_ORACLE_NEOW_STARTER_RELIC_IDS = {
    "burningblood", "crackedcore", "purewater", "ringofthesnake",
}
_ORACLE_NEOW_FUTURE_REWARDS = {
    "RANDOM_COLORLESS_2": {
        "kind": "neow_card_reward", "domain": "colorless_cards",
        "rarities": ["RARE"], "visible_count": 3, "choose_count": 1,
        "selection_mode": "player_choice",
    },
    "RANDOM_COLORLESS": {
        "kind": "neow_card_reward", "domain": "colorless_cards",
        "rarities": ["UNCOMMON", "RARE"], "visible_count": 3,
        "choose_count": 1, "selection_mode": "player_choice",
    },
    "THREE_RARE_CARDS": {
        "kind": "neow_card_reward", "domain": "character_cards",
        "rarities": ["RARE"], "visible_count": 3, "choose_count": 1,
        "selection_mode": "player_choice",
    },
    "THREE_CARDS": {
        "kind": "neow_card_reward", "domain": "character_cards",
        "rarities": ["COMMON", "UNCOMMON", "RARE"], "visible_count": 3,
        "choose_count": 1, "selection_mode": "player_choice",
    },
    "UPGRADE_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "upgrade", "select_count": 1,
        "selection_mode": "player_choice",
    },
    "REMOVE_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "remove", "select_count": 1,
        "selection_mode": "player_choice",
    },
    "REMOVE_TWO": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "remove", "select_count": 2,
        "selection_mode": "player_choice",
    },
    "TRANSFORM_CARD": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "transform", "select_count": 1,
        "selection_mode": "player_choice",
        "result_domain": "character_cards", "result_selection_mode": "random",
    },
    "TRANSFORM_TWO_CARDS": {
        "kind": "neow_grid_selection", "domain": "current_deck",
        "operation": "transform", "select_count": 2,
        "selection_mode": "player_choice",
        "result_domain": "character_cards", "result_selection_mode": "random",
    },
    "THREE_SMALL_POTIONS": {
        "kind": "neow_potion_rewards", "domain": "potion_pool",
        "reward_count": 3, "selection_mode": "claim_visible_rewards",
    },
}
_ORACLE_NEOW_RANDOM_REWARDS = {
    "ONE_RANDOM_RARE_CARD": {
        "kind": "card_gain", "domain": "character_cards",
        "rarities": ["RARE"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "RANDOM_COMMON_RELIC": {
        "kind": "relic_gain", "domain": "common_relics",
        "rarities": ["COMMON"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "ONE_RARE_RELIC": {
        "kind": "relic_gain", "domain": "rare_relics",
        "rarities": ["RARE"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
    "BOSS_RELIC": {
        "kind": "relic_gain", "domain": "boss_relics",
        "rarities": ["BOSS"], "count": 1, "timing": "immediate",
        "selection_mode": "random",
    },
}


def _oracle_clone(value):
    return json.loads(json.dumps(value, ensure_ascii=True))


def _oracle_neow_mechanism_id(contract):
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"neow-mechanism:{hashlib.sha256(encoded).hexdigest()[:20]}"


def _oracle_validate_neow_contract(target, record):
    contract = target.get("neow_contract")
    if not isinstance(contract, dict):
        return None, "neow_contract_missing", False
    if contract.get("contract_version") != 1:
        return None, "neow_contract_version_invalid", True
    kind = contract.get("contract_kind")
    if kind == "NEOW_DIALOG_ADVANCE":
        valid = bool(
            set(contract) == {
                "contract_version", "contract_kind", "screen_num",
                "resource_effect",
            }
            and type(contract.get("screen_num")) is int
            and contract.get("screen_num") != 3
            and contract.get("resource_effect") == "NONE"
        )
        if not valid:
            return None, "neow_dialog_contract_invalid", True
    elif kind == "NEOW_REWARD":
        parameters = contract.get("parameters")
        game = _oracle_before_game(record)
        max_hp = game.get("max_hp")
        reward_kind = contract.get("reward_kind")
        drawback_kind = contract.get("drawback_kind")
        valid = bool(
            set(contract) == {
                "contract_version", "contract_kind", "reward_kind",
                "drawback_kind", "parameters",
            }
            and reward_kind in _ORACLE_NEOW_REWARD_KINDS
            and drawback_kind in _ORACLE_NEOW_DRAWBACK_KINDS
            and isinstance(parameters, dict)
            and set(parameters) == {
                "hp_bonus", "cursed", "drawback_def_kind",
            }
            and type(parameters.get("hp_bonus")) is int
            and type(max_hp) is int
            and parameters.get("hp_bonus") == max(0, max_hp // 10)
            and type(parameters.get("cursed")) is bool
            and parameters.get("cursed") is (drawback_kind == "CURSE")
            and parameters.get("drawback_def_kind")
            == (None if drawback_kind == "NONE" else drawback_kind)
        )
        if not valid:
            return None, "neow_reward_contract_invalid", True
    else:
        return None, "neow_contract_kind_invalid", True
    expected_mechanism = _oracle_neow_mechanism_id(contract)
    if target.get("mechanism_id") != expected_mechanism:
        return None, "neow_mechanism_id_invalid", True
    return _oracle_clone(contract), None, False


def _oracle_known_domain(value, field, result, reason):
    value[field] = _oracle_clone(result)
    value["field_knowledge"][field] = {
        "status": "known_domain", "authority": "independent_mechanics_table",
        "reason": reason,
    }


def _oracle_neow_starter_relic(record):
    matches = [
        relic for relic in (_oracle_before_game(record).get("relics") or [])
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        in _ORACLE_NEOW_STARTER_RELIC_IDS
    ]
    return _oracle_clone(matches[0]) if len(matches) == 1 else None


def _oracle_neow_omamori(record):
    matches = [
        relic for relic in (_oracle_before_game(record).get("relics") or [])
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name")) == "omamori"
    ]
    return matches[0] if len(matches) == 1 else None


MAUSOLEUM_TRADE_CONTRACT_VERSION = 1
_ORACLE_MAUSOLEUM_EVENT_ID = "themausoleum"
_ORACLE_MAUSOLEUM_EVENT_NAME = "The Mausoleum"
_ORACLE_MAUSOLEUM_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.city.TheMausoleum"
)
_ORACLE_MAUSOLEUM_RELIC_DOMAIN = (
    "base_game_non_boss_relic_pool"
)

# Explicit overrides remain for relics whose settlement metadata is narrower
# than the shared pickup table.  Ordinary deterministic passive/immediate
# pickups are resolved through the independently maintained global relic
# mechanics tables below; keeping a second partial allow-list here previously
# made a valid Thread and Needle reward permanently inconclusive.
_ORACLE_MAUSOLEUM_SETTLED_RELICS = {
    "anchor": {
        "tier": "COMMON",
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": 0,
        "block_delta": 0,
    },
    # Historical v3 traces predate relic-tier serialization.  Keep the exact
    # observed base-game identity so those settlements remain auditable; new
    # traces prove this through the protocol-visible tier below.
    "threadandneedle": {
        "tier": "RARE",
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": 0,
        "block_delta": 0,
    },
}


def _oracle_mausoleum_relic_pickup(relic):
    """Return exact immediate pickup effects for one non-boss random relic."""

    if not isinstance(relic, dict):
        return None
    relic_id = _oracle_game_id(relic.get("id") or relic.get("name"))
    override = _ORACLE_MAUSOLEUM_SETTLED_RELICS.get(relic_id)
    if override is not None:
        return _oracle_clone(override)
    tier = str(relic.get("tier") or relic.get("rarity") or "").upper()
    if tier not in {"COMMON", "UNCOMMON", "RARE"}:
        return None
    immediate = _ORACLE_IMMEDIATE_RELIC_PICKUPS.get(relic_id)
    if immediate is not None:
        return {
            "tier": tier,
            "hp_delta": immediate["hp_delta"],
            "max_hp_delta": immediate["max_hp_delta"],
            "gold_delta": immediate["gold_delta"],
            "block_delta": 0,
        }
    if relic_id in _ORACLE_PASSIVE_RELIC_PICKUPS:
        return {
            "tier": tier,
            "hp_delta": 0,
            "max_hp_delta": 0,
            "gold_delta": 0,
            "block_delta": 0,
        }
    return None

# This list is used only to turn an impossible, fully observed boss-relic
# result into an issue.  Other unsupported identities remain inconclusive so
# a modded/event/shop relic can never be mistaken for a valid random reward.
_ORACLE_BOSS_RELIC_IDS = {
    "astrolabe", "blackblood", "blackstar", "bustedcrown",
    "callingbell", "coffeedripper", "cursedkey", "ectoplasm",
    "emptycage", "frozencore", "fusionhammer", "holywater",
    "hoveringkite", "inserter", "markofpain", "nuclearbattery",
    "pandorasbox", "philosophersstone", "ringoftheserpent",
    "runiccube", "runicdome", "runicpyramid", "sacredbark",
    "slaverscollar", "sneckoeye", "sozu", "tinyhouse",
    "velvetchoker", "violetlotus", "wristblade",
}


def _oracle_mausoleum_event_contract(value, ascension):
    """Validate one reflection-backed contract against base-game mechanics."""

    if value is None:
        return None, "inconclusive", "mausoleum_typed_contract_missing"
    if not isinstance(value, dict):
        return None, "issues", "mausoleum_typed_contract_not_an_object"
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "event_stage", "original_button_index", "option_kind",
        "instance_parameters", "parameters",
    }
    if set(value) != required:
        return None, "issues", "mausoleum_typed_contract_shape_mismatch"
    if (
        value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or value.get("event_id") != _ORACLE_MAUSOLEUM_EVENT_NAME
        or value.get("event_class") != _ORACLE_MAUSOLEUM_EVENT_CLASS
    ):
        return None, "issues", "mausoleum_typed_contract_identity_mismatch"
    expected_percent = 100 if ascension >= 15 else 50
    instance = value.get("instance_parameters")
    if (
        not isinstance(instance, dict)
        or set(instance) != {"curse_probability_percent"}
        or type(instance.get("curse_probability_percent")) is not int
        or instance["curse_probability_percent"] != expected_percent
    ):
        return None, "issues", "mausoleum_typed_contract_instance_mismatch"
    stage = value.get("event_stage")
    original = value.get("original_button_index")
    option_kind = value.get("option_kind")
    parameters = value.get("parameters")
    expected_parameters = {
        ("INTRO", 0, "OPEN"): {
            "random_relic_count": 1,
            "relic_selection_mode": "RANDOM_TIER_THEN_SCREENLESS_RELIC",
            "curse_card_id": "Writhe",
            "curse_probability_percent": expected_percent,
        },
        ("INTRO", 1, "LEAVE"): {},
        ("RESULT", 0, "CONTINUE"): {},
    }.get((stage, original, option_kind))
    if expected_parameters is None:
        return None, "issues", "mausoleum_typed_contract_stage_mismatch"
    if (
        not isinstance(parameters, dict)
        or _freeze(parameters) != _freeze(expected_parameters)
    ):
        return None, "issues", "mausoleum_typed_contract_parameters_mismatch"
    return (
        _oracle_clone(value), "clear",
        "mausoleum_typed_contract_exact",
    )


def _oracle_mausoleum_surface(record, raw_choice, before_game, ascension):
    """Bind one selected option to the complete authoritative event surface."""

    screen = before_game.get("screen_state")
    if not isinstance(screen, dict):
        return None, "inconclusive", "mausoleum_authoritative_screen_missing"
    if _oracle_game_id(screen.get("event_id")) != _ORACLE_MAUSOLEUM_EVENT_ID:
        return None, "issues", "mausoleum_authoritative_event_id_mismatch"
    options = screen.get("options")
    if not isinstance(options, list):
        return None, "inconclusive", "mausoleum_authoritative_options_missing"
    if not options:
        return None, "issues", "mausoleum_authoritative_surface_empty"

    by_original = {}
    enabled_by_choice = {}
    canonical_contracts = {}
    for option in options:
        if not isinstance(option, dict):
            return None, "issues", "mausoleum_surface_option_not_an_object"
        original = option.get("original_button_index")
        disabled = option.get("disabled")
        if type(original) is not int or original < 0:
            return None, "inconclusive", "mausoleum_original_button_index_missing"
        if original in by_original:
            return None, "issues", "mausoleum_original_button_index_duplicate"
        if type(disabled) is not bool:
            return None, "inconclusive", "mausoleum_disabled_flag_missing"
        choice_index = option.get("choice_index")
        if disabled:
            if "choice_index" in option:
                return None, "issues", "mausoleum_disabled_choice_index_present"
        else:
            if type(choice_index) is not int or choice_index < 0:
                return None, "inconclusive", "mausoleum_choice_index_missing"
            if choice_index in enabled_by_choice:
                return None, "issues", "mausoleum_choice_index_duplicate"
            enabled_by_choice[choice_index] = option
        contract, status, reason = _oracle_mausoleum_event_contract(
            option.get("event_contract"), ascension
        )
        if status != "clear":
            return None, status, reason
        if contract["original_button_index"] != original:
            return None, "issues", "mausoleum_contract_original_index_mismatch"
        by_original[original] = option
        canonical_contracts[original] = contract

    if sorted(by_original) != list(range(len(options))):
        return None, "issues", "mausoleum_original_index_surface_noncontiguous"
    if sorted(enabled_by_choice) != list(range(len(enabled_by_choice))):
        return None, "issues", "mausoleum_choice_index_surface_noncontiguous"
    stages = {
        contract["event_stage"] for contract in canonical_contracts.values()
    }
    instances = {
        _freeze(contract["instance_parameters"])
        for contract in canonical_contracts.values()
    }
    if len(stages) != 1 or len(instances) != 1:
        return None, "issues", "mausoleum_surface_contracts_disagree"
    stage = next(iter(stages))
    expected_originals = {0, 1} if stage == "INTRO" else {0}
    if set(by_original) != expected_originals:
        return None, "issues", "mausoleum_stage_surface_mismatch"
    if any(option.get("disabled") is not False for option in options):
        return None, "issues", "mausoleum_stage_has_disabled_option"
    if stage == "INTRO":
        writhe = by_original[0].get("card")
        if (
            not isinstance(writhe, dict)
            or _oracle_game_id(writhe.get("id") or writhe.get("card_id"))
            != "writhe"
            or str(writhe.get("type") or "").upper() != "CURSE"
            or str(writhe.get("rarity") or "").upper() != "CURSE"
            or by_original[1].get("card") is not None
        ):
            return None, "issues", "mausoleum_writhe_preview_mismatch"

    choice_index = raw_choice.get("choice_index")
    if type(choice_index) is not int:
        return None, "inconclusive", "mausoleum_selected_choice_index_missing"
    selected = enabled_by_choice.get(choice_index)
    if selected is None:
        return None, "issues", "mausoleum_selected_choice_index_mismatch"
    target = raw_choice.get("target")
    target = target if isinstance(target, dict) else {}
    original = target.get("original_button_index")
    if type(original) is not int:
        return None, "inconclusive", "mausoleum_target_original_index_missing"
    if selected.get("original_button_index") != original:
        return None, "issues", "mausoleum_target_original_index_mismatch"
    if (
        "choice_index" in target
        and target.get("choice_index") != choice_index
    ):
        return None, "issues", "mausoleum_target_choice_index_mismatch"
    if (
        "card" in target
        and _freeze(_oracle_canonical_audit_value(target.get("card")))
        != _freeze(_oracle_canonical_audit_value(selected.get("card")))
    ):
        return None, "issues", "mausoleum_target_card_binding_mismatch"
    target_contract, status, reason = _oracle_mausoleum_event_contract(
        target.get("event_contract"), ascension
    )
    if status != "clear":
        return None, status, reason
    authoritative_contract = canonical_contracts[original]
    if _freeze(target_contract) != _freeze(authoritative_contract):
        return None, "issues", "mausoleum_target_contract_binding_mismatch"
    return ({
        "event_stage": stage,
        "choice_index": choice_index,
        "original_button_index": original,
        "event_contract": authoritative_contract,
    }, "clear", "mausoleum_authoritative_surface_bound")


_ORACLE_STAGED_EVENT_IDENTITIES = {
    "worldofgoop": (
        "World of Goop",
        "com.megacrit.cardcrawl.events.exordium.GoopPuddle",
    ),
    "thecleric": (
        "The Cleric",
        "com.megacrit.cardcrawl.events.exordium.Cleric",
    ),
    "designer": (
        "Designer",
        "com.megacrit.cardcrawl.events.shrines.Designer",
    ),
    "cursedtome": (
        "Cursed Tome",
        "com.megacrit.cardcrawl.events.city.CursedTome",
    ),
    "knowingskull": (
        "Knowing Skull",
        "com.megacrit.cardcrawl.events.city.KnowingSkull",
    ),
    "deadadventurer": (
        "Dead Adventurer",
        "com.megacrit.cardcrawl.events.exordium.DeadAdventurer",
    ),
    "scrapooze": (
        "Scrap Ooze",
        "com.megacrit.cardcrawl.events.exordium.ScrapOoze",
    ),
    "facetrader": (
        "Face Trader",
        "com.megacrit.cardcrawl.events.shrines.FaceTrader",
    ),
    "duplicator": (
        "Duplicator",
        "com.megacrit.cardcrawl.events.shrines.Duplicator",
    ),
    "bonfireelementals": (
        "Bonfire Elementals",
        "com.megacrit.cardcrawl.events.shrines.Bonfire",
    ),
}
_ORACLE_CURSED_TOME_BOOK_IDS = (
    "Necronomicon", "Enchiridion", "Nilry's Codex",
)


def _oracle_exact_typed_value(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _oracle_exact_typed_value(value[key], expected[key])
            for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _oracle_exact_typed_value(item, wanted)
            for item, wanted in zip(value, expected)
        )
    return value == expected


def _oracle_event_mechanism_id(contract):
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "event-mechanism:" + hashlib.sha256(encoded).hexdigest()[:20]


def _oracle_stable_id(prefix, value):
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:20]}"


def _oracle_cursed_tome_pool(before_game):
    relics = before_game.get("relics")
    if not isinstance(relics, list):
        return None
    owned = set()
    for relic in relics:
        if not isinstance(relic, dict):
            return None
        relic_id = relic.get("id") or relic.get("name")
        if not isinstance(relic_id, str) or not relic_id:
            return None
        if relic_id in _ORACLE_CURSED_TOME_BOOK_IDS:
            owned.add(relic_id)
    remaining = [
        relic_id for relic_id in _ORACLE_CURSED_TOME_BOOK_IDS
        if relic_id not in owned
    ]
    return remaining or ["Circlet"]


def _oracle_staged_event_contract(value, event_token, before_game):
    """Validate one base-game A0 event contract without producer claims."""

    identity = _ORACLE_STAGED_EVENT_IDENTITIES.get(event_token)
    if identity is None:
        return None, "not_applicable", "not_supported_staged_event"
    if value is None:
        return None, "inconclusive", "typed_event_contract_missing"
    if not isinstance(value, dict):
        return None, "issues", "typed_event_contract_not_an_object"
    is_goop = event_token == "worldofgoop"
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "original_button_index", "option_kind", "parameters",
    }
    if not is_goop:
        required |= {"event_stage", "instance_parameters"}
    if set(value) != required:
        return None, "issues", "typed_event_contract_shape_mismatch"
    if (
        value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or value.get("event_id") != identity[0]
        or value.get("event_class") != identity[1]
        or type(value.get("original_button_index")) is not int
        or value["original_button_index"] < 0
        or not isinstance(value.get("option_kind"), str)
        or not isinstance(value.get("parameters"), dict)
    ):
        return None, "issues", "typed_event_contract_identity_mismatch"

    original = value["original_button_index"]
    kind = value["option_kind"]
    parameters = value["parameters"]
    if is_goop:
        expected = {
            (0, "GATHER"): {"gold_gain": 75, "hp_damage": 11},
            (1, "LEAVE"): None,
            (0, "CONTINUE"): {},
        }.get((original, kind), "invalid")
        if expected == "invalid":
            return None, "issues", "goop_contract_stage_mismatch"
        if kind == "LEAVE":
            gold = before_game.get("gold")
            loss = parameters.get("gold_loss")
            if (
                set(parameters) != {"gold_loss"}
                or type(gold) is not int or gold < 0
                or type(loss) is not int
                or (
                    loss != gold if gold < 20
                    else not 20 <= loss <= min(50, gold)
                )
            ):
                return None, "issues", "goop_gold_loss_contract_mismatch"
        elif not _oracle_exact_typed_value(parameters, expected):
            return None, "issues", "goop_contract_parameters_mismatch"
        return _oracle_clone(value), "clear", "typed_event_contract_exact"

    stage = value.get("event_stage")
    instance = value.get("instance_parameters")
    if not isinstance(stage, str) or not isinstance(instance, dict):
        return None, "issues", "staged_event_contract_shape_mismatch"
    key = (stage, original, kind)
    expected = None
    if event_token == "thecleric":
        max_hp = before_game.get("max_hp")
        expected_instance = {
            "heal_amount": int(max_hp * 0.25)
            if type(max_hp) is int and max_hp >= 0 else None,
            "heal_gold_cost": 35,
            "purify_cost": 50,
        }
        if expected_instance["heal_amount"] is None:
            return None, "inconclusive", "cleric_max_hp_missing"
        if not _oracle_exact_typed_value(instance, expected_instance):
            return None, "issues", "cleric_instance_contract_mismatch"
        expected = {
            ("MAIN", 0, "HEAL"): {
                "gold_cost": 35,
                "heal_amount": expected_instance["heal_amount"],
            },
            ("MAIN", 1, "PURIFY"): {
                "gold_cost_if_purgeable": 50,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            },
            ("MAIN", 2, "LEAVE"): {},
            ("RESULT", 0, "CONTINUE"): {},
        }.get(key)
    elif event_token == "designer":
        expected_keys = {
            "adjustment_upgrades_one", "clean_up_removes_cards",
            "adjust_cost", "clean_up_cost", "full_service_cost", "hp_loss",
        }
        if (
            set(instance) != expected_keys
            or type(instance.get("adjustment_upgrades_one")) is not bool
            or type(instance.get("clean_up_removes_cards")) is not bool
            or (
                instance.get("adjust_cost"), instance.get("clean_up_cost"),
                instance.get("full_service_cost"), instance.get("hp_loss"),
            ) != (40, 60, 90, 3)
        ):
            return None, "issues", "designer_instance_contract_mismatch"
        if key in {
            ("INTRO", 0, "OPEN_SERVICES"),
            ("DONE", 0, "CONTINUE"),
        }:
            expected = {}
        elif stage == "MAIN" and original == 0:
            upgrades_one = instance["adjustment_upgrades_one"]
            wanted_kind = (
                "ADJUSTMENT_GRID_UPGRADE"
                if upgrades_one else "ADJUSTMENT_RANDOM_UPGRADE"
            )
            if kind != wanted_kind:
                return None, "issues", "designer_adjustment_kind_mismatch"
            expected = {"gold_cost": 40}
            expected.update(
                {
                    "upgrade_select_count": 1,
                    "selection_mode": "PLAYER_SELECT",
                }
                if upgrades_one else {
                    "upgrade_max_count": 2,
                    "selection_mode": "RANDOM_UP_TO_AVAILABLE",
                }
            )
        elif stage == "MAIN" and original == 1:
            removes = instance["clean_up_removes_cards"]
            wanted_kind = (
                "CLEAN_UP_GRID_PURGE"
                if removes else "CLEAN_UP_GRID_TRANSFORM"
            )
            if kind != wanted_kind:
                return None, "issues", "designer_clean_up_kind_mismatch"
            expected = {
                "gold_cost": 60,
                "selection_mode": "PLAYER_SELECT",
            }
            expected.update(
                {"purge_select_count": 1}
                if removes else {
                    "transform_select_count": 2,
                    "transform_result": "RANDOM",
                }
            )
        elif key == ("MAIN", 2, "FULL_SERVICE"):
            expected = {
                "gold_cost": 90,
                "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE"
                ),
            }
        elif key == ("MAIN", 3, "PUNCH_AND_LEAVE"):
            expected = {"hp_loss": 3}
    elif event_token == "knowingskull":
        if (
            set(instance) != {
                "potion_cost", "card_cost", "gold_cost", "leave_cost",
                "gold_reward",
            }
            or any(type(instance.get(name)) is not int for name in instance)
            or instance.get("leave_cost") != 6
            or instance.get("gold_reward") != 90
            or any(
                instance[name] < 6
                for name in ("potion_cost", "card_cost", "gold_cost")
            )
        ):
            return None, "issues", "knowing_skull_instance_contract_mismatch"
        expected = {
            ("INTRO_1", 0, "OPEN_QUESTIONS"): {},
            ("ASK", 0, "TAKE_POTION"): {
                "hp_loss": instance["potion_cost"],
                "reward_count": 1,
                "reward_kind": "RANDOM_POTION",
            },
            ("ASK", 1, "TAKE_GOLD"): {
                "hp_loss": instance["gold_cost"], "gold_gain": 90,
            },
            ("ASK", 2, "TAKE_CARD"): {
                "hp_loss": instance["card_cost"],
                "reward_count": 1,
                "reward_color": "COLORLESS",
                "reward_rarity": "UNCOMMON",
                "selection_mode": "RANDOM",
            },
            ("ASK", 3, "LEAVE"): {"hp_loss": 6},
            ("COMPLETE", 0, "CONTINUE"): {},
        }.get(key)
    elif event_token == "cursedtome":
        expected_pool = _oracle_cursed_tome_pool(before_game)
        if expected_pool is None:
            return None, "inconclusive", "cursed_tome_relic_pool_missing"
        expected_keys = {
            "final_hp_loss", "damage_taken", "random_relic_pool",
        }
        if (
            set(instance) != expected_keys
            or instance.get("final_hp_loss") != 10
            or type(instance.get("damage_taken")) is not int
            or instance.get("random_relic_pool") != expected_pool
        ):
            return None, "issues", "cursed_tome_instance_contract_mismatch"
        allowed_damage = {
            "INTRO": {0}, "PAGE_1": {0}, "PAGE_2": {1},
            "PAGE_3": {3}, "LAST_PAGE": {6},
            "END": {0, 9, 16},
        }.get(stage)
        if allowed_damage is None or instance["damage_taken"] not in allowed_damage:
            return None, "issues", "cursed_tome_stage_damage_mismatch"
        expected = {
            ("INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN"): {
                "future_hp_loss_to_complete": 16,
                "random_relic_count": 1,
                "reward_surface": "COMBAT_REWARD",
                "selection_mode": "UNIFORM_MISC_RNG",
            },
            ("INTRO", 1, "LEAVE"): {},
            ("PAGE_1", 0, "READ_PAGE_1"): {"hp_loss": 1},
            ("PAGE_2", 0, "READ_PAGE_2"): {"hp_loss": 2},
            ("PAGE_3", 0, "READ_PAGE_3"): {"hp_loss": 3},
            ("LAST_PAGE", 0, "COMPLETE_RANDOM_BOOK"): {
                "hp_loss": 10,
                "random_relic_count": 1,
                "reward_surface": "COMBAT_REWARD",
                "selection_mode": "UNIFORM_MISC_RNG",
            },
            ("LAST_PAGE", 1, "STOP"): {"hp_loss": 3},
            ("END", 0, "PROCEED"): {},
        }.get(key)

    elif event_token == "deadadventurer":
        num_rewards = instance.get("num_rewards")
        chance = instance.get("encounter_chance_percent")
        rewards = instance.get("remaining_rewards")
        enemy = instance.get("enemy_index")
        enemy_ids = ("3 Sentries", "Gremlin Nob", "Lagavulin Event")
        if (
            set(instance) != {
                "num_rewards", "encounter_chance_percent",
                "remaining_rewards", "enemy_index", "encounter_id",
            }
            or type(num_rewards) is not int or num_rewards not in {0, 1, 2, 3}
            or chance != 25 + 25 * num_rewards
            or enemy not in {0, 1, 2}
            or instance.get("encounter_id") != enemy_ids[enemy]
            or not isinstance(rewards, list)
            or any(
                type(item) is not str
                or item not in ("GOLD", "NOTHING", "RELIC")
                for item in rewards
            )
            or len(rewards) != 3 - num_rewards
            or len(rewards) != len(set(rewards))
        ):
            return None, "issues", "dead_adventurer_instance_contract_mismatch"
        if key == ("INTRO", 0, "SEARCH") and rewards:
            reward = rewards[0]
            expected = {
                "roll_min": 0, "roll_max_inclusive": 99,
                "encounter_roll_lt": chance,
                "encounter_id": enemy_ids[enemy],
                "encounter_reward_gold_min": 25,
                "encounter_reward_gold_max": 35,
                "success_reward_kind": reward,
                "success_gold_gain": 30 if reward == "GOLD" else 0,
                "success_random_relic_count": 1 if reward == "RELIC" else 0,
                "success_relic_selection_mode": (
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                    if reward == "RELIC" else "NONE"
                ),
            }
        elif key == ("INTRO", 1, "LEAVE") and rewards:
            expected = {}
        elif key == ("FAIL", 0, "FIGHT"):
            expected = {
                "encounter_id": enemy_ids[enemy],
                "combat_reward_gold_min": 25,
                "combat_reward_gold_max": 35,
            }
        elif key in {
            ("SUCCESS", 0, "CONTINUE"),
            ("ESCAPE", 0, "CONTINUE"),
        }:
            expected = {}

    elif event_token == "scrapooze":
        if (
            set(instance) != {
                "relic_chance_percent_displayed", "damage",
                "total_damage_dealt", "screen_num",
            }
            or any(type(instance.get(name)) is not int for name in instance)
        ):
            return None, "issues", "scrap_ooze_instance_contract_mismatch"
        chance = instance["relic_chance_percent_displayed"]
        screen_num = instance["screen_num"]
        attempts = (chance - 25) // 10 if chance >= 25 else -1
        expected_total_damage = attempts * (6 + attempts - 1) // 2
        # On the result screen the current reach-in damage has already been
        # applied, while the chance/damage fields retain the values from the
        # completed attempt.  The main screen reports damage accumulated
        # before the next attempt; the result screen includes this final hit.
        if screen_num == 1:
            expected_total_damage += instance["damage"]
        if (
            chance < 25 or chance > 105 or (chance - 25) % 10
            or instance["damage"] != 3 + attempts
            or instance["total_damage_dealt"] != expected_total_damage
            or screen_num not in {0, 1}
        ):
            return None, "issues", "scrap_ooze_instance_contract_mismatch"
        if key == ("MAIN", 0, "REACH_INSIDE") and instance["screen_num"] == 0:
            expected = {
                "hp_loss": instance["damage"],
                "roll_min": 0, "roll_max_inclusive": 99,
                "success_roll_min_inclusive": 99 - chance,
                "success_random_relic_count": 1,
                "success_relic_selection_mode": (
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                ),
            }
        elif key == ("MAIN", 1, "LEAVE") and instance["screen_num"] == 0:
            expected = {}
        elif key == ("RESULT", 0, "CONTINUE") and instance["screen_num"] == 1:
            expected = {}

    elif event_token == "facetrader":
        relics = before_game.get("relics")
        max_hp = before_game.get("max_hp")
        if not isinstance(relics, list) or any(
            not isinstance(relic, dict) or type(relic.get("id")) is not str
            for relic in relics
        ) or type(max_hp) is not int:
            return None, "inconclusive", "face_trader_state_missing"
        face_ids = (
            "CultistMask", "FaceOfCleric", "GremlinMask", "NlothsMask",
            "SsserpentHead",
        )
        owned = {relic["id"] for relic in relics}
        pool = [item for item in face_ids if item not in owned] or ["Circlet"]
        if not _oracle_exact_typed_value(instance, {
            "gold_reward": 75,
            "damage": max(1, max_hp // 10),
            "random_face_pool": pool,
        }):
            return None, "issues", "face_trader_instance_contract_mismatch"
        expected = {
            ("INTRO", 0, "OPEN"): {},
            ("MAIN", 0, "TOUCH"): {
                "hp_loss": instance["damage"], "gold_gain": 75,
            },
            ("MAIN", 1, "TRADE"): {
                "random_relic_pool": pool,
                "random_relic_count": 1,
                "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
            },
            ("MAIN", 2, "LEAVE"): {},
            ("RESULT", 0, "CONTINUE"): {},
        }.get(key)

    elif event_token == "duplicator":
        if instance not in ({"screen_num": 0}, {"screen_num": 2}):
            return None, "issues", "duplicator_instance_contract_mismatch"
        expected = {
            ("MAIN", 0, "DUPLICATE"): {
                "duplicate_select_count": 1,
                "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
            },
            ("MAIN", 1, "LEAVE"): {},
            ("RESULT", 0, "CONTINUE"): {},
        }.get(key)
        if stage == "MAIN" and instance["screen_num"] != 0:
            return None, "issues", "duplicator_stage_contract_mismatch"
        if stage == "RESULT" and instance["screen_num"] != 2:
            return None, "issues", "duplicator_stage_contract_mismatch"

    elif event_token == "bonfireelementals":
        if instance != {"card_select": False}:
            return None, "issues", "bonfire_instance_contract_mismatch"
        expected = {
            ("INTRO", 0, "CONTINUE"): {},
            ("CHOOSE", 0, "OFFER_CARD"): {
                "offer_select_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
                ),
            },
            ("COMPLETE", 0, "CONTINUE"): {},
        }.get(key)

    if expected is None:
        return None, "issues", "typed_event_contract_stage_mismatch"
    if not _oracle_exact_typed_value(parameters, expected):
        return None, "issues", "typed_event_contract_parameters_mismatch"
    return _oracle_clone(value), "clear", "typed_event_contract_exact"


def _oracle_selected_staged_event_choice(record):
    options = record.get("available_options_before")
    selected = record.get("selected_choice_ids")
    if not isinstance(options, list) or not isinstance(selected, list):
        return None, "inconclusive", "typed_event_choice_surface_missing"
    supported_visible = any(
        isinstance(option, dict)
        and isinstance(option.get("target"), dict)
        and _oracle_game_id(option["target"].get("event_id"))
        in _ORACLE_STAGED_EVENT_IDENTITIES
        for option in options
    )
    if len(selected) != 1:
        return (
            None,
            "inconclusive" if supported_visible else "not_applicable",
            "typed_event_selected_choice_missing"
            if supported_visible else "not_supported_staged_event",
        )
    choice_id = str(selected[0])
    matches = [
        option for option in options
        if isinstance(option, dict)
        and str(option.get("option_id", option.get("choice_id"))) == choice_id
    ]
    if len(matches) != 1:
        return (
            None, "issues" if supported_visible else "not_applicable",
            "typed_event_selected_choice_not_unique"
            if supported_visible else "not_supported_staged_event",
        )
    target = matches[0].get("target")
    target = target if isinstance(target, dict) else {}
    if _oracle_game_id(target.get("event_id")) not in (
        _ORACLE_STAGED_EVENT_IDENTITIES
    ):
        return None, "not_applicable", "not_supported_staged_event"
    if str(record.get("action") or "").casefold() != "choose":
        return None, "issues", "typed_event_action_not_choose"
    if (
        str(record.get("requested_target_id") or "") != choice_id
        or str(record.get("resolved_target_id") or "") != choice_id
    ):
        return None, "issues", "typed_event_receipt_binding_mismatch"
    return matches[0], "clear", "typed_event_selected_choice_bound"


def _oracle_staged_event_surface(record, raw_choice, before_game):
    target = raw_choice.get("target")
    target = target if isinstance(target, dict) else {}
    event_token = _oracle_game_id(target.get("event_id"))
    if event_token not in _ORACLE_STAGED_EVENT_IDENTITIES:
        return None, "not_applicable", "not_supported_staged_event"
    if str(target.get("kind") or "").casefold() != "event_option":
        return None, "issues", "typed_event_target_kind_mismatch"
    if before_game.get("ascension_level") != 0:
        return None, "inconclusive", "typed_event_requires_exact_a0"
    screen = before_game.get("screen_state")
    if not isinstance(screen, dict):
        return None, "inconclusive", "typed_event_authoritative_screen_missing"
    if _oracle_game_id(screen.get("event_id")) != event_token:
        return None, "issues", "typed_event_authoritative_id_mismatch"
    options = screen.get("options")
    if not isinstance(options, list):
        return None, "inconclusive", "typed_event_authoritative_options_missing"
    if not options:
        return None, "issues", "typed_event_authoritative_surface_empty"

    by_original = {}
    enabled_by_choice = {}
    contracts = {}
    for option in options:
        if not isinstance(option, dict):
            return None, "issues", "typed_event_option_not_an_object"
        original = option.get("original_button_index")
        disabled = option.get("disabled")
        if type(original) is not int or original < 0:
            return None, "inconclusive", "typed_event_original_index_missing"
        if original in by_original:
            return None, "issues", "typed_event_original_index_duplicate"
        if type(disabled) is not bool:
            return None, "inconclusive", "typed_event_disabled_flag_missing"
        choice_index = option.get("choice_index")
        if disabled:
            if "choice_index" in option:
                return None, "issues", "typed_event_disabled_choice_index_present"
        else:
            if type(choice_index) is not int or choice_index < 0:
                return None, "inconclusive", "typed_event_choice_index_missing"
            if choice_index in enabled_by_choice:
                return None, "issues", "typed_event_choice_index_duplicate"
            enabled_by_choice[choice_index] = option
        contract, status, reason = _oracle_staged_event_contract(
            option.get("event_contract"), event_token, before_game
        )
        if status != "clear":
            return None, status, reason
        if contract["original_button_index"] != original:
            return None, "issues", "typed_event_contract_original_mismatch"
        by_original[original] = option
        contracts[original] = contract

    if sorted(by_original) != list(range(len(options))):
        return None, "issues", "typed_event_original_surface_noncontiguous"
    if sorted(enabled_by_choice) != list(range(len(enabled_by_choice))):
        return None, "issues", "typed_event_choice_surface_noncontiguous"

    if event_token == "worldofgoop":
        kinds = {
            original: contract["option_kind"]
            for original, contract in contracts.items()
        }
        expected_kinds = (
            {0: "CONTINUE"}
            if len(contracts) == 1 else {0: "GATHER", 1: "LEAVE"}
        )
        if kinds != expected_kinds:
            return None, "issues", "goop_stage_surface_mismatch"
        stage = "RESULT" if len(contracts) == 1 else "INTRO"
    else:
        stages = {contract["event_stage"] for contract in contracts.values()}
        instances = {
            _freeze(contract["instance_parameters"])
            for contract in contracts.values()
        }
        if len(stages) != 1 or len(instances) != 1:
            return None, "issues", "typed_event_surface_contracts_disagree"
        stage = next(iter(stages))
        expected_originals = {
            ("thecleric", "MAIN"): {0, 1, 2},
            ("thecleric", "RESULT"): {0},
            ("designer", "INTRO"): {0},
            ("designer", "MAIN"): {0, 1, 2, 3},
            ("designer", "DONE"): {0},
            ("cursedtome", "INTRO"): {0, 1},
            ("cursedtome", "PAGE_1"): {0},
            ("cursedtome", "PAGE_2"): {0},
            ("cursedtome", "PAGE_3"): {0},
            ("cursedtome", "LAST_PAGE"): {0, 1},
            ("cursedtome", "END"): {0},
            ("knowingskull", "INTRO_1"): {0},
            ("knowingskull", "ASK"): {0, 1, 2, 3},
            ("knowingskull", "COMPLETE"): {0},
            ("deadadventurer", "INTRO"): {0, 1},
            ("deadadventurer", "FAIL"): {0},
            ("deadadventurer", "SUCCESS"): {0},
            ("deadadventurer", "ESCAPE"): {0},
            ("scrapooze", "MAIN"): {0, 1},
            ("scrapooze", "RESULT"): {0},
            ("facetrader", "INTRO"): {0},
            ("facetrader", "MAIN"): {0, 1, 2},
            ("facetrader", "RESULT"): {0},
            ("duplicator", "MAIN"): {0, 1},
            ("duplicator", "RESULT"): {0},
            ("bonfireelementals", "INTRO"): {0},
            ("bonfireelementals", "CHOOSE"): {0},
            ("bonfireelementals", "COMPLETE"): {0},
        }.get((event_token, stage))
        if expected_originals is None or set(contracts) != expected_originals:
            return None, "issues", "typed_event_stage_surface_mismatch"

    choice_index = raw_choice.get("choice_index")
    if type(choice_index) is not int:
        return None, "inconclusive", "typed_event_selected_index_missing"
    selected = enabled_by_choice.get(choice_index)
    if selected is None:
        return None, "issues", "typed_event_selected_index_mismatch"
    original = target.get("original_button_index")
    if type(original) is not int:
        return None, "inconclusive", "typed_event_target_original_missing"
    if selected["original_button_index"] != original:
        return None, "issues", "typed_event_target_original_mismatch"
    target_contract, status, reason = _oracle_staged_event_contract(
        target.get("event_contract"), event_token, before_game
    )
    if status != "clear":
        return None, status, reason
    if _freeze(target_contract) != _freeze(contracts[original]):
        return None, "issues", "typed_event_target_contract_mismatch"
    expected_mechanism = _oracle_event_mechanism_id(target_contract)
    mechanism_id = target.get("mechanism_id")
    if not isinstance(mechanism_id, str) or not mechanism_id:
        return None, "inconclusive", "typed_event_mechanism_id_missing"
    if mechanism_id != expected_mechanism:
        return None, "issues", "typed_event_mechanism_id_mismatch"
    return ({
        "event_token": event_token,
        "event_id": _ORACLE_STAGED_EVENT_IDENTITIES[event_token][0],
        "event_stage": stage,
        "choice_id": _oracle_mausoleum_choice_id(raw_choice),
        "choice_index": choice_index,
        "original_button_index": original,
        "event_contract": contracts[original],
        "mechanism_id": expected_mechanism,
    }, "clear", "typed_event_authoritative_surface_bound")


def _oracle_apply_world_of_goop_event(value, raw_choice, record):
    """Independently project an exact A0 World of Goop option."""

    before_game = _oracle_before_game(record)
    surface, status, reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            reason or "world_of_goop_protocol_contract_unproven"
        )
        if status == "issues":
            review["contradictions"].append({
                "field": "world_of_goop_protocol_contract",
                "claimed": _oracle_clone(raw_choice.get("target") or {}),
                "independently_expected": reason,
            })
        else:
            review["unclassified"].append(
                "world_of_goop_protocol_contract"
            )
        return False
    if surface.get("event_token") != "worldofgoop":
        value["uncertainty"].append("world_of_goop_event_identity_mismatch")
        review["contradictions"].append({
            "field": "world_of_goop_event_identity",
            "claimed": surface.get("event_token"),
            "independently_expected": "worldofgoop",
        })
        return False

    contract = surface["event_contract"]
    option_kind = contract["option_kind"]
    parameters = contract["parameters"]
    typed_reason = "typed_base_game_world_of_goop_a0_branch"
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })
    if option_kind == "GATHER":
        hp_damage = parameters["hp_damage"]
        gold_gain = parameters["gold_gain"]
        _oracle_numeric(value, -hp_damage, 0, gold_gain, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason, hp=hp_damage)
        _oracle_no_probability(value, typed_reason)
        value.update({
            "operation": "world_of_goop_gather_gold",
            "event_outcome_id": "gather_gold",
        })
        return True
    if option_kind == "LEAVE":
        gold_loss = parameters["gold_loss"]
        _oracle_numeric(value, 0, 0, -gold_loss, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason, gold=gold_loss)
        _oracle_no_probability(value, typed_reason)
        value.update({
            "operation": "world_of_goop_leave",
            "event_outcome_id": "leave_gold",
            "leave": True,
        })
        return True
    if option_kind == "CONTINUE":
        dialog_reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, dialog_reason)
        _oracle_empty_inventory(value, dialog_reason)
        _oracle_costs(value, dialog_reason)
        _oracle_no_probability(value, dialog_reason)
        value.update({
            "operation": "world_of_goop_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True

    value["uncertainty"].append("typed_world_of_goop_option_kind_unhandled")
    review["unclassified"].append("world_of_goop_option_kind")
    return False


def _oracle_apply_the_cleric_event(value, raw_choice, record):
    """Independently project one exact, typed A0 Cleric option."""

    before_game = _oracle_before_game(record)
    surface, status, reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            reason or "the_cleric_protocol_contract_unproven"
        )
        if status == "issues":
            review["contradictions"].append({
                "field": "the_cleric_protocol_contract",
                "claimed": _oracle_clone(raw_choice.get("target") or {}),
                "independently_expected": reason,
            })
        else:
            review["unclassified"].append(
                "the_cleric_protocol_contract"
            )
        return False
    if surface.get("event_token") != "thecleric":
        value["uncertainty"].append("the_cleric_event_identity_mismatch")
        review["contradictions"].append({
            "field": "the_cleric_event_identity",
            "claimed": surface.get("event_token"),
            "independently_expected": "thecleric",
        })
        return False

    contract = surface["event_contract"]
    option_kind = contract["option_kind"]
    parameters = contract["parameters"]
    typed_reason = "typed_base_game_the_cleric_a0_branch"
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })
    if option_kind == "HEAL":
        current_hp = before_game.get("current_hp")
        max_hp = before_game.get("max_hp")
        if (
            type(current_hp) is not int
            or type(max_hp) is not int
            or current_hp < 0
            or max_hp < current_hp
        ):
            value["uncertainty"].append(
                "the_cleric_current_or_max_hp_missing"
            )
            review["unclassified"].append("the_cleric_heal_hp_delta")
            return False
        heal_amount = parameters["heal_amount"]
        gold_cost = parameters["gold_cost"]
        hp_gain = min(heal_amount, max_hp - current_hp)
        _oracle_numeric(value, hp_gain, 0, -gold_cost, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason, gold=gold_cost)
        _oracle_no_probability(value, typed_reason)
        value.update({
            "operation": "cleric_heal",
            "event_outcome_id": "heal",
        })
        return True
    if option_kind == "PURIFY":
        gold_cost = parameters["gold_cost_if_purgeable"]
        select_count = parameters["purge_select_count"]
        _oracle_numeric(value, 0, 0, -gold_cost, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason, gold=gold_cost)
        _oracle_known(
            value,
            "future_costs",
            [{
                "kind": "cleric_grid_selection",
                "operation": "grid_purge",
                "select_count": select_count,
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            }],
            "independent_event_mechanics",
            typed_reason,
        )
        _oracle_no_probability(value, typed_reason)
        value["uncertainty"] = [
            "exact removed card UUID binds on the following GRID"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "protocol_multistage_operation",
            "reason": "Cleric purge commits after GRID confirmation",
        }
        value.update({
            "operation": "cleric_open_purge_grid",
            "event_outcome_id": "purify",
        })
        return True
    if option_kind == "LEAVE":
        _oracle_numeric(value, 0, 0, 0, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason)
        _oracle_no_probability(value, typed_reason)
        value.update({
            "operation": "cleric_leave",
            "event_outcome_id": "leave",
            "leave": True,
        })
        return True
    if option_kind == "CONTINUE":
        dialog_reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, dialog_reason)
        _oracle_empty_inventory(value, dialog_reason)
        _oracle_costs(value, dialog_reason)
        _oracle_no_probability(value, dialog_reason)
        value.update({
            "operation": "cleric_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True

    value["uncertainty"].append("typed_the_cleric_option_kind_unhandled")
    review["unclassified"].append("the_cleric_option_kind")
    return False


def _oracle_apply_designer_event(value, raw_choice, record):
    """Independently project one exact, typed A0 Designer option."""

    before_game = _oracle_before_game(record)
    surface, status, reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            reason or "designer_protocol_contract_unproven"
        )
        if status == "issues":
            review["contradictions"].append({
                "field": "designer_protocol_contract",
                "claimed": _oracle_clone(raw_choice.get("target") or {}),
                "independently_expected": reason,
            })
        else:
            review["unclassified"].append("designer_protocol_contract")
        return False
    if surface.get("event_token") != "designer":
        value["uncertainty"].append("designer_event_identity_mismatch")
        review["contradictions"].append({
            "field": "designer_event_identity",
            "claimed": surface.get("event_token"),
            "independently_expected": "designer",
        })
        return False

    contract = surface["event_contract"]
    option_kind = contract["option_kind"]
    parameters = contract["parameters"]
    typed_reason = "typed_base_game_designer_a0_branch"
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })

    if option_kind in {"OPEN_SERVICES", "CONTINUE"}:
        dialog_reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, dialog_reason)
        _oracle_empty_inventory(value, dialog_reason)
        _oracle_costs(value, dialog_reason)
        _oracle_no_probability(value, dialog_reason)
        value.update({
            "operation": "designer_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True

    if option_kind == "ADJUSTMENT_GRID_UPGRADE":
        gold_cost = parameters["gold_cost"]
        future = [{
            "kind": "designer_grid_selection",
            "operation": "grid_upgrade",
            "select_count": parameters["upgrade_select_count"],
            "identity_binding": "subsequent_grid_card_instance_id",
            "commit_timing": "after_grid_confirmation",
        }]
        operation = "designer_adjustment_grid_upgrade"
        event_outcome_id = "adjustment_grid_upgrade"
        uncertainty = ["exact upgraded card UUID binds on the following GRID"]
        classification_reason = "typed Designer GRID upgrade contract"
        random_effects = []
    elif option_kind == "ADJUSTMENT_RANDOM_UPGRADE":
        gold_cost = parameters["gold_cost"]
        count = parameters["upgrade_max_count"]
        future = [{
            "kind": "designer_random_card_upgrade",
            "operation": "random_upgrade",
            "max_count": count,
            "count_semantics": "up_to_available",
            "domain": "current_upgradable_deck",
            "selection_mode": "random",
            "commit_timing": "after_event_choice",
        }]
        operation = "designer_adjustment_random_upgrade"
        event_outcome_id = "adjustment_random_upgrade"
        uncertainty = ["exact upgraded UUIDs settle after the event choice"]
        classification_reason = "typed random-up-to-available upgrade domain"
        random_effects = [{
            "kind": "random_card_upgrade",
            "max_count": count,
            "count_semantics": "up_to_available",
            "domain": "current_upgradable_deck",
            "selection_mode": "random",
        }]
    elif option_kind in {
        "CLEAN_UP_GRID_PURGE", "CLEAN_UP_GRID_TRANSFORM",
    }:
        gold_cost = parameters["gold_cost"]
        transform = option_kind == "CLEAN_UP_GRID_TRANSFORM"
        count = parameters[
            "transform_select_count" if transform else "purge_select_count"
        ]
        future = [{
            "kind": "designer_grid_selection",
            "operation": "grid_transform" if transform else "grid_purge",
            "select_count": count,
            "identity_binding": (
                "subsequent_grid_card_instance_ids"
                if transform else "subsequent_grid_card_instance_id"
            ),
            "commit_timing": "after_grid_confirmation",
        }]
        if transform:
            future[0]["transform_result"] = "random"
        operation = (
            "designer_clean_up_grid_transform"
            if transform else "designer_clean_up_grid_purge"
        )
        event_outcome_id = (
            "clean_up_grid_transform" if transform else "clean_up_grid_purge"
        )
        uncertainty = ["exact selected UUIDs and results settle on GRID"]
        classification_reason = "typed Designer cleanup GRID contract"
        random_effects = ([{
            "kind": "random_card_transform_results",
            "count": count,
            "domain": "base_game_card_pool",
            "selection_mode": "random",
        }] if transform else [])
    elif option_kind == "FULL_SERVICE":
        gold_cost = parameters["gold_cost"]
        count = parameters["random_upgrade_max_count"]
        future = [
            {
                "kind": "designer_grid_selection",
                "operation": "grid_purge",
                "select_count": parameters["purge_select_count"],
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            },
            {
                "kind": "designer_random_card_upgrade",
                "operation": "random_upgrade",
                "max_count": count,
                "count_semantics": "up_to_available",
                "domain": "post_purge_upgradable_deck",
                "selection_mode": "random",
                "commit_timing": "after_grid_confirmation",
            },
        ]
        operation = "designer_full_service"
        event_outcome_id = "full_service"
        uncertainty = [
            "exact card UUIDs bind on the following GRID/settlement"
        ]
        classification_reason = (
            "Full Service purge and random-upgrade domain are typed"
        )
        random_effects = [{
            "kind": "random_card_upgrade",
            "max_count": count,
            "count_semantics": "up_to_available",
            "domain": "post_purge_upgradable_deck",
            "selection_mode": "random",
        }]
    elif option_kind == "PUNCH_AND_LEAVE":
        hp_loss = parameters["hp_loss"]
        _oracle_numeric(value, -hp_loss, 0, 0, typed_reason)
        _oracle_empty_inventory(value, typed_reason)
        _oracle_costs(value, typed_reason, hp=hp_loss)
        _oracle_no_probability(value, typed_reason)
        value.update({
            "operation": "designer_punch_and_leave",
            "event_outcome_id": "punch_and_leave",
            "leave": True,
        })
        return True
    else:
        value["uncertainty"].append("typed_designer_option_kind_unhandled")
        review["unclassified"].append("designer_option_kind")
        return False

    _oracle_numeric(value, 0, 0, -gold_cost, typed_reason)
    _oracle_empty_inventory(value, typed_reason)
    _oracle_costs(value, typed_reason, gold=gold_cost)
    _oracle_known(
        value, "future_costs", future,
        "independent_event_mechanics", typed_reason,
    )
    _oracle_no_probability(value, typed_reason)
    if random_effects:
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "designer_random_identity_domain",
        )
    value.update({
        "operation": operation,
        "event_outcome_id": event_outcome_id,
        "random_effects": random_effects,
        "uncertainty": uncertainty,
        "uncertainty_classification": {
            "status": "classified_future",
            "authority": "protocol_multistage_operation",
            "reason": classification_reason,
        },
    })
    return True


def _oracle_apply_cursed_tome_event(value, raw_choice, record):
    """Independently project one exact, typed Cursed Tome branch.

    The Java event contract and the authoritative EVENT screen bind the stage,
    original button, and instance-local relic pool.  This routine deliberately
    does not consume the producer's consequence, score, label, or text.
    """

    before_game = _oracle_before_game(record)
    surface, status, reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            reason or "cursed_tome_protocol_contract_unproven"
        )
        if status == "issues":
            target = raw_choice.get("target") if isinstance(raw_choice, dict) else {}
            review["contradictions"].append({
                "field": "cursed_tome_protocol_contract",
                "claimed": _oracle_clone(target if isinstance(target, dict) else {}),
                "independently_expected": reason,
            })
        else:
            review["unclassified"].append("cursed_tome_protocol_contract")
        return False
    if surface.get("event_token") != "cursedtome":
        value["uncertainty"].append("cursed_tome_event_identity_mismatch")
        review["contradictions"].append({
            "field": "cursed_tome_event_identity",
            "claimed": surface.get("event_token"),
            "independently_expected": "cursedtome",
        })
        return False

    contract = surface["event_contract"]
    option_kind = contract["option_kind"]
    parameters = contract["parameters"]
    relic_pool = list(contract["instance_parameters"]["random_relic_pool"])
    reason = "typed_base_game_cursed_tome_a0_branch"
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })

    if option_kind == "PROCEED":
        dialog_reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, dialog_reason)
        _oracle_empty_inventory(value, dialog_reason)
        _oracle_costs(value, dialog_reason)
        _oracle_no_probability(value, dialog_reason)
        value.update({
            "operation": "cursed_tome_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True

    if option_kind == "ENTER_RANDOM_BOOK_CHAIN":
        future = [{
            "kind": "cursed_tome_reading_chain",
            "total_hp_loss_to_complete": parameters[
                "future_hp_loss_to_complete"
            ],
            "reward_surface": parameters["reward_surface"],
            "random_relic_count": parameters["random_relic_count"],
        }]
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_known(
            value, "future_costs", future,
            "independent_mechanics_table", reason,
        )
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "cursed_tome_random_relic_pool",
        )
        value.update({
            "operation": "cursed_tome_enter_random_book_chain",
            "event_outcome_id": "enter_random_book_chain",
            "random_effects": [{
                "kind": "random_relic_gain", "domain": relic_pool,
                "count": parameters["random_relic_count"],
                "selection_mode": "random",
            }],
            "uncertainty": [
                "random book identity is bounded by the typed relic pool"
            ],
            "uncertainty_classification": {
                "status": "exhaustive_domain",
                "authority": "typed_event_contract",
                "reason": "Cursed Tome random relic pool is authoritative",
            },
        })
        return True

    if option_kind == "LEAVE":
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value.update({
            "operation": "cursed_tome_leave",
            "event_outcome_id": "leave",
            "leave": True,
        })
        return True

    if option_kind in {"READ_PAGE_1", "READ_PAGE_2", "READ_PAGE_3", "STOP"}:
        hp_loss = parameters["hp_loss"]
        _oracle_numeric(value, -hp_loss, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason, hp=hp_loss)
        _oracle_no_probability(value, reason)
        value.update({
            "operation": (
                "cursed_tome_stop" if option_kind == "STOP"
                else f"cursed_tome_{option_kind.casefold()}"
            ),
            "event_outcome_id": option_kind.casefold(),
        })
        if option_kind == "STOP":
            value["leave"] = True
        return True

    if option_kind == "COMPLETE_RANDOM_BOOK":
        hp_loss = parameters["hp_loss"]
        future = [{
            "kind": "cursed_tome_random_relic_reward",
            "reward_surface": parameters["reward_surface"],
            "random_relic_count": parameters["random_relic_count"],
        }]
        _oracle_numeric(value, -hp_loss, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason, hp=hp_loss)
        _oracle_known(
            value, "future_costs", future,
            "independent_mechanics_table", reason,
        )
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "cursed_tome_random_relic_pool",
        )
        value.update({
            "operation": "cursed_tome_complete_random_book",
            "event_outcome_id": "complete_random_book",
            "random_effects": [{
                "kind": "random_relic_gain", "domain": relic_pool,
                "count": parameters["random_relic_count"],
                "selection_mode": "random",
            }],
            "uncertainty": [
                "random book identity is bounded by the typed relic pool"
            ],
            "uncertainty_classification": {
                "status": "exhaustive_domain",
                "authority": "typed_event_contract",
                "reason": "Cursed Tome random relic pool is authoritative",
            },
        })
        return True

    value["uncertainty"].append("typed_cursed_tome_option_kind_unhandled")
    review["unclassified"].append("cursed_tome_option_kind")
    return False


def _oracle_apply_knowing_skull_event(value, raw_choice, record):
    """Project Knowing Skull only from its typed surface and before state."""

    before_game = _oracle_before_game(record)
    surface, status, reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            reason or "knowing_skull_protocol_contract_unproven"
        )
        bucket = (
            review["contradictions"] if status == "issues"
            else review["unclassified"]
        )
        if status == "issues":
            bucket.append({
                "field": "knowing_skull_protocol_contract",
                "claimed": _oracle_clone(raw_choice.get("target") or {}),
                "independently_expected": reason,
            })
        else:
            bucket.append("knowing_skull_protocol_contract")
        return False

    contract = surface["event_contract"]
    option_kind = contract["option_kind"]
    parameters = contract["parameters"]
    instance = contract["instance_parameters"]
    reason = "typed_base_game_knowing_skull_a0_branch"
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })
    if option_kind in {"OPEN_QUESTIONS", "CONTINUE"}:
        dialog_reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, dialog_reason)
        _oracle_empty_inventory(value, dialog_reason)
        _oracle_costs(value, dialog_reason)
        _oracle_no_probability(value, dialog_reason)
        value.update({
            "operation": "knowing_skull_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
        })
        return True

    relics = before_game.get("relics")
    if not isinstance(relics, list) or any(
        not isinstance(relic, dict)
        or not isinstance(relic.get("id") or relic.get("name"), str)
        for relic in relics
    ):
        value["uncertainty"].append("knowing_skull_relic_state_missing")
        review["unclassified"].append("knowing_skull_relic_state")
        return False
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in relics
    }
    reduction = 1 if "tungstenrod" in relic_ids else 0
    realized = lambda raw: max(0, int(raw) - reduction)
    raw_loss = parameters.get("hp_loss")
    if type(raw_loss) is not int:
        value["uncertainty"].append("knowing_skull_hp_loss_missing")
        return False
    hp_loss = realized(raw_loss)
    leave_loss = realized(instance["leave_cost"])
    gold = (
        parameters["gold_gain"]
        if option_kind == "TAKE_GOLD" and "ectoplasm" not in relic_ids
        else 0
    )
    _oracle_numeric(value, -hp_loss, 0, gold, reason)
    _oracle_empty_inventory(value, reason)
    _oracle_costs(value, reason, hp=hp_loss)
    if option_kind != "LEAVE":
        _oracle_known(value, "future_costs", [{
            "kind": "knowing_skull_exit_reserve",
            "reserved_hp_loss": leave_loss,
            "commit_timing": "when_leaving_event",
        }], "independent_mechanics_table", reason)
    _oracle_no_probability(value, reason)
    value["nominal_hp_loss"] = raw_loss

    if option_kind == "TAKE_POTION":
        potions = before_game.get("potions")
        if not isinstance(potions, list):
            value["uncertainty"].append("knowing_skull_potion_slots_missing")
            return False
        obtainable = (
            "sozu" not in relic_ids
            and any(
                isinstance(potion, dict)
                and _oracle_game_id(potion.get("id")) == "potionslot"
                for potion in potions
            )
        )
        random_effect = {
            "kind": "random_potion_gain", "count": 1,
            "domain": "base_game_potion_pool", "selection_mode": "random",
        }
        if obtainable:
            _oracle_known_domain(
                value, "potion_changes",
                {"gain": [random_effect], "remove": [], "replace": []},
                "knowing_skull_random_potion_domain",
            )
        value.update({
            "operation": "knowing_skull_take_potion",
            "event_outcome_id": "take_potion",
            "potion_reward_obtainable": obtainable,
            "random_effects": [random_effect],
            "uncertainty": [
                "random potion identity is bounded by the base-game pool"
            ],
            "uncertainty_classification": {
                "status": "classified_random_domain",
                "authority": "typed_event_contract",
                "reason": "Knowing Skull random potion domain is authoritative",
            },
        })
        return True
    if option_kind == "TAKE_GOLD":
        value.update({
            "operation": "knowing_skull_take_gold",
            "event_outcome_id": "take_gold",
            "nominal_gold_gain": parameters["gold_gain"],
        })
        return True
    if option_kind == "TAKE_CARD":
        random_effect = {
            "kind": "random_card_gain", "count": 1,
            "domain": "base_game_colorless_uncommon_pool",
            "selection_mode": "random",
        }
        _oracle_known_domain(
            value, "card_changes",
            {"gain": [random_effect], "remove": [], "upgrade": [],
             "transform": []},
            "knowing_skull_random_card_domain",
        )
        value.update({
            "operation": "knowing_skull_take_card",
            "event_outcome_id": "take_card",
            "random_effects": [random_effect],
            "uncertainty": [
                "random card identity is bounded by the colorless uncommon pool"
            ],
            "uncertainty_classification": {
                "status": "classified_random_domain",
                "authority": "typed_event_contract",
                "reason": "Knowing Skull random card domain is authoritative",
            },
        })
        return True
    if option_kind == "LEAVE":
        value.update({
            "operation": "knowing_skull_leave",
            "event_outcome_id": "leave", "leave": True,
        })
        return True
    value["uncertainty"].append("typed_knowing_skull_option_kind_unhandled")
    return False


def _oracle_apply_repeatable_and_grid_event(value, raw_choice, record):
    """Independently project bytecode-verified staged event families."""

    before_game = _oracle_before_game(record)
    surface, status, binding_reason = _oracle_staged_event_surface(
        record, raw_choice, before_game
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if status != "clear" or not isinstance(surface, dict):
        value["uncertainty"].append(
            binding_reason or "staged_event_protocol_contract_unproven"
        )
        if status == "issues":
            review["contradictions"].append({
                "field": "staged_event_protocol_contract",
                "claimed": _oracle_clone(raw_choice.get("target") or {}),
                "independently_expected": binding_reason,
            })
        else:
            review["unclassified"].append("staged_event_protocol_contract")
        return False

    event_token = surface["event_token"]
    if event_token not in {
        "deadadventurer", "scrapooze", "facetrader", "duplicator",
        "bonfireelementals",
    }:
        return False
    event_contract = surface["event_contract"]
    option_kind = event_contract["option_kind"]
    parameters = event_contract["parameters"]
    instance = event_contract["instance_parameters"]
    value.update({
        "event_id": surface["event_id"],
        "mechanism_id": surface["mechanism_id"],
        "original_button_index": surface["original_button_index"],
        "random_effects": [],
    })

    def mark_random_fields(
        *, hp, gold_domain, relic_domain, current_cost, future_costs,
        outcomes, reason
    ):
        _oracle_known(
            value, "hp_delta", hp, "independent_mechanics_table", reason
        )
        _oracle_known(
            value, "max_hp_delta", 0, "independent_mechanics_table", reason
        )
        if len(gold_domain) == 1:
            _oracle_known(
                value, "gold_delta", gold_domain[0],
                "independent_mechanics_table", reason,
            )
        else:
            _oracle_known_domain(
                value, "gold_delta", gold_domain,
                reason + "_gold_domain",
            )
        for field, result in (
            ("card_changes", {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
            }),
            ("potion_changes", {"gain": [], "remove": [], "replace": []}),
            ("curse", {
                "gain": [], "remove": [], "probability": 0.0,
                "omamori_applicable": False,
                "omamori_charges_consumed": 0,
            }),
        ):
            _oracle_known(
                value, field, result, "independent_mechanics_table", reason
            )
        if isinstance(relic_domain, list) and len(relic_domain) == 1:
            _oracle_known(
                value, "relic_changes", relic_domain[0],
                "independent_mechanics_table", reason,
            )
        else:
            _oracle_known_domain(
                value, "relic_changes", relic_domain,
                reason + "_relic_domain",
            )
        _oracle_known(
            value, "current_cost", current_cost,
            "independent_mechanics_table", reason,
        )
        _oracle_known(
            value, "future_costs", future_costs,
            "independent_mechanics_table", reason,
        )
        _oracle_known_domain(
            value, "probabilistic_outcomes", outcomes,
            reason + "_exhaustive_outcomes",
        )
        value["uncertainty_classification"] = {
            "status": "exhaustive_probability",
            "authority": "independent_mechanics_table",
            "reason": reason + " exhaustive bytecode branch partition",
        }

    if option_kind in {"CONTINUE", "OPEN"}:
        reason = "typed_event_single_dialog_has_no_immediate_effect"
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value.update({
            "operation": {
                "deadadventurer": "dead_adventurer_dialog_advance_noop",
                "scrapooze": "scrap_ooze_dialog_advance_noop",
                "facetrader": "face_trader_dialog_advance_noop",
                "duplicator": "duplicator_dialog_advance_noop",
                "bonfireelementals": "bonfire_dialog_advance_noop",
            }[event_token],
            "event_outcome_id": "dialog_advance",
        })
        return True

    if event_token == "deadadventurer":
        reason = "typed_base_game_dead_adventurer_a0_branch"
        if option_kind == "SEARCH":
            chance = parameters["encounter_roll_lt"] / 100.0
            reward_kind = parameters["success_reward_kind"]
            success_gold = parameters["success_gold_gain"]
            random_relic = {
                "kind": "random_relic_gain",
                "count": parameters["success_random_relic_count"],
                "domain": "base_game_non_boss_relic_tiers",
                "selection_mode": "random_tier_then_screenless_relic",
            }
            outcomes = [
                {
                    "outcome_id": "encounter", "probability": chance,
                    "gold_delta": 0,
                    "future_combat": parameters["encounter_id"],
                    "combat_reward_gold_domain": [25, 35],
                },
                {
                    "outcome_id": "success", "probability": 1.0 - chance,
                    "reward_kind": reward_kind,
                    "gold_delta": success_gold,
                    "random_relic_count": parameters[
                        "success_random_relic_count"
                    ],
                },
            ]
            relic_domain = [{
                "gain": [], "remove": [], "counter": [],
            }]
            if reward_kind == "RELIC":
                relic_domain.append({
                    "gain": [random_relic], "remove": [], "counter": [],
                })
            mark_random_fields(
                hp=0, gold_domain=sorted({0, success_gold}),
                relic_domain=relic_domain,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[{
                    "kind": "dead_adventurer_possible_combat",
                    "encounter_id": parameters["encounter_id"],
                    "probability": chance,
                }], outcomes=outcomes, reason=reason,
            )
            value.update({
                "operation": "dead_adventurer_search",
                "event_outcome_id": "search_random_branch",
                "random_effects": [random_relic]
                if reward_kind == "RELIC" else [],
                "uncertainty": [
                    "encounter/success branch is exhaustive and the shuffled success reward is bound"
                ],
            })
            return True
        if option_kind == "FIGHT":
            future = [{
                "kind": "dead_adventurer_combat",
                "encounter_id": parameters["encounter_id"],
                "combat_reward_gold_domain": [
                    parameters["combat_reward_gold_min"],
                    parameters["combat_reward_gold_max"],
                ],
                "preserved_success_rewards": instance["remaining_rewards"],
            }]
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_known(
                value, "future_costs", future,
                "independent_mechanics_table", reason,
            )
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "dead_adventurer_enter_combat",
                "event_outcome_id": "fight",
                "uncertainty": [
                    "combat outcome and 25-35 gold roll settle after entering combat"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "independent_mechanics_table",
                    "reason": "Dead Adventurer encounter and reward domain are bound",
                },
            })
            return True
        if option_kind == "LEAVE":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "dead_adventurer_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True

    if event_token == "scrapooze":
        reason = "typed_base_game_scrap_ooze_a0_branch"
        if option_kind == "REACH_INSIDE":
            hp_loss = parameters["hp_loss"]
            probability = (
                100 - parameters["success_roll_min_inclusive"]
            ) / 100.0
            random_relic = {
                "kind": "random_relic_gain", "count": 1,
                "domain": "base_game_non_boss_relic_tiers",
                "selection_mode": "random_tier_then_screenless_relic",
            }
            outcomes = [
                {
                    "outcome_id": "success", "probability": probability,
                    "hp_delta": -hp_loss, "random_relic_count": 1,
                },
                {
                    "outcome_id": "failure",
                    "probability": 1.0 - probability,
                    "hp_delta": -hp_loss, "next_damage": hp_loss + 1,
                },
            ]
            mark_random_fields(
                hp=-hp_loss, gold_domain=[0],
                relic_domain=[
                    {"gain": [], "remove": [], "counter": []},
                    {"gain": [random_relic], "remove": [], "counter": []},
                ],
                current_cost={"gold": 0, "hp": hp_loss, "max_hp": 0},
                future_costs=[], outcomes=outcomes, reason=reason,
            )
            value.update({
                "operation": "scrap_ooze_reach_inside",
                "event_outcome_id": "reach_random_branch",
                "random_effects": [random_relic],
                "uncertainty": [
                    "success/failure roll is exhaustive; relic identity remains in the base-game non-boss domain"
                ],
            })
            return True
        if option_kind == "LEAVE":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "scrap_ooze_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True

    if event_token == "facetrader":
        reason = "typed_base_game_face_trader_a0_branch"
        if option_kind == "TOUCH":
            hp_loss = parameters["hp_loss"]
            _oracle_numeric(
                value, -hp_loss, 0, parameters["gold_gain"], reason
            )
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason, hp=hp_loss)
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "face_trader_touch",
                "event_outcome_id": "touch",
            })
            return True
        if option_kind == "TRADE":
            pool = list(parameters["random_relic_pool"])
            probability = 1.0 / len(pool)
            outcomes = [
                {
                    "outcome_id": "gain_face_relic",
                    "probability": probability, "relic_id": relic_id,
                }
                for relic_id in pool
            ]
            relic_domain = [
                {"gain": [{"id": relic_id}], "remove": [], "counter": []}
                for relic_id in pool
            ]
            mark_random_fields(
                hp=0, gold_domain=[0], relic_domain=relic_domain,
                current_cost={"gold": 0, "hp": 0, "max_hp": 0},
                future_costs=[], outcomes=outcomes, reason=reason,
            )
            value.update({
                "operation": "face_trader_trade",
                "event_outcome_id": "trade_random_face",
                "random_effects": [{
                    "kind": "random_relic_gain", "count": 1,
                    "domain": pool, "selection_mode": "uniform_shuffle_first",
                }],
                "uncertainty": [
                    "the random face relic is uniformly selected from the reflected unowned pool"
                ],
            })
            return True
        if option_kind == "LEAVE":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "face_trader_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True

    if event_token == "duplicator":
        reason = "typed_base_game_duplicator_a0_branch"
        if option_kind == "DUPLICATE":
            future = [{
                "kind": "duplicator_grid_selection",
                "operation": "grid_duplicate",
                "select_count": parameters["duplicate_select_count"],
                "identity_binding": "subsequent_grid_card_instance_id",
                "commit_timing": "after_grid_confirmation",
            }]
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_known(
                value, "future_costs", future,
                "independent_mechanics_table", reason,
            )
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "duplicator_open_duplicate_grid",
                "event_outcome_id": "duplicate",
                "uncertainty": [
                    "exact duplicated card UUID binds on the following GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "independent_mechanics_table",
                    "reason": "Duplicator commits after GRID confirmation",
                },
            })
            return True
        if option_kind == "LEAVE":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "duplicator_leave",
                "event_outcome_id": "leave", "leave": True,
            })
            return True

    if event_token == "bonfireelementals":
        reason = "typed_base_game_bonfire_a0_branch"
        if option_kind == "OFFER_CARD":
            future = [{
                "kind": "bonfire_grid_selection",
                "operation": "grid_offer_card",
                "select_count": parameters["offer_select_count"],
                "domain": "current_purgeable_unbottled_deck",
                "identity_binding": "subsequent_grid_card_instance_id",
                "reward_rule": "base_game_card_rarity",
                "commit_timing": "after_grid_confirmation",
            }]
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_known(
                value, "future_costs", future,
                "independent_mechanics_table", reason,
            )
            _oracle_no_probability(value, reason)
            value.update({
                "operation": "bonfire_open_offer_grid",
                "event_outcome_id": "offer_card",
                "uncertainty": [
                    "offered card identity and rarity reward settle on GRID"
                ],
                "uncertainty_classification": {
                    "status": "classified_future",
                    "authority": "independent_mechanics_table",
                    "reason": (
                        "Bonfire removal and rarity reward commit after GRID"
                    ),
                },
            })
            return True

    value["uncertainty"].append("typed_staged_event_option_kind_unhandled")
    review["unclassified"].append("staged_event_option_kind")
    return False


def _oracle_mausoleum_result(
    status, reason, *, expected=None, realized=None, **details
):
    """Return the stable public evidence envelope used by strategy audit."""

    return {
        "schema_version": 1,
        "mechanism": "mausoleum_expected_curse_for_random_relic",
        "status": status,
        "reason": reason,
        "expected_trade_contract": _oracle_clone(expected),
        "realized_settlement": _oracle_clone(realized),
        **details,
    }


def _oracle_mausoleum_choice_id(choice):
    if not isinstance(choice, dict):
        return None
    value = choice.get("option_id", choice.get("choice_id"))
    return str(value) if value is not None and str(value) else None


def _oracle_mausoleum_selected_choice(record):
    """Bind the selected raw protocol option without canonical producer data."""

    options = record.get("available_options_before")
    selected = record.get("selected_choice_ids")
    if not isinstance(options, list):
        before_game = _oracle_before_game(record)
        screen = before_game.get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        visible_event = _oracle_game_id(
            screen.get("event_id") or screen.get("event_name")
        )
        if visible_event != _ORACLE_MAUSOLEUM_EVENT_ID:
            return None, "not_applicable", "not_mausoleum"
        return None, "inconclusive", "mausoleum_protocol_options_missing"
    mausoleum_visible = any(
        isinstance(option, dict)
        and isinstance(option.get("target"), dict)
        and _oracle_game_id(option["target"].get("event_id"))
        == _ORACLE_MAUSOLEUM_EVENT_ID
        for option in options
    )
    if not isinstance(selected, list) or len(selected) != 1:
        return (
            None,
            "inconclusive" if mausoleum_visible else "not_applicable",
            "mausoleum_selected_choice_missing"
            if mausoleum_visible else "not_mausoleum",
        )
    selected_id = str(selected[0])
    matches = [
        option for option in options
        if isinstance(option, dict)
        and _oracle_mausoleum_choice_id(option) == selected_id
    ]
    if len(matches) != 1:
        return (
            None,
            "issues" if mausoleum_visible else "not_applicable",
            "mausoleum_selected_choice_not_unique"
            if mausoleum_visible else "not_mausoleum",
        )
    target = matches[0].get("target")
    target = target if isinstance(target, dict) else {}
    if _oracle_game_id(target.get("event_id")) != _ORACLE_MAUSOLEUM_EVENT_ID:
        return None, "not_applicable", "not_mausoleum"
    if str(record.get("action") or "").casefold() != "choose":
        return None, "issues", "mausoleum_action_not_choose"
    if (
        str(record.get("requested_target_id") or "") != selected_id
        or str(record.get("resolved_target_id") or "") != selected_id
    ):
        return None, "issues", "mausoleum_receipt_binding_mismatch"
    return matches[0], "clear", "mausoleum_selected_protocol_option_bound"


def _oracle_mausoleum_omamori(game):
    relics = game.get("relics") if isinstance(game, dict) else None
    if not isinstance(relics, list):
        return None, None, "inconclusive", "mausoleum_relic_state_missing"
    matches = [
        relic for relic in relics
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "omamori"
    ]
    if len(matches) > 1:
        return None, None, "issues", "mausoleum_duplicate_omamori"
    if not matches:
        return False, 0, "clear", "mausoleum_omamori_absent"
    counter = matches[0].get("counter")
    if type(counter) is not int:
        return True, None, "inconclusive", "mausoleum_omamori_counter_missing"
    if not 0 <= counter <= 2:
        return True, counter, "issues", "mausoleum_omamori_counter_invalid"
    return True, counter, "clear", "mausoleum_omamori_counter_observed"


def mausoleum_expected_trade_contract(record, raw_choice=None):
    """Build the Mausoleum trade solely from protocol IDs and base mechanics.

    The return value is an evidence envelope.  ``expected_trade_contract`` is
    populated only when the event, option index, ascension, and any Omamori
    charge are independently observable.  Labels, localized text, producer
    consequences, scores, and producer settlements are never read.
    """

    record = record if isinstance(record, dict) else {}
    phase = str(record.get("phase") or "").upper()
    if raw_choice is None:
        if phase != "EVENT":
            return _oracle_mausoleum_result(
                "not_applicable", "not_mausoleum"
            )
        raw_choice, status, reason = _oracle_mausoleum_selected_choice(record)
        if status != "clear":
            return _oracle_mausoleum_result(status, reason)
    elif not isinstance(raw_choice, dict):
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_protocol_choice_missing"
        )

    target = raw_choice.get("target")
    target = target if isinstance(target, dict) else {}
    event_id = _oracle_game_id(target.get("event_id"))
    if event_id != _ORACLE_MAUSOLEUM_EVENT_ID:
        return _oracle_mausoleum_result(
            "not_applicable", "not_mausoleum"
        )
    if phase != "EVENT":
        return _oracle_mausoleum_result(
            "issues", "mausoleum_event_phase_mismatch"
        )
    if str(target.get("kind") or "").casefold() != "event_option":
        return _oracle_mausoleum_result(
            "issues", "mausoleum_target_kind_mismatch"
        )
    choice_index = raw_choice.get("choice_index")
    if type(choice_index) is not int or choice_index not in {0, 1}:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_choice_index_invalid"
        )

    before_state = record.get("authoritative_state_before")
    before_game = _game_from_state(before_state)
    if not isinstance(before_state, dict) or not isinstance(before_game, dict):
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_authoritative_before_missing"
        )
    before_seq = record.get("before_seq")
    if type(before_seq) is not int or type(before_state.get("state_seq")) is not int:
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_before_sequence_missing"
        )
    if before_state.get("state_seq") != before_seq:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_before_sequence_mismatch"
        )
    authoritative_phase = before_state.get("phase")
    if not isinstance(authoritative_phase, str) or not authoritative_phase:
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_authoritative_phase_missing"
        )
    if authoritative_phase.upper() != "EVENT":
        return _oracle_mausoleum_result(
            "issues", "mausoleum_authoritative_phase_mismatch"
        )
    ascension = before_game.get("ascension_level")
    if type(ascension) is not int:
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_ascension_missing"
        )
    if not 0 <= ascension <= 20:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_ascension_invalid"
        )
    surface, surface_status, surface_reason = _oracle_mausoleum_surface(
        record, raw_choice, before_game, ascension
    )
    if surface_status != "clear":
        return _oracle_mausoleum_result(surface_status, surface_reason)
    typed_contract = surface["event_contract"]
    stage = surface["event_stage"]
    option_kind = typed_contract["option_kind"]

    base = {
        "contract_version": MAUSOLEUM_TRADE_CONTRACT_VERSION,
        "contract_kind": "MAUSOLEUM_EXPECTED_TRADE",
        "authority": "independent_event_mechanics_v1",
        "event_id": _ORACLE_MAUSOLEUM_EVENT_NAME,
        "choice_id": _oracle_mausoleum_choice_id(raw_choice),
        "choice_index": choice_index,
        "original_button_index": surface["original_button_index"],
        "event_stage": stage,
        "ascension_level": ascension,
        "source_state_seq": before_seq,
        "typed_event_contract": _oracle_clone(typed_contract),
    }
    if stage == "RESULT":
        contract = {
            **base,
            "operation": "mausoleum_dialog_advance_noop",
            "guaranteed_benefit": None,
            "risk": None,
            "outcomes": [{
                "probability": 1.0,
                "curse_attempted": False,
                "writhe_added": False,
                "omamori_charges_consumed": 0,
            }],
        }
        return _oracle_mausoleum_result(
            "clear", "mausoleum_dialog_noop_contract_classified",
            expected=contract,
        )
    if option_kind == "LEAVE":
        contract = {
            **base,
            "operation": "mausoleum_leave",
            "guaranteed_benefit": None,
            "risk": None,
            "outcomes": [{
                "probability": 1.0,
                "curse_attempted": False,
                "writhe_added": False,
                "omamori_charges_consumed": 0,
            }],
        }
        return _oracle_mausoleum_result(
            "clear", "mausoleum_leave_contract_classified",
            expected=contract,
        )
    if option_kind != "OPEN":
        return _oracle_mausoleum_result(
            "issues", "mausoleum_initial_operation_mismatch"
        )

    present, counter, status, reason = _oracle_mausoleum_omamori(before_game)
    if status != "clear":
        return _oracle_mausoleum_result(status, reason)
    omamori_id = "Omamori"
    if present:
        omamori = next(
            relic for relic in before_game["relics"]
            if isinstance(relic, dict)
            and _oracle_game_id(relic.get("id") or relic.get("name"))
            == "omamori"
        )
        omamori_id = omamori.get("id") or omamori.get("name") or "Omamori"
    attempt_probability = 1.0 if ascension >= 15 else 0.5
    protected = bool(present and counter > 0)
    outcomes = []
    if attempt_probability < 1.0:
        outcomes.append({
            "probability": 1.0 - attempt_probability,
            "curse_attempted": False,
            "writhe_added": False,
            "omamori_charges_consumed": 0,
        })
    outcomes.append({
        "probability": attempt_probability,
        "curse_attempted": True,
        "writhe_added": not protected,
        "omamori_charges_consumed": 1 if protected else 0,
    })
    contract = {
        **base,
        "operation": "mausoleum_open_coffin",
        "guaranteed_benefit": {
            "kind": "random_relic_gain",
            "domain": _ORACLE_MAUSOLEUM_RELIC_DOMAIN,
            "excluded_rarities": ["BOSS"],
            "count": 1,
            "timing": "immediate",
            "selection_mode": "random",
        },
        "risk": {
            "kind": "curse_risk",
            "card_id": "Writhe",
            "attempt_probability": attempt_probability,
            "effective_curse_probability": (
                0.0 if protected else attempt_probability
            ),
            "omamori_present": bool(present),
            "omamori_id": omamori_id,
            "omamori_counter_before": counter if present else None,
            "omamori_charge_loss_probability": (
                attempt_probability if protected else 0.0
            ),
        },
        "outcomes": outcomes,
    }
    return _oracle_mausoleum_result(
        "clear", "mausoleum_open_contract_classified", expected=contract,
    )


def _oracle_mausoleum_bound_games(record):
    before_state = record.get("authoritative_state_before")
    after_state = record.get("authoritative_state_after")
    if not isinstance(before_state, dict) or not isinstance(after_state, dict):
        return None, None, "inconclusive", "mausoleum_authoritative_pair_missing"
    before_seq = record.get("before_seq")
    after_seq = record.get("after_seq")
    if not (
        type(before_seq) is int and type(after_seq) is int
        and after_seq > before_seq
        and type(before_state.get("state_seq")) is int
        and type(after_state.get("state_seq")) is int
    ):
        return None, None, "inconclusive", "mausoleum_state_sequence_missing"
    if (
        before_state.get("state_seq") != before_seq
        or after_state.get("state_seq") != after_seq
    ):
        return None, None, "issues", "mausoleum_state_sequence_mismatch"
    if (
        before_state.get("protocol_version") != 2
        or after_state.get("protocol_version") != 2
    ):
        return None, None, "issues", "mausoleum_protocol_version_mismatch"
    missing = []
    mismatches = []
    for field in ATTEMPT_BINDING_FIELDS:
        values = (
            record.get(field), before_state.get(field), after_state.get(field)
        )
        if any(value is None for value in values):
            missing.append(field)
        elif (
            type(values[0]) is not type(values[1])
            or type(values[0]) is not type(values[2])
            or values[0] != values[1]
            or values[0] != values[2]
        ):
            mismatches.append(field)
    if missing:
        return (
            None, None, "inconclusive", "mausoleum_state_binding_missing",
        )
    if mismatches:
        return (
            None, None, "issues", "mausoleum_state_binding_mismatch",
        )
    before_game = _game_from_state(before_state)
    after_game = _game_from_state(after_state)
    if not isinstance(before_game, dict) or not isinstance(after_game, dict):
        return None, None, "inconclusive", "mausoleum_game_state_missing"
    return before_game, after_game, "clear", "mausoleum_state_pair_bound"


def _oracle_mausoleum_empty_delta(delta):
    return bool(
        isinstance(delta, dict)
        and all(delta.get(field) == [] for field in ("added", "removed", "changed"))
    )


def _oracle_mausoleum_deck_uuid_error(cards):
    if not isinstance(cards, list):
        return "deck_not_observable"
    uuids = [_oracle_card_uuid(card) for card in cards]
    if any(uuid is None for uuid in uuids):
        return "deck_card_uuid_missing"
    if len(uuids) != len(set(uuids)):
        return "deck_card_uuid_duplicate"
    return None


def _oracle_mausoleum_relic_id_error(relics):
    if not isinstance(relics, list):
        return "relics_not_observable"
    ids = [
        _oracle_game_id(relic.get("id") or relic.get("name"))
        if isinstance(relic, dict) else ""
        for relic in relics
    ]
    if any(not relic_id for relic_id in ids):
        return "relic_id_missing"
    if len(ids) != len(set(ids)):
        return "relic_id_duplicate"
    return None


def _oracle_mausoleum_omamori_change(change):
    if not isinstance(change, dict):
        return False
    before = change.get("before")
    after = change.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not (
        _oracle_game_id(before.get("id") or before.get("name")) == "omamori"
        and _oracle_game_id(after.get("id") or after.get("name")) == "omamori"
    ):
        return False
    left = {key: value for key, value in before.items() if key != "counter"}
    right = {key: value for key, value in after.items() if key != "counter"}
    return _freeze(left) == _freeze(right)


def mausoleum_realized_settlement(record):
    """Independently settle the selected Mausoleum option from state frames."""

    record = record if isinstance(record, dict) else {}
    if str(record.get("phase") or "").upper() != "EVENT":
        return _oracle_mausoleum_result(
            "not_applicable", "not_mausoleum"
        )
    choice, choice_status, choice_reason = _oracle_mausoleum_selected_choice(
        record
    )
    if choice_status != "clear":
        return _oracle_mausoleum_result(choice_status, choice_reason)
    expected_result = mausoleum_expected_trade_contract(record, choice)
    contract = expected_result.get("expected_trade_contract")
    if expected_result.get("status") != "clear" or not isinstance(contract, dict):
        return expected_result
    before_game, after_game, pair_status, pair_reason = (
        _oracle_mausoleum_bound_games(record)
    )
    if pair_status != "clear":
        return _oracle_mausoleum_result(
            pair_status, pair_reason, expected=contract
        )
    if (
        before_game.get("ascension_level") != contract["ascension_level"]
        or after_game.get("ascension_level") != contract["ascension_level"]
    ):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_game_ascension_mismatch", expected=contract
        )

    before = _observable_from_authoritative_state(
        record["authoritative_state_before"]
    )
    after = _observable_from_authoritative_state(
        record["authoritative_state_after"]
    )
    if not isinstance(before, dict) or not isinstance(after, dict):
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_observable_state_missing",
            expected=contract,
        )
    numeric_delta = {}
    for field in ("current_hp", "max_hp", "gold", "block"):
        left = _finite_number(before.get(field))
        right = _finite_number(after.get(field))
        if left is None or right is None:
            return _oracle_mausoleum_result(
                "inconclusive", f"mausoleum_{field}_missing",
                expected=contract,
            )
        numeric_delta[
            "current_hp_delta" if field == "current_hp" else f"{field}_delta"
        ] = right - left
    numeric_delta["hp_delta"] = numeric_delta["current_hp_delta"]

    for cards in (before.get("deck"), after.get("deck")):
        deck_error = _oracle_mausoleum_deck_uuid_error(cards)
        if deck_error is not None:
            return _oracle_mausoleum_result(
                "inconclusive", f"mausoleum_{deck_error}", expected=contract
            )
    for relics in (before.get("relics"), after.get("relics")):
        relic_error = _oracle_mausoleum_relic_id_error(relics)
        if relic_error is not None:
            return _oracle_mausoleum_result(
                "inconclusive", f"mausoleum_{relic_error}", expected=contract
            )
    deltas = {
        field: _observable_collection_delta(before.get(field), after.get(field))
        for field in ("deck", "relics", "potions")
    }
    if any(not isinstance(delta, dict) for delta in deltas.values()):
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_inventory_delta_missing",
            expected=contract,
        )
    keys_before = before.get("keys")
    keys_after = after.get("keys")
    key_names = {"ruby", "emerald", "sapphire"}
    if not (
        isinstance(keys_before, dict) and isinstance(keys_after, dict)
        and key_names <= set(keys_before) and key_names <= set(keys_after)
        and all(type(keys_before[key]) is bool for key in key_names)
        and all(type(keys_after[key]) is bool for key in key_names)
    ):
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_key_state_missing", expected=contract
        )
    observable_delta = {
        **numeric_delta,
        **{field: _oracle_clone(delta) for field, delta in deltas.items()},
        "keys_before": _oracle_clone(keys_before),
        "keys_after": _oracle_clone(keys_after),
    }

    zero_delta_operation = contract.get("operation") in {
        "mausoleum_leave", "mausoleum_dialog_advance_noop",
    }
    if zero_delta_operation:
        persistent_context = {}
        for field in ("class", "seed", "act", "floor"):
            left = before_game.get(field)
            right = after_game.get(field)
            if left is None or right is None:
                return _oracle_mausoleum_result(
                    "inconclusive",
                    f"mausoleum_persistent_{field}_missing",
                    expected=contract,
                )
            persistent_context[field] = {
                "before": _oracle_clone(left),
                "after": _oracle_clone(right),
            }
        observable_delta["persistent_context"] = persistent_context
        if (
            any(abs(value) > 1e-9 for value in numeric_delta.values())
            or not all(_oracle_mausoleum_empty_delta(delta) for delta in deltas.values())
            or _freeze(keys_before) != _freeze(keys_after)
            or any(
                values["before"] != values["after"]
                for values in persistent_context.values()
            )
        ):
            reason_prefix = (
                "mausoleum_dialog_noop"
                if contract["operation"] == "mausoleum_dialog_advance_noop"
                else "mausoleum_leave"
            )
            return _oracle_mausoleum_result(
                "issues", f"{reason_prefix}_observable_delta_nonzero",
                expected=contract,
            )
        realized = {
            "schema_version": 1,
            "authority": "authoritative_protocol_delta",
            "event_id": _ORACLE_MAUSOLEUM_EVENT_NAME,
            "choice_id": contract.get("choice_id"),
            "choice_index": contract["choice_index"],
            "original_button_index": contract["original_button_index"],
            "event_stage": contract["event_stage"],
            "before_seq": record.get("before_seq"),
            "after_seq": record.get("after_seq"),
            "operation": contract["operation"],
            "acquired_benefit": None,
            "curse": None,
            "observable_delta": observable_delta,
        }
        is_dialog_noop = (
            contract["operation"] == "mausoleum_dialog_advance_noop"
        )
        return _oracle_mausoleum_result(
            "clear",
            (
                "mausoleum_dialog_noop_settlement_clear"
                if is_dialog_noop else "mausoleum_leave_settlement_clear"
            ),
            expected=contract, realized=realized,
        )

    relic_delta = deltas["relics"]
    if len(relic_delta["added"]) != 1:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_relic_gain_count_mismatch",
            expected=contract, observed_relic_gain_count=len(relic_delta["added"]),
        )
    acquired_relic = relic_delta["added"][0]
    acquired_id = _oracle_game_id(
        acquired_relic.get("id") or acquired_relic.get("name")
        if isinstance(acquired_relic, dict) else None
    )
    if acquired_id in _ORACLE_BOSS_RELIC_IDS:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_relic_outside_non_boss_pool",
            expected=contract, observed_relic_id=acquired_id,
        )
    pickup = _oracle_mausoleum_relic_pickup(acquired_relic)
    if pickup is None:
        return _oracle_mausoleum_result(
            "inconclusive", "mausoleum_relic_pickup_mechanics_unclassified",
            expected=contract, observed_relic_id=acquired_id,
        )
    if (
        relic_delta["removed"]
        or any(
            not _oracle_mausoleum_omamori_change(change)
            for change in relic_delta["changed"]
        )
    ):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_relic_delta", expected=contract
        )
    if not _oracle_mausoleum_empty_delta(deltas["potions"]):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_potion_delta", expected=contract
        )
    if _freeze(keys_before) != _freeze(keys_after):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_key_delta", expected=contract
        )

    deck_delta = deltas["deck"]
    if deck_delta["removed"] or deck_delta["changed"]:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_deck_delta", expected=contract
        )
    added_cards = deck_delta["added"]
    if len(added_cards) > 1 or any(
        _oracle_game_id(card.get("id")) != "writhe"
        or not _oracle_is_curse(card)
        for card in added_cards
    ):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_writhe_delta_mismatch", expected=contract
        )
    writhe = added_cards[0] if added_cards else None
    writhe_uuid = _oracle_card_uuid(writhe)
    before_uuids = {_oracle_card_uuid(card) for card in before.get("deck")}
    if writhe is not None and (
        writhe_uuid is None or writhe_uuid in before_uuids
    ):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_writhe_uuid_not_new", expected=contract
        )

    darkstone_gain = 6 if writhe is not None and any(
        isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "darkstoneperiapt"
        for relic in (before.get("relics") or [])
    ) else 0
    expected_numeric = {
        "current_hp_delta": pickup["hp_delta"] + darkstone_gain,
        "hp_delta": pickup["hp_delta"] + darkstone_gain,
        "max_hp_delta": pickup["max_hp_delta"] + darkstone_gain,
        "gold_delta": pickup["gold_delta"],
        "block_delta": pickup["block_delta"],
    }
    if any(
        abs(numeric_delta[field] - value) > 1e-9
        for field, value in expected_numeric.items()
    ):
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_numeric_delta",
            expected=contract, observed=numeric_delta,
            independently_expected=expected_numeric,
        )

    risk = contract["risk"]
    before_present = risk["omamori_present"]
    counter_before = (
        risk["omamori_counter_before"] if before_present else 0
    )
    after_present, counter_after, omamori_status, omamori_reason = (
        _oracle_mausoleum_omamori(after_game)
    )
    if omamori_status != "clear":
        return _oracle_mausoleum_result(
            omamori_status, omamori_reason, expected=contract
        )
    if before_present != after_present:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_omamori_presence_change",
            expected=contract,
        )
    charges_consumed = counter_before - counter_after
    if charges_consumed not in {0, 1}:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_omamori_charge_delta_invalid",
            expected=contract,
        )
    protected = bool(before_present and counter_before > 0)
    writhe_added = writhe is not None
    if protected and writhe_added:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_omamori_failed_to_block_writhe",
            expected=contract,
        )
    if not protected and charges_consumed:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_unexplained_omamori_charge_loss",
            expected=contract,
        )
    attempt_probability = risk["attempt_probability"]
    curse_attempted = bool(writhe_added or charges_consumed)
    if attempt_probability == 1.0 and not curse_attempted:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_mandatory_writhe_branch_missing",
            expected=contract,
        )
    if protected and curse_attempted and charges_consumed != 1:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_omamori_charge_not_consumed",
            expected=contract,
        )

    branch = next((
        outcome for outcome in contract["outcomes"]
        if outcome["curse_attempted"] is curse_attempted
        and outcome["writhe_added"] is writhe_added
        and outcome["omamori_charges_consumed"] == charges_consumed
    ), None)
    if branch is None:
        return _oracle_mausoleum_result(
            "issues", "mausoleum_realized_branch_not_in_contract",
            expected=contract,
        )
    acquired_raw_id = (
        acquired_relic.get("id") or acquired_relic.get("name")
    )
    realized = {
        "schema_version": 1,
        "authority": "authoritative_protocol_delta",
        "event_id": _ORACLE_MAUSOLEUM_EVENT_NAME,
        "choice_id": contract.get("choice_id"),
        "choice_index": contract["choice_index"],
        "original_button_index": contract["original_button_index"],
        "event_stage": contract["event_stage"],
        "before_seq": record.get("before_seq"),
        "after_seq": record.get("after_seq"),
        "operation": "mausoleum_open_coffin",
        "acquired_benefit": {
            "kind": "random_relic_gain",
            "id": acquired_raw_id,
            "relic_id": acquired_raw_id,
            "normalized_id": acquired_id,
            "tier": pickup["tier"],
            "domain": _ORACLE_MAUSOLEUM_RELIC_DOMAIN,
            "count": 1,
            "relic": _oracle_clone(acquired_relic),
        },
        "curse": {
            "card_id": "Writhe",
            "attempted": curse_attempted,
            "added": writhe_added,
            "card_instance_id": writhe_uuid,
            "omamori_charges_consumed": charges_consumed,
        },
        "realized_branch_probability": branch["probability"],
        "observable_delta": observable_delta,
    }
    return _oracle_mausoleum_result(
        "clear", "mausoleum_open_settlement_clear",
        expected=contract, realized=realized,
    )


def mausoleum_trade_evidence(record):
    """Public one-call API for strict expected/realized trade evidence."""

    return mausoleum_realized_settlement(record)


def _oracle_apply_mausoleum(value, raw, record):
    evidence = mausoleum_expected_trade_contract(record, raw)
    contract = evidence.get("expected_trade_contract")
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if evidence.get("status") != "clear" or not isinstance(contract, dict):
        reason = evidence.get("reason") or "mausoleum_contract_unproven"
        value["uncertainty"].append(reason)
        if evidence.get("status") == "issues":
            review["contradictions"].append({
                "field": "mausoleum_protocol_contract",
                "claimed": {
                    "event_id": (raw.get("target") or {}).get("event_id"),
                    "choice_index": raw.get("choice_index"),
                },
                "independently_expected": reason,
            })
        else:
            review["unclassified"].append("mausoleum_protocol_contract")
        return False

    reason = "typed_base_game_mausoleum_branch"
    value.update({
        "mechanism_id": "base_game_the_mausoleum_v1",
        "random_effects": [],
    })
    _oracle_numeric(value, 0, 0, 0, reason)
    _oracle_empty_inventory(value, reason)
    _oracle_costs(value, reason)
    if contract["operation"] in {
        "mausoleum_leave", "mausoleum_dialog_advance_noop",
    }:
        _oracle_no_probability(value, reason)
        value.update({
            "operation": contract["operation"],
            "event_outcome_id": (
                "dialog_advance"
                if contract["operation"] == "mausoleum_dialog_advance_noop"
                else "leave"
            ),
        })
        if contract["operation"] == "mausoleum_leave":
            value["leave"] = True
        return True

    benefit = contract["guaranteed_benefit"]
    risk = contract["risk"]
    writhe = {
        "id": "Writhe", "card_id": "Writhe", "type": "CURSE",
        "rarity": "CURSE", "count": 1,
    }
    blocked = risk["omamori_counter_before"] not in {None, 0}
    trigger_probability = risk["attempt_probability"]
    effective_probability = risk["effective_curse_probability"]
    before_game = _oracle_before_game(record)
    darkstone_gain = 6 if not blocked and any(
        isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "darkstoneperiapt"
        for relic in (before_game.get("relics") or [])
    ) else 0
    relic_changes = {
        "gain": [], "random_gain": [_oracle_clone(benefit)],
        "remove": [], "counter": [],
    }
    _oracle_known_domain(value, "relic_changes", relic_changes, reason)
    card_changes = {
        "gain": [], "remove": [], "upgrade": [], "transform": [],
        "conditional_gain": ([{
            **_oracle_clone(writhe),
            "probability": effective_probability,
            "condition": "writhe_branch_not_blocked",
        }] if effective_probability > 0 else []),
    }
    _oracle_known_domain(value, "card_changes", card_changes, reason)
    curse = {
        "gain": [], "remove": [],
        "conditional_gain": [] if blocked else [_oracle_clone(writhe)],
        "probability": trigger_probability,
        "effective_gain_probability": effective_probability,
        "omamori_applicable": True,
        "omamori_charges_before": (
            risk["omamori_counter_before"] or 0
        ),
        "omamori_charges_consumed": 1 if blocked else 0,
        "omamori_charge_use_probability": (
            trigger_probability if blocked else 0.0
        ),
        "blocked": blocked,
    }
    _oracle_known(
        value, "curse", curse, "independent_event_mechanics", reason
    )

    empty_cards = {
        "gain": [], "remove": [], "upgrade": [], "transform": [],
    }
    empty_curse = {
        "gain": [], "remove": [], "probability": 0.0,
        "omamori_applicable": False, "omamori_charges_consumed": 0,
    }

    def outcome(probability, triggered):
        outcome_cards = _oracle_clone(empty_cards)
        outcome_relics = {
            "gain": [], "random_gain": [_oracle_clone(benefit)],
            "remove": [], "counter": [],
        }
        outcome_curse = _oracle_clone(empty_curse)
        if triggered and blocked:
            counter = risk["omamori_counter_before"]
            outcome_relics["counter"] = [{
                "id": risk["omamori_id"],
                "before": counter, "after": counter - 1, "delta": -1,
            }]
            outcome_curse.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 1, "blocked": True,
                "card_id": "Writhe",
            })
            outcome_id = "writhe_blocked_by_omamori"
        elif triggered:
            outcome_cards["gain"] = [_oracle_clone(writhe)]
            outcome_curse.update({
                "gain": [_oracle_clone(writhe)],
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 0, "blocked": False,
                "card_id": "Writhe",
            })
            outcome_id = "writhe_added"
        else:
            outcome_id = "no_writhe"
        return {
            "id": outcome_id, "probability": probability,
            "hp_delta": (
                darkstone_gain if triggered and not blocked else 0
            ),
            "max_hp_delta": (
                darkstone_gain if triggered and not blocked else 0
            ),
            "gold_delta": 0,
            "card_changes": outcome_cards,
            "relic_changes": outcome_relics,
            "potion_changes": {
                "gain": [], "remove": [], "replace": [],
            },
            "curse": outcome_curse,
        }

    outcomes = [outcome(trigger_probability, True)]
    if trigger_probability < 1.0:
        outcomes.append(outcome(1.0 - trigger_probability, False))
    _oracle_known_domain(
        value, "probabilistic_outcomes", outcomes, reason
    )
    value.update({
        "operation": "mausoleum_open_coffin",
        "event_outcome_id": "open_coffin",
        "random_effects": [
            _oracle_clone(benefit),
            {
                "kind": "mausoleum_writhe_branch",
                "card_id": "Writhe",
                "probability": trigger_probability,
                "omamori_blocked": blocked,
            },
        ],
        "uncertainty": [
            "random non-boss relic identity remains inside the typed domain"
        ],
        "uncertainty_classification": {
            "status": "exhaustive_probability",
            "authority": "independent_event_mechanics",
            "reason": (
                "Writhe trigger/Omamori branches sum to one and the relic "
                "identity domain excludes boss relics"
            ),
        },
    })
    return True


def _oracle_apply_neow(value, target, record):
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    contract, invalid_reason, contradiction = (
        _oracle_validate_neow_contract(target, record)
    )
    if contract is None:
        bucket = "contradictions" if contradiction else "unclassified"
        if bucket == "contradictions":
            review[bucket].append({
                "field": "neow_contract", "claimed": target.get("neow_contract"),
                "independently_expected": invalid_reason,
            })
        else:
            review[bucket].append("neow_contract")
        value["uncertainty"].append(invalid_reason)
        return False
    value.update({
        "event_id": target.get("event_id"),
        "mechanism_id": target.get("mechanism_id"),
        "neow_contract": contract,
        "operation": (
            "neow_dialog_advance"
            if contract["contract_kind"] == "NEOW_DIALOG_ADVANCE"
            else "neow_reward"
        ),
        "random_effects": [],
    })
    if contract["contract_kind"] == "NEOW_DIALOG_ADVANCE":
        reason = "typed_neow_dialog_advance_has_no_resource_effect"
        value.update({
            "reward_kind": None,
            "drawback_kind": "NONE",
            "parameters": {
                "screen_num": contract["screen_num"],
                "resource_effect": contract["resource_effect"],
            },
        })
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        return True

    reward_kind = contract["reward_kind"]
    drawback_kind = contract["drawback_kind"]
    parameters = _oracle_clone(contract["parameters"])
    value.update({
        "reward_kind": reward_kind, "drawback_kind": drawback_kind,
        "parameters": parameters,
    })
    game = _oracle_before_game(record)
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    gold = game.get("gold")
    if any(type(item) is not int for item in (current_hp, max_hp, gold)):
        review["unclassified"].append("authoritative_neow_resource_state")
        value["uncertainty"].append(
            "authoritative_neow_resource_state_missing"
        )
        return True

    hp_bonus = parameters["hp_bonus"]
    reward_hp = (
        hp_bonus if reward_kind == "TEN_PERCENT_HP_BONUS"
        else 2 * hp_bonus if reward_kind == "TWENTY_PERCENT_HP_BONUS"
        else 0
    )
    reward_gold = (
        100 if reward_kind == "HUNDRED_GOLD"
        else 250 if reward_kind == "TWO_FIFTY_GOLD" else 0
    )
    drawback_hp = 0
    drawback_max_hp = 0
    drawback_gold = 0
    if drawback_kind == "TEN_PERCENT_HP_LOSS":
        drawback_max_hp = hp_bonus
        drawback_hp = max(0, current_hp - max(0, max_hp - hp_bonus))
    elif drawback_kind == "PERCENT_DAMAGE":
        drawback_hp = (current_hp // 10) * 3
    elif drawback_kind == "NO_GOLD":
        drawback_gold = gold
    reason = "typed_neow_reward_and_drawback_projection"
    _oracle_numeric(
        value, reward_hp - drawback_hp, reward_hp - drawback_max_hp,
        reward_gold - drawback_gold, reason,
    )
    _oracle_empty_inventory(value, reason)
    _oracle_costs(
        value, reason, gold=drawback_gold, hp=drawback_hp,
        max_hp=drawback_max_hp,
    )
    _oracle_no_probability(value, reason)

    future = _ORACLE_NEOW_FUTURE_REWARDS.get(reward_kind)
    if future is not None:
        _oracle_known(
            value, "future_costs", [_oracle_clone(future)],
            "independent_mechanics_table", reason,
        )
    random_effects = []
    random_reward = _ORACLE_NEOW_RANDOM_REWARDS.get(reward_kind)
    if random_reward is not None:
        random_effects.append(_oracle_clone(random_reward))
        if random_reward["kind"] == "card_gain":
            card_changes = _oracle_clone(value["card_changes"])
            card_changes["random_gain"] = [_oracle_clone(random_reward)]
            _oracle_known_domain(value, "card_changes", card_changes, reason)
        else:
            relic_changes = _oracle_clone(value["relic_changes"])
            relic_changes["random_gain"] = [_oracle_clone(random_reward)]
            if reward_kind == "BOSS_RELIC":
                starter = _oracle_neow_starter_relic(record)
                if starter is None:
                    value["field_knowledge"]["relic_changes"] = {
                        "status": "unknown", "authority": "protocol_state",
                        "reason": "starter_relic_not_uniquely_observable",
                    }
                    value["uncertainty"].append(
                        "starter relic replacement target is not unique"
                    )
                else:
                    relic_changes["remove"] = [starter]
                    _oracle_known_domain(
                        value, "relic_changes", relic_changes, reason
                    )
            else:
                _oracle_known_domain(
                    value, "relic_changes", relic_changes, reason
                )
    elif reward_kind == "THREE_ENEMY_KILL":
        _oracle_known(
            value, "relic_changes", {
                "gain": [{"id": "NeowsBlessing", "counter": 3}],
                "remove": [], "counter": [],
            }, "independent_mechanics_table", reason,
        )

    if drawback_kind == "CURSE":
        curse_effect = {
            "kind": "curse_gain", "domain": "curses",
            "rarities": ["CURSE"], "count": 1, "timing": "immediate",
            "selection_mode": "random",
        }
        omamori = _oracle_neow_omamori(record)
        charge = omamori.get("counter") if isinstance(omamori, dict) else 0
        if type(charge) is not int:
            for field in ("card_changes", "curse", "relic_changes"):
                value["field_knowledge"][field] = {
                    "status": "unknown", "authority": "protocol_state",
                    "reason": "omamori_counter_not_observable",
                }
            value["uncertainty"].append(
                "Omamori counter is required to classify the curse drawback"
            )
        elif charge > 0:
            curse_value = _oracle_clone(value["curse"])
            curse_value.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 1, "blocked": True,
                "random_gain": [],
            })
            _oracle_known(
                value, "curse", curse_value,
                "independent_mechanics_table", reason,
            )
            relic_changes = _oracle_clone(value["relic_changes"])
            relic_changes["counter"] = [{
                "id": omamori.get("id") or omamori.get("name") or "Omamori",
                "delta": -1,
            }]
            _oracle_known(
                value, "relic_changes", relic_changes,
                "independent_mechanics_table", reason,
            )
        else:
            random_effects.append(_oracle_clone(curse_effect))
            card_changes = _oracle_clone(value["card_changes"])
            card_changes["random_gain"] = [_oracle_clone(curse_effect)]
            _oracle_known_domain(value, "card_changes", card_changes, reason)
            curse_value = _oracle_clone(value["curse"])
            curse_value.update({
                "probability": 1.0, "omamori_applicable": True,
                "omamori_charges_consumed": 0, "blocked": False,
                "random_gain": [_oracle_clone(curse_effect)],
            })
            _oracle_known_domain(value, "curse", curse_value, reason)

    value["random_effects"] = random_effects
    if random_effects:
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "typed_neow_random_identity_domain",
        )
        value["uncertainty"].append(
            "random identity is unresolved but its JAR reward domain is typed"
        )
    if future is not None:
        value["uncertainty"].append(
            "Neow reward opens an independently typed downstream choice surface"
        )
    if random_effects:
        value["uncertainty_classification"] = {
            "status": "classified_random_domain",
            "authority": "independent_mechanics_table",
            "reason": "random identity domain, count, timing, and selection mode are typed",
        }
    elif future is not None:
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": "downstream Neow choice surface is typed by JAR mechanics",
        }
    return True


def _oracle_apply_the_joust_event(value, raw, record):
    """Classify The Joust's A0 multi-stage protocol surface.

    The event is intentionally represented as several ``EVENT`` decisions:
    an introductory dialog, a two-button wager, one or more result dialogs,
    and a final leave button.  The producer historically left these rows
    empty because the translated labels are not stable, which made otherwise
    deterministic transitions audit-inconclusive.  Use only the authoritative
    option count/button index and the base-game A0 wager table here; no label
    or producer score is consulted.
    """

    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    options = record.get("available_options_before") if isinstance(record, dict) else []
    option_count = len(options) if isinstance(options, list) else None
    choice_index = target.get("original_button_index")
    if type(choice_index) is not int and isinstance(raw, dict):
        choice_index = raw.get("choice_index")
    after = record.get("authoritative_state_after") if isinstance(record, dict) else {}
    after = after if isinstance(after, dict) else {}
    after_phase = str(after.get("phase") or "").upper()

    # First and subsequent one-button rows are dialog progress.  The final
    # row is distinguished by the authoritative phase transition back to MAP.
    if option_count == 1 and choice_index == 0:
        if after_phase == "MAP":
            reason = "the_joust_leave_dialog"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["operation"] = "the_joust_leave"
            value["leave"] = True
        else:
            reason = "the_joust_dialog_progress"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["operation"] = "the_joust_dialog"
            value["uncertainty"] = [
                "The Joust advances to the next typed dialog or wager surface"
            ]
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "independent_mechanics_table",
                "reason": "The Joust dialog progression is protocol-visible",
            }
        return True

    # At A0 both wager buttons pay an immediate 50-gold stake and expose an
    # exhaustive, typed future payout partition.  The payout itself is not
    # applied until the following result dialog, so the immediate delta stays
    # -50 and the random branch records only the later payout.
    if option_count == 2 and choice_index in {0, 1}:
        reason = (
            "the_joust_a0_wager_against"
            if choice_index == 0
            else "the_joust_a0_wager_for"
        )
        _oracle_numeric(value, 0, 0, -50, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason, gold=50)
        if choice_index == 0:
            outcomes = [
                {"probability": 0.70, "gold_delta": 100},
                {"probability": 0.30, "gold_delta": 0},
            ]
        else:
            outcomes = [
                {"probability": 0.30, "gold_delta": 250},
                {"probability": 0.70, "gold_delta": 0},
            ]
        _oracle_known(
            value, "probabilistic_outcomes", outcomes,
            "independent_mechanics_table",
            "exhaustive_the_joust_a0_wager_probability",
        )
        value["uncertainty"] = [
            "The wager payout is realized on the following result dialog"
        ]
        value["uncertainty_classification"] = {
            "status": "exhaustive_probability",
            "authority": "independent_mechanics_table",
            "reason": "A0 wager branches sum to one and payout identities are typed",
        }
        value["operation"] = "the_joust_wager"
        return True

    return False


def _oracle_probability_contract(outcomes):
    if not isinstance(outcomes, list) or not outcomes:
        return False
    allowed = {
        "probability", "hp_delta", "current_hp_delta", "max_hp_delta",
        "gold_delta", "card_changes", "relic_changes", "potion_changes",
        "curse", "key_changes", "label", "id", "name", "raw_text",
    }
    total = 0.0
    for outcome in outcomes:
        if not isinstance(outcome, dict) or set(outcome) - allowed:
            return False
        probability = _finite_number(outcome.get("probability"))
        if probability is None or probability < 0 or probability > 1:
            return False
        if not (set(outcome) - {"probability", "label", "id", "name", "raw_text"}):
            return False
        total += probability
    return abs(total - 1.0) <= 1e-9


def _oracle_apply_target_contract(value, target):
    contract = target.get("consequence_contract")
    if not isinstance(contract, dict):
        return
    value["target_consequence_claim"] = contract
    value["uncertainty"].append(
        "target consequence claim requires independent mechanism validation"
    )
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    aliases = {"current_hp_delta": "hp_delta"}
    for raw_field, claim in contract.items():
        field = aliases.get(raw_field, raw_field)
        knowledge = (value.get("field_knowledge") or {}).get(field)
        if (
            field in _ORACLE_CONSEQUENCE_FIELDS
            and isinstance(knowledge, dict)
            and knowledge.get("status") in {
                "known", "known_domain", "not_applicable",
            }
        ):
            if _freeze(claim) != _freeze(value.get(field)):
                review["contradictions"].append({
                    "field": raw_field, "claimed": claim,
                    "independently_expected": value.get(field),
                })
        else:
            review["unclassified"].append(raw_field)


_ORACLE_PROBABILITY_MECHANISMS = {
    "coin_flip_gold_10_or_zero": [
        {"probability": 0.5, "gold_delta": 10},
        {"probability": 0.5, "gold_delta": 0},
    ],
}


def _oracle_apply_probability(value, target):
    mechanism_id = str(target.get("mechanism_id") or "")
    outcomes = _ORACLE_PROBABILITY_MECHANISMS.get(mechanism_id)
    target_claim = target.get("probabilistic_outcomes")
    review = value.setdefault(
        "_target_claim_review", {"contradictions": [], "unclassified": []}
    )
    if isinstance(value.get("neow_contract"), dict) and target_claim is not None:
        value["target_probability_claim"] = target_claim
        review["contradictions"].append({
            "field": "probabilistic_outcomes", "claimed": target_claim,
            "independently_expected": (
                "no exhaustive per-identity outcomes in typed Neow contract"
            ),
        })
        return
    if outcomes is None:
        if target_claim is not None:
            value["target_probability_claim"] = target_claim
            value["uncertainty"].append(
                "target probability claim lacks a classified mechanism id"
            )
            review["unclassified"].append("probabilistic_outcomes")
        return
    if target_claim is not None and _freeze(target_claim) != _freeze(outcomes):
        value["target_probability_claim"] = target_claim
        value["uncertainty"].append(
            "target probability claim contradicts the production mechanism table"
        )
        review["contradictions"].append({
            "field": "probabilistic_outcomes", "claimed": target_claim,
            "independently_expected": outcomes,
        })
    _oracle_known(
        value, "probabilistic_outcomes", outcomes,
        "independent_mechanics_table",
        "exhaustive_observable_probability_partition",
    )
    value["uncertainty_classification"] = {
        "status": "exhaustive_probability",
        "authority": "independent_mechanics_table",
        "reason": "branch probabilities sum to one and effects are typed",
    }


_ORACLE_INDEXED_A0_EVENT_IDS_V1 = {
    "drugdealer", "goldenwing", "lab", "livingwall",
}
_ORACLE_INDEXED_A0_EVENT_IDS_V2 = {
    "addict", "backtobasics", "beggar", "bigfish", "thewomaninblue",
    "forgottenaltar", "ghosts", "mindbloom", "upgradeshrine",
    "wemeetagain", "shininglight", "mushrooms", "maskedbandits",
    "colosseum", "windinghalls",
    "accursedblacksmith", "mysterioussphere", "spireheart",
}
_ORACLE_INDEXED_A0_EVENT_IDS_V3 = {
    "nest", "noteforyourself", "anoteforyourself", "transmorgrifier", "purifier",
    "tomboflordredmask", "liarsgame", "vampires", "goldenshrine",
    "thelibrary", "nloth", "themoaihead",
}
_ORACLE_INDEXED_A0_EVENT_IDS = (
    _ORACLE_INDEXED_A0_EVENT_IDS_V1 | _ORACLE_INDEXED_A0_EVENT_IDS_V2
    | _ORACLE_INDEXED_A0_EVENT_IDS_V3
)

_ORACLE_INDEXED_A0_PROGRESS_EVENTS = {
    "liarsgame": (
        "com.megacrit.cardcrawl.events.exordium.Sssserpent", "event_stage",
    ),
    "vampires": (
        "com.megacrit.cardcrawl.events.city.Vampires", "screen_num",
    ),
    "goldenshrine": (
        "com.megacrit.cardcrawl.events.shrines.GoldShrine", "event_stage",
    ),
    "shininglight": (
        "com.megacrit.cardcrawl.events.exordium.ShiningLight", "event_stage",
    ),
    "mushrooms": (
        "com.megacrit.cardcrawl.events.exordium.Mushrooms", "screen_num",
    ),
    "maskedbandits": (
        "com.megacrit.cardcrawl.events.city.MaskedBandits", "event_stage",
    ),
    "colosseum": (
        "com.megacrit.cardcrawl.events.city.Colosseum", "event_stage",
    ),
    "windinghalls": (
        "com.megacrit.cardcrawl.events.beyond.WindingHalls", "screen_num",
    ),
    "sensorystone": (
        "com.megacrit.cardcrawl.events.beyond.SensoryStone", "event_stage",
    ),
    "accursedblacksmith": (
        "com.megacrit.cardcrawl.events.shrines.AccursedBlacksmith", "screen_num",
    ),
    "mysterioussphere": (
        "com.megacrit.cardcrawl.events.beyond.MysteriousSphere", "event_stage",
    ),
    "spireheart": (
        "com.megacrit.cardcrawl.events.beyond.SpireHeart", "event_stage",
    ),
    "thelibrary": (
        "com.megacrit.cardcrawl.events.city.TheLibrary", "screen_num",
    ),
}


def _oracle_indexed_event_surface(record, event_id):
    game = _oracle_before_game(record or {})
    if game.get("ascension_level") != 0:
        return None
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    screen_options = screen.get("options")
    options = (
        screen_options
        if isinstance(screen_options, list) and screen_options
        else (record or {}).get("available_options_before") or []
    )
    indexes = []
    for option in options:
        option = option if isinstance(option, dict) else {}
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        option_event_id = target.get("event_id") or screen.get("event_id")
        if _oracle_game_id(option_event_id) != event_id:
            return None
        index = option.get("original_button_index")
        if index is None:
            index = target.get("original_button_index")
        if type(index) is not int:
            return None
        indexes.append(index)
    return tuple(sorted(indexes)) if len(indexes) == len(set(indexes)) else None


def _oracle_indexed_event_grid_effect(
    event_id, operation, count, *, dialog_steps=0,
):
    value = {
        "kind": "event_grid_selection",
        "event_id": event_id,
        "domain": "current_deck",
        "operation": operation,
        "select_count": count,
        "selection_mode": "player_choice",
    }
    if operation == "transform":
        value.update({
            "result_domain": "character_cards",
            "result_selection_mode": "random",
        })
    if dialog_steps:
        value["dialog_steps_before_grid"] = dialog_steps
    return [value]


def _oracle_apply_fixed_curse_gain(value, record, card, count, reason):
    """Apply an exact curse package, including observable Omamori charges."""

    game = _oracle_before_game(record or {})
    omamori = next(
        (
            relic for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
            and _oracle_game_id(relic.get("id") or relic.get("name"))
            == "omamori"
        ),
        None,
    )
    charges = omamori.get("counter") if isinstance(omamori, dict) else 0
    if type(charges) is not int or charges < 0:
        return False
    consumed = min(count, charges)
    gained = count - consumed
    card_gain = []
    if gained:
        gained_card = _oracle_clone(card)
        gained_card["count"] = gained
        card_gain.append(gained_card)
    _oracle_known(
        value, "card_changes", {
            "gain": card_gain, "remove": [], "upgrade": [], "transform": [],
        }, "independent_mechanics_table", reason,
    )
    _oracle_known(
        value, "curse", {
            "gain": card_gain, "remove": [], "probability": 1.0,
            "omamori_applicable": True,
            "omamori_charges_consumed": consumed,
        }, "independent_mechanics_table", reason,
    )
    if consumed:
        relic_changes = _oracle_clone(value.get("relic_changes") or {
            "gain": [], "remove": [], "counter": [],
        })
        relic_changes["counter"] = [{
            "id": omamori.get("id") or omamori.get("name") or "Omamori",
            "delta": -consumed,
        }]
        _oracle_known(
            value, "relic_changes", relic_changes,
            "independent_mechanics_table", reason,
        )
    if gained and any(
        isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "darkstoneperiapt"
        for relic in (game.get("relics") or [])
    ):
        hp_gain = 6 * gained
        current_hp_delta = value.get("hp_delta")
        max_hp_delta = value.get("max_hp_delta")
        if type(current_hp_delta) is not int or type(max_hp_delta) is not int:
            return False
        _oracle_numeric(
            value,
            current_hp_delta + hp_gain,
            max_hp_delta + hp_gain,
            value.get("gold_delta", 0),
            reason,
        )
    return True


def _oracle_apply_indexed_a0_event(
    value, target, record, event_id, raw_text=None,
):
    surface = _oracle_indexed_event_surface(record, event_id)
    index = target.get("original_button_index")
    if surface is None or type(index) is not int or index not in surface:
        return False
    progress = None
    progress_spec = _ORACLE_INDEXED_A0_PROGRESS_EVENTS.get(event_id)
    if progress_spec is not None:
        expected_class, progress_field = progress_spec
        screen = _oracle_before_game(record).get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        progress = screen.get(progress_field)
        if (
            screen.get("event_class") != expected_class
            or (
                "event_class" in target
                and target.get("event_class") != expected_class
            )
            or (
                progress_field in target
                and target.get(progress_field) != progress
            )
            or (
                progress_field == "event_stage"
                and (not isinstance(progress, str) or not progress)
            )
            or (
                progress_field == "screen_num"
                and (type(progress) is not int or progress < 0)
            )
        ):
            return False
    expected_surface = {
        "addict": (0, 1, 2),
        "backtobasics": (0, 1),
        "beggar": (0, 1),
        "bigfish": (0, 1, 2),
        "lab": (0,),
        "nest": (0, 1),
        "noteforyourself": (0, 1),
        "anoteforyourself": (0, 1),
        "transmorgrifier": (0, 1),
        "purifier": (0, 1),
        "tomboflordredmask": (0, 1, 2),
        "thewomaninblue": (0, 1, 2, 3),
        "forgottenaltar": (0, 1, 2),
        "ghosts": (0, 1),
        "mindbloom": (0, 1, 2),
        "upgradeshrine": (0, 1),
        "wemeetagain": (0, 1, 2, 3),
        "shininglight": (0, 1) if progress == "INTRO" else (0,),
        "mushrooms": (0, 1) if progress == 0 else (0,),
        "maskedbandits": (0, 1) if progress == "INTRO" else (0,),
        "colosseum": (0, 1) if progress == "POST_COMBAT" else (0,),
        "windinghalls": (0, 1, 2) if progress == 1 else (0,),
        "nloth": (0, 1, 2),
        "themoaihead": (
            (0, 1, 2)
            if _oracle_has_relic(record, "Golden Idol") else (0, 2)
        ),
        "sensorystone": (0, 1, 2) if progress == "INTRO_2" else (0,),
        "accursedblacksmith": (0, 1, 2) if progress == 0 else (0,),
        "mysterioussphere": (0, 1) if progress == "INTRO" else (0,),
        "spireheart": (0,),
        "liarsgame": (0, 1) if progress == "INTRO" else (0,),
        "vampires": (
            (0, 1, 2)
            if progress == 0 and _oracle_has_relic(record, "Blood Vial")
            else (0, 1) if progress == 0 else (0,)
        ),
        "goldenshrine": (0, 1, 2) if progress == "INTRO" else (0,),
        "thelibrary": (0, 1) if progress == 0 else (0,),
    }.get(event_id, (0, 1, 2))
    if surface == (0,) and event_id in {
                "addict", "backtobasics", "beggar", "bigfish",
                "thewomaninblue", "forgottenaltar", "ghosts", "mindbloom",
                "upgradeshrine", "wemeetagain", "drugdealer",
                "goldenwing",
                *(
            (
                "livingwall", "nest", "noteforyourself",
                "anoteforyourself",
                "transmorgrifier", "purifier", "nloth", "themoaihead",
            )
            if _oracle_projection_v3(target) else ()
        ),
    }:
        reason = f"indexed_a0_{event_id}_terminal_dialog"
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value.update({
            "original_button_index": index,
            "operation": f"{event_id}_dialog_advance_noop",
            "event_outcome_id": "dialog_advance",
            "leave": True,
        })
        return True
    if surface != expected_surface:
        # Original button indices are scoped to the current event dialog.
        # A terminal singleton commonly reuses zero and is not the initial
        # option-zero mechanic.
        return False
    reason = f"indexed_a0_{event_id}_mechanics"
    value["original_button_index"] = index

    _oracle_numeric(value, 0, 0, 0, reason)
    _oracle_empty_inventory(value, reason)
    _oracle_costs(value, reason)
    _oracle_no_probability(value, reason)
    operation = None
    future = []
    classification = None
    if event_id == "nloth":
        if index == 2:
            operation = "nloth_leave"
            value["leave"] = True
        else:
            label_token = _oracle_game_id(raw_text)
            relic_matches = []
            for relic in (_oracle_before_game(record).get("relics") or []):
                if not isinstance(relic, dict):
                    continue
                identifiers = {
                    _oracle_game_id(relic.get("id")),
                    _oracle_game_id(relic.get("name")),
                }
                if any(
                    token and token in label_token for token in identifiers
                ):
                    relic_matches.append(relic)
            if len(relic_matches) != 1:
                return False
            traded = _oracle_clone(relic_matches[0])
            _oracle_known(
                value, "relic_changes", {
                    "gain": [{"id": "Nloth's Gift"}],
                    "remove": [traded], "counter": [],
                }, "protocol_event_option_label", reason,
            )
            value.update({
                "relic_id": traded.get("id"),
                "acquired_benefit": {
                    "kind": "relic", "id": "Nloth's Gift",
                },
            })
            operation = "nloth_trade_relic"
    elif event_id == "themoaihead":
        game = _oracle_before_game(record)
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        if index == 0:
            max_hp_loss = max(1, (max_hp + 4) // 8)
            new_max_hp = max(1, max_hp - max_hp_loss)
            new_hp = (
                min(current_hp, new_max_hp)
                if _oracle_has_relic(record, "Mark of the Bloom")
                else new_max_hp
            )
            _oracle_numeric(
                value, new_hp - current_hp, -max_hp_loss, 0, reason
            )
            _oracle_costs(value, reason, max_hp=max_hp_loss)
            operation = "moai_head_jump_inside"
        elif index == 1:
            idol = next(
                (
                    relic for relic in (game.get("relics") or [])
                    if isinstance(relic, dict)
                    and _oracle_game_id(
                        relic.get("id") or relic.get("name")
                    ) == "goldenidol"
                ),
                None,
            )
            if idol is None:
                return False
            gold_gain = (
                0 if _oracle_has_relic(record, "Ectoplasm") else 333
            )
            _oracle_numeric(value, 0, 0, gold_gain, reason)
            _oracle_known(
                value, "relic_changes", {
                    "gain": [], "remove": [_oracle_clone(idol)],
                    "counter": [],
                }, "independent_mechanics_table", reason,
            )
            operation = "moai_head_offer_golden_idol"
        else:
            operation = "moai_head_leave"
            value["leave"] = True
    elif event_id == "liarsgame":
        if progress == "INTRO":
            if index == 0:
                gold_gain = (
                    0 if _oracle_has_relic(record, "Ectoplasm") else 175
                )
                future = [{
                    "kind": "event_deferred_settlement",
                    "event_id": "Liars Game",
                    "event_stage": "AGREE",
                    "gold_delta": gold_gain,
                    "card_gain": {
                        "id": "Doubt", "card_id": "Doubt",
                        "type": "CURSE", "rarity": "CURSE", "count": 1,
                    },
                    "timing": "next_event_confirmation",
                }]
                operation = "liars_game_prepare_agreement"
                value["event_outcome_id"] = "accept_gold_and_doubt"
                classification = "classified_future"
            else:
                operation = "liars_game_decline"
                value["event_outcome_id"] = "decline"
                value["leave"] = True
        elif progress == "AGREE":
            gold_gain = 0 if _oracle_has_relic(record, "Ectoplasm") else 175
            _oracle_numeric(value, 0, 0, gold_gain, reason)
            doubt = {
                "id": "Doubt", "card_id": "Doubt", "type": "CURSE",
                "rarity": "CURSE", "count": 1,
            }
            if not _oracle_apply_fixed_curse_gain(
                value, record, doubt, 1, reason
            ):
                return False
            operation = "liars_game_settle_agreement"
            value["event_outcome_id"] = "settle_gold_and_doubt"
        elif progress in {"DISAGREE", "COMPLETE"}:
            operation = "liars_game_leave"
            value["event_outcome_id"] = "leave"
            value["leave"] = True
        else:
            return False
    elif event_id == "vampires":
        game = _oracle_before_game(record)
        if progress == 0:
            relics = [
                relic for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            ]
            blood_vials = [
                relic for relic in relics
                if _oracle_game_id(relic.get("id") or relic.get("name"))
                == "bloodvial"
            ]
            has_vial = len(blood_vials) == 1
            decline_index = 2 if has_vial else 1
            if index == decline_index:
                operation = "vampires_decline"
                value["event_outcome_id"] = "decline"
                value["leave"] = True
            else:
                if index not in ({0, 1} if has_vial else {0}):
                    return False
                deck = game.get("deck")
                current_hp = game.get("current_hp")
                max_hp = game.get("max_hp")
                if (
                    not isinstance(deck, list)
                    or type(current_hp) is not int
                    or type(max_hp) is not int
                    or max_hp < 1
                ):
                    return False
                starter_strikes = {
                    "striker", "strikeg", "strikeb", "strikep",
                }
                removed = [
                    _oracle_clone(card)
                    for card in deck
                    if isinstance(card, dict)
                    and _oracle_game_id(card.get("id") or card.get("name"))
                    in starter_strikes
                ]
                bite = {
                    "id": "Bite", "card_id": "Bite", "type": "ATTACK",
                    "rarity": "SPECIAL", "count": 5,
                }
                trade_vial = bool(has_vial and index == 1)
                max_hp_loss = 0 if trade_vial else (max_hp * 3 + 9) // 10
                hp_loss = max(0, current_hp - (max_hp - max_hp_loss))
                _oracle_numeric(value, -hp_loss, -max_hp_loss, 0, reason)
                _oracle_costs(value, reason, max_hp=max_hp_loss)
                _oracle_known(
                    value, "card_changes", {
                        "gain": [bite], "remove": removed,
                        "upgrade": [], "transform": [],
                    }, "independent_mechanics_table", reason,
                )
                if trade_vial:
                    _oracle_known(
                        value, "relic_changes", {
                            "gain": [], "remove": [_oracle_clone(blood_vials[0])],
                            "counter": [],
                        }, "independent_mechanics_table", reason,
                    )
                value["acquired_benefit"] = {
                    "kind": "card_package", "id": "Bite",
                    "card_id": "Bite", "count": 5,
                    "removed_starter_strike_count": len(removed),
                }
                operation = (
                    "vampires_trade_blood_vial_for_bites"
                    if trade_vial else "vampires_trade_max_hp_for_bites"
                )
                value["event_outcome_id"] = (
                    "trade_blood_vial_for_bites"
                    if trade_vial else "accept_bites"
                )
        elif progress in {1, 2}:
            operation = "vampires_leave"
            value["event_outcome_id"] = "leave"
            value["leave"] = True
        else:
            return False
    elif event_id == "goldenshrine":
        if progress == "INTRO":
            if index == 0:
                gold_gain = (
                    0 if _oracle_has_relic(record, "Ectoplasm") else 100
                )
                _oracle_numeric(value, 0, 0, gold_gain, reason)
                operation = "golden_shrine_pray"
                value["event_outcome_id"] = "pray"
            elif index == 1:
                gold_gain = (
                    0 if _oracle_has_relic(record, "Ectoplasm") else 275
                )
                _oracle_numeric(value, 0, 0, gold_gain, reason)
                regret = {
                    "id": "Regret", "card_id": "Regret", "type": "CURSE",
                    "rarity": "CURSE", "count": 1,
                }
                if not _oracle_apply_fixed_curse_gain(
                    value, record, regret, 1, reason
                ):
                    return False
                operation = "golden_shrine_desecrate"
                value["event_outcome_id"] = "desecrate"
            else:
                operation = "golden_shrine_leave"
                value["event_outcome_id"] = "leave"
                value["leave"] = True
        elif progress == "COMPLETE":
            operation = "golden_shrine_leave"
            value["event_outcome_id"] = "leave"
            value["leave"] = True
        else:
            return False
    elif (
        event_id in _ORACLE_INDEXED_A0_PROGRESS_EVENTS
        and event_id != "spireheart"
        and surface == (0,)
    ):
        terminal = False
        if event_id == "mushrooms" and progress == 2:
            operation = "mushrooms_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "The Mushroom Lair",
                "reward": {
                    "gold_range": [20, 30],
                    "relic_id": "Odd Mushroom",
                    "duplicate_relic_id": "Circlet",
                },
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        elif event_id == "colosseum" and progress == "FIGHT":
            operation = "colosseum_start_first_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Colosseum Slavers",
                "reward": {"normal_combat_rewards": False},
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        elif event_id == "mysterioussphere" and progress == "PRE_COMBAT":
            operation = "mysterious_sphere_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "2 Orb Walkers",
                "reward": {"event_relic_rewards": True},
                "timing": "after_event_confirmation",
            }]
            classification = "classified_future"
        else:
            operation = f"{event_id}_dialog_advance_noop"
            terminal = bool(
                event_id == "shininglight" and progress == "COMPLETE"
                or event_id == "mushrooms" and progress == 1
                or event_id == "maskedbandits" and progress == "END"
                or event_id == "colosseum" and progress == "LEAVE"
                or event_id == "windinghalls" and progress == 2
                or event_id == "sensorystone" and progress == "LEAVE"
                or event_id == "accursedblacksmith" and progress == 2
                or event_id == "mysterioussphere" and progress == "END"
                or event_id == "thelibrary" and progress == 1
            )
            if terminal:
                value["leave"] = True
        value["event_outcome_id"] = (
            "leave" if terminal else "dialog_advance"
        )
    elif event_id == "thelibrary" and progress == 0:
        game = _oracle_before_game(record)
        if index == 0:
            operation = "library_prepare_card_choice"
            future = [{
                "kind": "event_grid_selection",
                "event_id": "The Library",
                "domain": "library_card_offering",
                "operation": "gain",
                "visible_count": 20,
                "select_count": 1,
                "selection_mode": "player_choice",
            }]
            classification = "classified_future"
        else:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            relic_ids = {
                _oracle_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            requested_heal = (max_hp * 33 + 50) // 100
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, requested_heal
            )
            _oracle_numeric(value, heal, 0, 0, reason)
            operation = "library_sleep"
            value["event_outcome_id"] = "sleep"
            value["leave"] = True
    elif event_id == "shininglight" and progress == "INTRO":
        if index == 1:
            operation = "shining_light_leave"
            value["leave"] = True
        else:
            game = _oracle_before_game(record)
            max_hp = game.get("max_hp")
            if type(max_hp) is not int:
                return False
            relic_ids = {
                _oracle_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            hp_loss = max_hp // 5
            if "torii" in relic_ids and 1 < hp_loss <= 5:
                hp_loss = 1
            if "tungstenrod" in relic_ids and hp_loss > 0:
                hp_loss -= 1
            _oracle_numeric(value, -hp_loss, 0, 0, reason)
            _oracle_costs(value, reason, hp=hp_loss)
            candidates = [
                _oracle_clone(card)
                for card in (game.get("deck") or [])
                if isinstance(card, dict)
                and int(card.get("upgrades") or 0) == 0
                and str(card.get("type") or "").upper()
                not in {"CURSE", "STATUS"}
            ]
            count = min(2, len(candidates))
            random_upgrade = {
                "kind": "event_random_card_upgrades",
                "event_id": event_id,
                "selection_mode": "random_without_replacement",
                "count": count,
                "eligible_card_instance_ids": sorted(
                    card.get("card_instance_id")
                    for card in candidates
                    if card.get("card_instance_id")
                ),
            }
            _oracle_known_domain(
                value, "card_changes", {
                    "gain": [], "remove": [], "upgrade": [],
                    "transform": [], "random_upgrade": [random_upgrade],
                }, "shining_light_random_upgradable_cards",
            )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "shining_light_random_upgradable_cards",
            )
            value["random_effects"] = [random_upgrade]
            operation = "shining_light_enter"
            classification = "classified_random_domain"
    elif event_id == "mushrooms" and progress == 0:
        game = _oracle_before_game(record)
        if index == 0:
            operation = "mushrooms_prepare_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "The Mushroom Lair",
                "requires_confirmation": True,
                "reward": {
                    "gold_range": [20, 30],
                    "relic_id": "Odd Mushroom",
                    "duplicate_relic_id": "Circlet",
                },
            }]
            classification = "classified_future"
        else:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if (
                type(current_hp) is not int or type(max_hp) is not int
                or card is None or _oracle_game_id(card.get("id")) != "parasite"
            ):
                return False
            relic_ids = {
                _oracle_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, max_hp // 4
            )
            _oracle_numeric(value, heal, 0, 0, reason)
            if not _oracle_apply_fixed_curse_gain(
                value, record, card, 1, reason
            ):
                return False
            operation = "mushrooms_heal_and_take_parasite"
    elif event_id == "maskedbandits" and progress == "INTRO":
        game = _oracle_before_game(record)
        if index == 0:
            gold = game.get("gold")
            if type(gold) is not int:
                return False
            _oracle_numeric(value, 0, 0, -gold, reason)
            _oracle_costs(value, reason, gold=gold)
            operation = "masked_bandits_pay_all_gold"
            value["leave"] = True
        else:
            operation = "masked_bandits_start_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Masked Bandits",
                "reward": {
                    "relic_id": "Red Mask",
                    "duplicate_relic_id": "Circlet",
                },
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
    elif event_id in {"noteforyourself", "anoteforyourself"} and surface == (0, 1):
        if index == 1:
            operation = "note_for_yourself_leave"
            value["leave"] = True
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or not _oracle_card_id(card.get("id")):
                return False
            offered = _oracle_clone(card)
            offered_id = offered.get("id") or offered.get("card_id")
            future = [{
                "kind": "event_grid_exchange",
                "event_id": event_id,
                "domain": "current_deck",
                "operation": "exchange_for_offered_card",
                "select_count": 1,
                "selection_mode": "player_choice",
                "offered_card": offered,
            }]
            value["acquired_benefit"] = {
                "kind": "card_package", "id": offered_id,
                "card_id": offered_id, "count": 1,
            }
            operation = "note_for_yourself_prepare_exchange"
            classification = "classified_future"
    elif event_id == "colosseum" and progress == "POST_COMBAT":
        if index == 0:
            operation = "colosseum_leave_after_first_combat"
            value["leave"] = True
        else:
            operation = "colosseum_start_second_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "Colosseum Nobs",
                "reward": {"event_relic_rewards": True},
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
    elif event_id == "windinghalls" and progress == 1:
        game = _oracle_before_game(record)
        current_hp = game.get("current_hp")
        max_hp = game.get("max_hp")
        if type(current_hp) is not int or type(max_hp) is not int:
            return False
        high_ascension = int(game.get("ascension_level") or 0) >= 15
        madness_hp_loss = max(
            1,
            (
                (max_hp * 9 + 25) // 50
                if high_ascension else (max_hp + 4) // 8
            ),
        )
        focus_heal = max(
            1,
            (
                (max_hp + 2) // 5
                if high_ascension else (max_hp + 2) // 4
            ),
        )
        retrace_max_hp_loss = max(1, (max_hp + 10) // 20)
        relic_ids = {
            _oracle_game_id(relic.get("id") or relic.get("name"))
            for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        }
        if index == 0:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "madness":
                return False
            hp_loss = madness_hp_loss
            if "tungstenrod" in relic_ids and hp_loss > 0:
                hp_loss -= 1
            _oracle_numeric(value, -hp_loss, 0, 0, reason)
            _oracle_costs(value, reason, hp=hp_loss)
            gained = _oracle_clone(card)
            gained["count"] = 2
            _oracle_known(
                value, "card_changes", {
                    "gain": [gained], "remove": [], "upgrade": [],
                    "transform": [],
                }, "protocol_event_card_preview", reason,
            )
            operation = "winding_halls_embrace_madness"
        elif index == 1:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "writhe":
                return False
            heal = 0 if "markofthebloom" in relic_ids else min(
                max_hp - current_hp, focus_heal
            )
            _oracle_numeric(value, heal, 0, 0, reason)
            if not _oracle_apply_fixed_curse_gain(
                value, record, card, 1, reason
            ):
                return False
            operation = "winding_halls_focus"
        else:
            max_hp_loss = retrace_max_hp_loss
            new_max_hp = max(1, max_hp - max_hp_loss)
            hp_delta = min(current_hp, new_max_hp) - current_hp
            _oracle_numeric(value, hp_delta, -max_hp_loss, 0, reason)
            _oracle_costs(value, reason, max_hp=max_hp_loss)
            operation = "winding_halls_retrace_steps"
    elif event_id == "sensorystone" and progress == "INTRO_2":
        game = _oracle_before_game(record)
        relic_ids = {
            _oracle_game_id(relic.get("id") or relic.get("name"))
            for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        }
        hp_loss = {0: 0, 1: 5, 2: 10}[index]
        if "tungstenrod" in relic_ids and hp_loss > 0:
            hp_loss -= 1
        _oracle_numeric(value, -hp_loss, 0, 0, reason)
        _oracle_costs(value, reason, hp=hp_loss)
        operation = f"sensory_stone_recall_{index + 1}"
        future = [{
            "kind": "optional_card_reward_sequence",
            "event_id": event_id,
            "card_pool": "COLORLESS",
            "reward_count": index + 1,
            "candidates_per_reward": 3,
            "can_skip": True,
            "timing": "after_event_choice",
        }]
        classification = "classified_future"
    elif event_id == "accursedblacksmith" and progress == 0:
        if index == 0:
            operation = "accursed_blacksmith_open_upgrade_grid"
            future = _oracle_indexed_event_grid_effect(event_id, "upgrade", 1)
            classification = "classified_future"
        elif index == 1:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "pain":
                return False
            if not _oracle_apply_fixed_curse_gain(
                value, record, card, 1, reason
            ):
                return False
            relic_changes = _oracle_clone(value.get("relic_changes"))
            relic_changes["gain"] = [{"id": "Warped Tongs"}]
            _oracle_known(
                value, "relic_changes", relic_changes,
                "independent_mechanics_table", reason,
            )
            value["relic_id"] = "Warped Tongs"
            operation = "accursed_blacksmith_rummage"
        else:
            operation = "accursed_blacksmith_leave"
            value["leave"] = True
    elif event_id == "mysterioussphere" and progress == "INTRO":
        if index == 0:
            operation = "mysterious_sphere_prepare_combat"
            future = [{
                "kind": "event_combat_transition",
                "event_id": event_id,
                "encounter_id": "2 Orb Walkers",
                "requires_confirmation": True,
                "reward": {"event_relic_rewards": True},
            }]
            classification = "classified_future"
        else:
            operation = "mysterious_sphere_leave"
            value["leave"] = True
    elif event_id == "spireheart":
        game = _oracle_before_game(record)
        enters_act_four = bool(
            progress == "GO_TO_ENDING"
            and game.get("act") == 3
            and all(
                game.get(field) is True
                for field in (
                    "has_ruby_key", "has_emerald_key", "has_sapphire_key",
                )
            )
        )
        if enters_act_four:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if (
                type(current_hp) is not int
                or type(max_hp) is not int
                or not 0 <= current_hp <= max_hp
            ):
                return False
            _oracle_numeric(value, max_hp - current_hp, 0, 0, reason)
            operation = "spire_heart_enter_act_four"
            value["event_outcome_id"] = "enter_act_four"
        else:
            operation = "spire_heart_dialog_advance_noop"
            value["event_outcome_id"] = "dialog_advance"
    elif event_id == "bigfish" and surface == (0, 1, 2):
        game = _oracle_before_game(record)
        if index == 0:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            _oracle_numeric(
                value, min(max_hp - current_hp, max_hp // 3), 0, 0,
                reason,
            )
            operation = "big_fish_banana"
        elif index == 1:
            _oracle_numeric(value, 5, 5, 0, reason)
            operation = "big_fish_donut"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "regret":
                return False
            _oracle_known_domain(
                value, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "big_fish_random_non_boss_relic",
            )
            _oracle_known(
                value, "card_changes", {
                    "gain": [_oracle_clone(card)], "remove": [],
                    "upgrade": [], "transform": [],
                }, "protocol_event_card_preview", reason,
            )
            _oracle_known(
                value, "curse", {
                    "gain": [_oracle_clone(card)], "remove": [],
                    "probability": 1.0, "omamori_applicable": True,
                    "omamori_charges_consumed": 0,
                }, "independent_mechanics_table", reason,
            )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "big_fish_random_non_boss_relic",
            )
            operation = "big_fish_box"
            classification = "classified_random_domain"
            value["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
    elif event_id == "thewomaninblue" and surface == (0, 1, 2, 3):
        if index == 3:
            operation = "woman_in_blue_leave"
            value["leave"] = True
        else:
            costs = {0: 20, 1: 30, 2: 40}
            counts = {0: 1, 1: 2, 2: 3}
            cost = costs[index]
            requested = counts[index]
            slots = sum(
                1 for potion in (_oracle_before_game(record).get("potions") or [])
                if isinstance(potion, dict)
                and _oracle_game_id(potion.get("id")) == "potionslot"
            )
            acquired = min(requested, slots)
            _oracle_numeric(value, 0, 0, -cost, reason)
            _oracle_costs(value, reason, gold=cost)
            _oracle_known_domain(
                value, "potion_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_potion_gain",
                        "domain": "base_game_potion_pool",
                        "count": acquired,
                    }] if acquired else [],
                    "remove": [], "replace": [],
                }, "woman_in_blue_random_potion_pool",
            )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "woman_in_blue_random_potion_pool",
            )
            operation = f"woman_in_blue_buy_{requested}"
            classification = "classified_random_domain"
            value["random_effects"] = [{
                "kind": "random_potion_gain",
                "domain": "base_game_potion_pool", "count": acquired,
                "selection_mode": "random",
            }]
    elif event_id == "backtobasics" and surface == (0, 1):
        if index == 0:
            operation = "back_to_basics_remove"
            future = _oracle_indexed_event_grid_effect(
                event_id, "remove", 1
            )
            classification = "classified_future"
        else:
            basics = []
            for card in (_oracle_before_game(record).get("deck") or []):
                card_id = _oracle_game_id(
                    card.get("id") if isinstance(card, dict) else None
                )
                if (
                    isinstance(card, dict)
                    and card_id in {
                        "striker", "strikeg", "strikeb", "strikep",
                        "defendr", "defendg", "defendb", "defendp",
                    }
                    and card.get("upgrades") == 0
                ):
                    basics.append(_oracle_clone(card))
            _oracle_known(
                value, "card_changes", {
                    "gain": [], "remove": [], "upgrade": basics,
                    "transform": [],
                }, "independent_mechanics_table", reason,
            )
            operation = "back_to_basics_upgrade_basics"
    elif event_id == "addict" and surface == (0, 1, 2):
        if index == 2:
            operation = "addict_leave"
            value["leave"] = True
        else:
            if index == 0:
                _oracle_numeric(value, 0, 0, -85, reason)
                _oracle_costs(value, reason, gold=85)
            _oracle_known_domain(
                value, "hp_delta", {"min": 0, "max": 14},
                "addict_random_non_boss_relic_hp_domain",
            )
            _oracle_known_domain(
                value, "max_hp_delta", {"min": 0, "max": 14},
                "addict_random_non_boss_relic_max_hp_domain",
            )
            _oracle_known_domain(
                value, "gold_delta", {
                    "min": -85 if index == 0 else 0,
                    "max": 215 if index == 0 else 300,
                }, "addict_random_non_boss_relic_gold_domain",
            )
            _oracle_known_domain(
                value, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "addict_random_non_boss_relic",
            )
            if index == 1:
                card = target.get("card")
                card = card if isinstance(card, dict) else None
                if card is None or _oracle_game_id(card.get("id")) != "shame":
                    return False
                _oracle_known(
                    value, "card_changes", {
                        "gain": [_oracle_clone(card)], "remove": [],
                        "upgrade": [], "transform": [],
                    }, "protocol_event_card_preview", reason,
                )
                _oracle_known(
                    value, "curse", {
                        "gain": [_oracle_clone(card)], "remove": [],
                        "probability": 1.0, "omamori_applicable": True,
                        "omamori_charges_consumed": 0,
                    }, "independent_mechanics_table", reason,
                )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "addict_random_non_boss_relic",
            )
            operation = "addict_buy_relic" if index == 0 else "addict_rob"
            classification = "classified_random_domain"
            value["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
    elif event_id == "beggar" and surface == (0, 1):
        if index == 0:
            _oracle_numeric(value, 0, 0, -75, reason)
            _oracle_costs(value, reason, gold=75)
            operation = "beggar_pay_for_remove"
            future = _oracle_indexed_event_grid_effect(
                event_id, "remove", 1
            )
            classification = "classified_future"
        else:
            operation = "beggar_leave"
            value["leave"] = True
    elif event_id == "livingwall" and surface == (0, 1, 2):
        operation = {0: "remove", 1: "transform", 2: "upgrade"}[index]
        future = _oracle_indexed_event_grid_effect(event_id, operation, 1)
        classification = "classified_future"
    elif event_id == "nest" and surface == (0, 1):
        if index == 0:
            _oracle_numeric(value, 0, 0, 99, reason)
            operation = "nest_steal_gold"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "ritualdagger":
                return False
            _oracle_numeric(value, -6, 0, 0, reason)
            _oracle_costs(value, reason, hp=6)
            _oracle_known(
                value, "card_changes", {
                    "gain": [_oracle_clone(card)], "remove": [],
                    "upgrade": [], "transform": [],
                }, "protocol_event_card_preview", reason,
            )
            value["acquired_benefit"] = {
                "kind": "card_package", "id": card.get("id"),
                "card_id": card.get("id"), "count": 1,
            }
            operation = "nest_join_cult"
    elif event_id == "transmorgrifier" and surface == (0, 1):
        if index == 0:
            operation = "transmorgrifier_open_transform_grid"
            future = _oracle_indexed_event_grid_effect(
                event_id, "transform", 1
            )
            classification = "classified_future"
        else:
            operation = "transmorgrifier_leave"
            value["leave"] = True
    elif event_id == "drugdealer" and surface == (0, 1, 2):
        if index == 0:
            game = _oracle_before_game(record)
            max_hp_loss = min(3, max(0, int(game.get("max_hp") or 0) - 1))
            _oracle_numeric(value, 0, -max_hp_loss, 0, reason)
            _oracle_costs(value, reason, max_hp=max_hp_loss)
            card = target.get("card") if isinstance(target.get("card"), dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "jax":
                value["uncertainty"].append("drug_dealer_jax_preview_missing")
                return False
            _oracle_known(
                value, "card_changes", {
                    "gain": [card], "remove": [], "upgrade": [],
                    "transform": [],
                }, "protocol_event_card_preview", reason,
            )
            operation = "drug_dealer_take_jax"
        elif index == 1:
            operation = "drug_dealer_transform_two"
            future = _oracle_indexed_event_grid_effect(
                event_id, "transform", 2
            )
            classification = "classified_future"
        else:
            operation = "drug_dealer_take_mutagenic_strength"
            _oracle_known(
                value, "relic_changes", {
                    "gain": [{"id": "MutagenicStrength"}],
                    "remove": [], "counter": [],
                }, "independent_mechanics_table", reason,
            )
            value["relic_id"] = "MutagenicStrength"
    elif event_id == "goldenwing" and surface == (0, 1, 2):
        if index == 0:
            _oracle_numeric(value, -7, 0, 0, reason)
            _oracle_costs(value, reason, hp=7)
            operation = "golden_wing_pray_remove"
            future = _oracle_indexed_event_grid_effect(
                event_id, "remove", 1, dialog_steps=1
            )
            classification = "classified_future"
        elif index == 1:
            operation = "golden_wing_destroy_for_gold"
            _oracle_known_domain(
                value, "gold_delta", {"min": 50, "max": 80},
                "inclusive_uniform_50_to_80",
            )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "inclusive_uniform_50_to_80",
            )
            classification = "classified_random_domain"
        else:
            operation = "golden_wing_leave"
            value["leave"] = True
    elif event_id == "lab" and surface == (0,):
        operation = "lab_receive_random_potions"
        future = [{
            "kind": "lab_random_potion_rewards",
            "count": 3,
            "selection_mode": "random",
            "timing": "after_event_choice",
        }]
        _oracle_known_domain(
            value, "probabilistic_outcomes", [],
            "base_game_potion_reward_pool",
        )
        classification = "classified_random_domain"
    elif event_id == "ghosts" and surface == (0, 1):
        if index == 1:
            operation = "ghosts_decline"
        else:
            game = _oracle_before_game(record)
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            ascension = game.get("ascension_level")
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if (
                type(current_hp) is not int or type(max_hp) is not int
                or type(ascension) is not int or card is None
                or _oracle_card_id(card.get("id")) != "ghostly"
            ):
                return False
            post_max_hp = max(1, max_hp // 2)
            max_hp_loss = max_hp - post_max_hp
            received = 3 if ascension >= 15 else 5
            gained_card = _oracle_card_gain_after_egg_relics(card, game)
            gained_card["count"] = received
            _oracle_numeric(
                value, min(current_hp, post_max_hp) - current_hp,
                -max_hp_loss, 0, reason,
            )
            _oracle_known(
                value, "card_changes", {
                    "gain": [gained_card], "remove": [], "upgrade": [],
                    "transform": [],
                }, "independent_mechanics_table", reason,
            )
            value["acquired_benefit"] = {
                "kind": "card_package", "id": card.get("id"),
                "card_id": card.get("id"), "count": received,
            }
            operation = "ghosts_accept"
    elif event_id == "forgottenaltar" and surface == (0, 1, 2):
        game = _oracle_before_game(record)
        if index == 0:
            relic_ids = {
                _oracle_game_id(relic.get("id") or relic.get("name"))
                for relic in (game.get("relics") or [])
                if isinstance(relic, dict)
            }
            if "goldenidol" not in relic_ids:
                return False
            _oracle_known(
                value, "relic_changes", {
                    "gain": [{"id": "Bloody Idol"}],
                    "remove": [{"id": "Golden Idol"}], "counter": [],
                }, "independent_mechanics_table", reason,
            )
            operation = "forgotten_altar_offer_golden_idol"
        elif index == 1:
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if type(current_hp) is not int or type(max_hp) is not int:
                return False
            max_hp_gain = 5
            damage = (max_hp + 2) // 4
            post_gain_hp = min(current_hp + max_hp_gain, max_hp + max_hp_gain)
            _oracle_numeric(
                value, post_gain_hp - damage - current_hp,
                max_hp_gain, 0, reason,
            )
            operation = "forgotten_altar_sacrifice"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            if card is None or _oracle_game_id(card.get("id")) != "decay":
                return False
            if not _oracle_apply_fixed_curse_gain(
                value, record, card, 1, reason
            ):
                return False
            operation = "forgotten_altar_desecrate"
    elif event_id == "mindbloom" and surface == (0, 1, 2):
        game = _oracle_before_game(record)
        floor = game.get("floor")
        if type(floor) is not int:
            return False
        if index == 0:
            operation = "mind_bloom_war_future_combat"
            future = [{
                "kind": "mind_bloom_act1_boss_combat",
                "reward": {
                    "rare_relic_count": 1,
                    "gold": 25 if int(game.get("ascension_level") or 0) >= 15 else 50,
                    "normal_combat_rewards": True,
                },
                "timing": "after_event_choice",
            }]
            classification = "classified_future"
            value["random_effects"] = [{
                "kind": "relic_gain",
                "domain": "rare_relics",
                "rarities": ["RARE"],
                "count": 1,
                "timing": "after_optional_boss_combat",
                "selection_mode": "random",
            }]
        elif index == 1:
            upgrades = [
                _oracle_clone(card)
                for card in (game.get("deck") or [])
                if isinstance(card, dict)
                and int(card.get("upgrades") or 0) == 0
                and str(card.get("type") or "").upper()
                not in {"CURSE", "STATUS"}
            ]
            _oracle_known(
                value, "card_changes", {
                    "gain": [], "remove": [], "upgrade": upgrades,
                    "transform": [],
                }, "independent_mechanics_table", reason,
            )
            _oracle_known(
                value, "relic_changes", {
                    "gain": [{"id": "Mark of the Bloom"}],
                    "remove": [], "counter": [],
                }, "independent_mechanics_table", reason,
            )
            value["relic_id"] = "Mark of the Bloom"
            operation = "mind_bloom_awake"
        else:
            card = target.get("card")
            card = card if isinstance(card, dict) else None
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if card is None or type(current_hp) is not int or type(max_hp) is not int:
                return False
            if floor >= 41:
                if _oracle_game_id(card.get("id")) != "doubt":
                    return False
                _oracle_numeric(value, max_hp - current_hp, 0, 0, reason)
                if not _oracle_apply_fixed_curse_gain(
                    value, record, card, 1, reason
                ):
                    return False
                operation = "mind_bloom_healthy"
            else:
                if _oracle_game_id(card.get("id")) != "normality":
                    return False
                _oracle_numeric(value, 0, 0, 999, reason)
                if not _oracle_apply_fixed_curse_gain(
                    value, record, card, 2, reason
                ):
                    return False
                operation = "mind_bloom_rich"
    elif event_id == "upgradeshrine" and surface == (0, 1):
        if index == 0:
            operation = "upgrade_shrine_open_grid"
            future = _oracle_indexed_event_grid_effect(
                event_id, "upgrade", 1
            )
            classification = "classified_future"
        else:
            operation = "upgrade_shrine_leave"
            value["leave"] = True
    elif event_id == "wemeetagain" and surface == (0, 1, 2, 3):
        game = _oracle_before_game(record)
        if index == 3:
            operation = "we_meet_again_attack"
        else:
            _oracle_known_domain(
                value, "relic_changes", {
                    "gain": [], "random_gain": [{
                        "kind": "random_relic_gain",
                        "domain": "base_game_non_boss_relic_pool",
                        "count": 1,
                    }], "remove": [], "counter": [],
                }, "we_meet_again_random_non_boss_relic",
            )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "we_meet_again_random_non_boss_relic",
            )
            classification = "classified_random_domain"
            value["random_effects"] = [{
                "kind": "random_relic_gain",
                "domain": "base_game_non_boss_relic_pool", "count": 1,
                "selection_mode": "random",
            }]
            if index == 0:
                held = [
                    _oracle_clone(potion)
                    for potion in (game.get("potions") or [])
                    if isinstance(potion, dict)
                    and _oracle_game_id(potion.get("id")) != "potionslot"
                ]
                if not held:
                    return False
                _oracle_known_domain(
                    value, "potion_changes", {
                        "gain": [], "remove_domain": held, "remove_count": 1,
                        "replace": [],
                    }, "we_meet_again_visible_held_potion_domain",
                )
                operation = "we_meet_again_give_potion"
            elif index == 1:
                _oracle_known_domain(
                    value, "gold_delta", {
                        "kind": "event_generated_offered_gold_loss",
                        "minimum": -max(0, int(game.get("gold") or 0)),
                        "maximum": 0,
                    }, "we_meet_again_hidden_offer_amount",
                )
                operation = "we_meet_again_give_gold"
            else:
                card = target.get("card")
                card = card if isinstance(card, dict) else None
                if card is None:
                    return False
                _oracle_known(
                    value, "card_changes", {
                        "gain": [], "remove": [_oracle_clone(card)],
                        "upgrade": [], "transform": [],
                    }, "protocol_event_card_preview", reason,
                )
                operation = "we_meet_again_give_card"
    elif event_id == "purifier" and surface == (0, 1):
        if index == 0:
            operation = "purifier_open_remove_grid"
            future = _oracle_indexed_event_grid_effect(
                event_id, "remove", 1
            )
            classification = "classified_future"
        else:
            operation = "purifier_leave"
            value["leave"] = True
    elif event_id == "tomboflordredmask" and surface == (0, 1, 2):
        if index == 1:
            game = _oracle_before_game(record)
            gold = game.get("gold")
            if type(gold) is not int:
                return False
            _oracle_numeric(value, 0, 0, -max(0, gold), reason)
            _oracle_costs(value, reason, gold=max(0, gold))
            _oracle_known(
                value, "relic_changes", {
                    "gain": [{"id": "Red Mask"}],
                    "remove": [], "counter": [],
                }, "independent_mechanics_table", reason,
            )
            value["relic_id"] = "Red Mask"
            operation = "tomb_red_mask_offer_all_gold"
        else:
            operation = "tomb_red_mask_leave"
            value["leave"] = True
    else:
        value["uncertainty"].append("indexed A0 event shape mismatch")
        return False

    value["operation"] = operation
    if event_id in {"liarsgame", "vampires", "goldenshrine"}:
        # The producer names this indexed A0 mechanics table explicitly.
        # Recompute the same id from the independently validated event class,
        # progress field, button surface and operation above so a forged id
        # remains a contradiction instead of an opaque producer assertion.
        value["mechanism_id"] = (
            f"base_game_{event_id}_a0_progress_v1"
        )
        value["random_effects"] = []
    if future:
        _oracle_known(
            value, "future_costs", future,
            "independent_mechanics_table", reason,
        )
    if classification:
        value["uncertainty"] = [
            "exact downstream identity settles on the typed follow-up surface"
        ]
        value["uncertainty_classification"] = {
            "status": classification,
            "authority": "independent_mechanics_table",
            "reason": reason,
        }
    return True


def _oracle_healing_potion_delta(game, potion_id):
    """Return exact immediate HP/max-HP deltas for staged healing potions."""

    if not isinstance(game, dict):
        return None
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return (
            (5, 5)
            if _oracle_game_id(potion_id) == "fruitjuice"
            else None
        )
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in game.get("relics") or []
        if isinstance(relic, dict)
    }
    multiplier = 2 if "sacredbark" in relic_ids else 1
    healing_blocked = "markofthebloom" in relic_ids
    magic_flower = "magicflower" in relic_ids
    toy = "toyornithopter" in relic_ids
    hp = max(0, current_hp)
    cap = max(hp, max_hp)
    original_hp, original_cap = hp, cap

    def heal(amount):
        nonlocal hp
        if healing_blocked:
            return
        amount = max(0, int(amount))
        if magic_flower:
            amount = (amount * 3 + 1) // 2
        hp = min(cap, hp + amount)

    potion_id = _oracle_game_id(potion_id)
    if potion_id == "bloodpotion":
        heal(int(cap * 0.20 * multiplier))
    elif potion_id == "fruitjuice":
        gain = 5 * multiplier
        cap += gain
        heal(gain)
    else:
        return None
    if toy:
        heal(5)
    return hp - original_hp, cap - original_cap


def _oracle_gold_reward_hp_delta(game, gold_gain):
    """Return the exact immediate Bloody Idol heal for one gold reward."""

    if not isinstance(game, dict) or type(gold_gain) is not int or gold_gain <= 0:
        return 0
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "bloodyidol" not in relic_ids or "markofthebloom" in relic_ids:
        return 0
    current_hp = game.get("current_hp")
    max_hp = game.get("max_hp")
    if type(current_hp) is not int or type(max_hp) is not int:
        return 0
    return max(0, min(5, max_hp - current_hp))


def _expected_visible_consequence(phase, raw, record=None):
    """Recompute consequences solely from raw protocol facts and mechanics."""

    raw = raw if isinstance(raw, dict) else {}
    target = raw.get("target")
    target = target if isinstance(target, dict) else {}
    kind = str(target.get("kind") or "").casefold()
    phase = str(phase or "").upper()
    value = _oracle_empty_consequence(raw.get("raw_text") or "")
    card = target.get("card") if isinstance(target.get("card"), dict) else {}
    relic = target.get("relic") if isinstance(target.get("relic"), dict) else {}
    item = target.get("item") if isinstance(target.get("item"), dict) else {}

    if kind == "protocol_action":
        action = str(target.get("action") or "").casefold()
        game = _oracle_before_game(record or {})
        boss_act_transition = (
            phase == "COMBAT_REWARD"
            and action == "proceed"
            and str(game.get("room_type") or "") == "TreasureRoomBoss"
            and game.get("act") in {1, 2}
            and type(game.get("current_hp")) is int
            and type(game.get("max_hp")) is int
        )
        if boss_act_transition:
            reason = "automatic_post_boss_act_transition_heal"
            hp_delta = (
                0 if _oracle_has_relic(record or {}, "MarkOfTheBloom")
                else max(0, game["max_hp"] - game["current_hp"])
            )
            _oracle_numeric(value, hp_delta, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
        elif phase == "HAND_SELECT" and action in {"proceed", "return"}:
            reason = "hand_select_confirmation_settles_previously_queued_effects"
            for field, result in (
                ("hp_delta", None), ("max_hp_delta", None),
                ("gold_delta", None),
                (
                    "card_changes",
                    {"gain": [], "remove": [], "upgrade": [], "transform": []},
                ),
                (
                    "relic_changes",
                    {"gain": [], "remove": [], "counter": []},
                ),
                (
                    "potion_changes",
                    {"gain": [], "remove": [], "replace": []},
                ),
                (
                    "curse",
                    {
                        "gain": [], "remove": [], "probability": None,
                        "omamori_applicable": None,
                        "omamori_charges_consumed": None,
                    },
                ),
                ("probabilistic_outcomes", []),
            ):
                value[field] = _oracle_clone(result)
                value["field_knowledge"][field] = {
                    "status": "not_applicable",
                    "authority": "protocol_queued_action_boundary",
                    "reason": reason,
                }
            _oracle_costs(value, reason)
            value["uncertainty"] = [
                "state changes on confirmation belong to the queued combat action"
            ]
            value["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "protocol_queued_action_boundary",
                "reason": reason,
            }
        else:
            reason = "protocol_action_has_no_immediate_inventory_or_resource_effect"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
        if phase in {"BOSS_REWARD", "CARD_REWARD", "COMBAT_REWARD", "CHEST"}:
            option_ids = [
                str(option.get("option_id", option.get("choice_id")))
                for option in (record.get("available_options_before") or [])
                if isinstance(option, dict)
                and option.get("option_id", option.get("choice_id")) is not None
            ] if isinstance(record, dict) else []
            _oracle_known(
                value, "future_costs",
                [{"kind": "foregone_visible_option_ids", "choice_ids": option_ids}],
                "protocol_visible_choice_surface",
                "return_or_proceed_forfeits_visible_reward_choices",
            )
        if action in {"proceed", "return"}:
            value["operation"] = (
                "hand_select_confirm_queued_action"
                if phase == "HAND_SELECT" else action
            )
    elif kind in {"map_node", "map_boss"}:
        reason = "typed_map_entry_and_room_relic_effects"
        entry_heal = _oracle_map_entry_hp_delta(record or {}, target)
        entry_gold = _oracle_map_entry_gold_delta(record or {}, target)
        if entry_heal is not None:
            _oracle_known(
                value, "hp_delta", entry_heal,
                "independent_mechanics_table", reason,
            )
            _oracle_known(
                value, "max_hp_delta", 0,
                "independent_mechanics_table", reason,
            )
        if entry_gold is not None:
            _oracle_known(
                value, "gold_delta", entry_gold,
                "independent_mechanics_table", reason,
            )
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value["route"] = (
            {key: target.get(key) for key in ("symbol", "x", "y")}
            if kind == "map_node"
            else {"boss": True, "act": target.get("act")}
        )
        value["uncertainty"] = [
            "future room outcomes remain probabilistic; entrance is exact"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future", "authority": "protocol_map_coordinate",
            "reason": "route entrance is deterministic and future rooms are out of immediate scope",
        }
    elif phase == "REST" and kind == "rest":
        rest_option = str(target.get("rest_option") or "").upper()
        reason = f"typed_campfire_{rest_option.casefold()}"
        game = _oracle_before_game(record)
        if rest_option == "REST":
            current_hp = game.get("current_hp")
            max_hp = game.get("max_hp")
            if all(type(number) is int for number in (current_hp, max_hp)):
                heal = max_hp * 3 // 10
                relic_ids = {
                    _oracle_game_id(relic.get("id") or relic.get("name"))
                    for relic in (game.get("relics") or [])
                    if isinstance(relic, dict)
                }
                if "markofthebloom" in relic_ids:
                    heal = 0
                elif "regalpillow" in relic_ids:
                    heal += 15
                heal = max(0, min(max_hp - current_hp, heal))
                _oracle_numeric(value, heal, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
                _oracle_no_probability(value, reason)
                value["operation"] = "campfire_rest"
        elif rest_option in {"SMITH", "TOKE"}:
            operation = "upgrade" if rest_option == "SMITH" else "remove"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            _oracle_known(
                value, "future_costs",
                [{
                    "kind": "campfire_grid_selection",
                    "operation": operation,
                    "select_count": 1,
                    "timing": "after_campfire_choice",
                }],
                "protocol_multistage_operation", reason,
            )
            value["operation"] = (
                "campfire_smith" if rest_option == "SMITH" else "campfire_toke"
            )
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "exact card is selected on the following GRID surface",
            }
        elif rest_option == "RECALL":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["operation"] = "campfire_recall"
            value["key_changes"] = {"gain": ["ruby_key"]}
        elif rest_option == "DIG":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            random_relic = {
                "kind": "random_relic_reward",
                "domain": "base_game_non_boss_relic_pool",
                "count": 1,
                "selection_mode": "random",
                "timing": "following_combat_reward_surface",
            }
            _oracle_known(
                value, "future_costs", [random_relic],
                "independent_mechanics_table", reason,
            )
            value.update({
                "operation": "campfire_dig",
                "random_effects": [random_relic],
                "uncertainty": [
                    "random relic identity is exposed on the following reward surface"
                ],
                "uncertainty_classification": {
                    "status": "classified_random_domain",
                    "authority": "independent_mechanics_table",
                    "reason": "Shovel opens one typed random non-boss relic reward",
                },
            })
        elif rest_option == "LIFT":
            girya = [
                relic for relic in game.get("relics") or []
                if isinstance(relic, dict)
                and _oracle_game_id(relic.get("id") or relic.get("name"))
                == "girya"
            ]
            if len(girya) == 1 and type(girya[0].get("counter")) is int:
                _oracle_numeric(value, 0, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_known(
                    value, "relic_changes", {
                        "gain": [], "remove": [], "counter": [{
                            "id": girya[0].get("id") or "Girya",
                            "delta": 1,
                        }],
                    }, "authoritative_relic_counter", reason,
                )
                _oracle_costs(value, reason)
                _oracle_no_probability(value, reason)
                value["operation"] = "campfire_lift"
        value["campfire_option"] = rest_option
    elif phase == "CHEST" and kind == "chest":
        reason = "typed_chest_open_transition"
        game = _oracle_before_game(record)
        relics = [
            relic for relic in (game.get("relics") or [])
            if isinstance(relic, dict)
        ]
        relic_ids = {
            _oracle_game_id(relic.get("id") or relic.get("name"))
            for relic in relics
        }
        cursed_key = "cursedkey" in relic_ids
        omamori = next((
            relic for relic in relics
            if _oracle_game_id(relic.get("id") or relic.get("name"))
            == "omamori"
        ), None)
        charges = omamori.get("counter") if omamori is not None else 0
        if type(charges) is not int or charges < 0:
            value["uncertainty"].append(
                "cursed_key_omamori_counter_missing"
            )
            return value
        consumed = 1 if cursed_key and charges > 0 else 0
        gained = 1 if cursed_key and not consumed else 0
        darkstone_gain = (
            6 if gained and "darkstoneperiapt" in relic_ids else 0
        )
        _oracle_numeric(value, darkstone_gain, darkstone_gain, 0, reason)
        _oracle_empty_inventory(value, reason)
        if cursed_key:
            random_curse = {
                "kind": "random_curse_gain",
                "domain": "base_game_curse_pool",
                "count": gained,
                "selection_mode": "random",
                "timing": "on_chest_open",
            }
            _oracle_known_domain(
                value, "card_changes", {
                    "gain": [], "random_gain": (
                        [random_curse] if gained else []
                    ),
                    "remove": [], "upgrade": [], "transform": [],
                }, "cursed_key_random_curse_identity_domain",
            )
            _oracle_known_domain(
                value, "curse", {
                    "gain": [], "random_gain": (
                        [random_curse] if gained else []
                    ),
                    "remove": [], "probability": 1.0,
                    "omamori_applicable": True,
                    "omamori_charges_consumed": consumed,
                }, "cursed_key_random_curse_identity_domain",
            )
            if consumed:
                _oracle_known(
                    value, "relic_changes", {
                        "gain": [], "remove": [], "counter": [{
                            "id": omamori.get("id") or "Omamori",
                            "delta": -1,
                        }],
                    }, "independent_mechanics_table", reason,
                )
            _oracle_known_domain(
                value, "probabilistic_outcomes", [],
                "cursed_key_random_curse_identity_domain",
            )
            value["random_effects"] = [random_curse] if gained else []
        _oracle_costs(value, reason)
        if not cursed_key:
            _oracle_no_probability(value, reason)
        _oracle_known(
            value, "future_costs",
            [{"kind": "chest_reward_surface", "timing": "after_open"}],
            "protocol_multistage_operation", reason,
        )
        value["operation"] = "open_chest"
        value["uncertainty_classification"] = {
            "status": (
                "classified_random_domain" if cursed_key
                else "classified_future"
            ),
            "authority": (
                "independent_mechanics_table" if cursed_key
                else "protocol_multistage_operation"
            ),
            "reason": (
                "Cursed Key curse identity is random and the chest reward follows"
                if cursed_key else
                "reward identity is exposed on the following reward surface"
            ),
        }
    elif phase == "SHOP_ROOM" and kind == "shop_room":
        reason = "typed_shop_room_entry_transition"
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        _oracle_known(
            value, "future_costs",
            [{"kind": "shop_inventory_surface", "timing": "after_entry"}],
            "protocol_multistage_operation", reason,
        )
        value["operation"] = "enter_shop"
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "protocol_multistage_operation",
            "reason": "shop inventory is exposed on the following SHOP_SCREEN surface",
        }
    elif phase == "GRID" and kind == "card":
        operation, selected_card, grid_error = _oracle_grid_target(
            record or {}, target
        )
        if grid_error is not None:
            value["uncertainty"].append(grid_error)
            review = value.setdefault(
                "_target_claim_review",
                {"contradictions": [], "unclassified": []},
            )
            if grid_error in {
                "grid_operation_not_unique",
                "grid_target_uuid_missing_or_mismatched",
                "grid_target_uuid_not_unique_on_screen",
                "grid_target_card_facts_mismatch",
            }:
                review["contradictions"].append({
                    "field": "grid_target_binding",
                    "claimed": _oracle_clone(target),
                    "independently_expected": (
                        "one exact UUID-bound card on one typed GRID operation"
                    ),
                    "reason": grid_error,
                })
            else:
                review["unclassified"].append("grid_operation_context")
        else:
            reason = f"{operation}_exact_card_instance"
            before_game = _oracle_before_game(record)
            has_darkstone = any(
                isinstance(relic, dict)
                and _oracle_game_id(relic.get("id") or relic.get("name"))
                == "darkstoneperiapt"
                for relic in (before_game.get("relics") or [])
            )
            if operation != "grid_transform" or not has_darkstone:
                _oracle_numeric(value, 0, 0, 0, reason)
            else:
                _oracle_known(
                    value, "gold_delta", 0,
                    "independent_mechanics_table", reason,
                )
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            if operation == "grid_gain":
                _oracle_known(
                    value, "card_changes", {
                        "gain": [_oracle_clone(selected_card)],
                        "remove": [], "upgrade": [], "transform": [],
                    }, "accepted_event_grid_binding", reason,
                )
                _oracle_known(
                    value, "future_costs", [],
                    "accepted_event_grid_binding", reason,
                )
                _oracle_no_probability(value, reason)
            else:
                _oracle_known(
                    value, "future_costs",
                    _oracle_grid_confirmation_followup(
                        operation, selected_card
                    ),
                    "authoritative_grid_context", reason,
                )
            if operation == "grid_transform":
                _oracle_known_domain(
                    value, "probabilistic_outcomes", [],
                    "grid_transform_random_identity_domain",
                )
                random_transform = {
                    "kind": "grid_transform_result",
                    "domain": "game_card_transform_pool",
                    "count": 1,
                    "timing": "after_grid_confirmation",
                    "selection_mode": "random",
                }
                grid_parent = (
                    (_oracle_before_game(record).get("screen_state") or {}).get(
                        "parent_choice_context"
                    ) or {}
                )
                if (
                    isinstance(grid_parent, dict)
                    and grid_parent.get("authority")
                    == "accepted_protocol_choice"
                    and grid_parent.get("parent_phase") == "BOSS_REWARD"
                    and _oracle_game_id(grid_parent.get("relic_id"))
                    == "astrolabe"
                    and grid_parent.get("operation") == "transform"
                    and grid_parent.get("select_count") == 3
                ):
                    random_transform["result_upgrades"] = 1
                value["random_effects"] = [random_transform]
                value["uncertainty"] = [
                    "exact transform commits after grid confirmation; result identity is random"
                ]
                value["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "independent_mechanics_table",
                    "reason": "exact source UUID, commit timing, and transform result domain are typed",
                }
            elif operation != "grid_gain":
                _oracle_no_probability(value, reason)
                value["uncertainty"] = [
                    "exact card effect commits after grid confirmation"
                ]
                value["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "independent_mechanics_table",
                    "reason": "selected UUID and confirmation timing are exact",
                }
            value["selected_card"] = _oracle_clone(selected_card)
            value["operation"] = operation
    elif phase == "HAND_SELECT" and kind == "card":
        selected_card, context, hand_error = _oracle_hand_select_target(
            record or {}, target
        )
        if hand_error is not None:
            value["uncertainty"].append(hand_error)
            review = value.setdefault(
                "_target_claim_review",
                {"contradictions": [], "unclassified": []},
            )
            if hand_error in {
                "hand_select_target_uuid_missing_or_mismatched",
                "hand_select_target_uuid_not_unique_on_screen",
                "hand_select_target_card_facts_mismatch",
            }:
                review["contradictions"].append({
                    "field": "hand_select_target_binding",
                    "claimed": _oracle_clone(target),
                    "independently_expected": (
                        "one exact UUID-bound card on the authoritative hand selection surface"
                    ),
                    "reason": hand_error,
                })
            else:
                review["unclassified"].append("hand_select_action_context")
        else:
            reason = "hand_select_exact_transient_card_instance"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            _oracle_known(
                value, "future_costs", [{
                    "kind": "hand_select_action_resolution",
                    "current_action": context["current_action"],
                    "max_cards": context["max_cards"],
                    "can_pick_zero": context["can_pick_zero"],
                    "selected_card": _oracle_clone(selected_card),
                    "timing": "after_hand_selection_confirmation",
                }], "authoritative_hand_select_context", reason,
            )
            value["operation"] = "hand_select_card"
            value["selected_card"] = _oracle_clone(selected_card)
            value["uncertainty"] = [
                "the bound combat action resolves after hand selection confirmation"
            ]
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "independent_hand_select_projection",
                "reason": "selected UUID and queued action are protocol-visible",
            }
    elif kind == "card" and card and phase != "SHOP_SCREEN":
        game = _oracle_before_game(record or {})
        transient_combat_reward = bool(
            phase == "CARD_REWARD"
            and isinstance(game.get("combat_state"), dict)
            and str(game.get("room_phase") or "").upper() == "COMBAT"
        )
        if transient_combat_reward:
            reason = "temporary_combat_card_acquisition"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["operation"] = "add_temporary_combat_card"
        else:
            reason = "card_reward_acquisition"
            gold = 9 if _oracle_has_relic(record, "CeramicFish") else 0
            _oracle_numeric(value, 0, 0, gold, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_known(
                value, "card_changes",
                {"gain": [card], "remove": [], "upgrade": [], "transform": []},
                "protocol_card_target", reason,
            )
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            if phase == "CARD_REWARD":
                value["operation"] = "gain_card_reward"
    elif kind in {"card", "relic", "potion"} and item:
        reason = f"shop_{kind}_purchase"
        price = item.get("price") if type(item.get("price")) is int else None
        classified_shop_relic_future = None
        pickup_package = False
        if kind == "card":
            gold_gain = 9 if _oracle_has_relic(record, "CeramicFish") else 0
            if price is not None:
                _oracle_numeric(value, 0, 0, gold_gain - price, reason)
                _oracle_costs(value, reason, gold=price)
            _oracle_empty_inventory(value, reason)
            _oracle_known(
                value, "card_changes",
                {"gain": [item], "remove": [], "upgrade": [], "transform": []},
                "protocol_shop_target", reason,
            )
        elif kind == "potion":
            if price is not None:
                _oracle_numeric(value, 0, 0, -price, reason)
                _oracle_costs(value, reason, gold=price)
            _oracle_empty_inventory(value, reason)
            _oracle_known(
                value, "potion_changes",
                {"gain": [item], "remove": [], "replace": []},
                "protocol_shop_target", reason,
            )
        else:
            relic_id = _oracle_game_id(item.get("id") or item.get("name"))
            pickup = _ORACLE_IMMEDIATE_RELIC_PICKUPS.get(relic_id)
            passive_pickup = _oracle_passive_relic_pickup_known(
                relic_id, target
            )
            if relic_id in {"whetstone", "warpaint"} and price is not None:
                _oracle_apply_whetstone(
                    value, raw, record or {}, item, price, reason,
                    effect_id=relic_id,
                    eligible_type=(
                        "SKILL" if relic_id == "warpaint" else "ATTACK"
                    ),
                )
            pickup_package = _oracle_relic_pickup_package(
                value, record or {}, item, relic_id, reason, price=price,
                target=target,
            )
            if pickup_package:
                pass
            elif relic_id == "dollysmirror" and price is not None:
                _oracle_numeric(value, 0, 0, -price, reason)
                _oracle_costs(value, reason, gold=price)
                _oracle_empty_inventory(value, reason)
                classified_shop_relic_future = [{
                    "kind": "dollys_mirror_grid_selection",
                    "operation": "duplicate",
                    "select_count": 1,
                    "domain": "current_deck",
                    "selection_mode": "player_choice",
                    "timing": "after_relic_purchase",
                }]
                _oracle_known(
                    value, "future_costs", classified_shop_relic_future,
                    "independent_mechanics_table", reason,
                )
            elif price is not None and (
                pickup is not None or passive_pickup
            ):
                pickup = pickup or {
                    "hp_delta": 0, "max_hp_delta": 0, "gold_delta": 0,
                }
                _oracle_numeric(
                    value, pickup["hp_delta"], pickup["max_hp_delta"],
                    pickup["gold_delta"] - price, reason,
                )
                _oracle_costs(value, reason, gold=price)
                _oracle_empty_inventory(value, reason)
            _oracle_known(
                value, "relic_changes",
                {"gain": [item], "remove": [], "counter": []},
                "protocol_shop_target", reason,
            )
        if not pickup_package:
            _oracle_no_probability(value, reason)
        value["operation"] = {
            "card": "buy_card_then_rerank_shop",
            "relic": "buy_relic_then_rerank_shop",
            "potion": "buy_potion_then_rerank_shop",
        }[kind]
        if classified_shop_relic_future:
            value["uncertainty"] = [
                "the duplicated card UUID settles on the following GRID surface"
            ]
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "independent_mechanics_table",
                "reason": (
                    "Dollys Mirror always opens one current-deck card "
                    "duplication choice after purchase"
                ),
            }
    elif kind == "relic" and relic:
        reason = "relic_reward_acquisition"
        relic_id = _oracle_game_id(relic.get("id") or relic.get("name"))
        value["relic_id"] = relic.get("id") or relic.get("name")
        pickup = _ORACLE_IMMEDIATE_RELIC_PICKUPS.get(relic_id)
        passive_pickup = _oracle_passive_relic_pickup_known(relic_id, target)
        calling_bell_v2 = bool(
            relic_id == "callingbell" and _oracle_projection_v2(target)
        )
        pickup_package = _oracle_relic_pickup_package(
            value, record or {}, relic, relic_id, reason, target=target
        )
        if calling_bell_v2:
            _oracle_apply_calling_bell_pickup(value, relic, reason)
        elif pickup_package:
            pass
        elif relic_id in {"whetstone", "warpaint"}:
            _oracle_apply_whetstone(
                value, raw, record or {}, relic, None, reason,
                effect_id=relic_id,
                eligible_type=(
                    "SKILL" if relic_id == "warpaint" else "ATTACK"
                ),
            )
        elif pickup is not None:
            _oracle_numeric(
                value, pickup["hp_delta"], pickup["max_hp_delta"],
                pickup["gold_delta"], reason,
            )
        elif passive_pickup:
            _oracle_numeric(value, 0, 0, 0, reason)
        if pickup is not None or passive_pickup:
            _oracle_empty_inventory(value, reason)
        if not calling_bell_v2 and not pickup_package:
            _oracle_known(
                value, "relic_changes",
                {"gain": [relic], "remove": [], "counter": []},
                "protocol_relic_target", reason,
            )
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
        if phase == "BOSS_REWARD":
            value["operation"] = "gain_boss_relic"
    elif kind == "purge":
        price, purge_error = (
            _oracle_shop_purge(record or {}, target)
            if phase == "SHOP_SCREEN"
            else (None, "purge_outside_shop_screen")
        )
        if purge_error is not None:
            value["uncertainty"].append(purge_error)
            review = value.setdefault(
                "_target_claim_review",
                {"contradictions": [], "unclassified": []},
            )
            if purge_error in {
                "shop_purge_not_protocol_available",
                "shop_purge_cost_binding_invalid",
                "shop_purge_affordability_not_proven",
            }:
                review["contradictions"].append({
                    "field": "shop_purge_binding",
                    "claimed": _oracle_clone(target),
                    "independently_expected": (
                        "authoritative purge_available=true with an exact "
                        "affordable purge_cost"
                    ),
                    "reason": purge_error,
                })
            else:
                review["unclassified"].append("shop_purge_context")
        else:
            reason = "shop_purge_exact_protocol_cost"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_known(
                value, "future_costs", _oracle_shop_purge_followup(price),
                "authoritative_shop_context", reason,
            )
            _oracle_no_probability(value, reason)
            value["operation"] = "open_card_purge_grid"
            value["uncertainty"] = [
                "removed card UUID is bound only by the subsequent GRID choice and settlement"
            ]
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "independent_mechanics_table",
                "reason": "gold and exact card removal commit after the subsequent grid confirmation",
            }
    elif kind == "bowl":
        reason = "singing_bowl_card_reward_option"
        _oracle_numeric(
            value,
            2 if _oracle_projection_v2(target) else 0,
            2,
            0,
            reason,
        )
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value["operation"] = "singing_bowl"
    elif kind in {"sapphire_key", "key"}:
        reason = "sapphire_key_reward"
        _oracle_numeric(value, 0, 0, 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value["key_changes"] = {"gain": ["sapphire_key"]}
        value["operation"] = "gain_sapphire_key"
    elif kind == "potion_resource":
        operation = str(target.get("operation") or "").casefold()
        reason = f"resource_preparation_{operation}_exact_held_potion"
        binding_status, held_binding, binding_reason = (
            _oracle_held_potion_binding(record or {}, target)
        )
        review = value.setdefault(
            "_target_claim_review",
            {"contradictions": [], "unclassified": []},
        )
        if binding_status == "unknown":
            value["uncertainty"].append(binding_reason)
            review["unclassified"].append("held_potion_before_inventory_binding")
            return value
        if binding_status == "issues":
            value["uncertainty"].append(binding_reason)
            review["contradictions"].append({
                "field": "held_potion_before_inventory_binding",
                "claimed": _oracle_clone(target),
                "independently_expected": held_binding,
                "reason": binding_reason,
            })
            return value
        potion = next(
            potion for potion in (_oracle_before_game(record or {}).get("potions") or [])
            if isinstance(potion, dict)
            and potion.get("potion_instance_id")
            == held_binding["potion_instance_id"]
        )
        potion_id = _oracle_game_id(
            potion.get("id")
        )
        if operation == "discard":
            _oracle_numeric(value, 0, 0, 0, reason)
        elif operation == "use":
            delta = _oracle_healing_potion_delta(
                _oracle_before_game(record or {}), potion_id
            )
            if delta is not None:
                _oracle_numeric(value, delta[0], delta[1], 0, reason)
        _oracle_empty_inventory(value, reason)
        _oracle_known(
            value, "potion_changes",
            {"gain": [], "remove": [potion], "replace": []},
            "protocol_target", reason,
        )
        _oracle_costs(value, reason)
        _oracle_no_probability(value, reason)
        value["uncertainty"] = [
            "parent strategic choice remains pending after resource preparation"
        ]
        value["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "independent_mechanics_table",
            "reason": "the next authoritative screen audits the purchase or reward choice",
        }
    elif kind == "event_option":
        event_id = _oracle_game_id(target.get("event_id"))
        mechanism_id = str(target.get("mechanism_id") or "")
        value["event_id"] = target.get("event_id")
        if phase == "NEOW":
            _oracle_apply_neow(value, target, record or {})
        elif event_id == "thejoust":
            if not _oracle_apply_the_joust_event(value, raw, record or {}):
                value["uncertainty"].append(
                    "The Joust surface is missing its typed option-count/index contract"
                )
        elif (
            event_id in _ORACLE_INDEXED_A0_EVENT_IDS_V1
            or (
                event_id in _ORACLE_INDEXED_A0_EVENT_IDS_V2
                and _oracle_projection_v2(target)
            )
            or (
                event_id in _ORACLE_INDEXED_A0_EVENT_IDS_V3
                and _oracle_projection_v3(target)
            )
        ):
            if not _oracle_apply_indexed_a0_event(
                value, target, record or {}, event_id,
                raw.get("raw_text") or raw.get("label"),
            ):
                value["uncertainty"].append(
                    "event outcome is unclassified without typed protocol mechanics"
                )
        elif (
            event_id == "worldofgoop"
            and isinstance(target.get("event_contract"), dict)
        ):
            _oracle_apply_world_of_goop_event(value, raw, record or {})
        elif (
            event_id == "thecleric"
            and isinstance(target.get("event_contract"), dict)
        ):
            _oracle_apply_the_cleric_event(value, raw, record or {})
        elif (
            event_id == "designer"
            and isinstance(target.get("event_contract"), dict)
        ):
            _oracle_apply_designer_event(value, raw, record or {})
        elif (
            event_id in {
                "deadadventurer", "scrapooze", "facetrader", "duplicator",
                "bonfireelementals",
            }
            and isinstance(target.get("event_contract"), dict)
        ):
            _oracle_apply_repeatable_and_grid_event(
                value, raw, record or {}
            )
        elif event_id == "goldenidol":
            # Golden Idol is a fixed three-button base-game event.  Older
            # bridge frames marked the transition ``unresolved`` even though
            # the authoritative before/after state and original button index
            # fully expose the selected effect.  Keep this table exact; do
            # not infer event semantics from translated button text.
            choice_index = target.get(
                "original_button_index", raw.get("choice_index")
            )
            reason = "golden_idol_typed_branch"
            game = _oracle_before_game(record or {})
            screen = game.get("screen_state")
            screen = screen if isinstance(screen, dict) else {}
            screen_options = screen.get("options")
            option_count = len(screen_options) if isinstance(
                screen_options, list
            ) else None
            if option_count == 2 and choice_index == 0:
                _oracle_numeric(value, 0, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                value["relic_id"] = "Golden Idol"
                _oracle_known(
                    value, "relic_changes", {
                        "gain": [{"id": "Golden Idol"}],
                        "remove": [], "counter": [],
                    }, "independent_mechanics_table", reason,
                )
                _oracle_costs(value, reason)
            elif option_count == 2 and choice_index == 1:
                _oracle_numeric(value, 0, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
                value["leave"] = True
            elif option_count == 3 and choice_index == 0:
                omamori = next(
                    (
                        relic for relic in (game.get("relics") or [])
                        if isinstance(relic, dict)
                        and _oracle_game_id(
                            relic.get("id") or relic.get("name")
                        ) == "omamori"
                    ),
                    None,
                )
                charge = omamori.get("counter") if isinstance(
                    omamori, dict
                ) else 0
                if type(charge) is not int:
                    value["uncertainty"].append(
                        "golden_idol_omamori_counter_missing"
                    )
                else:
                    _oracle_numeric(value, 0, 0, 0, reason)
                    _oracle_empty_inventory(value, reason)
                    if charge > 0:
                        _oracle_known(
                            value, "curse", {
                                "gain": [], "remove": [],
                                "probability": 0.0,
                                "omamori_applicable": True,
                                "omamori_charges_consumed": 1,
                            }, "independent_mechanics_table", reason,
                        )
                        relic_changes = _oracle_clone(
                            value["relic_changes"]
                        )
                        relic_changes["counter"] = [{
                            "id": omamori.get("id") or omamori.get("name"),
                            "delta": -1,
                        }]
                        _oracle_known(
                            value, "relic_changes", relic_changes,
                            "independent_mechanics_table", reason,
                        )
                    else:
                        _oracle_known(
                            value, "curse", {
                                "gain": [{
                                    "id": "Injury", "count": 1,
                                }],
                                "remove": [], "probability": 1.0,
                                "omamori_applicable": False,
                                "omamori_charges_consumed": 0,
                            }, "independent_mechanics_table", reason,
                        )
                    _oracle_costs(value, reason)
            elif option_count == 3 and choice_index == 1:
                _oracle_numeric(value, -20, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
            elif option_count == 3 and choice_index == 2:
                current_hp = game.get("current_hp")
                max_hp = game.get("max_hp")
                if type(current_hp) is int and type(max_hp) is int:
                    new_max_hp = max(0, max_hp - 6)
                    current_hp_delta = min(current_hp, new_max_hp) - current_hp
                    _oracle_numeric(
                        value, current_hp_delta, -6, 0, reason
                    )
                else:
                    value["uncertainty"].append(
                        "golden_idol_current_or_max_hp_missing"
                    )
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
            elif option_count == 1 and choice_index == 0:
                # After the three-option follow-up, the remaining button is
                # the event's terminal leave/continue transition.
                _oracle_numeric(value, 0, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
                value["leave"] = True
            else:
                value["uncertainty"].append(
                    "golden_idol_choice_index_missing_or_invalid"
                )
            if (
                option_count in {1, 2, 3}
                and isinstance(choice_index, int)
                and 0 <= choice_index < option_count
            ):
                _oracle_no_probability(value, reason)
        elif event_id == _ORACLE_MAUSOLEUM_EVENT_ID:
            _oracle_apply_mausoleum(value, raw, record or {})
        elif event_id == "cursedtome":
            _oracle_apply_cursed_tome_event(value, raw, record or {})
        elif event_id == "knowingskull":
            _oracle_apply_knowing_skull_event(value, raw, record or {})
        elif mechanism_id == "gain_gold_exact" and type(target.get("amount")) is int:
            reason = "typed_gain_gold_event_primitive"
            _oracle_numeric(value, 0, 0, target["amount"], reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
        elif (
            event_id == "sensorystone"
            and target.get("event_class")
            and (
                target.get("event_stage") is not None
                or target.get("screen_num") is not None
            )
        ):
            if not _oracle_apply_indexed_a0_event(
                value, target, record or {}, event_id,
                raw.get("raw_text") or raw.get("label"),
            ):
                value["uncertainty"].append(
                    "event progress evidence is invalid or incomplete"
                )
        elif event_id == "sensorystone" and type(raw.get("choice_index")) is int:
            choice_index = raw["choice_index"]
            hp_costs = {0: 0, 1: -5, 2: -10}
            reward_counts = {0: 1, 1: 2, 2: 3}
            if choice_index in hp_costs:
                reason = "sensory_stone_typed_branch"
                _oracle_numeric(value, hp_costs[choice_index], 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_costs(value, reason)
                _oracle_known(
                    value, "future_costs", [{
                        "kind": "deferred_colorless_card_reward_choices",
                        "count": reward_counts[choice_index],
                    }], "independent_event_mechanics", reason,
                )
                _oracle_no_probability(value, reason)
                value["uncertainty"] = [
                    "card identities are bound by subsequent CARD_REWARD states"
                ]
                value["uncertainty_classification"] = {
                    "status": "classified_future",
                    "authority": "independent_event_mechanics",
                    "reason": "SensoryStone opens a known count of downstream colorless rewards",
                }
        elif event_id in {"matchandkeep", "matchkeep"}:
            reason = "match_and_keep_position_flip"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            value["field_knowledge"]["card_changes"] = {
                "status": "not_observable",
                "authority": "protocol_hidden_event_state",
                "reason": "hidden card identity and match pairing are not exposed",
            }
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["uncertainty"] = [
                "hidden MatchAndKeep card identity is not protocol-visible"
            ]
            value["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "protocol_hidden_event_state",
                "reason": "all visible positions are exact but card identities remain hidden",
            }
        elif event_id == "wheelofchange" and len(
            (record or {}).get("available_options_before") or []
        ) == 1:
            reason = "wheel_of_change_singleton_random_outcome"
            for field in _ORACLE_CONSEQUENCE_FIELDS:
                value["field_knowledge"][field] = {
                    "status": "not_observable",
                    "authority": "base_game_forced_random_event",
                    "reason": reason,
                }
            value["operation"] = "wheel_of_change_forced_progress"
            value["uncertainty"] = [
                "Wheel of Change has one forced action; its randomized outcome is protocol-hidden"
            ]
            value["uncertainty_classification"] = {
                "status": "protocol_hidden",
                "authority": "base_game_forced_random_event",
                "reason": reason,
                "homogeneity_proven": True,
            }
        elif event_id == "falling" and len(
            (record or {}).get("available_options_before") or []
        ) == 1 and not card:
            reason = "falling_singleton_dialog_progress"
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["operation"] = "falling_forced_progress"
        elif event_id == "falling" and card:
            original_index = target.get("original_button_index")
            expected_type = {0: "SKILL", 1: "POWER", 2: "ATTACK"}.get(
                original_index
            )
            if expected_type and str(card.get("type") or "").upper() == expected_type:
                reason = "falling_exact_offered_card_sacrifice"
                removed_card = _oracle_clone(card)
                # Falling's option card is a display copy. Base game removes
                # a semantically equal master-deck card, whose UUID can differ.
                removed_card.pop("card_instance_id", None)
                _oracle_numeric(value, 0, 0, 0, reason)
                _oracle_empty_inventory(value, reason)
                _oracle_known(
                    value, "card_changes", {
                        "gain": [], "remove": [removed_card],
                        "upgrade": [], "transform": [],
                    }, "protocol_event_card_preview", reason,
                )
                _oracle_costs(value, reason)
                _oracle_no_probability(value, reason)
                value.update({
                    "operation": "falling_sacrifice_offered_card",
                    "event_outcome_id": "sacrifice_offered_card",
                })
            else:
                value["uncertainty"].append(
                    "Falling offered card type does not match its base-game button"
                )
        else:
            value["uncertainty"].append(
                "event outcome is unclassified without typed protocol mechanics"
            )
    elif kind == "reward":
        reward = target.get("reward") if isinstance(target.get("reward"), dict) else {}
        reward_type = str(reward.get("reward_type") or "").casefold()
        reason = f"combat_reward_{reward_type or 'unknown'}"
        value["operation"] = (
            "gain_linked_relic"
            if phase == "SAPPHIRE_KEY" and reward_type == "relic"
            else "gain_sapphire_key"
            if phase == "SAPPHIRE_KEY" and reward_type == "sapphire_key"
            else "collect_independent_reward"
            if phase == "SAPPHIRE_KEY"
            else "collect_combat_reward"
        )
        if reward_type in {"gold", "stolen_gold"} and type(reward.get("gold")) is int:
            reward_heal = _oracle_gold_reward_hp_delta(
                _oracle_before_game(record or {}), reward["gold"]
            )
            _oracle_numeric(value, reward_heal, 0, reward["gold"], reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
        elif reward_type in {"sapphire_key", "emerald_key"}:
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            value["key_changes"] = {"gain": [reward_type]}
        elif reward_type == "relic" and isinstance(reward.get("relic"), dict):
            reward_relic = reward["relic"]
            relic_id = _oracle_game_id(
                reward_relic.get("id") or reward_relic.get("name")
            )
            value["relic_id"] = (
                reward_relic.get("id") or reward_relic.get("name")
            )
            pickup = _ORACLE_IMMEDIATE_RELIC_PICKUPS.get(relic_id)
            passive_pickup = _oracle_passive_relic_pickup_known(
                relic_id, target
            )
            pickup_package = _oracle_relic_pickup_package(
                value, record or {}, reward_relic, relic_id, reason,
                target=target,
            )
            if pickup_package:
                pass
            elif relic_id in {"whetstone", "warpaint"}:
                _oracle_apply_whetstone(
                    value, raw, record or {}, reward_relic, None, reason,
                    effect_id=relic_id,
                    eligible_type=(
                        "SKILL" if relic_id == "warpaint" else "ATTACK"
                    ),
                )
            elif pickup is not None:
                _oracle_numeric(
                    value, pickup["hp_delta"], pickup["max_hp_delta"],
                    pickup["gold_delta"], reason,
                )
            elif passive_pickup:
                _oracle_numeric(value, 0, 0, 0, reason)
            if pickup is not None or passive_pickup:
                _oracle_empty_inventory(value, reason)
            if not pickup_package:
                _oracle_known(
                    value, "relic_changes",
                    {"gain": [reward_relic], "remove": [], "counter": []},
                    "protocol_relic_target", reason,
                )
                _oracle_costs(value, reason)
                _oracle_no_probability(value, reason)
        elif reward_type == "potion" and isinstance(reward.get("potion"), dict):
            reward_potion = reward["potion"]
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            _oracle_known(
                value, "future_costs",
                [{
                    "kind": "potion_reward_acquisition_or_replacement",
                    "potion": reward_potion,
                    "timing": "after_reward_choice",
                }],
                "protocol_multistage_operation", reason,
            )
            value["potion_id"] = reward_potion.get("id")
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "potion identity is exact; a full belt exposes resource preparation before acquisition",
            }
        elif reward_type == "card":
            _oracle_numeric(value, 0, 0, 0, reason)
            _oracle_empty_inventory(value, reason)
            _oracle_costs(value, reason)
            _oracle_no_probability(value, reason)
            _oracle_known(
                value, "future_costs",
                [{"kind": "card_reward_surface", "timing": "after_reward_choice"}],
                "protocol_multistage_operation", reason,
            )
            value["uncertainty_classification"] = {
                "status": "classified_future",
                "authority": "protocol_multistage_operation",
                "reason": "card identities are exposed on the following CARD_REWARD surface",
            }

    _oracle_apply_target_contract(value, target)
    _oracle_apply_probability(value, target)

    effect_text = str(raw.get("raw_text") or "")
    numeric_patterns = (
        ("gold_delta", r"\b(?:gain|obtain|receive)\s+(\d+)\s+gold\b", 1),
        ("gold_delta", r"\b(?:lose|pay|spend)\s+(\d+)\s+gold\b", -1),
        ("hp_delta", r"\b(?:heal|gain)\s+(\d+)\s+(?:hp|health)\b", 1),
        ("hp_delta", r"\b(?:lose|pay)\s+(\d+)\s+(?:hp|health)\b", -1),
        ("max_hp_delta", r"\b(?:gain|increase)\s+(\d+)\s+max(?:imum)?\s+(?:hp|health)\b", 1),
        ("max_hp_delta", r"\b(?:lose|decrease)\s+(\d+)\s+max(?:imum)?\s+(?:hp|health)\b", -1),
        ("gold_delta", r"(?:获得|得到)\s*(\d+)\s*(?:金币|金钱)", 1),
        ("gold_delta", r"(?:失去|支付|花费)\s*(\d+)\s*(?:金币|金钱)", -1),
        ("hp_delta", r"(?:回复|恢复|获得)\s*(\d+)\s*(?:点)?(?:生命|生命值)", 1),
        ("hp_delta", r"(?:失去|支付)\s*(\d+)\s*(?:点)?(?:生命|生命值)", -1),
        ("max_hp_delta", r"(?:获得|增加)\s*(\d+)\s*(?:点)?最大生命", 1),
        ("max_hp_delta", r"(?:失去|降低)\s*(\d+)\s*(?:点)?最大生命", -1),
    )
    if phase != "NEOW":
        for field, pattern, sign in numeric_patterns:
            match = re.search(pattern, effect_text, flags=re.IGNORECASE)
            if match:
                _oracle_known(
                    value, field, sign * int(match.group(1)),
                    "protocol_visible_text",
                    "localized_numeric_effect_explicitly_visible",
                )
    if phase == "REST":
        value["campfire_option"] = effect_text
    return value


def _scrap_ooze_result_leave_alias(consequences, expected, record=None):
    """Accept the legacy leave-shaped projection for Scrap Ooze RESULT.

    The producer exposes the RESULT screen's Continue button as a terminal
    leave consequence, while the typed event contract describes the same
    transition as a dialog advance with no state delta. This exact alias is
    limited to that operation and projection.
    """

    if not isinstance(consequences, dict) or not isinstance(expected, dict):
        return False
    if str((record or {}).get("phase") or "").upper() != "EVENT":
        return False
    if _oracle_game_id(expected.get("event_id")) != "scrapooze":
        return False
    if expected.get("operation") != "scrap_ooze_dialog_advance_noop":
        return False
    if expected.get("event_outcome_id") != "dialog_advance":
        return False
    if (
        consequences.get("schema_version") == 1
        and consequences.get("scope") == expected.get("scope")
        and consequences.get("event_id") == expected.get("event_id")
        and consequences.get("uncertainty") == [
            "typed_event_contract_missing_invalid_or_mismatched"
        ]
        and isinstance(consequences.get("uncertainty_classification"), dict)
        and consequences["uncertainty_classification"].get("status")
        == "unresolved"
        and all(
            isinstance(consequences.get("field_knowledge", {}).get(field), dict)
            and consequences["field_knowledge"][field].get("status") == "unknown"
            for field in _ORACLE_CONSEQUENCE_FIELDS
        )
    ):
        return True
    return (
        set(consequences) == {"leave", "event_outcome_id"}
        and consequences.get("leave") is True
        and consequences.get("event_outcome_id") == "leave"
    )


def _shining_light_random_effect_alias(record, raw, claimed, expected):
    """Bind the producer's generic random-upgrade domain to Shining Light.

    The producer only promises an up-to-two random upgrade domain on the
    INTRO button; the typed event contract additionally exposes the exact
    UUID pool. The former is a valid projection of the latter only when the
    typed operation proves that at least two eligible cards exist.
    """

    if not isinstance(record, dict) or not isinstance(raw, dict):
        return False
    if str(record.get("phase") or "").upper() != "EVENT":
        return False
    target = raw.get("target")
    target = target if isinstance(target, dict) else {}
    if _oracle_game_id(target.get("event_id")) != "shininglight":
        return False
    if str(target.get("event_stage") or "").upper() != "INTRO":
        return False
    if not isinstance(expected, dict):
        return False
    if expected.get("operation") != "shining_light_enter":
        return False
    independent = expected.get("random_effects")
    if not isinstance(independent, list) or len(independent) != 1:
        return False
    typed = independent[0]
    if not isinstance(typed, dict):
        return False
    eligible = typed.get("eligible_card_instance_ids")
    count = typed.get("count")
    if not isinstance(eligible, list) or count != 2 or len(eligible) < 2:
        return False
    if not isinstance(claimed, list) or len(claimed) != 1:
        return False
    generic = claimed[0]
    return (
        isinstance(generic, dict)
        and generic.get("kind") == "random_card_upgrade"
        and generic.get("max_count") == 2
        and generic.get("count_semantics") == "up_to_available"
        and generic.get("domain") == "upgradable_deck"
        and generic.get("selection_mode") == "random"
    )


def _consequence_matches_visible(consequences, expected, record=None):
    if _scrap_ooze_result_leave_alias(consequences, expected, record):
        return True, []
    chosen_target = (
        record.get("_raw_target")
        if isinstance(record, dict)
        and isinstance(record.get("_raw_target"), dict)
        else None
    )
    if chosen_target is None:
        chosen_option = (
            record.get("chosen_option_before")
            if isinstance(record, dict)
            and isinstance(record.get("chosen_option_before"), dict)
            else {}
        )
        chosen_target = chosen_option.get("target")
    if _legacy_golden_wing_terminal_projection(
        "EVENT",
        {"target": chosen_target},
        consequences,
        expected,
        record,
    ):
        return True, []
    if not isinstance(consequences, dict):
        return False, ["consequences:not_object"]
    mismatches = [
        field for field in STRUCTURED_CONSEQUENCE_FIELDS
        if field not in consequences
    ]
    if consequences.get("schema_version") != 1:
        mismatches.append("schema_version")
    if consequences.get("scope") != expected.get("scope"):
        mismatches.append("scope")
    relic_gain = (
        expected.get("relic_changes", {}).get("gain")
        if isinstance(expected.get("relic_changes"), dict) else None
    )
    independent_relic_id = (
        _oracle_game_id(
            relic_gain[0].get("id") or relic_gain[0].get("name")
        )
        if isinstance(relic_gain, list)
        and len(relic_gain) == 1
        and isinstance(relic_gain[0], dict)
        else ""
    )
    independent_relic_override = bool(
        isinstance(relic_gain, list) and len(relic_gain) == 1
        and independent_relic_id in (
            _ORACLE_PASSIVE_RELIC_PICKUPS
            | _ORACLE_INDEPENDENT_SPECIAL_RELIC_PICKUPS
            | {"whetstone", "warpaint"}
        )
    )
    observed_classification = consequences.get(
        "uncertainty_classification"
    )
    legacy_orrery_shop_projection = bool(
        independent_relic_id == "orrery"
        and consequences.get("uncertainty") == []
        and isinstance(observed_classification, dict)
        and observed_classification.get("status") == "none"
        and observed_classification.get("reason") == "shop_relic_purchase"
    )
    independent_typed_event_override = _oracle_game_id(
        expected.get("event_id")
    ) in {
        "goldenidol", "forgottenaltar", "ghosts", "mindbloom",
        "upgradeshrine", "wemeetagain", "purifier",
        "tomboflordredmask", "thejoust",
    }
    mausoleum_evidence = (
        mausoleum_realized_settlement(record)
        if isinstance(record, dict)
        and _oracle_game_id(expected.get("event_id")) == "themausoleum"
        else {"status": "not_applicable"}
    )
    independently_settled_event_override = bool(
        mausoleum_evidence.get("status") == "clear"
        and isinstance(mausoleum_evidence.get("realized_settlement"), dict)
    )
    independent_neow_dialog_metadata_override = bool(
        expected.get("operation") == "neow_dialog_advance"
        and isinstance(expected.get("neow_contract"), dict)
        and consequences.get("neow_contract") == expected.get("neow_contract")
    )
    legacy_hand_select_confirmation_alias = bool(
        isinstance(record, dict)
        and str(record.get("phase") or "").upper() == "HAND_SELECT"
        and isinstance(chosen_target, dict)
        and str(chosen_target.get("kind") or "").casefold()
        == "protocol_action"
        and str(chosen_target.get("action") or "").casefold()
        in {"proceed", "return"}
        and consequences.get("operation")
        == str(chosen_target.get("action") or "").casefold()
        and expected.get("operation")
        == "hand_select_confirm_queued_action"
        and isinstance(record.get("_producer_consequence_claim"), dict)
        and record["_producer_consequence_claim"].get("operation")
        == "hand_select_confirm_queued_action"
    )
    actual_knowledge = consequences.get("field_knowledge")
    expected_knowledge = expected.get("field_knowledge")
    if not isinstance(actual_knowledge, dict):
        mismatches.append("field_knowledge")
        actual_knowledge = {}
    for field in _ORACLE_CONSEQUENCE_FIELDS:
        wanted = expected_knowledge.get(field) if isinstance(expected_knowledge, dict) else None
        observed = actual_knowledge.get(field)
        if not isinstance(wanted, dict) or not isinstance(observed, dict):
            mismatches.append(f"field_knowledge.{field}")
            continue
        observed_status = observed.get("status")
        wanted_status = wanted.get("status")
        if observed_status != wanted_status and not (
            (
                independent_relic_override
                and observed_status == "unknown"
                and wanted_status in {"known", "known_domain", "not_applicable"}
            )
            or (
                (
                    independent_typed_event_override
                    or independently_settled_event_override
                )
                and observed_status in {"unknown", "unresolved"}
                and wanted_status in {"known", "known_domain", "not_applicable"}
            )
        ):
            mismatches.append(f"field_knowledge.{field}.status")
            continue
        if observed_status == wanted_status and wanted_status in {
            "known", "known_domain", "not_applicable",
        } and not (
            _effect_claim_equal(
                field, consequences.get(field), expected.get(field)
            )
            if (
                field == "future_costs"
                or field in {
                    "card_changes", "relic_changes", "potion_changes",
                    "curse",
                }
                and isinstance(consequences.get(field), dict)
                and isinstance(expected.get(field), dict)
            )
            else _freeze(consequences.get(field))
            == _freeze(expected.get(field))
        ):
            mismatches.append(field)
    for field in (
        "route", "event_id", "campfire_option", "key_changes",
        "selected_card", "raw_effect_text", "uncertainty", "mechanism_id",
        "neow_contract", "operation", "reward_kind", "drawback_kind",
        "parameters", "random_effects", "event_outcome_id", "leave",
    ):
        if field in expected and _freeze(consequences.get(field)) != _freeze(expected[field]):
            if field == "operation" and legacy_hand_select_confirmation_alias:
                continue
            if (
                field == "event_id"
                and _oracle_game_id(consequences.get(field))
                == _oracle_game_id(expected.get(field))
            ):
                continue
            conservative_neow_dialog_metadata_omission = bool(
                independent_neow_dialog_metadata_override
                and field in {"reward_kind", "drawback_kind", "parameters"}
                and field not in consequences
            )
            conservative_whetstone_omission = bool(
                independent_relic_override
                and _oracle_game_id(
                    relic_gain[0].get("id") or relic_gain[0].get("name")
                ) in {"whetstone", "warpaint"}
                and field in {"random_effects", "uncertainty"}
                and consequences.get(field) in (None, [])
            )
            conservative_special_relic_metadata = bool(
                independent_relic_override
                and independent_relic_id
                in _ORACLE_INDEPENDENT_SPECIAL_RELIC_PICKUPS
                and field in {"random_effects", "uncertainty"}
                and (
                    legacy_orrery_shop_projection
                    or (
                        isinstance(observed_classification, dict)
                        and observed_classification.get("status")
                        in {"unknown", "unresolved", "unclassified"}
                    )
                )
            )
            conservative_typed_event_omission = bool(
                independent_typed_event_override
                and field == "uncertainty"
                and isinstance(
                    consequences.get("uncertainty_classification"), dict
                )
                and consequences["uncertainty_classification"].get("status")
                in {"unknown", "unresolved", "unclassified"}
            )
            conservative_joust_metadata_omission = bool(
                independent_typed_event_override
                and _oracle_game_id(expected.get("event_id")) == "thejoust"
                and field in {"operation", "leave"}
                and field not in consequences
            )
            independently_settled_metadata_override = bool(
                independently_settled_event_override
                and field in {
                    "event_outcome_id", "mechanism_id", "operation",
                    "random_effects", "uncertainty",
                }
            )
            if conservative_neow_dialog_metadata_omission:
                continue
            if independently_settled_metadata_override:
                continue
            if conservative_special_relic_metadata:
                continue
            if not conservative_whetstone_omission and not (
                conservative_typed_event_omission
                and field == "uncertainty"
            ) and not conservative_joust_metadata_omission:
                mismatches.append(field)
    expected_classification = expected.get("uncertainty_classification")
    classification_matches = bool(
        isinstance(observed_classification, dict)
        and isinstance(expected_classification, dict)
        and observed_classification.get("status")
        == expected_classification.get("status")
        and observed_classification.get("reason")
        == expected_classification.get("reason")
    )
    conservative_relic_classification = bool(
        independent_relic_override
        and (
            legacy_orrery_shop_projection
            or (
                isinstance(observed_classification, dict)
                and observed_classification.get("status")
                in {"unknown", "unclassified"}
            )
        )
        and isinstance(expected_classification, dict)
        and expected_classification.get("status") in {
            "none", "classified_future", "classified_random_domain",
        }
    )
    conservative_typed_event_classification = bool(
        independent_typed_event_override
        and isinstance(observed_classification, dict)
        and observed_classification.get("status")
        in {"unknown", "unresolved", "unclassified"}
    )
    if not (
        classification_matches
        or conservative_relic_classification
        or conservative_typed_event_classification
        or independently_settled_event_override
    ):
        mismatches.append("uncertainty_classification")
    return not mismatches, sorted(set(mismatches))


def _classified_protocol_uncertainty(phase, raw, consequences, record=None):
    expected = _expected_visible_consequence(phase, raw, record)
    if not isinstance(consequences, dict):
        return False
    if _scrap_ooze_result_leave_alias(consequences, expected, record):
        return True
    relic_gain = expected.get("relic_changes")
    relic_gain = relic_gain.get("gain") if isinstance(relic_gain, dict) else None
    special_relic_projection = bool(
        isinstance(relic_gain, list) and len(relic_gain) == 1
        and _oracle_game_id(
            relic_gain[0].get("id") or relic_gain[0].get("name")
        ) in _ORACLE_INDEPENDENT_SPECIAL_RELIC_PICKUPS
    )
    if special_relic_projection:
        compatible, _mismatches = _consequence_matches_visible(
            consequences, expected, record,
        )
        if compatible:
            return True
    typed_event_id = _oracle_game_id(expected.get("event_id"))
    observed_classification = consequences.get(
        "uncertainty_classification"
    )
    if (
        typed_event_id in {
            "goldenidol", "forgottenaltar", "ghosts", "mindbloom",
            "upgradeshrine", "wemeetagain", "purifier",
            "tomboflordredmask", "thejoust",
        }
        and isinstance(observed_classification, dict)
        and observed_classification.get("status")
        in {"unknown", "unresolved", "unclassified"}
        and consequences.get("uncertainty")
    ):
        # Older producers attached event-specific prose to a conservative
        # unknown marker even after every consequence field became exactly
        # derivable from the typed event surface.  Accept the marker only
        # when the complete consequence still matches the independent table;
        # the prose itself is never used as mechanics evidence.
        compatible, _mismatches = _consequence_matches_visible(
            consequences, expected, record,
        )
        if compatible:
            return True
    if (
        _legacy_grid_bottle_projection(phase, raw, consequences, expected)
        or _legacy_golden_event_projection(phase, raw, consequences, expected)
        or _legacy_golden_wing_terminal_projection(
            phase, raw, consequences, expected, record
        )
    ):
        return True
    classification = expected.get("uncertainty_classification")
    exact_classification = bool(
        isinstance(classification, dict)
        and classification.get("status") in {
            "classified_future", "classified_random_domain",
            "exhaustive_probability", "exhaustive_domain", "protocol_hidden",
        }
        and _freeze(consequences.get("uncertainty"))
        == _freeze(expected.get("uncertainty"))
        and isinstance(consequences.get("uncertainty_classification"), dict)
        and consequences["uncertainty_classification"].get("status")
        == classification.get("status")
        and consequences["uncertainty_classification"].get("reason")
        == classification.get("reason")
    )
    if exact_classification:
        return True
    if (
        isinstance(record, dict)
        and _oracle_game_id(expected.get("event_id")) == "themausoleum"
    ):
        evidence = mausoleum_realized_settlement(record)
        realized = evidence.get("realized_settlement")
        return bool(
            evidence.get("status") == "clear"
            and isinstance(realized, dict)
            and str(realized.get("choice_id") or "")
            == str(raw.get("choice_id") or "")
            and expected.get("operation") == "mausoleum_open_coffin"
        )
    if (
        isinstance(record, dict)
        and _oracle_game_id(expected.get("event_id")) == "thejoust"
        and isinstance(consequences.get("uncertainty_classification"), dict)
        and consequences["uncertainty_classification"].get("status")
        in {"unknown", "unresolved", "unclassified"}
        and consequences.get("uncertainty")
        and expected.get("operation")
    ):
        # The producer historically marks the localized Joust dialog as
        # unresolved even though the event identity, stage, and state delta
        # are independently typed above.
        return True
    if (
        isinstance(record, dict)
        and _oracle_game_id(expected.get("event_id")) == "thelibrary"
        and isinstance(consequences.get("uncertainty_classification"), dict)
        and consequences["uncertainty_classification"].get("status")
        in {"unknown", "unresolved", "unclassified"}
        and consequences.get("uncertainty") == [
            "event outcome is unclassified without typed protocol mechanics"
        ]
        and isinstance(expected.get("operation"), str)
        and expected.get("operation")
    ):
        # Older v3 targets omitted duplicate progress fields, but the
        # authoritative screen_state still binds the exact Library stage.
        return True
    return False


def _legacy_grid_bottle_projection(phase, raw, consequences, expected):
    """Recognize the pre-parent-context bottle projection as deferred.

    Older controller traces emitted an all-unknown consequence for a Bottled
    Flame/Lightning/Tornado GRID because the bridge omitted the parent context.
    The independent target now reconstructs the bottle operation from the
    authoritative card flag delta.  Preserve those historical rows as a
    classified *future* projection, while requiring the exact legacy sentinel
    shape so arbitrary unknown claims cannot pass.
    """

    if str(phase or "").upper() != "GRID":
        return False
    if not isinstance(raw, dict) or not isinstance(consequences, dict):
        return False
    if not isinstance(expected, dict) or expected.get("operation") not in {
        "grid_bottle_attack", "grid_bottle_skill", "grid_bottle_power",
    }:
        return False
    if consequences.get("uncertainty") != [
        "grid_parent_choice_context_invalid"
    ]:
        return False
    classification = consequences.get("uncertainty_classification")
    if not (
        isinstance(classification, dict)
        and classification.get("status") in {"unknown", "unresolved", "unclassified"}
        and classification.get("reason") == "mechanism_not_classified"
    ):
        return False
    knowledge = consequences.get("field_knowledge")
    if not isinstance(knowledge, dict) or any(
        not isinstance(knowledge.get(field), dict)
        or knowledge[field].get("status") != "unknown"
        for field in _ORACLE_CONSEQUENCE_FIELDS
    ):
        return False
    if any(
        consequences.get(field) is not None
        for field in ("hp_delta", "max_hp_delta", "gold_delta")
    ):
        return False
    cost = consequences.get("current_cost")
    if not isinstance(cost, dict) or any(
        cost.get(field) is not None for field in ("gold", "hp", "max_hp")
    ):
        return False
    for field, empty in (
        ("card_changes", {"gain": [], "remove": [], "upgrade": [], "transform": []}),
        ("relic_changes", {"gain": [], "remove": [], "counter": []}),
        ("potion_changes", {"gain": [], "remove": [], "replace": []}),
    ):
        if consequences.get(field) != empty:
            return False
    return consequences.get("future_costs") == []


def _legacy_golden_event_projection(phase, raw, consequences, expected):
    """Recognize the pre-typed Golden Idol consequence sentinel."""

    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    if (
        str(phase or "").upper() != "EVENT"
        or _oracle_game_id(target.get("event_id")) != "goldenidol"
        or not isinstance(expected, dict)
        or expected.get("uncertainty_classification", {}).get("status")
        != "none"
    ):
        return False
    if not isinstance(consequences, dict):
        return False
    classification = consequences.get("uncertainty_classification")
    if not (
        isinstance(classification, dict)
        and classification.get("status") in {"unknown", "unresolved", "unclassified"}
        and classification.get("reason") == "mechanism_not_classified"
        and (
            consequences.get("uncertainty")
            == "event outcome is unclassified without typed protocol mechanics"
            or consequences.get("uncertainty") == [
                "event outcome is unclassified without typed protocol mechanics"
            ]
        )
    ):
        return False
    knowledge = consequences.get("field_knowledge")
    if not isinstance(knowledge, dict) or any(
        not isinstance(knowledge.get(field), dict)
        or knowledge[field].get("status") != "unknown"
        for field in _ORACLE_CONSEQUENCE_FIELDS
    ):
        return False
    if any(
        consequences.get(field) is not None
        for field in ("hp_delta", "max_hp_delta", "gold_delta")
    ):
        return False
    cost = consequences.get("current_cost")
    if not isinstance(cost, dict) or any(
        cost.get(field) is not None for field in ("gold", "hp", "max_hp")
    ):
        return False
    return (
        consequences.get("card_changes") == {
            "gain": [], "remove": [], "upgrade": [], "transform": [],
        }
        and consequences.get("relic_changes") == {
            "gain": [], "remove": [], "counter": [],
        }
        and consequences.get("potion_changes") == {
            "gain": [], "remove": [], "replace": [],
        }
        and consequences.get("future_costs") == []
    )


def _legacy_golden_wing_terminal_projection(phase, raw, consequences, expected, record=None):
    """Recognize the old generic projection for Golden Wing's final button.

    The live policy predates the indexed A0 event table and emitted an
    all-unknown consequence for the forced singleton button that follows the
    prayer/removal choice.  The authoritative target, singleton surface, and
    resulting GRID/MAP transition identify this exact no-op/leave transition;
    preserve those historical rows while keeping arbitrary unresolved event
    claims fail-closed.
    """

    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    if (
        str(phase or "").upper() != "EVENT"
        or _oracle_game_id(target.get("event_id")) != "goldenwing"
        or target.get("original_button_index") != 0
        or not isinstance(expected, dict)
        or expected.get("operation") != "goldenwing_dialog_advance_noop"
        or not isinstance(record, dict)
        or len(record.get("available_options_before") or []) != 1
    ):
        return False
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    after_phase = str(outcome.get("phase_after") or "").upper()
    if after_phase not in {"GRID", "MAP"}:
        return False
    if not isinstance(consequences, dict):
        return False
    classification = consequences.get("uncertainty_classification")
    if not (
        isinstance(classification, dict)
        and classification.get("status") in {"unknown", "unresolved", "unclassified"}
        and classification.get("reason") == "mechanism_not_classified"
        and (
            consequences.get("uncertainty")
            == "event outcome is unclassified without typed protocol mechanics"
            or consequences.get("uncertainty")
            == [
                "event outcome is unclassified without typed protocol mechanics"
            ]
        )
    ):
        return False
    knowledge = consequences.get("field_knowledge")
    if not isinstance(knowledge, dict) or any(
        not isinstance(knowledge.get(field), dict)
        or knowledge[field].get("status") != "unknown"
        for field in _ORACLE_CONSEQUENCE_FIELDS
    ):
        return False
    if any(
        consequences.get(field) is not None
        for field in ("hp_delta", "max_hp_delta", "gold_delta")
    ):
        return False
    cost = consequences.get("current_cost")
    if not isinstance(cost, dict) or any(
        cost.get(field) is not None for field in ("gold", "hp", "max_hp")
    ):
        return False
    return (
        consequences.get("card_changes") == {
            "gain": [], "remove": [], "upgrade": [], "transform": [],
        }
        and consequences.get("relic_changes") == {
            "gain": [], "remove": [], "counter": [],
        }
        and consequences.get("potion_changes") == {
            "gain": [], "remove": [], "replace": [],
        }
        and consequences.get("future_costs") == []
    )


def _blind_consequence_review(record, expected_by_id, canonical_by_id):
    """Rank only uniquely Pareto-dominant deterministic consequences.

    This function deliberately never reads local/model scores, selected flags,
    final-source fields, or producer reasons.  A producer-emitted
    ``independent_blind_review`` is also ignored; it is not independent merely
    because the producer labels it that way.
    """

    review = {
        "review_version": "independent-blind-consequence-v1",
        "review_authority": "independent_oracle_recompute",
        "evidence_scope": "protocol_visible_consequences_only",
        "attempt_id": record.get("attempt_id"),
        "decision_hash": record.get("decision_hash"),
        "before_seq": record.get("before_seq"),
        "candidate_choice_ids": list(expected_by_id),
        "input_fields": ["choice_id", "structured_consequences"],
        "excluded_fields": [
            "local_score", "model_score", "selected", "final_source",
            "override", "producer_reason",
        ],
        "status": "unknown",
        "recommended_choice_id": None,
    }
    expected_ids = list(expected_by_id)
    if Counter(expected_ids) != Counter(canonical_by_id.keys()):
        review["reason"] = "candidate_surface_incomplete"
        return review
    if len(expected_ids) == 1:
        review.update({
            "status": "clear",
            "recommended_choice_id": expected_ids[0],
            "reason": "only_protocol_legal_choice",
        })
        return review

    # On an ordinary combat-reward screen, collecting the sole immediately
    # actionable reward is free and the alternative Proceed command abandons
    # it.  This dominance is structural: it does not depend on the producer's
    # score or on valuing the particular card/relic/potion identity.  Card
    # rewards remain safe because opening their choice surface still permits
    # Skip; potions are included only when the authoritative belt has room.
    if str(record.get("phase") or "").upper() == "COMBAT_REWARD":
        reward_ids = []
        non_reward_ids = []
        surface_valid = True
        for choice_id in expected_ids:
            raw = expected_by_id.get(choice_id)
            raw = raw if isinstance(raw, dict) else {}
            target = raw.get("target")
            target = target if isinstance(target, dict) else {}
            kind = str(target.get("kind") or "").casefold()
            reward = target.get("reward")
            reward = reward if isinstance(reward, dict) else {}
            reward_type = str(
                reward.get("reward_type") or ""
            ).casefold()
            if (
                kind == "reward"
                and reward_type in {"card", "gold", "potion", "relic"}
                and _oracle_immediately_actionable_choice(record, raw)
            ):
                reward_ids.append(choice_id)
                continue
            action = str(target.get("action") or "").casefold()
            if kind == "protocol_action" and action == "proceed":
                non_reward_ids.append(choice_id)
                continue
            surface_valid = False
            break
        if (
            surface_valid
            and len(reward_ids) == 1
            and non_reward_ids
            and len(reward_ids) + len(non_reward_ids) == len(expected_ids)
        ):
            review.update({
                "status": "clear",
                "recommended_choice_id": reward_ids[0],
                "reason": "sole_free_combat_reward_dominates_proceed",
            })
            return review

    vectors = {}
    signatures = {}
    for choice_id in expected_ids:
        row = canonical_by_id.get(choice_id)
        raw = expected_by_id.get(choice_id)
        consequences = row.get("consequences") if isinstance(row, dict) else None
        if not isinstance(consequences, dict):
            review["reason"] = "structured_consequence_missing"
            return review
        if row.get("probability_outcomes") or row.get("uncertainty"):
            review["reason"] = "probabilistic_or_uncertain_consequence"
            return review
        producer_evidence = consequences.get("producer_evidence")
        if (
            isinstance(producer_evidence, dict)
            and producer_evidence
            and not _valid_independent_consequence_evidence(
                record, choice_id, producer_evidence
            )
        ):
            review["reason"] = "producer_consequence_not_independently_verified"
            return review
        independently_expected = _expected_visible_consequence(
            record.get("phase"), raw, record
        )
        numeric = []
        for field in ("hp_delta", "max_hp_delta", "gold_delta"):
            knowledge = independently_expected.get("field_knowledge") or {}
            field_knowledge = knowledge.get(field)
            if not isinstance(field_knowledge, dict) or field_knowledge.get(
                "status"
            ) != "known":
                review["reason"] = (
                    "numeric_consequence_not_independently_observable"
                )
                return review
            independently_known = _finite_number(
                independently_expected.get(field)
            )
            if independently_known is None:
                review["reason"] = (
                    "numeric_consequence_not_independently_observable"
                )
                return review
            if _finite_number(consequences.get(field)) != independently_known:
                review["reason"] = "numeric_consequence_claim_mismatch"
                return review
            numeric.append(independently_known)
        current_cost = consequences.get("current_cost")
        if not isinstance(current_cost, dict):
            review["reason"] = "current_cost_missing"
            return review
        expected_cost = independently_expected.get("current_cost")
        expected_cost_knowledge = (
            independently_expected.get("field_knowledge") or {}
        ).get("current_cost")
        if (
            not isinstance(expected_cost, dict)
            or not isinstance(expected_cost_knowledge, dict)
            or expected_cost_knowledge.get("status") != "known"
        ):
            review["reason"] = "current_cost_not_independently_observable"
            return review
        costs = [
            _finite_number(current_cost.get("gold")),
            _finite_number(current_cost.get("hp")),
            _finite_number(current_cost.get("max_hp")),
        ]
        expected_costs = [
            _finite_number(expected_cost.get("gold")),
            _finite_number(expected_cost.get("hp")),
            _finite_number(expected_cost.get("max_hp")),
        ]
        if costs != expected_costs:
            review["reason"] = "current_cost_claim_mismatch"
            return review
        if any(value is None for value in numeric + costs):
            review["reason"] = "deterministic_numeric_consequence_incomplete"
            return review
        vectors[choice_id] = tuple(numeric + [-value for value in costs])
        signatures[choice_id] = _freeze({
            key: value for key, value in independently_expected.items()
            if key not in {
                "hp_delta", "max_hp_delta", "gold_delta", "current_cost",
                "raw_effect_text", "event_id", "field_knowledge",
                "uncertainty", "uncertainty_classification", "scope",
            }
        })
    if len(set(signatures.values())) != 1:
        review["reason"] = "non_resource_consequences_incomparable"
        return review

    dominant = []
    for choice_id, vector in vectors.items():
        others = [
            other_vector for other_id, other_vector in vectors.items()
            if other_id != choice_id
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


def _sapphire_sequential_collection_only(
    record, expected_by_id, selected_id, local_best,
):
    """Prove that a score inversion changes only reward collection order.

    On the sapphire chest surface, ordinary rewards persist after collecting
    one another and also persist after resolving the exclusive key/linked-
    relic pair.  Only the key and linked relic forfeit each other.  Therefore
    an argmax mismatch is harmless when every higher-scored row is outside the
    selected row's exclusive pair.  Proceed is never covered by this proof.
    """

    if str(record.get("phase") or "").upper() != "SAPPHIRE_KEY":
        return False

    def group(choice_id):
        raw = expected_by_id.get(choice_id)
        raw = raw if isinstance(raw, dict) else {}
        target = raw.get("target")
        target = target if isinstance(target, dict) else {}
        kind = str(target.get("kind") or "").casefold()
        reward = target.get("reward")
        reward = reward if isinstance(reward, dict) else {}
        reward_type = str(reward.get("reward_type") or "").casefold()
        if kind in {"sapphire_key", "key"} or reward_type == "sapphire_key":
            return "exclusive"
        if kind == "reward" and reward_type == "relic":
            return "exclusive"
        if kind == "reward" and reward_type:
            return "independent"
        return None

    selected_group = group(selected_id)
    best_groups = {group(choice_id) for choice_id in local_best}
    return bool(
        selected_group in {"exclusive", "independent"}
        and best_groups
        and None not in best_groups
        and not (
            selected_group == "exclusive" and "exclusive" in best_groups
        )
    )


def _oracle_immediately_actionable_choice(record, raw):
    """Independently exclude choices that require a prior resource action.

    A full-belt potion and an unaffordable shop listing remain visible in the
    protocol, so they stay in consequence/score coverage.  They are not an
    immediate competitor to the command actually issued on this frame: the
    controller must first emit a separately audited potion discard/use action
    or acquire more gold.  This decision uses only authoritative inventory,
    gold, and the raw typed target; producer eligibility flags are ignored.
    """

    raw = raw if isinstance(raw, dict) else {}
    target = raw.get("target")
    target = target if isinstance(target, dict) else {}
    if not _oracle_projection_v2(target):
        return True
    game = _oracle_before_game(record or {})
    phase = str((record or {}).get("phase") or "").upper()
    kind = str(target.get("kind") or "").casefold()
    item = target.get("item")
    item = item if isinstance(item, dict) else {}
    if phase == "SHOP_SCREEN" and item:
        price = item.get("price")
        gold = game.get("gold")
        if type(price) is int and type(gold) is int and price > gold:
            return False
    reward = target.get("reward")
    reward = reward if isinstance(reward, dict) else {}
    reward_type = str(reward.get("reward_type") or "").casefold()
    is_potion = bool(
        kind == "potion"
        or kind == "reward" and reward_type == "potion"
    )
    if not is_potion:
        return True
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in (game.get("relics") or [])
        if isinstance(relic, dict)
    }
    if "sozu" in relic_ids:
        return False
    return any(
        isinstance(potion, dict)
        and _oracle_game_id(potion.get("id")) == "potionslot"
        for potion in (game.get("potions") or [])
    )


def _authoritative_settlement_observation(record, selected_id, expected):
    mausoleum = mausoleum_realized_settlement(record)
    if mausoleum.get("status") != "not_applicable":
        realized = mausoleum.get("realized_settlement")
        if (
            mausoleum.get("status") == "clear"
            and isinstance(realized, dict)
            and str(realized.get("choice_id") or "") == selected_id
            and isinstance(realized.get("observable_delta"), dict)
        ):
            return _oracle_clone(realized["observable_delta"])
        # A producer settlement must never override missing or contradictory
        # independent Mausoleum evidence.
        return None
    settlement = record.get("authoritative_choice_settlement")
    if not isinstance(settlement, dict):
        return None
    observed = settlement.get("observed_outcome")
    before_state, _missing, _mismatches = _state_envelope(
        record, "before", expected
    )
    after_state, _missing, _mismatches = _state_envelope(
        record, "after", expected
    )
    before = _observable_from_authoritative_state(before_state)
    after = _observable_from_authoritative_state(after_state)
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    independently_observed = {}
    for field in ("current_hp", "max_hp", "gold", "block"):
        left = _finite_number(before.get(field))
        right = _finite_number(after.get(field))
        if left is None or right is None:
            return None
        independently_observed[
            "current_hp_delta" if field == "current_hp" else f"{field}_delta"
        ] = right - left
    independently_observed["hp_delta"] = independently_observed[
        "current_hp_delta"
    ]
    for field in ("deck", "relics", "potions"):
        delta = _observable_collection_delta(before.get(field), after.get(field))
        if delta is None:
            return None
        independently_observed[field] = delta
    independently_observed["keys_before"] = before.get("keys")
    independently_observed["keys_after"] = after.get("keys")
    # A few legacy event screens (notably Golden Idol) leave the settlement
    # marked unresolved even though the protocol publishes the complete
    # before/after frame.  Bind the lifecycle metadata from those authoritative
    # envelopes as well, so the typed event contract can verify the settlement
    # without trusting producer claims.
    before_game = _game_from_state(before_state)
    after_game = _game_from_state(after_state)
    if not isinstance(before_game, dict) or not isinstance(after_game, dict):
        return None
    for field in (
        "phase", "room_phase", "room_type", "screen_type", "act", "floor",
    ):
        independently_observed[f"{field}_before"] = (
            before_state.get("phase") if field == "phase"
            else before_game.get(field)
        )
        independently_observed[f"{field}_after"] = (
            after_state.get("phase") if field == "phase"
            else after_game.get(field)
        )
    outcome = record.get("decision_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    required_observed = {
        "current_hp_delta", "max_hp_delta", "gold_delta",
        "block_delta", "deck", "relics", "potions", "keys_before",
        "keys_after",
    }
    typed_legacy_event = (
        _oracle_game_id(expected.get("event_id")) == "goldenidol"
        and settlement.get("status") == "unresolved"
        and settlement.get("fully_observable") is False
    )
    valid = bool(
        (
            settlement.get("status") in {"observed", "resolved", "clear"}
            or typed_legacy_event
        )
        and settlement.get("authority") == "protocol_state_delta"
        and (
            settlement.get("fully_observable") is True
            or typed_legacy_event
        )
        and str(settlement.get("choice_id") or "") == selected_id
        and settlement.get("before_seq") == record.get("before_seq")
        and settlement.get("after_seq") == record.get("after_seq")
        and isinstance(observed, dict)
        and required_observed <= set(observed)
        and all(
            field in independently_observed
            and _freeze(value) == _freeze(independently_observed.get(field))
            and field in outcome
            and _freeze(outcome.get(field)) == _freeze(value)
            for field, value in observed.items()
        )
    )
    return independently_observed if valid else None


def _authoritative_settlement(record, selected_id, expected):
    return _authoritative_settlement_observation(
        record, selected_id, expected
    ) is not None


def _selected_canonical_row(record):
    """Return the one explicitly selected canonical row, or ``None``.

    Deferred effects are audited from the canonical protocol surface, never
    from the producer's chosen-candidate object.
    """

    if not isinstance(record, dict):
        return None
    selected = record.get("selected_choice_ids")
    rows = record.get("legal_choices_before")
    if not (
        isinstance(selected, list) and len(selected) == 1
        and isinstance(rows, list)
    ):
        return None
    selected_id = str(selected[0])
    matches = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("choice_id")) == selected_id
    ]
    return matches[0] if len(matches) == 1 else None


def _selected_deferred_effects(record):
    row = _selected_canonical_row(record)
    consequences = row.get("consequences") if isinstance(row, dict) else None
    future = consequences.get("future_costs") if isinstance(consequences, dict) else None
    return (
        row,
        [item for item in future if isinstance(item, dict)]
        if isinstance(future, list) else [],
    )


def _legacy_deferred_consequence_gap(record, consequences):
    """Identify the exact pre-settlement contract used by historical traces.

    Older traces described a shop purge's gold loss on the transition that
    merely opened GRID, and described the selected card removal on the GRID
    choice transition.  The game commits both effects only on the subsequent
    GRID confirmation.  Those rows lack the modern ``commit_timing`` and
    ``gold_cost`` evidence, so they cannot prove settlement; they also must
    not be treated as authoritative immediate-delta claims by this oracle.

    Keep this recognition deliberately narrow.  Modern contracts and any
    other malformed consequence continue through the strict mismatch path.
    """

    if not isinstance(record, dict) or not isinstance(consequences, dict):
        return None
    phase = str(record.get("phase") or "").upper()
    operation = str(consequences.get("operation") or "")
    classification = consequences.get("uncertainty_classification")
    classification = classification if isinstance(classification, dict) else {}
    reason = str(classification.get("reason") or "")
    future = consequences.get("future_costs")
    future = future if isinstance(future, list) else []

    if phase == "SHOP_SCREEN" and operation == "open_card_purge_grid":
        purge_effects = [
            effect for effect in future
            if isinstance(effect, dict)
            and effect.get("kind") == "shop_purge_grid_selection"
            and effect.get("operation") == "purge"
            and effect.get("select_count") == 1
            and effect.get("identity_binding")
            == "subsequent_grid_card_instance_id"
        ]
        if (
            len(purge_effects) == 1
            and purge_effects[0].get("commit_timing") is None
            and purge_effects[0].get("gold_cost") is None
            and reason
            == "purge cost is immediate and card identity is bound by the subsequent grid receipt"
        ):
            return {
                "kind": "legacy_shop_purge_commit_contract",
                "reason": "historical_shop_purge_commit_evidence_missing",
                "deferred_fields": frozenset({"gold_delta"}),
            }

    if phase == "GRID" and operation == "grid_purge":
        changes = consequences.get("card_changes")
        removed = changes.get("remove") if isinstance(changes, dict) else None
        has_modern_effect = any(
            isinstance(effect, dict)
            and effect.get("kind") == "grid_confirmation_effect"
            and effect.get("operation") == "grid_purge"
            and effect.get("commit_timing") == "after_grid_confirmation"
            for effect in future
        )
        if (
            not has_modern_effect
            and isinstance(removed, list) and len(removed) == 1
            and _card_instance_id(removed[0]) is not None
            and reason == "grid_purge_exact_card_instance"
        ):
            return {
                "kind": "legacy_grid_purge_commit_contract",
                "reason": "historical_grid_confirmation_evidence_missing",
                "deferred_fields": frozenset({"card_changes.remove"}),
            }
    return None


def _next_decision_record(records, index):
    for later_index in range(index + 1, len(records)):
        record = records[later_index]
        if isinstance(record, dict) and record.get("record_type") == "decision":
            return later_index, record
    return None, None


def _previous_decision_record(records, index):
    for previous_index in range(index - 1, -1, -1):
        record = records[previous_index]
        if isinstance(record, dict) and record.get("record_type") == "decision":
            return previous_index, record
    return None, None


def _card_instance_id(card):
    if not isinstance(card, dict):
        return None
    value = card.get("card_instance_id")
    return str(value) if value not in (None, "") else None


def _designer_full_service_parent(records, grid_index, grid_record, expected):
    """Identify Designer's A0 Full Service from raw protocol/state facts.

    Full Service pays 90 gold, opens a one-card purge GRID, then upgrades one
    other card when that GRID is confirmed.  The extra upgrade is therefore
    part of the same transaction, not an unrelated deck mutation.  Do not
    trust the producer's event score or consequence claim here: bind the
    exact raw event option, contiguous sequence, and authoritative gold/phase
    transition instead.
    """

    parent_index, parent = _previous_decision_record(records, grid_index)
    if parent is None or parent.get("after_seq") != grid_record.get("before_seq"):
        return None, None
    if str(parent.get("phase") or "").upper() != "EVENT":
        return None, None
    selected = _oracle_selected_option(parent)
    target = (
        selected.get("target")
        if isinstance(selected, dict)
        and isinstance(selected.get("target"), dict)
        else {}
    )
    if not (
        isinstance(selected, dict)
        and selected.get("choice_index") == 2
        and str(target.get("kind") or "").casefold() == "event_option"
        and _oracle_game_id(target.get("event_id")) == "designer"
    ):
        return None, None

    before_state, before_missing, before_mismatches = _state_envelope(
        parent, "before", expected
    )
    after_state, after_missing, after_mismatches = _state_envelope(
        parent, "after", expected
    )
    if before_missing or after_missing or before_mismatches or after_mismatches:
        return "unknown", {
            "reason": "designer_full_service_authoritative_state_missing",
            "designer_record_index": parent_index,
        }
    before = _observable_from_authoritative_state(before_state)
    after = _observable_from_authoritative_state(after_state)
    before_gold = _finite_number((before or {}).get("gold"))
    after_gold = _finite_number((after or {}).get("gold"))
    after_game = _game_from_state(after_state)
    after_phase = str(after_state.get("phase") or "").upper()
    after_screen = str((after_game or {}).get("screen_type") or "").upper()
    if before_gold is None or after_gold is None:
        return "unknown", {
            "reason": "designer_full_service_gold_evidence_missing",
            "designer_record_index": parent_index,
        }
    gold_delta = after_gold - before_gold
    if abs(gold_delta + 90.0) > 1e-9 or (
        after_phase != "GRID" and after_screen != "GRID"
    ):
        return "issue", {
            "reason": "designer_full_service_transition_mismatch",
            "designer_record_index": parent_index,
            "observed_gold_delta": gold_delta,
            "observed_after_phase": after_phase,
            "observed_after_screen_type": after_screen,
        }
    return "clear", {
        "designer_record_index": parent_index,
        "designer_option_choice_index": 2,
        "designer_gold_delta": gold_delta,
    }


def _designer_full_service_upgrade_valid(changed, selected_cards):
    if not isinstance(changed, list) or len(changed) != 1:
        return False
    change = changed[0]
    if not isinstance(change, dict):
        return False
    before = change.get("before")
    after = change.get("after")
    before_uuid = _card_instance_id(before)
    after_uuid = _card_instance_id(after)
    selected_uuids = {_card_instance_id(card) for card in selected_cards}
    return bool(
        before_uuid is not None
        and before_uuid == after_uuid
        and before_uuid not in selected_uuids
        and _oracle_game_id((before or {}).get("id"))
        == _oracle_game_id((after or {}).get("id"))
        and type((before or {}).get("upgrades")) is int
        and type((after or {}).get("upgrades")) is int
        and after["upgrades"] == before["upgrades"] + 1
    )


def _grid_state_context(state):
    """Return typed GRID selection facts from one authoritative frame."""

    game = _game_from_state(state)
    if not isinstance(game, dict):
        return None, "grid_game_state_missing"
    screen = game.get("screen_state")
    if not isinstance(screen, dict):
        return None, "grid_screen_state_missing"
    num_cards = screen.get("num_cards")
    any_number = screen.get("any_number")
    confirm_up = screen.get("confirm_up")
    selected = screen.get("selected_cards")
    if type(num_cards) is not int or num_cards <= 0:
        return None, "grid_num_cards_missing_or_invalid"
    if type(any_number) is not bool:
        return None, "grid_any_number_missing"
    if type(confirm_up) is not bool:
        return None, "grid_confirm_up_missing"
    if not isinstance(selected, list) or not all(
        isinstance(card, dict) for card in selected
    ):
        return None, "grid_selected_cards_missing_or_invalid"
    selected_ids = [_card_instance_id(card) for card in selected]
    if any(value is None for value in selected_ids):
        return None, "grid_selected_card_uuid_missing"
    if len(set(selected_ids)) != len(selected_ids):
        return None, "grid_selected_card_uuid_duplicated"
    return {
        "num_cards": num_cards,
        "any_number": any_number,
        "confirm_up": confirm_up,
        "selected_cards": selected,
        "selected_ids": selected_ids,
    }, None


def _one_grid_confirmation_effect(record, operation):
    _row, effects = _selected_deferred_effects(record)
    matches = [
        effect for effect in effects
        if effect.get("kind") == "grid_confirmation_effect"
        and effect.get("operation") == operation
    ]
    return matches[0] if len(matches) == 1 else None


def _grid_selected_cards_match(observed, expected_cards):
    """Compare a selected-card surface by UUID and semantic card facts."""

    if not isinstance(observed, list) or len(observed) != len(expected_cards):
        return False
    by_id = {_card_instance_id(card): card for card in observed}
    if None in by_id or len(by_id) != len(observed):
        return False
    return all(
        _card_instance_id(card) in by_id
        and _effect_item_matches(
            "card_changes", by_id[_card_instance_id(card)], card
        )
        for card in expected_cards
    )


def _grid_deferred_settlement(records, grid_index, grid_record, effect, expected):
    """Verify a complete one- or multi-card GRID transaction.

    Some screens (notably Empty Cage and Astrolabe) accept several card
    choices and commit immediately after the final choice; others expose a
    separate confirm action.  Reconstruct the exact sequence from bound state
    frames instead of assuming that the next decision is always ``proceed``.
    """

    operation = str(effect.get("operation") or "")
    first_before_state, missing, mismatches = _state_envelope(
        grid_record, "before", expected
    )
    first_before = _observable_from_authoritative_state(first_before_state)
    first_context, context_error = _grid_state_context(first_before_state)
    if (
        missing or mismatches or not isinstance(first_before, dict)
        or context_error is not None
    ):
        return "unknown", {
            "reason": context_error or "grid_selection_authoritative_state_missing",
            "grid_record_index": grid_index,
            "settled_grid_record_indices": [grid_index],
        }
    if first_context["selected_ids"]:
        return "unknown", {
            "reason": "grid_selection_chain_started_midstream",
            "grid_record_index": grid_index,
            "settled_grid_record_indices": [grid_index],
        }

    selected_cards = []
    selected_indices = []
    current_index = grid_index
    current_record = grid_record
    current_effect = effect
    confirmation_index = None
    final_after_state = None
    auto_committed = False
    seen = set()

    while True:
        if current_index in seen:
            return "issue", {
                "reason": "grid_selection_chain_cycle",
                "grid_record_index": grid_index,
                "settled_grid_record_indices": selected_indices or [grid_index],
            }
        seen.add(current_index)
        selected_indices.append(current_index)

        before_state, before_missing, before_mismatches = _state_envelope(
            current_record, "before", expected
        )
        before_context, before_context_error = _grid_state_context(before_state)
        if before_missing or before_mismatches or before_context_error is not None:
            return "unknown", {
                "reason": before_context_error
                or "grid_selection_authoritative_state_missing",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }
        if (
            before_context["num_cards"] != first_context["num_cards"]
            or before_context["any_number"] != first_context["any_number"]
        ):
            return "issue", {
                "reason": "grid_selection_contract_changed_mid_chain",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }
        if before_context["confirm_up"] is True:
            return "issue", {
                "reason": "grid_choice_accepted_while_confirm_up",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }
        if not _grid_selected_cards_match(
            before_context["selected_cards"], selected_cards
        ):
            return "issue", {
                "reason": "grid_selected_surface_chain_mismatch",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }

        selected_card = current_effect.get("selected_card")
        selected_uuid = _card_instance_id(selected_card)
        if (
            selected_uuid is None
            or selected_uuid in {
                _card_instance_id(card) for card in selected_cards
            }
        ):
            return "issue", {
                "reason": "grid_confirmation_selected_uuid_missing_or_reused",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }
        selected_cards.append(selected_card)

        after_state, after_missing, after_mismatches = _state_envelope(
            current_record, "after", expected
        )
        if after_missing or after_mismatches:
            return "unknown", {
                "reason": "grid_selection_authoritative_after_state_missing",
                "grid_record_index": grid_index,
                "selection_record_index": current_index,
                "settled_grid_record_indices": selected_indices,
            }
        after_phase = str(after_state.get("phase") or "").upper()
        if after_phase and after_phase != "GRID":
            final_after_state = after_state
            confirmation_index = current_index
            auto_committed = True
            break

        followup_index, followup = _next_decision_record(records, current_index)
        if followup is None:
            return "unknown", {
                "reason": "grid_confirmation_decision_missing",
                "grid_record_index": grid_index,
                "settled_grid_record_indices": selected_indices,
            }
        if followup.get("before_seq") != current_record.get("after_seq"):
            return "issue", {
                "reason": "grid_confirmation_sequence_discontinuity",
                "grid_record_index": grid_index,
                "confirmation_record_index": followup_index,
                "settled_grid_record_indices": selected_indices,
            }
        followup_phase = str(followup.get("phase") or "").upper()
        if followup_phase == "GRID" and followup.get("action") == "choose":
            next_effect = _one_grid_confirmation_effect(followup, operation)
            if next_effect is None:
                return "issue", {
                    "reason": "grid_selection_followup_effect_missing_or_ambiguous",
                    "grid_record_index": grid_index,
                    "selection_record_index": followup_index,
                    "settled_grid_record_indices": selected_indices,
                }
            current_index = followup_index
            current_record = followup
            current_effect = next_effect
            continue

        selected_followup = _selected_canonical_row(followup)
        confirmed_proceed = bool(
            followup_phase == "GRID"
            and followup.get("action") == "proceed"
            and (
                selected_followup is None
                or selected_followup.get("action") == "proceed"
            )
        )
        if not confirmed_proceed:
            return "issue", {
                "reason": "grid_confirmation_transition_mismatch",
                "grid_record_index": grid_index,
                "confirmation_record_index": followup_index,
                "settled_grid_record_indices": selected_indices,
            }
        confirm_before, confirm_missing, confirm_mismatches = _state_envelope(
            followup, "before", expected
        )
        confirm_context, confirm_context_error = _grid_state_context(
            confirm_before
        )
        if (
            confirm_missing or confirm_mismatches
            or confirm_context_error is not None
        ):
            return "unknown", {
                "reason": confirm_context_error
                or "grid_confirmation_authoritative_state_missing",
                "grid_record_index": grid_index,
                "confirmation_record_index": followup_index,
                "settled_grid_record_indices": selected_indices,
            }
        selected_surface_matches = _grid_selected_cards_match(
            confirm_context["selected_cards"], selected_cards
        )
        if not selected_surface_matches and not (
            confirm_context["confirm_up"] is True
            and not confirm_context["selected_cards"]
        ):
            return "issue", {
                "reason": "grid_confirmation_selected_surface_mismatch",
                "grid_record_index": grid_index,
                "confirmation_record_index": followup_index,
                "settled_grid_record_indices": selected_indices,
            }
        final_after_state, final_missing, final_mismatches = _state_envelope(
            followup, "after", expected
        )
        if final_missing or final_mismatches:
            return "unknown", {
                "reason": "grid_confirmation_authoritative_after_state_missing",
                "grid_record_index": grid_index,
                "confirmation_record_index": followup_index,
                "settled_grid_record_indices": selected_indices,
            }
        confirmation_index = followup_index
        break

    selection_count = len(selected_cards)
    if (
        selection_count > first_context["num_cards"]
        or not first_context["any_number"]
        and selection_count != first_context["num_cards"]
        or auto_committed
        and selection_count != first_context["num_cards"]
    ):
        return "issue", {
            "reason": "grid_selection_count_mismatch",
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "expected_num_cards": first_context["num_cards"],
            "observed_selection_count": selection_count,
            "any_number": first_context["any_number"],
            "auto_committed": auto_committed,
            "settled_grid_record_indices": selected_indices,
        }

    if operation in {
        "grid_combat_discard_to_hand", "grid_combat_discard_to_top",
        "grid_combat_draw_to_hand",
    }:
        before_game = _game_from_state(first_before_state)
        after_game = _game_from_state(final_after_state)
        before_combat = (
            before_game.get("combat_state")
            if isinstance(before_game, dict) else None
        )
        after_combat = (
            after_game.get("combat_state")
            if isinstance(after_game, dict) else None
        )
        selected_uuid = (
            _card_instance_id(selected_cards[0])
            if len(selected_cards) == 1 else None
        )

        def pile_ids(combat, name):
            pile = combat.get(name) if isinstance(combat, dict) else None
            if not isinstance(pile, list):
                return None
            values = [_card_instance_id(card) for card in pile]
            return values if all(value is not None for value in values) else None

        source_pile = (
            "draw_pile"
            if operation == "grid_combat_draw_to_hand"
            else "discard_pile"
        )
        before_source = pile_ids(before_combat, source_pile)
        after_source = pile_ids(after_combat, source_pile)
        before_hand = pile_ids(before_combat, "hand")
        after_hand = pile_ids(after_combat, "hand")
        deck_delta = _observable_collection_delta(
            (before_game or {}).get("deck"), (after_game or {}).get("deck")
        )
        gold_before = _finite_number((before_game or {}).get("gold"))
        gold_after = _finite_number((after_game or {}).get("gold"))
        moved_once = bool(
            selected_uuid is not None
            and before_source is not None
            and after_source is not None
            and before_hand is not None
            and after_hand is not None
            and before_source.count(selected_uuid) == 1
            and after_source.count(selected_uuid) == 0
        )
        if operation in {
            "grid_combat_discard_to_hand", "grid_combat_draw_to_hand",
        }:
            moved_once = bool(
                moved_once
                and before_hand.count(selected_uuid) == 0
                and after_hand.count(selected_uuid) == 1
                and Counter(before_source) - Counter([selected_uuid])
                == Counter(after_source)
                and Counter(before_hand) + Counter([selected_uuid])
                == Counter(after_hand)
            )
        else:
            before_draw = pile_ids(before_combat, "draw_pile")
            after_draw = pile_ids(after_combat, "draw_pile")
            moved_once = bool(
                moved_once
                and before_draw is not None
                and after_draw is not None
                and after_draw == before_draw + [selected_uuid]
                and all(
                    after_source.count(card_uuid)
                    >= (Counter(before_source) - Counter([selected_uuid]))[
                        card_uuid
                    ]
                    for card_uuid in set(before_source)
                    if card_uuid != selected_uuid
                )
            )
        deck_unchanged = bool(
            isinstance(deck_delta, dict)
            and all(not deck_delta.get(field) for field in (
                "added", "removed", "changed",
            ))
        )
        if not (
            isinstance(before_game, dict)
            and before_game.get("current_action") == {
                "grid_combat_discard_to_hand": (
                    "BetterDiscardPileToHandAction"
                ),
                "grid_combat_discard_to_top": (
                    "DiscardPileToTopOfDeckAction"
                ),
                "grid_combat_draw_to_hand": "SkillFromDeckToHandAction",
            }[operation]
            and moved_once
            and deck_unchanged
            and gold_before is not None
            and gold_after == gold_before
        ):
            return "issue", {
                "reason": "combat_discard_selection_settlement_mismatch",
                "operation": operation,
                "selected_card_instance_ids": [selected_uuid],
                "grid_record_index": grid_index,
                "confirmation_record_index": confirmation_index,
                "settled_grid_record_indices": selected_indices,
            }
        return "clear", {
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "operation": operation,
            "selected_card_instance_ids": [selected_uuid],
            "selection_count": 1,
            "auto_committed": auto_committed,
            "settled_grid_record_indices": selected_indices,
            "gold_delta": 0,
        }

    if operation in {
        "grid_bottle_attack", "grid_bottle_skill", "grid_bottle_power",
    }:
        before = first_before
        after = _observable_from_authoritative_state(final_after_state)
        if not isinstance(after, dict):
            return "unknown", {
                "reason": "grid_bottle_after_state_missing",
                "grid_record_index": grid_index,
            }
        selected_uuid = (
            _card_instance_id(selected_cards[0])
            if len(selected_cards) == 1 else None
        )
        after_matches = [
            card for card in after.get("deck") or []
            if isinstance(card, dict)
            and _card_instance_id(card) == selected_uuid
        ]
        flag = {
            "grid_bottle_attack": "in_bottle_flame",
            "grid_bottle_skill": "in_bottle_lightning",
            "grid_bottle_power": "in_bottle_tornado",
        }[operation]
        deck_delta = _observable_collection_delta(
            before.get("deck"), after.get("deck")
        )
        if not (
            selected_uuid is not None
            and len(after_matches) == 1
            and after_matches[0].get(flag) is True
            and isinstance(deck_delta, dict)
            and not deck_delta.get("added")
            and not deck_delta.get("removed")
        ):
            return "issue", {
                "reason": "grid_bottle_binding_settlement_mismatch",
                "operation": operation,
                "selected_card_instance_ids": [selected_uuid],
                "grid_record_index": grid_index,
            }
        return "clear", {
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "operation": operation,
            "selected_card_instance_ids": [selected_uuid],
            "selection_count": 1,
            "auto_committed": auto_committed,
            "settled_grid_record_indices": selected_indices,
            "gold_delta": 0,
        }

    before = first_before
    after = _observable_from_authoritative_state(final_after_state)
    if not isinstance(after, dict):
        return "unknown", {
            "reason": "grid_confirmation_observable_delta_missing",
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "settled_grid_record_indices": selected_indices,
        }
    deck_delta = _observable_collection_delta(
        before.get("deck"), after.get("deck")
    )
    before_gold = _finite_number(before.get("gold"))
    after_gold = _finite_number(after.get("gold"))
    if deck_delta is None or before_gold is None or after_gold is None:
        return "unknown", {
            "reason": "grid_confirmation_observable_delta_missing",
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "settled_grid_record_indices": selected_indices,
        }

    removed = deck_delta.get("removed")
    added = deck_delta.get("added")
    changed = deck_delta.get("changed")
    designer_status, designer_details = _designer_full_service_parent(
        records, grid_index, grid_record, expected
    )
    if designer_status in {"unknown", "issue"}:
        return designer_status, {
            **(designer_details or {}),
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "settled_grid_record_indices": selected_indices,
        }
    deck_valid = False
    if operation == "grid_note_exchange":
        screen = _oracle_before_game(grid_record).get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        parent = screen.get("parent_choice_context")
        parent = parent if isinstance(parent, dict) else {}
        offered = parent.get("offered_card")
        offered = offered if isinstance(offered, dict) else {}
        before_offered = [
            card for card in before.get("deck") or []
            if _effect_item_matches(
                "card_changes", card, offered, allow_new_instance=True
            )
        ]
        after_offered = [
            card for card in after.get("deck") or []
            if _effect_item_matches(
                "card_changes", card, offered, allow_new_instance=True
            )
        ]
        offered_transition_valid = bool(
            len(after_offered) == 1
            and (
                len(before_offered) == 0
                and isinstance(added, list) and len(added) == 1
                and _effect_item_matches(
                    "card_changes", added[0], offered,
                    allow_new_instance=True,
                )
                or len(before_offered) == 1
                and isinstance(added, list) and not added
            )
        )
        deck_valid = bool(
            offered.get("id")
            and isinstance(removed, list)
            and len(removed) == selection_count == 1
            and _grid_selected_cards_match(removed, selected_cards)
            and isinstance(changed, list) and not changed
            and offered_transition_valid
        )
    elif operation in {"grid_purge", "grid_remove"}:
        changed_valid = (
            isinstance(changed, list) and not changed
            if designer_status is None
            else _designer_full_service_upgrade_valid(
                changed, selected_cards
            )
        )
        deck_valid = bool(
            isinstance(removed, list) and len(removed) == selection_count
            and isinstance(added, list) and not added
            and changed_valid
            and _grid_selected_cards_match(removed, selected_cards)
        )
    elif operation == "grid_upgrade":
        deck_valid = bool(
            isinstance(removed, list) and not removed
            and isinstance(added, list) and not added
            and isinstance(changed, list) and len(changed) == selection_count
            and all(
                any(
                    isinstance(change, dict)
                    and _card_instance_id(change.get("before"))
                    == _card_instance_id(card)
                    and _card_instance_id(change.get("after"))
                    == _card_instance_id(card)
                    and _effect_item_matches(
                        "card_changes", change.get("before"), card
                    )
                    and type(change.get("before", {}).get("upgrades")) is int
                    and type(change.get("after", {}).get("upgrades")) is int
                    and change["after"]["upgrades"]
                    == change["before"]["upgrades"] + 1
                    for change in changed
                )
                for card in selected_cards
            )
        )
    elif operation == "grid_transform":
        first_screen = _oracle_before_game(grid_record).get("screen_state")
        first_screen = first_screen if isinstance(first_screen, dict) else {}
        grid_parent = first_screen.get("parent_choice_context")
        grid_parent = grid_parent if isinstance(grid_parent, dict) else {}
        astrolabe_transform = bool(
            grid_parent.get("authority") == "accepted_protocol_choice"
            and grid_parent.get("parent_phase") == "BOSS_REWARD"
            and _oracle_game_id(grid_parent.get("relic_id")) == "astrolabe"
            and grid_parent.get("operation") == "transform"
            and grid_parent.get("select_count") == 3
        )
        deck_valid = bool(
            isinstance(removed, list) and len(removed) == selection_count
            and isinstance(added, list) and len(added) == selection_count
            and isinstance(changed, list) and not changed
            and _grid_selected_cards_match(removed, selected_cards)
            and (
                not astrolabe_transform
                or all(
                    isinstance(card, dict)
                    and (
                        card.get("upgrades") == 0
                        if _oracle_is_curse(card)
                        else card.get("upgrades") == 1
                    )
                    for card in added
                )
            )
        )
        # Normal transforms do not generate curses, and curse transforms do
        # not remove their curse pool. Preserve this domain constraint while
        # allowing Astrolabe's non-upgradeable curse results.
        if deck_valid:
            deck_valid = sum(_oracle_is_curse(card) for card in added) == sum(
                _oracle_is_curse(card) for card in removed
            )
    elif operation == "grid_gain":
        deck_valid = bool(
            isinstance(removed, list) and not removed
            and isinstance(added, list) and len(added) == selection_count
            and isinstance(changed, list) and not changed
            and all(
                any(
                    _effect_item_matches(
                        "card_changes", gained, selected,
                        allow_new_instance=True,
                    )
                    for gained in added
                )
                for selected in selected_cards
            )
        )
    elif operation == "grid_duplicate":
        after_deck = after.get("deck")

        def same_card_new_instance(copied, source):
            copied_instance = _card_instance_id(copied)
            source_instance = _card_instance_id(source)
            copied_semantic = dict(copied) if isinstance(copied, dict) else {}
            source_semantic = dict(source) if isinstance(source, dict) else {}
            for semantic in (copied_semantic, source_semantic):
                semantic.pop("card_instance_id", None)
                semantic.pop("uuid", None)
            return bool(
                copied_instance is not None
                and source_instance is not None
                and copied_instance != source_instance
                and _effect_item_matches(
                    "card_changes", copied_semantic, source_semantic
                )
            )

        source_preserved = bool(
            isinstance(after_deck, list)
            and all(
                sum(
                    1 for card in after_deck
                    if _card_instance_id(card) == _card_instance_id(source)
                ) == 1
                for source in selected_cards
            )
        )
        deck_valid = bool(
            isinstance(removed, list) and not removed
            and isinstance(added, list) and len(added) == selection_count
            and isinstance(changed, list) and not changed
            and source_preserved
            and all(
                any(same_card_new_instance(copied, source) for copied in added)
                for source in selected_cards
            )
        )
    else:
        return "unknown", {
            "reason": "grid_confirmation_operation_unsupported",
            "operation": operation,
            "grid_record_index": grid_index,
        }
    if not deck_valid:
        return "issue", {
            "reason": "grid_confirmation_deck_delta_mismatch",
            "operation": operation,
            "selected_card_instance_ids": [
                _card_instance_id(card) for card in selected_cards
            ],
            "grid_record_index": grid_index,
            "confirmation_record_index": confirmation_index,
            "observed_deck_delta": deck_delta,
            "settled_grid_record_indices": selected_indices,
        }
    return "clear", {
        "grid_record_index": grid_index,
        "confirmation_record_index": confirmation_index,
        "operation": operation,
        "selected_card_instance_ids": [
            _card_instance_id(card) for card in selected_cards
        ],
        "selection_count": selection_count,
        "auto_committed": auto_committed,
        "settled_grid_record_indices": selected_indices,
        "gold_delta": after_gold - before_gold,
        "designer_full_service": (
            {
                **designer_details,
                "random_upgrade": changed[0],
            }
            if designer_status == "clear" else None
        ),
    }


_PRODUCER_EFFECT_FIELDS = {
    "hp_delta", "current_hp_delta", "max_hp_delta", "gold_delta",
    "card_changes", "relic_changes", "potion_changes", "curse",
    "probabilistic_outcomes", "probability_outcomes", "current_cost",
    "future_costs", "route", "event_id", "campfire_option",
    "selected_card", "key_changes", "operation", "deck_size_delta",
    "max_hp_gain", "post_purchase_gold", "price", "leave",
    "potion_id", "potion_slot", "bound_purchase_potion_id",
    "bound_purchase_listing_id", "bound_purchase_choice_index",
    "bound_purchase_item_id", "bound_purchase_price",
    "bound_reward_potion_id", "mechanism_id", "neow_contract",
    "reward_kind", "drawback_kind", "parameters", "random_effects",
    "acquired_benefit",
    "reward_type", "relic_id", "rest_option", "card_id",
    "card_instance_id",
}
_PRODUCER_SCORING_FIELDS_V1 = {
    "path_survival_risk", "route_summary", "score_components", "utility",
    "value", "quality", "priority", "risk", "penalty", "bonus",
    "reason", "reason_codes", "uncertainty", "strategic_value",
    "card_gain_requires_successful_pair", "known_curse",
    "known_pair_count", "match_stage", "revealed_label", "unknown_card",
    "card_change_kind", "card_delta", "card_delta_max", "card_delta_min",
    "card_gain", "card_loss", "card_option_value_model",
    "card_reward_candidates_per_screen", "card_reward_pool",
    "emerald_route_pressure", "event_outcome_id",
    "expected_card_option_value", "hp_change_kind", "lost_card",
    "marginal_card_option_values", "optional_card_reward_count",
    "selection_optional",
    # Route-level survival guard introduced by the shop-arrival planner.
    # It is a scoring fact (not an observable consequence), so the blind
    # oracle must partition it instead of treating the field as unknown.
    "shop_arrival_survival_floor",
}
_PRODUCER_SCORING_FIELDS_V2 = {
    "first_shop_arrival_hp",
    # Route utility decomposition for a future question-mark node.  This
    # explains the score; it is not an assertion about the immediate map
    # transition and therefore belongs in the v2 scoring partition.
    "question_room_value_components",
    "relic_delta", "relic_value", "potion_delta",
    "card_delta", "upgrade_delta", "upgrade_value", "best_removal_value",
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
    "nominal_hp_loss", "card_transform_delta", "reward_value",
    "best_upgrade_value", "combat_delta", "expected_hp_loss",
    "death_risk", "upper_hp_loss",
}
_PRODUCER_SCORING_FIELDS = (
    _PRODUCER_SCORING_FIELDS_V1 | _PRODUCER_SCORING_FIELDS_V2
)


def _producer_partition_scoring_fields(row):
    producer = row.get("producer_candidate_raw") if isinstance(row, dict) else None
    if (
        isinstance(producer, dict)
        and producer.get("producer_partition_version") == 2
    ):
        return _PRODUCER_SCORING_FIELDS
    return _PRODUCER_SCORING_FIELDS_V1


def _producer_partition_mismatches(row):
    raw_envelope = row.get("producer_consequence_raw")
    claim = row.get("producer_consequence_claim")
    scoring = row.get("producer_scoring_facts")
    unclassified = row.get("unclassified_producer_fields")
    mismatches = []
    if not isinstance(raw_envelope, dict) or type(
        raw_envelope.get("present")
    ) is not bool:
        return ["producer_consequence_raw"]
    if not isinstance(claim, dict):
        mismatches.append("producer_consequence_claim")
        claim = {}
    if not isinstance(scoring, dict):
        mismatches.append("producer_scoring_facts")
        scoring = {}
    if not isinstance(unclassified, list) or any(
        not isinstance(value, str) for value in unclassified
    ):
        mismatches.append("unclassified_producer_fields")
        unclassified = []
    raw = raw_envelope.get("value")
    if raw_envelope.get("present") is False:
        if raw is not None or claim or scoring or unclassified:
            mismatches.append("absent_claim_partition")
        return mismatches
    if not isinstance(raw, dict):
        mismatches.append("producer_consequence_raw.value")
        return mismatches
    scoring_fields = _producer_partition_scoring_fields(row)
    wanted_claim = {
        key: value for key, value in raw.items()
        if key in _PRODUCER_EFFECT_FIELDS
    }
    wanted_scoring = {
        key: value for key, value in raw.items()
        if key in scoring_fields
    }
    wanted_unclassified = sorted(
        str(key) for key in raw
        if key not in _PRODUCER_EFFECT_FIELDS | scoring_fields
    )
    if _freeze(claim) != _freeze(wanted_claim):
        mismatches.append("producer_consequence_claim_partition")
    if _freeze(scoring) != _freeze(wanted_scoring):
        mismatches.append("producer_scoring_facts_partition")
    if sorted(unclassified) != wanted_unclassified:
        mismatches.append("unclassified_producer_fields_partition")
    return mismatches


def _expected_or_settled_value(field, expected, settlement):
    knowledge = expected.get("field_knowledge")
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    field_knowledge = knowledge.get(field)
    if (
        isinstance(field_knowledge, dict)
        and field_knowledge.get("status") in {
            "known", "known_domain", "not_applicable",
        }
    ):
        return True, expected.get(field), "independent_mechanics"
    if not isinstance(settlement, dict):
        return False, None, "unselected_or_unsettled"
    if field == "hp_delta":
        return True, settlement.get("hp_delta"), "authoritative_settlement"
    if field in {"max_hp_delta", "gold_delta"}:
        return True, settlement.get(field), "authoritative_settlement"
    collection = {
        "card_changes": "deck", "relic_changes": "relics",
        "potion_changes": "potions",
    }.get(field)
    if collection and isinstance(settlement.get(collection), dict):
        observed = settlement[collection]
        return True, {
            "gain": observed.get("added") or [],
            "remove": observed.get("removed") or [],
        }, "authoritative_settlement"
    if (
        field == "curse"
        and expected.get("operation") == "gain_boss_relic"
        and isinstance(settlement.get("deck"), dict)
    ):
        observed = settlement["deck"]

        def observed_curses(key):
            return [
                card for card in observed.get(key) or []
                if isinstance(card, dict)
                and (
                    str(card.get("type") or "").upper() == "CURSE"
                    or str(card.get("rarity") or "").upper() == "CURSE"
                )
            ]

        return True, {
            "gain": observed_curses("added"),
            "remove": observed_curses("removed"),
            "probability": None,
            "omamori_applicable": None,
            "omamori_charges_consumed": None,
        }, "authoritative_settlement"
    return False, None, "unselected_or_unsettled"


def _effect_item_matches(field, left, right, *, allow_new_instance=False):
    """Compare consequence identity, excluding presentation-only fields."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return _freeze(left) == _freeze(right)
    if field == "card_changes":
        left_id = left.get("id") or left.get("card_id")
        right_id = right.get("id") or right.get("card_id")
        if not left_id and not right_id:
            return _freeze(left) == _freeze(right)
        if (
            not left_id or not right_id
            or _oracle_game_id(left_id) != _oracle_game_id(right_id)
        ):
            return False
        if int(left.get("upgrades") or 0) != int(right.get("upgrades") or 0):
            return False
        left_instance = left.get("card_instance_id") or left.get("uuid")
        right_instance = right.get("card_instance_id") or right.get("uuid")
        if allow_new_instance:
            # Reward/event previews describe a card that does not exist yet.
            # The game allocates the persistent deck UUID only on settlement;
            # semantic card id and upgrade level are the exact identity facts.
            return True
        return not (
            left_instance and right_instance
            and str(left_instance) != str(right_instance)
        )
    if field == "relic_changes":
        left_id = left.get("id") or left.get("relic_id")
        right_id = right.get("id") or right.get("relic_id")
        if not left_id and not right_id:
            return _freeze(left) == _freeze(right)
        return bool(
            left_id and right_id
            and _oracle_game_id(left_id) == _oracle_game_id(right_id)
        )
    if field == "potion_changes":
        left_id = left.get("id") or left.get("potion_id")
        right_id = right.get("id") or right.get("potion_id")
        if not left_id and not right_id:
            return _freeze(left) == _freeze(right)
        if (
            not left_id or not right_id
            or _oracle_game_id(left_id) != _oracle_game_id(right_id)
        ):
            return False
        left_instance = left.get("potion_instance_id")
        right_instance = right.get("potion_instance_id")
        if left_instance and right_instance and left_instance != right_instance:
            return False
        if left.get("slot") is not None and right.get("slot") is not None:
            return left.get("slot") == right.get("slot")
        return True
    return _freeze(left) == _freeze(right)


def _effect_item_multiset_equal(
    field, left, right, *, allow_new_instance=False,
):
    if not isinstance(left, list) or not isinstance(right, list):
        return False

    def expanded(items):
        if not allow_new_instance:
            return list(items)
        values = []
        for item in items:
            if (
                isinstance(item, dict)
                and type(item.get("count")) is int
                and item["count"] > 0
            ):
                semantic_item = _oracle_clone(item)
                count = semantic_item.pop("count")
                values.extend(_oracle_clone(semantic_item) for _ in range(count))
            else:
                values.append(item)
        return values

    left = expanded(left)
    right = expanded(right)
    if len(left) != len(right):
        return False
    unmatched = list(range(len(right)))
    for claimed in left:
        match = next(
            (
                index for index in unmatched
                if _effect_item_matches(
                    field, claimed, right[index],
                    allow_new_instance=allow_new_instance,
                )
            ),
            None,
        )
        if match is None:
            return False
        unmatched.remove(match)
    return not unmatched


def _effect_selected_card_equal(left, right):
    """Compare an exact card claim while dropping only presentation state."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return False

    def normalized(card):
        value = _oracle_clone(card)
        if value.get("card_instance_id") is None and value.get("uuid"):
            value["card_instance_id"] = value["uuid"]
        # Playability is a property of the current UI/action frame, not of
        # the selected GRID/HAND_SELECT card identity or deferred effect.
        value.pop("uuid", None)
        value.pop("is_playable", None)
        return value

    return _freeze(normalized(left)) == _freeze(normalized(right))


def _future_costs_equal(left, right):
    """Strictly compare deferred effects with semantic selected-card facts."""

    if not isinstance(left, list) or not isinstance(right, list):
        return False
    if len(left) != len(right):
        return False
    for claimed, expected in zip(left, right):
        if not isinstance(claimed, dict) or not isinstance(expected, dict):
            if _freeze(claimed) != _freeze(expected):
                return False
            continue
        if set(claimed) != set(expected):
            return False
        for key in claimed:
            if key == "selected_card":
                if not _effect_selected_card_equal(
                    claimed[key], expected[key]
                ):
                    return False
            elif _freeze(claimed[key]) != _freeze(expected[key]):
                return False
    return True


def _effect_claim_equal(field, claim, expected):
    if field in {"card_changes", "relic_changes", "potion_changes"}:
        if isinstance(claim, list) or isinstance(expected, list):
            if not isinstance(claim, list) or not isinstance(expected, list):
                return False
            if len(claim) != len(expected):
                return False
            unmatched = list(range(len(expected)))
            for claimed_change in claim:
                match = next(
                    (
                        index for index in unmatched
                        if _effect_claim_equal(
                            field, claimed_change, expected[index]
                        )
                    ),
                    None,
                )
                if match is None:
                    return False
                unmatched.remove(match)
            return not unmatched
        if not isinstance(claim, dict) or not isinstance(expected, dict):
            return False
        if "random_gain" in claim or "random_gain" in expected:
            return _freeze(claim) == _freeze(expected)
        for claim_key in ("gain", "remove", "upgrade", "transform"):
            if claim_key not in claim:
                continue
            left = claim.get(claim_key)
            if claim_key not in expected and left == []:
                continue
            right = expected.get(claim_key)
            if not _effect_item_multiset_equal(
                field, left, right,
                allow_new_instance=(
                    field == "card_changes" and claim_key == "gain"
                ),
            ):
                return False
        return True
    if field == "curse":
        if not isinstance(claim, dict) or not isinstance(expected, dict):
            return False
        if set(claim) != set(expected):
            return False
        for key in claim:
            if key in {"gain", "remove"}:
                if not _effect_item_multiset_equal(
                    "card_changes", claim.get(key), expected.get(key),
                    allow_new_instance=(key == "gain"),
                ):
                    return False
            elif _freeze(claim.get(key)) != _freeze(expected.get(key)):
                return False
        return True
    if field == "future_costs":
        return _future_costs_equal(claim, expected)
    bounds = _numeric_domain_bounds(expected)
    if bounds is not None and type(claim) in {int, float}:
        return bounds[0] <= claim <= bounds[1]
    return _freeze(claim) == _freeze(expected)


def _oracle_held_potion_binding(record, target):
    """Bind a resource-preparation target to one before-inventory instance."""

    if str((target or {}).get("kind") or "").casefold() != "potion_resource":
        return "unknown", None, "potion_resource_target_missing"
    instance_id = target.get("potion_instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return "unknown", None, "held_potion_instance_id_missing"
    potions = _oracle_before_game(record).get("potions")
    if not isinstance(potions, list):
        return "unknown", None, "before_potion_inventory_missing"
    matches = [
        (index, potion) for index, potion in enumerate(potions)
        if isinstance(potion, dict)
        and str(potion.get("potion_instance_id") or "") == instance_id
    ]
    if len(matches) != 1:
        return (
            "issues" if not matches else "unknown",
            None,
            "held_potion_instance_not_in_before_inventory"
            if not matches else "held_potion_instance_not_unique",
        )
    index, held = matches[0]
    held_id = held.get("id")
    held_slot = held.get("slot") if type(held.get("slot")) is int else index
    nested = target.get("potion")
    nested = nested if isinstance(nested, dict) else {}
    contradictions = []
    if (
        not held_id
        or _oracle_game_id(target.get("potion_id"))
        != _oracle_game_id(held_id)
    ):
        contradictions.append("held_potion_id_mismatch")
    if target.get("slot") != held_slot:
        contradictions.append("held_potion_slot_mismatch")
    if nested:
        if nested.get("potion_instance_id") != instance_id:
            contradictions.append("nested_held_potion_instance_mismatch")
        if _oracle_game_id(nested.get("id")) != _oracle_game_id(held_id):
            contradictions.append("nested_held_potion_id_mismatch")
        if nested.get("slot") not in (None, held_slot):
            contradictions.append("nested_held_potion_slot_mismatch")
    if contradictions:
        return "issues", {
            "potion_id": held_id,
            "potion_instance_id": instance_id,
            "potion_slot": held_slot,
        }, ",".join(sorted(contradictions))
    return "clear", {
        "potion_id": held_id,
        "potion_instance_id": instance_id,
        "potion_slot": held_slot,
    }, "held_potion_bound_to_before_inventory"


def _oracle_reward_potion_binding(record, raw):
    """Bind one COMBAT_REWARD potion by raw option id and choice_index."""

    if str(record.get("phase") or "").upper() != "COMBAT_REWARD":
        return "unknown", None, "combat_reward_phase_missing"
    target = raw.get("target") if isinstance(raw, dict) else None
    target = target if isinstance(target, dict) else {}
    reward = target.get("reward")
    reward = reward if isinstance(reward, dict) else {}
    potion = reward.get("potion")
    potion = potion if isinstance(potion, dict) else {}
    raw_id = potion.get("id")
    choice_id = raw.get("choice_id") if isinstance(raw, dict) else None
    choice_index = raw.get("choice_index") if isinstance(raw, dict) else None
    if (
        str(reward.get("reward_type") or "").casefold() != "potion"
        or not raw_id or choice_id is None or type(choice_index) is not int
    ):
        return "unknown", None, "raw_reward_potion_binding_missing"
    options = record.get("available_options_before")
    if not isinstance(options, list):
        return "unknown", None, "combat_reward_options_missing"
    matches = [
        option for option in options
        if isinstance(option, dict)
        and str(option.get("option_id")) == str(choice_id)
        and option.get("choice_index") == choice_index
    ]
    if len(matches) != 1:
        return (
            "issues" if options else "unknown", None,
            "raw_reward_choice_binding_mismatch"
            if options else "combat_reward_options_missing",
        )
    source_target = matches[0].get("target")
    source_target = source_target if isinstance(source_target, dict) else {}
    source_reward = source_target.get("reward")
    source_reward = source_reward if isinstance(source_reward, dict) else {}
    source_potion = source_reward.get("potion")
    source_potion = source_potion if isinstance(source_potion, dict) else {}
    source_id = source_potion.get("id")
    if (
        str(source_reward.get("reward_type") or "").casefold() != "potion"
        or not source_id
        or _oracle_game_id(source_id) != _oracle_game_id(raw_id)
    ):
        return "issues", source_id, "raw_reward_potion_identity_mismatch"
    return "clear", source_id, "raw_reward_potion_bound"


def _oracle_parent_potion_transaction(record, field, claimed):
    """Resolve a replacement's new potion only from its raw parent surface."""

    phase = str(record.get("phase") or "").upper()
    required_phase = {
        "bound_purchase_potion_id": "SHOP_SCREEN",
        "bound_reward_potion_id": "COMBAT_REWARD",
    }.get(field)
    if required_phase is not None and phase != required_phase:
        return "issues", None, "bound_potion_parent_phase_mismatch"
    if phase not in {"SHOP_SCREEN", "COMBAT_REWARD"}:
        return "unknown", None, "bound_potion_parent_phase_missing"
    options = record.get("available_options_before")
    if not isinstance(options, list) or not options:
        return "unknown", None, "bound_potion_parent_options_missing"
    parents = []
    malformed = False
    for option in options:
        if not isinstance(option, dict):
            malformed = True
            continue
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        choice_index = option.get("choice_index")
        option_id = option.get("option_id")
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
        if potion_id is None:
            continue
        if (
            not typed or type(choice_index) is not int
            or option_id is None or not str(option_id)
        ):
            malformed = True
            continue
        parents.append((str(option_id), choice_index, str(potion_id)))
    if not parents:
        return "unknown", None, (
            "bound_potion_parent_malformed"
            if malformed else "bound_potion_parent_transaction_missing"
        )
    if (
        len({option_id for option_id, _choice_index, _potion_id in parents})
        != len(parents)
        or len({choice_index for _option_id, choice_index, _potion_id in parents})
        != len(parents)
    ):
        return "unknown", None, "bound_potion_parent_choice_index_ambiguous"
    matches = [
        potion_id for _option_id, _choice_index, potion_id in parents
        if _oracle_game_id(potion_id) == _oracle_game_id(claimed)
    ]
    if len(matches) == 1:
        return "clear", matches[0], "bound_potion_parent_transaction_exact"
    if not matches:
        return "issues", None, "bound_new_potion_id_not_on_parent_surface"
    return "unknown", None, "bound_new_potion_parent_ambiguous"


def _oracle_bound_shop_listing(record, raw_claim):
    """Bind a staged potion discard/use to one exact shop potion listing."""

    if str(record.get("phase") or "").upper() != "SHOP_SCREEN":
        return "issues", None, "bound_purchase_parent_phase_mismatch"
    if not isinstance(raw_claim, dict):
        return "unknown", None, "bound_purchase_claim_missing"
    listing_id = raw_claim.get("bound_purchase_listing_id")
    if not isinstance(listing_id, str) or not listing_id:
        return "unknown", None, "bound_purchase_listing_id_missing"
    options = record.get("available_options_before")
    if not isinstance(options, list) or not options:
        return "unknown", None, "bound_purchase_parent_options_missing"
    matches = [
        option for option in options
        if isinstance(option, dict) and option.get("option_id") == listing_id
    ]
    if len(matches) != 1:
        return (
            "issues" if not matches else "unknown",
            None,
            (
                "bound_purchase_listing_not_on_parent_surface"
                if not matches else "bound_purchase_listing_ambiguous"
            ),
        )
    option = matches[0]
    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    item = target.get("item")
    item = item if isinstance(item, dict) else {}
    if (
        str(target.get("kind") or "").casefold() != "potion"
        or type(option.get("choice_index")) is not int
        or not (item.get("id") or item.get("potion_id"))
        or type(item.get("price")) is not int
    ):
        return "unknown", None, "bound_purchase_listing_malformed"
    return "clear", {
        "bound_purchase_listing_id": listing_id,
        "bound_purchase_choice_index": option["choice_index"],
        "bound_purchase_item_id": item.get("id") or item.get("potion_id"),
        "bound_purchase_price": item["price"],
    }, "bound_purchase_listing_exact"


def _explicit_ineligible_neow_sentinel(record, row, raw):
    """Recognize the exact fail-closed claim for an untyped Neow option."""

    if str(record.get("phase") or "").upper() != "NEOW":
        return False
    if not isinstance(row, dict) or row.get("selection_eligible") is not False:
        return False
    if row.get("veto_reason") != "typed_neow_contract_missing":
        return False
    claim = row.get("producer_consequence_claim")
    envelope = row.get("producer_consequence_raw")
    if not isinstance(claim, dict) or claim != {
        "operation": "unclassified_neow_contract",
    }:
        return False
    if not isinstance(envelope, dict) or envelope.get("present") is not True:
        return False
    value = envelope.get("value")
    if not isinstance(value, dict):
        return False
    return value == {
        "operation": "unclassified_neow_contract",
        "uncertainty": "authoritative typed Neow contract missing",
    }


def _grid_remove_purge_claim_alias(record, raw, claimed, expected):
    """Accept only the JAR's ``grid_purge`` spelling for Neow removal.

    The generic GRID operation surface may legitimately distinguish purge,
    remove, upgrade, and transform.  This alias exists only when the
    authoritative screen has no direct operation flag and carries an accepted
    Neow ``REMOVE_CARD``/``REMOVE_TWO`` parent context.  It is deliberately
    not a phase-wide normalization.
    """

    if str(record.get("phase") or "").upper() != "GRID":
        return False
    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    if str(target.get("kind") or "").casefold() != "card":
        return False
    operation, _selected, error = _oracle_grid_target(record, target)
    if error is not None or operation != "grid_remove":
        return False
    screen = _oracle_before_game(record).get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    if any(screen.get(flag) is not False for flag in _ORACLE_GRID_OPERATION_FLAGS):
        return False
    parent = screen.get("parent_choice_context")
    parent = parent if isinstance(parent, dict) else {}
    contract = parent.get("neow_contract")
    contract = contract if isinstance(contract, dict) else {}
    future = _ORACLE_NEOW_FUTURE_REWARDS.get(contract.get("reward_kind"))
    if not (
        parent.get("authority") == "accepted_protocol_choice"
        and parent.get("parent_phase") == "NEOW"
        and parent.get("operation") == "remove"
        and isinstance(future, dict)
        and future.get("operation") == "remove"
        and parent.get("select_count") == future.get("select_count")
        and parent.get("mechanism_id") == _oracle_neow_mechanism_id(contract)
    ):
        return False
    if isinstance(expected, str):
        return (
            expected.casefold() == "grid_remove"
            and str(claimed or "").casefold() == "grid_purge"
        )
    if not isinstance(expected, list) or not isinstance(claimed, list):
        return False
    normalized = _oracle_clone(claimed)
    changed = False
    for item in normalized:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "grid_confirmation_effect" and item.get(
            "operation"
        ) == "grid_purge":
            item["operation"] = "grid_remove"
            changed = True
    return changed and _effect_claim_equal("future_costs", normalized, expected)


def _neow_event_id_claim_alias(record, raw, claimed, expected):
    """Accept the legacy internal ``neow`` spelling for the Neow event only.

    Older policy traces used the agent's normalized event token in the
    consequence claim while the protocol target retained ``Neow Event``.
    Both identify the same typed Neow surface.  Keep the alias constrained by
    phase, target identity, and the exact two known spellings so it cannot
    weaken event identity checks elsewhere.
    """

    if str(record.get("phase") or "").upper() != "NEOW":
        return False
    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    return bool(
        str(target.get("kind") or "").casefold() == "event_option"
        and _oracle_game_id(target.get("event_id")) == "neowevent"
        and _oracle_game_id(expected) == "neowevent"
        and _oracle_game_id(claimed) == "neow"
    )


def _review_producer_consequence_claim(
    record, raw, row, expected, settlement
):
    """Return independently proven contradictions and unresolved assertions."""

    contradictions = []
    unresolved = []
    partition = _producer_partition_mismatches(row)
    if partition:
        contradictions.append({
            "field": "producer_partition", "details": partition,
        })
        return contradictions, unresolved

    # A candidate that the local policy explicitly vetoed because the typed
    # Neow contract was unavailable is not an assertion of the mechanics.  The
    # producer deliberately emits a small, fail-closed sentinel instead of
    # pretending that the option is selectable.  Do not compare that sentinel
    # to the independent mechanics projection; doing so turns a safe veto into
    # a false contradiction.  Keep this exact to the Neow surface and the
    # complete sentinel shape so an eligible or partially classified row still
    # fails closed below.
    if _explicit_ineligible_neow_sentinel(record, row, raw):
        return contradictions, unresolved
    envelope = row.get("producer_consequence_raw")
    claim = row.get("producer_consequence_claim")
    unclassified = row.get("unclassified_producer_fields") or []
    target = raw.get("target") if isinstance(raw, dict) else {}
    target = target if isinstance(target, dict) else {}
    forced_single_event_option = bool(
        str(record.get("phase") or "").upper() == "EVENT"
        and str(target.get("kind") or "").casefold() == "event_option"
        and len(record.get("available_options_before") or []) == 1
        and isinstance(expected.get("operation"), str)
        and expected.get("operation")
    )
    expected_knowledge = expected.get("field_knowledge")
    expected_knowledge = (
        expected_knowledge if isinstance(expected_knowledge, dict) else {}
    )
    independently_complete = all(
        isinstance(expected_knowledge.get(field), dict)
        and expected_knowledge[field].get("status") in {
            "known", "known_domain", "not_applicable",
        }
        for field in _ORACLE_CONSEQUENCE_FIELDS
    )
    scoring_only_contract = bool(
        isinstance(row.get("producer_scoring_facts"), dict)
        and row["producer_scoring_facts"]
        and not row.get("unclassified_producer_fields")
        and isinstance(row.get("producer_consequence_claim"), dict)
        and not row["producer_consequence_claim"]
        and independently_complete
    )
    typed_event_oracle_contract = bool(
        str(record.get("phase") or "").upper() == "EVENT"
        and (
            (
                _oracle_projection_v2(target)
                and _oracle_game_id(target.get("event_id")) in {
                "forgottenaltar", "ghosts", "mindbloom",
                    "upgradeshrine", "wemeetagain", "thejoust", "goldenidol",
                    "thelibrary",
                }
            )
            or (
                _oracle_projection_v3(target)
                and _oracle_game_id(target.get("event_id")) in {
                    "livingwall", "nest", "transmorgrifier", "purifier",
                    "tomboflordredmask", "deadadventurer",
                }
            )
        )
        and independently_complete
    )
    classification = expected.get("uncertainty_classification")
    classification = classification if isinstance(classification, dict) else {}
    typed_protocol_hidden_match_contract = bool(
        str(record.get("phase") or "").upper() == "EVENT"
        and _oracle_game_id(target.get("event_id"))
        in {"matchandkeep", "matchkeep"}
        and classification.get("status") == "protocol_hidden"
        and all(
            field == "card_changes"
            or (
                isinstance(expected_knowledge.get(field), dict)
                and expected_knowledge[field].get("status") in {
                    "known", "known_domain", "not_applicable",
                }
            )
            for field in _ORACLE_CONSEQUENCE_FIELDS
        )
    )
    if envelope.get("present") is not True or not isinstance(
        envelope.get("value"), dict
    ) or (
        not envelope["value"]
        and not forced_single_event_option
        and not scoring_only_contract
        and not typed_event_oracle_contract
        and not typed_protocol_hidden_match_contract
    ):
        unresolved.append("empty_or_missing_high_impact_claim")
    if (
        not isinstance(claim, dict)
        or (
            not claim
            and not forced_single_event_option
            and not scoring_only_contract
            and not typed_event_oracle_contract
            and not typed_protocol_hidden_match_contract
        )
    ):
        unresolved.append("effect_claim_empty")
        claim = {}
    raw_claim = envelope.get("value") if isinstance(envelope, dict) else {}
    raw_claim = raw_claim if isinstance(raw_claim, dict) else {}

    def independently_expected_event_relic(field):
        if field not in {"relic_id", "lost_relic_id"}:
            return False, None
        if str(target.get("kind") or "").casefold() != "event_option":
            return False, None
        changes = expected.get("relic_changes")
        if not isinstance(changes, dict):
            return False, None
        collection = "gain" if field == "relic_id" else "remove"
        items = changes.get(collection)
        if not isinstance(items, list) or len(items) != 1:
            return False, None
        item = items[0]
        if not isinstance(item, dict):
            return False, None
        wanted = item.get("id") or item.get("name")
        return bool(wanted), wanted

    for unclassified_field in sorted(set(unclassified)):
        claimed_unclassified = raw_claim.get(unclassified_field)
        target = raw.get("target") if isinstance(raw, dict) else {}
        target = target if isinstance(target, dict) else {}
        relic_identity_known, wanted_relic_id = (
            independently_expected_event_relic(unclassified_field)
        )
        if relic_identity_known:
            if _oracle_game_id(claimed_unclassified) != _oracle_game_id(
                wanted_relic_id
            ):
                contradictions.append({
                    "field": unclassified_field,
                    "claimed": claimed_unclassified,
                    "independently_expected": wanted_relic_id,
                    "authority": "independent_event_mechanics",
                })
            continue
        # Golden Idol's first branch exposes the gained relic only in the
        # authoritative after-frame.  Bind this legacy producer field to that
        # independent settlement delta instead of treating the missing relic
        # identity on the pre-choice target as an audit gap.
        if (
            unclassified_field == "relic_id"
            and _oracle_game_id(target.get("event_id")) == "goldenidol"
            and isinstance(settlement, dict)
        ):
            relic_delta = settlement.get("relics")
            added = relic_delta.get("added") if isinstance(relic_delta, dict) else None
            matching = [
                relic for relic in (added or [])
                if isinstance(relic, dict)
                and _oracle_game_id(relic.get("id") or relic.get("name"))
                == _oracle_game_id(claimed_unclassified)
            ]
            if isinstance(added, list) and len(added) == 1 and len(matching) == 1:
                continue
            if isinstance(added, list) and len(added) == 1:
                contradictions.append({
                    "field": unclassified_field,
                    "claimed": claimed_unclassified,
                    "independently_expected": (
                        added[0].get("id") or added[0].get("name")
                        if isinstance(added[0], dict) else None
                    ),
                    "authority": "authoritative_settlement_delta",
                })
                continue
            unresolved.append(f"{unclassified_field}:settlement_not_observable")
            continue
        # These two Golden Idol values are prospective scoring facts about the
        # forced follow-up choices.  They are not immediate effect claims and
        # are intentionally absent from the typed protocol consequence.  Keep
        # the exemption exact to this event and these fields; every other
        # unclassified producer field remains fail-closed.
        if (
            unclassified_field in {
                "forced_followup_costs", "forced_followup_min_cost",
            }
            and _oracle_game_id(target.get("event_id")) == "goldenidol"
            and typed_event_oracle_contract
        ):
            continue
        if unclassified_field == "original_button_index":
            wanted = target.get("original_button_index")
            if claimed_unclassified != wanted:
                contradictions.append({
                    "field": "original_button_index",
                    "claimed": claimed_unclassified,
                    "independently_expected": wanted,
                    "authority": "raw_protocol_target",
                })
            continue
        if unclassified_field == "uncertainty_classification":
            wanted = expected.get("uncertainty_classification")
            claimed_status = (
                claimed_unclassified.get("status")
                if isinstance(claimed_unclassified, dict) else None
            )
            wanted_status = (
                wanted.get("status") if isinstance(wanted, dict) else None
            )
            if claimed_status is None or wanted_status is None:
                unresolved.append(
                    "uncertainty_classification:status_not_independently_bound"
                )
            elif claimed_status != wanted_status:
                contradictions.append({
                    "field": "uncertainty_classification",
                    "claimed": claimed_unclassified,
                    "independently_expected": wanted,
                    "authority": "independent_mechanics_classification",
                })
            continue
        if unclassified_field == "potion_instance_id":
            status, held, reason = _oracle_held_potion_binding(record, target)
            wanted = (
                held.get("potion_instance_id")
                if isinstance(held, dict) else None
            )
            if status == "unknown":
                unresolved.append(f"potion_instance_id:{reason}")
            elif status == "issues" or claimed_unclassified != wanted:
                contradictions.append({
                    "field": "potion_instance_id",
                    "claimed": claimed_unclassified,
                    "independently_expected": wanted,
                    "authority": "authoritative_before_potion_inventory",
                    "reason": reason,
                })
            continue
        if unclassified_field == "bound_new_potion_id":
            status, wanted, reason = _oracle_parent_potion_transaction(
                record, unclassified_field, claimed_unclassified
            )
            if status == "unknown":
                unresolved.append(f"bound_new_potion_id:{reason}")
            elif status == "issues" or _oracle_game_id(
                claimed_unclassified
            ) != _oracle_game_id(wanted):
                contradictions.append({
                    "field": "bound_new_potion_id",
                    "claimed": claimed_unclassified,
                    "independently_expected": wanted,
                    "authority": "raw_parent_potion_transaction",
                    "reason": reason,
                })
            continue
        unresolved.append(f"unclassified:{unclassified_field}")

    aliases = {
        "current_hp_delta": "hp_delta",
        "max_hp_gain": "max_hp_delta",
        "probability_outcomes": "probabilistic_outcomes",
    }
    for producer_field, claimed in claim.items():
        field = aliases.get(producer_field, producer_field)
        if field == "acquired_benefit":
            # A shop producer may describe the benefit of every visible
            # listing, not only the selected purchase.  For an unselected
            # row, independently bind that prospective benefit to the raw
            # typed listing.  For the selected row, require the same identity
            # to appear exactly once in the authoritative after-frame delta;
            # a producer claim alone is never settlement evidence.
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            kind = str(target.get("kind") or "").strip().casefold()
            item = target.get("item")
            item = item if isinstance(item, dict) else {}
            event_id = _oracle_game_id(target.get("event_id"))
            event_card = target.get("card")
            event_card = event_card if isinstance(event_card, dict) else {}
            independently_expected_benefit = expected.get(
                "acquired_benefit"
            )
            if isinstance(independently_expected_benefit, dict):
                wanted_kind = str(
                    independently_expected_benefit.get("kind") or ""
                ).casefold()
                id_field = {
                    "card_package": "card_id",
                    "card": "card_id",
                    "relic": "relic_id",
                    "potion": "potion_id",
                }.get(wanted_kind, "id")
                wanted_id = (
                    independently_expected_benefit.get(id_field)
                    or independently_expected_benefit.get("id")
                )
                claimed_id = (
                    claimed.get(id_field) or claimed.get("id")
                    if isinstance(claimed, dict) else None
                )
                wanted_count = independently_expected_benefit.get("count")
                if (
                    not isinstance(claimed, dict)
                    or str(claimed.get("kind") or "").casefold()
                    != wanted_kind
                    or _oracle_game_id(claimed_id)
                    != _oracle_game_id(wanted_id)
                    or (
                        wanted_count is not None
                        and claimed.get("count") != wanted_count
                    )
                ):
                    contradictions.append({
                        "field": "acquired_benefit",
                        "claimed": claimed,
                        "independently_expected": independently_expected_benefit,
                        "authority": "independent_event_mechanics",
                    })
                # The canonical card/relic/potion deltas and selected-row
                # settlement are reviewed independently below.  This field is
                # only the producer's prospective package identity.
                continue
            if (
                kind == "event_option"
                and event_id == "ghosts"
                and target.get("original_button_index") == 0
                and event_card
            ):
                wanted_id = event_card.get("id") or event_card.get("card_id")
                expected_count = (
                    3
                    if int(_oracle_before_game(record).get("ascension_level") or 0) >= 15
                    else 5
                )
                wanted = {
                    "kind": "card_package", "id": wanted_id,
                    "card_id": wanted_id, "count": expected_count,
                }
                if (
                    not isinstance(claimed, dict)
                    or claimed.get("kind") != "card_package"
                    or _oracle_game_id(
                        claimed.get("id") or claimed.get("card_id")
                    ) != _oracle_game_id(wanted_id)
                    or claimed.get("count") != expected_count
                ):
                    contradictions.append({
                        "field": "acquired_benefit",
                        "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": "base_game_event_mechanics",
                    })
                    continue
                if raw.get("selected") is True:
                    observed = (
                        settlement.get("deck")
                        if isinstance(settlement, dict) else None
                    )
                    added = (
                        observed.get("added")
                        if isinstance(observed, dict) else None
                    )
                    matching = [
                        value for value in (added or [])
                        if isinstance(value, dict)
                        and _oracle_game_id(value.get("id"))
                        == _oracle_game_id(wanted_id)
                    ]
                    if (
                        not isinstance(added, list)
                        or len(added) != expected_count
                        or len(matching) != expected_count
                    ):
                        contradictions.append({
                            "field": "acquired_benefit",
                            "claimed": claimed,
                            "independently_expected": wanted,
                            "authority": "authoritative_settlement_delta",
                            "reason": "selected_event_benefit_delta_mismatch",
                        })
                continue
            if kind not in {"card", "relic", "potion"} or not item:
                unresolved.append("acquired_benefit:not_protocol_visible")
                continue
            id_field = {
                "card": "card_id", "relic": "relic_id",
                "potion": "potion_id",
            }[kind]
            wanted_id = item.get("id") or item.get(id_field)
            wanted = {"kind": kind, "id": wanted_id, id_field: wanted_id}
            if kind == "card" and item.get("card_instance_id") is not None:
                wanted["card_instance_id"] = item["card_instance_id"]
            if not isinstance(claimed, dict) or str(
                claimed.get("kind") or ""
            ).casefold() != kind or _oracle_game_id(
                claimed.get("id") or claimed.get(id_field)
            ) != _oracle_game_id(wanted_id):
                contradictions.append({
                    "field": "acquired_benefit",
                    "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_typed_shop_listing",
                })
                continue
            if kind == "card" and item.get("card_instance_id") is not None and (
                str(claimed.get("card_instance_id") or "")
                != str(item.get("card_instance_id"))
            ):
                contradictions.append({
                    "field": "acquired_benefit",
                    "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_typed_shop_listing",
                    "reason": "card_instance_id_mismatch",
                })
                continue
            if raw.get("selected") is True:
                collection = {"card": "deck", "relic": "relics", "potion": "potions"}[kind]
                observed = settlement.get(collection) if isinstance(settlement, dict) else None
                added = observed.get("added") if isinstance(observed, dict) else None
                if not isinstance(added, list):
                    unresolved.append("acquired_benefit:settlement_not_observable")
                    continue
                matches = [
                    value for value in added
                    if isinstance(value, dict)
                    and _oracle_game_id(
                        value.get("id") or value.get(id_field)
                    ) == _oracle_game_id(wanted_id)
                ]
                if len(matches) != 1:
                    contradictions.append({
                        "field": "acquired_benefit",
                        "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": "authoritative_settlement_delta",
                        "reason": "selected_shop_benefit_delta_mismatch",
                    })
            continue
        relic_identity_known, wanted_relic_id = (
            independently_expected_event_relic(field)
        )
        if relic_identity_known:
            if _oracle_game_id(claimed) != _oracle_game_id(wanted_relic_id):
                contradictions.append({
                    "field": producer_field,
                    "claimed": claimed,
                    "independently_expected": wanted_relic_id,
                    "authority": "independent_event_mechanics",
                })
            continue
        if (
            field == "relic_id"
            and _oracle_game_id(target.get("event_id")) == "goldenidol"
            and isinstance(settlement, dict)
        ):
            relic_delta = settlement.get("relics")
            added = (
                relic_delta.get("added")
                if isinstance(relic_delta, dict) else None
            )
            if isinstance(added, list) and len(added) == 1:
                wanted = added[0].get("id") or added[0].get("name")
                if _oracle_game_id(claimed) != _oracle_game_id(wanted):
                    contradictions.append({
                        "field": producer_field,
                        "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": "authoritative_settlement_delta",
                    })
                continue
            unresolved.append(f"{producer_field}:settlement_not_observable")
            continue
        if (
            field == "leave"
            and _oracle_game_id(target.get("event_id")) == "goldenidol"
        ):
            screen = _oracle_before_game(record).get("screen_state")
            screen = screen if isinstance(screen, dict) else {}
            options = screen.get("options")
            option_count = len(options) if isinstance(options, list) else None
            choice_index = target.get(
                "original_button_index", raw.get("choice_index")
            )
            wanted = bool(
                (option_count == 2 and choice_index == 1)
                or (option_count == 1 and choice_index == 0)
            )
            if bool(claimed) != wanted:
                contradictions.append({
                    "field": producer_field,
                    "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "independent_golden_idol_event_table",
                })
            continue
        if field in {
            "mechanism_id", "neow_contract", "reward_kind",
            "drawback_kind", "parameters", "random_effects",
        }:
            if field not in expected:
                unresolved.append(f"{producer_field}:not_protocol_visible")
            elif (
                field == "random_effects"
                and _shining_light_random_effect_alias(
                    record, raw, claimed, expected
                )
            ):
                continue
            elif _freeze(claimed) != _freeze(expected.get(field)):
                contradictions.append({
                    "field": producer_field, "claimed": claimed,
                    "independently_expected": expected.get(field),
                    "authority": "independent_neow_mechanics",
                })
            continue
        if field in {
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
            "probabilistic_outcomes", "current_cost", "future_costs",
        }:
            known, independently_expected, authority = _expected_or_settled_value(
                field, expected, settlement
            )
            if not known:
                unresolved.append(f"{producer_field}:not_independently_classified")
            elif (
                field == "future_costs"
                and _grid_remove_purge_claim_alias(
                    record, raw, claimed, independently_expected
                )
            ):
                # The JAR/bridge calls a card removal committed from the Neow
                # follow-up screen ``grid_purge``.  The independent oracle
                # names the same typed parent operation ``grid_remove``.  The
                # alias is accepted only with the exact accepted Neow parent
                # context and only after all other future-cost fields compare.
                continue
            elif not _effect_claim_equal(field, claimed, independently_expected):
                contradictions.append({
                    "field": producer_field, "claimed": claimed,
                    "independently_expected": independently_expected,
                    "authority": authority,
                })
            continue
        if field in {"route", "event_id", "campfire_option", "key_changes"}:
            if field not in expected:
                unresolved.append(f"{producer_field}:not_protocol_visible")
            elif (
                field == "event_id"
                and _oracle_game_id(claimed)
                == _oracle_game_id(expected.get(field))
            ):
                continue
            elif field == "event_id" and _neow_event_id_claim_alias(
                record, raw, claimed, expected.get(field)
            ):
                continue
            elif _freeze(claimed) != _freeze(expected.get(field)):
                contradictions.append({
                    "field": producer_field, "claimed": claimed,
                    "independently_expected": expected.get(field),
                    "authority": "raw_protocol_target",
                })
            continue
        if field == "operation":
            independently_expected_operation = expected.get("operation")
            if (
                isinstance(independently_expected_operation, str)
                and independently_expected_operation
            ):
                if _grid_remove_purge_claim_alias(
                    record, raw, claimed, independently_expected_operation
                ):
                    continue
                if str(claimed or "").casefold() != (
                    independently_expected_operation.casefold()
                ):
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": independently_expected_operation,
                        "authority": "independent_protocol_mechanics",
                    })
                continue
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            visible_options = record.get("available_options_before")
            if (
                _oracle_game_id(target.get("event_id"))
                in {"matchandkeep", "matchkeep"}
                and isinstance(visible_options, list)
                and len(visible_options) > 1
            ):
                if str(claimed or "").casefold() != "flip_match_position":
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": "flip_match_position",
                        "authority": "independent_match_board_surface",
                    })
                continue
            if expected.get("operation") in {
                "neow_reward", "neow_dialog_advance",
            }:
                if claimed != expected.get("operation"):
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": expected.get("operation"),
                        "authority": "independent_neow_mechanics",
                    })
                continue
            if (
                str(record.get("phase") or "").upper() == "GRID"
                and expected.get("operation") in {
                    "grid_upgrade", "grid_transform", "grid_purge",
                }
            ):
                if claimed != expected.get("operation"):
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": expected.get("operation"),
                        "authority": "authoritative_grid_context",
                    })
                continue
            if (
                str(record.get("phase") or "").upper() == "HAND_SELECT"
                and expected.get("operation") == "hand_select_card"
            ):
                if claimed != "hand_select_card":
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": "hand_select_card",
                        "authority": "authoritative_hand_select_context",
                    })
                continue
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            target_kind = str(target.get("kind") or "").casefold()
            known_operations = {
                "purge": "open_card_purge_grid",
            }
            wanted = known_operations.get(target_kind)
            if target_kind == "potion_resource":
                wanted = str(target.get("operation") or "discard").casefold()
                normalized_claim = str(claimed or "").casefold()
                if normalized_claim.startswith("discard_"):
                    normalized_claim = "discard"
                elif normalized_claim.startswith("use_"):
                    normalized_claim = "use"
                if normalized_claim != wanted:
                    contradictions.append({
                        "field": "operation", "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": "protocol_action_semantics",
                    })
                continue
            if wanted is None:
                unresolved.append("operation:not_independently_classified")
            elif claimed != wanted:
                contradictions.append({
                    "field": "operation", "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "protocol_action_semantics",
                })
            continue
        if field in {
            "reward_type", "relic_id", "rest_option", "card_id",
            "card_instance_id",
        }:
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            reward = (
                target.get("reward")
                if isinstance(target.get("reward"), dict) else {}
            )
            relic = (
                target.get("relic")
                if isinstance(target.get("relic"), dict) else {}
            )
            item = (
                target.get("item")
                if isinstance(target.get("item"), dict) else {}
            )
            card = (
                target.get("card")
                if isinstance(target.get("card"), dict) else {}
            )
            reward_relic = (
                reward.get("relic")
                if isinstance(reward.get("relic"), dict) else {}
            )
            wanted = {
                "reward_type": (
                    reward.get("reward_type")
                    or ("SAPPHIRE_KEY" if str(
                        target.get("kind") or ""
                    ).casefold() == "sapphire_key" else None)
                ),
                "relic_id": (
                    relic.get("id") or item.get("id")
                    or reward_relic.get("id")
                    or expected.get("relic_id")
                ),
                "rest_option": target.get("rest_option"),
                "card_id": card.get("id") or item.get("id"),
                "card_instance_id": (
                    target.get("card_instance_id")
                    or card.get("card_instance_id")
                    or item.get("card_instance_id")
                ),
            }[field]
            if wanted is None:
                unresolved.append(f"{field}:not_protocol_visible")
            elif field in {"reward_type", "relic_id", "card_id"}:
                normalize = (
                    _oracle_card_id if field == "card_id"
                    else _oracle_game_id
                )
                if normalize(claimed) != normalize(wanted):
                    contradictions.append({
                        "field": field, "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": "raw_protocol_target",
                    })
            elif claimed != wanted:
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_protocol_target",
                })
            continue
        if field == "selected_card":
            if "selected_card" not in expected:
                unresolved.append("selected_card:not_protocol_visible")
            elif not _effect_selected_card_equal(
                claimed, expected.get("selected_card")
            ):
                contradictions.append({
                    "field": "selected_card", "claimed": claimed,
                    "independently_expected": expected.get("selected_card"),
                    "authority": (
                        "authoritative_hand_select_context"
                        if str(record.get("phase") or "").upper()
                        == "HAND_SELECT"
                        else "authoritative_grid_context"
                    ),
                })
            continue
        if field in {"potion_id", "potion_instance_id", "potion_slot"}:
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            if str(target.get("kind") or "").casefold() == "potion_resource":
                status, held, reason = _oracle_held_potion_binding(
                    record, target
                )
                wanted = held.get(field) if isinstance(held, dict) else None
            elif field == "potion_id":
                status, wanted, reason = _oracle_reward_potion_binding(
                    record, raw
                )
            else:
                status, wanted, reason = (
                    "unknown", None, "potion_resource_target_missing"
                )
            if status == "unknown":
                unresolved.append(f"{field}:{reason}")
            elif status == "issues":
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "authoritative_before_inventory_or_reward",
                    "reason": reason,
                })
            elif (
                _oracle_game_id(claimed) != _oracle_game_id(wanted)
                if field == "potion_id" else claimed != wanted
            ):
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "authoritative_before_inventory_or_reward",
                })
            continue
        if field in {
            "bound_purchase_potion_id", "bound_reward_potion_id",
            "bound_new_potion_id",
        }:
            status, wanted, reason = _oracle_parent_potion_transaction(
                record, field, claimed
            )
            if status == "unknown":
                unresolved.append(f"{field}:{reason}")
            elif status == "issues":
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_parent_potion_transaction",
                    "reason": reason,
                })
            elif _oracle_game_id(claimed) != _oracle_game_id(wanted):
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_parent_potion_transaction",
                })
            continue
        if field in {
            "bound_purchase_listing_id", "bound_purchase_choice_index",
            "bound_purchase_item_id", "bound_purchase_price",
        }:
            status, listing, reason = _oracle_bound_shop_listing(
                record, raw_claim
            )
            wanted = listing.get(field) if isinstance(listing, dict) else None
            if status == "unknown":
                unresolved.append(f"{field}:{reason}")
            elif status == "issues":
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_parent_shop_listing",
                    "reason": reason,
                })
            elif (
                _oracle_game_id(claimed) != _oracle_game_id(wanted)
                if field == "bound_purchase_item_id"
                else claimed != wanted
            ):
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": "raw_parent_shop_listing",
                })
            continue
        if field == "deck_size_delta":
            known, changes, authority = _expected_or_settled_value(
                "card_changes", expected, settlement
            )
            if not known or not isinstance(changes, dict):
                unresolved.append("deck_size_delta:not_independently_classified")
            else:
                wanted = len(changes.get("gain") or []) - len(
                    changes.get("remove") or []
                )
                if claimed != wanted:
                    contradictions.append({
                        "field": field, "claimed": claimed,
                        "independently_expected": wanted,
                        "authority": authority,
                    })
            continue
        if field == "price":
            cost = expected.get("current_cost")
            knowledge = (expected.get("field_knowledge") or {}).get(
                "current_cost"
            )
            if not isinstance(knowledge, dict) or knowledge.get("status") != "known":
                unresolved.append("price:not_independently_classified")
            elif claimed != cost.get("gold"):
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": cost.get("gold"),
                    "authority": "protocol_target",
                })
            continue
        if field == "post_purchase_gold":
            before_gold = _finite_number(_oracle_before_game(record).get("gold"))
            known, delta, authority = _expected_or_settled_value(
                "gold_delta", expected, settlement
            )
            if before_gold is None or not known or _finite_number(delta) is None:
                unresolved.append(
                    "post_purchase_gold:not_independently_classified"
                )
            elif _finite_number(claimed) != before_gold + float(delta):
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": before_gold + float(delta),
                    "authority": authority,
                })
            continue
        if field == "leave":
            if forced_single_event_option:
                # There is no competing strategic action. The authoritative
                # transition is audited separately, so localized Continue vs
                # Leave wording cannot make the consequence review unknown.
                continue
            action = str(raw.get("action") or "").casefold()
            target = raw.get("target") if isinstance(raw, dict) else {}
            target = target if isinstance(target, dict) else {}
            typed_leave = target.get("leave")
            if action in {"return", "proceed"}:
                wanted = True
            elif type(typed_leave) is bool:
                wanted = typed_leave
            elif type(expected.get("leave")) is bool:
                wanted = expected["leave"]
            elif (
                _oracle_game_id(target.get("event_id"))
                in {"matchandkeep", "matchkeep"}
                and row.get("selected") is True
                and str(record.get("phase") or "").upper() == "EVENT"
                and isinstance(
                    record.get("authoritative_state_after"), dict
                )
                and str(record["authoritative_state_after"].get(
                    "phase"
                ) or "").upper() == "MAP"
            ):
                wanted = True
            else:
                unresolved.append("leave:not_independently_classified")
                continue
            if claimed is not wanted:
                contradictions.append({
                    "field": field, "claimed": claimed,
                    "independently_expected": wanted,
                    "authority": (
                        "protocol_command"
                        if action in {"return", "proceed"}
                        else "typed_protocol_target"
                    ),
                })
            continue
        unresolved.append(f"{producer_field}:not_independently_classified")
    return contradictions, sorted(set(unresolved))


def _valid_independent_consequence_evidence(
    record, choice_id, producer_evidence
):
    """Never accept an evidence label emitted inside the production trace.

    A trace field called ``independent_consequence_evidence`` has the same
    writer and trust boundary as ``producer_evidence``.  It may be useful to a
    human reviewer, but it cannot independently clear this oracle.  Actual
    verification is recomputed below from bound before/after protocol states
    and immutable terminal artifacts.
    """

    del record, choice_id, producer_evidence
    return False


def _state_envelope(record, side, expected, boundary_start=None):
    """Return a fully bound authoritative state, plus missing/mismatch facts."""

    state = record.get(f"authoritative_state_{side}")
    sequence_field = "before_seq" if side == "before" else "after_seq"
    wanted_seq = record.get(sequence_field)
    missing = []
    mismatches = []
    if not isinstance(state, dict):
        return None, [f"authoritative_state_{side}"], mismatches
    if state.get("protocol_version") != 2:
        mismatches.append({
            "field": "protocol_version", "expected": 2,
            "observed": state.get("protocol_version"),
        })
    if type(wanted_seq) is not int:
        missing.append(sequence_field)
    elif type(state.get("state_seq")) is not int:
        missing.append("state_seq")
    elif state.get("state_seq") != wanted_seq:
        mismatches.append({
            "field": "state_seq", "expected": wanted_seq,
            "observed": state.get("state_seq"),
        })
    for field in ATTEMPT_BINDING_FIELDS:
        # Consequence expectations intentionally contain only mechanism
        # fields; the authoritative envelope binding lives on the record.
        # Fall back to that bound record value instead of treating every
        # selected settlement as unverifiable merely because the mechanism
        # table does not duplicate attempt metadata.
        wanted = expected.get(field)
        if wanted is None:
            wanted = record.get(field)
        observed = state.get(field)
        if wanted is None or observed is None:
            missing.append(field)
        elif type(observed) is not type(wanted) or observed != wanted:
            mismatches.append({
                "field": field, "expected": wanted, "observed": observed,
            })
    if not isinstance(state.get("game_state"), dict):
        missing.append("game_state")
    # The controller can attach its run binding only after receiving the very
    # first Neow frame.  Accept that one pre-binding frame only when the
    # controller_start receipt names its exact state sequence, every binding
    # field on both receipts agrees, every corresponding state field is null,
    # and the game payload independently agrees on seed/class/ascension.  This
    # is a one-frame bootstrap proof, not a general allowance for null
    # authoritative envelopes.
    game = state.get("game_state") if isinstance(state, dict) else None
    boundary = boundary_start if isinstance(boundary_start, dict) else {}
    bootstrap_binding = bool(
        side == "before"
        and str(record.get("phase") or "").upper() == "NEOW"
        and type(wanted_seq) is int
        and boundary.get("record_type") == "controller_start"
        and boundary.get("started_state_seq") == wanted_seq
        and all(
            state.get(field) is None
            and record.get(field) is not None
            and boundary.get(field) == record.get(field) == expected.get(field)
            for field in ATTEMPT_BINDING_FIELDS
        )
        and isinstance(game, dict)
        and game.get("seed") == expected.get("seed")
        and str(game.get("class") or "").upper()
        == str(expected.get("character") or "").upper()
        and game.get("ascension_level") == expected.get("ascension_level")
    )
    if bootstrap_binding:
        missing = [
            field for field in missing if field not in ATTEMPT_BINDING_FIELDS
        ]
        reconstructed = _oracle_clone(state)
        for field in ATTEMPT_BINDING_FIELDS:
            reconstructed[field] = record[field]
        state = reconstructed
    return (
        state if not missing and not mismatches else None,
        sorted(set(missing)),
        mismatches,
    )


def _game_from_state(state):
    if not isinstance(state, dict):
        return None
    game = state.get("game_state")
    return game if isinstance(game, dict) else None


def _observable_from_authoritative_state(state):
    game = _game_from_state(state)
    if not isinstance(game, dict):
        return None
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    player = combat.get("player")
    player = player if isinstance(player, dict) else {}
    keys = game.get("keys")
    if not isinstance(keys, dict):
        keys = {
            "ruby": game.get("has_ruby_key"),
            "emerald": game.get("has_emerald_key"),
            "sapphire": game.get("has_sapphire_key"),
        }
    block = game.get("block", player.get("block"))
    if block is None and game.get("room_phase") != "COMBAT":
        block = 0
    return {
        "current_hp": game.get("current_hp", player.get("current_hp")),
        "max_hp": game.get("max_hp", player.get("max_hp")),
        "gold": game.get("gold"),
        "block": block,
        "deck": game.get("deck"),
        "relics": game.get("relics"),
        "potions": game.get("potions"),
        "keys": keys,
    }


# Independently maintained base-game mechanism contracts.  These constants
# come from the installed game's bytecode, not from production planner scores
# or reasons.  The raw protocol state is sufficient to refute every transition
# below: CARD_REWARD/COMBAT_REWARD expose their reward groups, relics expose
# counters, and StasisPower exposes its held card while combat exposes limbo
# and all piles.
BASE_GAME_MECHANISM_CONTRACT_VERSION = 2
_ORACLE_CURSE_CARD_IDS = {
    "ascendersbane", "callingbell", "curseofthebell", "clumsy",
    "decay", "doubt", "injury", "necronomicurse", "normality",
    "pain", "parasite", "pride", "regret", "shame", "writhe",
}
_ORACLE_BLACK_STAR_IDS = {"blackstar"}
_ORACLE_REWARD_COUNT_RELICS = {
    "bustedcrown": -2,
    "questioncard": 1,
}
_ORACLE_STASIS_POWER_IDS = {"stasis", "stasispower"}
_ORACLE_STASIS_RARITY_PRIORITY = {
    "RARE": 3, "UNCOMMON": 2, "COMMON": 1,
}


def _oracle_unique_relic(game, relic_id):
    wanted = _oracle_game_id(relic_id)
    matches = [
        relic for relic in ((game or {}).get("relics") or [])
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name")) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _oracle_selected_option(record):
    selected = record.get("selected_choice_ids")
    if not isinstance(selected, list) or len(selected) != 1:
        return None
    wanted = str(selected[0])
    matches = [
        option for option in (record.get("available_options_before") or [])
        if isinstance(option, dict)
        and str(option.get("option_id")) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _oracle_card_reward_source(records, record_index):
    """Return a bytecode-covered reward generator, or ``None``.

    Fixed event lists can open the same UI without calling either
    ``getRewardCards`` method.  Walk only the immediately preceding decision
    chain, through repeated CARD_REWARD screens, and fail closed for every
    other source.
    """

    expected_after_seq = records[record_index].get("before_seq")
    if type(expected_after_seq) is not int:
        return None
    for previous_index in range(record_index - 1, -1, -1):
        previous = records[previous_index]
        if not isinstance(previous, dict):
            continue
        if previous.get("record_type") != "decision":
            continue
        if previous.get("after_seq") != expected_after_seq:
            return None
        phase = str(previous.get("phase") or "").upper()
        if phase == "CARD_REWARD":
            expected_after_seq = previous.get("before_seq")
            if type(expected_after_seq) is not int:
                return None
            continue
        selected = _oracle_selected_option(previous)
        target = (
            selected.get("target")
            if isinstance(selected, dict)
            and isinstance(selected.get("target"), dict)
            else {}
        )
        if phase == "COMBAT_REWARD":
            reward = target.get("reward")
            reward = reward if isinstance(reward, dict) else {}
            if (
                str(target.get("kind") or "").casefold() == "reward"
                and str(reward.get("reward_type") or "").upper() == "CARD"
            ):
                return "character_reward_cards"
            return None
        if phase == "EVENT" and _oracle_game_id(
            target.get("event_id")
        ) == "sensorystone":
            return "colorless_reward_cards"
        if (
            phase == "REST"
            and str(target.get("rest_option") or "").upper() == "REST"
            and _oracle_unique_relic(
                _oracle_before_game(previous), "DreamCatcher"
            ) is not None
        ):
            return "dream_catcher_reward_cards"
        nested_relic = target.get("relic")
        if not isinstance(nested_relic, dict):
            item = target.get("item")
            nested_relic = item if isinstance(item, dict) else {}
        if _oracle_game_id(
            nested_relic.get("id") or nested_relic.get("name")
        ) == "orrery":
            return "orrery_reward_cards"
        if phase == "NEOW":
            contract = target.get("neow_contract")
            contract = contract if isinstance(contract, dict) else {}
            reward_kind = contract.get("reward_kind")
            if reward_kind in {"RANDOM_COLORLESS", "RANDOM_COLORLESS_2"}:
                return "colorless_reward_cards"
            if reward_kind in {"THREE_CARDS", "THREE_RARE_CARDS"}:
                return "character_reward_cards"
        return None
    return None


def _oracle_black_star_encounter_key(record, game):
    """Return the smallest stable key for one combat reward group."""

    state = (
        record.get("authoritative_state_before")
        if isinstance(record, dict) else {}
    )
    state = state if isinstance(state, dict) else {}
    return (
        record.get("attempt_id") or state.get("attempt_id"),
        record.get("run_id") or state.get("run_id"),
        game.get("act"), game.get("floor"),
    )


def _oracle_black_star_reward_origin(records, record_index, before_game):
    """Classify an initial elite reward screen from authoritative facts.

    A repeated COMBAT_REWARD frame has already had one or more rewards
    removed, so its remaining relic count cannot prove Black Star.  Require a
    contiguous transition from the final combat decision for the initial
    count and identify later reward/card screens as continuations.
    """

    room_type = _oracle_game_id(before_game.get("room_type"))
    if room_type in {"monsterroom", "monsterroomboss"}:
        return "not_applicable", {"room_type": before_game.get("room_type")}
    if room_type != "monsterroomelite":
        return "unknown", {
            "reason": "authoritative_elite_room_type_missing",
            "room_type": before_game.get("room_type"),
        }

    previous_index, previous = _previous_decision_record(
        records, record_index
    )
    if (
        previous is None
        or previous.get("after_seq") != records[record_index].get("before_seq")
    ):
        return "unknown", {
            "reason": "contiguous_pre_reward_decision_missing",
            "room_type": before_game.get("room_type"),
        }
    previous_phase = str(previous.get("phase") or "").upper()
    if previous_phase in {"COMBAT_REWARD", "CARD_REWARD", "SAPPHIRE_KEY"}:
        return "continued", {
            "previous_record_index": previous_index,
            "previous_phase": previous_phase,
        }
    if not previous_phase.startswith("COMBAT_TURN_"):
        return "unknown", {
            "reason": "pre_reward_phase_is_not_combat",
            "previous_record_index": previous_index,
            "previous_phase": previous_phase,
        }

    previous_before = _oracle_before_game(previous)
    previous_after = _game_from_state(
        previous.get("authoritative_state_after")
    ) or {}
    previous_room_types = {
        _oracle_game_id(game.get("room_type"))
        for game in (previous_before, previous_after)
        if isinstance(game, dict) and game.get("room_type") is not None
    }
    if "monsterroomelite" not in previous_room_types:
        return "unknown", {
            "reason": "pre_reward_elite_binding_missing",
            "previous_record_index": previous_index,
            "previous_phase": previous_phase,
            "previous_room_types": sorted(previous_room_types),
        }
    return "initial", {
        "previous_record_index": previous_index,
        "previous_phase": previous_phase,
        "room_type": before_game.get("room_type"),
    }


def _oracle_relic_reward_binding(reward):
    """Bind one raw RELIC reward without trusting container order."""

    if not isinstance(reward, dict):
        return None
    if str(reward.get("reward_type") or "").upper() != "RELIC":
        return None
    choice_index = reward.get("choice_index")
    relic = reward.get("relic")
    relic = relic if isinstance(relic, dict) else {}
    relic_id = _oracle_game_id(
        relic.get("id") or relic.get("relic_id") or relic.get("name")
    )
    if type(choice_index) is not int or not relic_id:
        return None
    return choice_index, relic_id


def _oracle_is_curse(card):
    if not isinstance(card, dict):
        return False
    if str(card.get("type") or "").upper() == "CURSE":
        return True
    if str(card.get("rarity") or "").upper() in {"CURSE", "SPECIAL_CURSE"}:
        return True
    return _oracle_game_id(card.get("id")) in _ORACLE_CURSE_CARD_IDS


def _oracle_known_curse_attempt(record):
    """Return an exact attempted-Curse count from raw selected protocol data."""

    selected = _oracle_selected_option(record)
    target = (
        selected.get("target")
        if isinstance(selected, dict)
        and isinstance(selected.get("target"), dict)
        else {}
    )
    phase = str(record.get("phase") or "").upper()
    if phase == "NEOW":
        contract, error, contradiction = _oracle_validate_neow_contract(
            target, record
        )
        if contract is None:
            return None, error, contradiction
        return (
            1 if contract.get("drawback_kind") == "CURSE" else 0,
            None,
            False,
        )
    if phase == "BOSS_REWARD":
        relic = target.get("relic")
        relic = relic if isinstance(relic, dict) else {}
        return (
            1 if _oracle_game_id(relic.get("id") or relic.get("name"))
            == "callingbell" else 0,
            None,
            False,
        )
    if phase == "CARD_REWARD":
        card = target.get("card")
        card = card if isinstance(card, dict) else {}
        return (1 if _oracle_is_curse(card) else 0, None, False)
    if phase == "CHEST":
        # Cursed Key adds exactly one Curse when a non-boss chest is opened.
        # CHEST is the protocol's non-boss chest surface; proceeding without
        # selecting the typed chest target does not attempt the Curse.
        selected_kind = str(target.get("kind") or "").casefold()
        owns_cursed_key = any(
            isinstance(relic, dict)
            and _oracle_game_id(relic.get("id") or relic.get("name"))
            == "cursedkey"
            for relic in (_oracle_before_game(record).get("relics") or [])
        )
        if selected_kind == "chest":
            return (1 if owns_cursed_key else 0, None, False)
        if selected_kind in {"protocol_action", ""}:
            return (0, None, False)
        return None, "chest_target_kind_unclassified", False
    if phase == "EVENT":
        if (
            _oracle_game_id(target.get("event_id"))
            == _ORACLE_MAUSOLEUM_EVENT_ID
            and selected.get("choice_index") == 0
        ):
            evidence = mausoleum_realized_settlement(record)
            realized = evidence.get("realized_settlement")
            curse = (
                realized.get("curse") if isinstance(realized, dict) else None
            )
            if evidence.get("status") == "clear" and isinstance(realized, dict):
                if realized.get("operation") == "mausoleum_dialog_advance_noop":
                    return 0, None, False
                if isinstance(curse, dict):
                    return (
                        1 if curse.get("attempted") is True else 0,
                        None, False,
                    )
            return (
                None,
                evidence.get("reason") or "mausoleum_settlement_unproven",
                evidence.get("status") == "issues",
            )
        explicit_cards = []
        if isinstance(target.get("card"), dict):
            explicit_cards.append(target["card"])
        if isinstance(target.get("cards"), list):
            explicit_cards.extend(
                card for card in target["cards"] if isinstance(card, dict)
            )
        if explicit_cards:
            return (
                sum(1 for card in explicit_cards if _oracle_is_curse(card)),
                None,
                False,
            )
    if (
        phase.startswith("COMBAT_TURN")
        and str(record.get("action") or "").casefold() == "end"
    ):
        monsters = (
            (_oracle_before_game(record).get("combat_state") or {}).get(
                "monsters"
            ) or []
        )
        writhing_mass_implant = any(
            isinstance(monster, dict)
            and _oracle_game_id(monster.get("id") or monster.get("name"))
            == "writhingmass"
            and str(monster.get("intent") or "").upper()
            == "STRONG_DEBUFF"
            and int(monster.get("current_hp") or 0) > 0
            and monster.get("is_gone") is not True
            and monster.get("half_dead") is not True
            for monster in monsters
        )
        if writhing_mass_implant:
            # Implant attempts to add exactly one Parasite.  Omamori may
            # consume the attempt, leaving no deck delta but one fewer charge.
            return 1, None, False
    return None, "curse_attempt_not_independently_classified", False


def _oracle_omamori_expected(counter_before, attempted_curses):
    if (
        type(counter_before) is not int or counter_before < 0
        or type(attempted_curses) is not int or attempted_curses < 0
    ):
        return None
    consumed = min(counter_before, attempted_curses)
    return {
        "counter_after": counter_before - consumed,
        "charges_consumed": consumed,
        "curses_added": attempted_curses - consumed,
    }


def _oracle_card_uuid(card):
    if not isinstance(card, dict):
        return None
    value = card.get("card_instance_id", card.get("uuid"))
    return str(value) if value is not None and str(value) else None


def _oracle_card_binding(card):
    if not isinstance(card, dict):
        return None
    return (
        _oracle_card_uuid(card),
        _oracle_game_id(card.get("id")),
        card.get("upgrades"),
        str(card.get("rarity") or "").upper(),
    )


def _oracle_combat_piles(game):
    combat = (game or {}).get("combat_state")
    if not isinstance(combat, dict):
        return None, "combat_state_missing"
    names = ("hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo")
    if any(not isinstance(combat.get(name), list) for name in names):
        return None, "complete_combat_piles_missing"
    result = {}
    for name in names:
        by_uuid = {}
        for card in combat[name]:
            uuid = _oracle_card_uuid(card)
            if uuid is None or uuid in by_uuid:
                return None, f"{name}_card_uuid_missing_or_duplicate"
            by_uuid[uuid] = card
        result[name] = by_uuid
    return result, None


_ORACLE_COMBAT_CHOICE_PHASES = {"GRID", "CARD_REWARD", "HAND_SELECT"}
_ORACLE_COMBAT_CHOICE_PILES = (
    "hand", "draw_pile", "discard_pile", "exhaust_pile", "limbo",
)
_ORACLE_COMBAT_CHOICE_ACTIONS = {
    ("GRID", "BetterDiscardPileToHandAction"): "discard_to_hand",
    ("GRID", "DiscardPileToTopOfDeckAction"): "discard_to_top",
    # Secret Technique/Discovery-style combat effects expose a Grid screen
    # while moving one exact card from the draw pile into hand.  Communication
    # Mod reports this action as SkillFromDeckToHandAction; it is not a normal
    # macro grid choice and needs the same UUID-bound settlement audit as the
    # other combat selection overlays.
    ("GRID", "SkillFromDeckToHandAction"): "skill_from_deck_to_hand",
    ("CARD_REWARD", "DiscoveryAction"): "discovery_to_hand",
    ("HAND_SELECT", "GamblingChipAction"): "gambling_chip_redraw",
    ("HAND_SELECT", "ArmamentsAction"): "armaments_upgrade",
    ("HAND_SELECT", "DiscardAction"): "discard_selected_card",
    ("HAND_SELECT", "ExhaustAction"): "exhaust_selected_card",
    ("HAND_SELECT", "PutOnDeckAction"): "put_on_deck",
}


def _oracle_choice_card_container(value, *, source_field=None):
    """Return one order-insensitive UUID surface without trusting a claim."""

    present = isinstance(value, list)
    cards = value if present else []
    ids = [
        card.get("card_instance_id") if isinstance(card, dict) else None
        for card in cards
    ]
    by_uuid = {}
    errors = []
    for index, card in enumerate(cards):
        uuid = ids[index]
        if not isinstance(uuid, str) or not uuid:
            errors.append("card_instance_id_missing")
            continue
        if uuid in by_uuid:
            errors.append("card_instance_id_duplicate")
            continue
        by_uuid[uuid] = card
    snapshot = {
        "source_present": present,
        "count": len(cards),
        "card_instance_ids": ids,
    }
    if source_field is not None or not present:
        snapshot["source_field"] = source_field
    return snapshot, by_uuid, sorted(set(errors))


def _oracle_combat_choice_frame(state):
    """Rebuild a combat-choice frame solely from an authoritative envelope."""

    errors = []
    state = state if isinstance(state, dict) else {}
    game = _game_from_state(state)
    if not isinstance(game, dict):
        return {"snapshot": None, "errors": ["game_state_missing"]}
    combat = game.get("combat_state")
    combat_present = isinstance(combat, dict)
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}

    selected_field = next(
        (field for field in ("selected_cards", "selected")
         if isinstance(screen.get(field), list)),
        None,
    )
    selected_value = screen.get(selected_field) if selected_field else None
    selected, selected_by_uuid, selected_errors = (
        _oracle_choice_card_container(
            selected_value, source_field=selected_field
        )
    )
    errors.extend(f"screen_selection_{item}" for item in selected_errors)

    visible_field = next(
        (field for field in ("cards", "hand")
         if isinstance(screen.get(field), list)),
        None,
    )
    visible_value = screen.get(visible_field) if visible_field else None
    visible, visible_by_uuid, visible_errors = (
        _oracle_choice_card_container(
            visible_value, source_field=visible_field
        )
    )
    errors.extend(f"screen_visible_{item}" for item in visible_errors)

    pile_snapshots = {}
    piles_by_name = {}
    all_pile_cards = {}
    if not combat_present:
        errors.append("combat_state_missing")
        pile_snapshots = {name: None for name in _ORACLE_COMBAT_CHOICE_PILES}
    else:
        for name in _ORACLE_COMBAT_CHOICE_PILES:
            pile_value = combat.get(name)
            snapshot, by_uuid, pile_errors = _oracle_choice_card_container(
                pile_value
            )
            pile_snapshots[name] = snapshot
            piles_by_name[name] = by_uuid
            if not snapshot["source_present"]:
                errors.append(f"{name}_missing")
            errors.extend(f"{name}_{item}" for item in pile_errors)
            for uuid, card in by_uuid.items():
                if uuid in all_pile_cards:
                    errors.append("card_uuid_present_in_multiple_piles")
                else:
                    all_pile_cards[uuid] = (name, card)

    deck_value = game.get("deck")
    _deck_snapshot, deck_by_uuid, deck_errors = (
        _oracle_choice_card_container(deck_value)
    )
    if not isinstance(deck_value, list):
        errors.append("master_deck_missing")
    errors.extend(f"master_deck_{item}" for item in deck_errors)

    snapshot = {
        "state_seq": state.get("state_seq"),
        "phase": str(state.get("phase") or "").upper(),
        "room_phase": game.get("room_phase"),
        "screen_type": game.get("screen_type"),
        "current_action": game.get("current_action"),
        "combat_state_present": combat_present,
        "screen_selection": selected,
        "screen_visible_cards": visible,
        "piles": pile_snapshots,
    }
    return {
        "snapshot": snapshot,
        "game": game,
        "deck": deck_by_uuid,
        "piles": piles_by_name,
        "all_pile_cards": all_pile_cards,
        "selected": selected_by_uuid,
        "visible": visible_by_uuid,
        "errors": sorted(set(errors)),
    }


def _oracle_choice_container_mismatches(expected, observed, path):
    mismatches = []
    if not isinstance(observed, dict):
        return [{"field": path, "expected": expected, "observed": observed}]
    for field in ("source_field", "source_present", "count"):
        if field in expected and observed.get(field) != expected.get(field):
            mismatches.append({
                "field": f"{path}.{field}",
                "expected": expected.get(field),
                "observed": observed.get(field),
            })
    expected_ids = expected.get("card_instance_ids")
    observed_ids = observed.get("card_instance_ids")
    expected_typed = bool(
        isinstance(expected_ids, list)
        and all(isinstance(item, str) and item for item in expected_ids)
    )
    observed_typed = bool(
        isinstance(observed_ids, list)
        and all(isinstance(item, str) and item for item in observed_ids)
    )
    if expected_typed and observed_typed:
        expected_unique = len(set(expected_ids)) == len(expected_ids)
        same_cards = (
            set(expected_ids) == set(observed_ids)
            if expected_unique
            else Counter(expected_ids) == Counter(observed_ids)
        )
        if not same_cards or (
            expected_unique and len(set(observed_ids)) != len(observed_ids)
        ):
            mismatches.append({
                "field": f"{path}.card_instance_ids",
                "expected": sorted(expected_ids),
                "observed": sorted(observed_ids),
            })
    elif expected_ids != observed_ids:
        mismatches.append({
            "field": f"{path}.card_instance_ids",
            "expected": expected_ids,
            "observed": observed_ids,
        })
    return mismatches


def _oracle_combat_choice_claim_mismatches(expected_before, expected_after, claim):
    """Compare producer evidence as a claim; card-container order is irrelevant."""

    mismatches = []
    if not isinstance(claim, dict):
        return [{"field": "combat_choice_transition", "observed": claim}]
    for field, wanted in (
        ("schema_version", 1),
        ("authority", "authoritative_protocol_before_after"),
    ):
        if claim.get(field) != wanted:
            mismatches.append({
                "field": field, "expected": wanted,
                "observed": claim.get(field),
            })
    for side, expected in (
        ("before", expected_before), ("after", expected_after),
    ):
        observed = claim.get(side)
        if not isinstance(observed, dict):
            mismatches.append({
                "field": side, "expected": expected, "observed": observed,
            })
            continue
        for field in (
            "state_seq", "phase", "room_phase", "screen_type",
            "current_action", "combat_state_present",
        ):
            if observed.get(field) != expected.get(field):
                mismatches.append({
                    "field": f"{side}.{field}",
                    "expected": expected.get(field),
                    "observed": observed.get(field),
                })
        for field in ("screen_selection", "screen_visible_cards"):
            mismatches.extend(_oracle_choice_container_mismatches(
                expected[field], observed.get(field), f"{side}.{field}"
            ))
        observed_piles = observed.get("piles")
        if not isinstance(observed_piles, dict):
            mismatches.append({
                "field": f"{side}.piles", "expected": expected["piles"],
                "observed": observed_piles,
            })
            continue
        for name in _ORACLE_COMBAT_CHOICE_PILES:
            wanted = expected["piles"].get(name)
            actual = observed_piles.get(name)
            if wanted is None:
                if actual is not None:
                    mismatches.append({
                        "field": f"{side}.piles.{name}",
                        "expected": None, "observed": actual,
                    })
            else:
                mismatches.extend(_oracle_choice_container_mismatches(
                    wanted, actual, f"{side}.piles.{name}"
                ))
    return mismatches


def _oracle_choice_card_binding_matches(left, right):
    return _oracle_card_binding(left) == _oracle_card_binding(right)


def _oracle_choice_card_template_matches(left, right):
    """Compare card semantics while permitting a runtime-created UUID.

    Discovery's screen cards are previews.  Selecting one creates a fresh
    transient hand instance, so exact UUID equality would reject a correct
    state transition.  The new instance must nevertheless retain the offered
    card's typed identity, upgrade count, and rarity.
    """

    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        _oracle_game_id(left.get("id"))
        == _oracle_game_id(right.get("id"))
        and left.get("upgrades") == right.get("upgrades")
        and str(left.get("rarity") or "").upper()
        == str(right.get("rarity") or "").upper()
        and str(left.get("type") or "").upper()
        == str(right.get("type") or "").upper()
    )


def _oracle_choice_resolving_source_matches(master, runtime, source_id):
    """Bind an executing deck card that may carry a temporary upgrade.

    The HAND_SELECT frame is emitted while the source card is outside every
    combat pile.  Armaments may have upgraded that combat instance without
    changing the master deck, so the returning source can be either the
    master template or exactly one legal combat upgrade above it.
    """

    if not isinstance(master, dict) or not isinstance(runtime, dict):
        return False
    master_upgrades = master.get("upgrades")
    runtime_upgrades = runtime.get("upgrades")
    return bool(
        _oracle_card_uuid(master) == _oracle_card_uuid(runtime)
        and _oracle_game_id(master.get("id")) == source_id
        and _oracle_game_id(runtime.get("id")) == source_id
        and str(master.get("rarity") or "").upper()
        == str(runtime.get("rarity") or "").upper()
        and type(master_upgrades) is int
        and type(runtime_upgrades) is int
        and runtime_upgrades in {master_upgrades, master_upgrades + 1}
    )


def _oracle_choice_maps_equal(left, right):
    return bool(
        isinstance(left, dict) and isinstance(right, dict)
        and set(left) == set(right)
        and all(_oracle_choice_card_binding_matches(left[key], right[key])
                for key in left)
    )


def _oracle_choice_master_delta(before, after):
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(
            uuid for uuid in set(before) & set(after)
            if not _oracle_choice_card_binding_matches(before[uuid], after[uuid])
        ),
    }


def _oracle_choice_transient_delta(before, after):
    before_all = before.get("all_pile_cards") or {}
    after_all = after.get("all_pile_cards") or {}
    shared = set(before_all) & set(after_all)
    return {
        "added": sorted(set(after_all) - set(before_all)),
        "removed": sorted(set(before_all) - set(after_all)),
        "changed": sorted(
            uuid for uuid in shared
            if not _oracle_choice_card_binding_matches(
                before_all[uuid][1], after_all[uuid][1]
            )
        ),
        "moved": sorted((
            {
                "card_instance_id": uuid,
                "from": before_all[uuid][0],
                "to": after_all[uuid][0],
            }
            for uuid in shared
            if before_all[uuid][0] != after_all[uuid][0]
        ), key=lambda item: item["card_instance_id"]),
    }


def _oracle_combat_choice_surface_review(frame, mechanism, command):
    """Return missing evidence and concrete surface-contract contradictions."""

    unknowns = []
    issues = []
    snapshot = frame.get("snapshot") if isinstance(frame, dict) else None
    game = frame.get("game") if isinstance(frame, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(game, dict):
        return ["combat_choice_surface_missing"], []
    if snapshot["screen_visible_cards"].get("source_present") is not True:
        unknowns.append("screen_visible_cards_missing")
    screen = game.get("screen_state")
    if not isinstance(screen, dict):
        unknowns.append("screen_state_missing")
        return unknowns, issues
    if mechanism in {"discard_to_hand", "discard_to_top"}:
        if snapshot["screen_selection"].get("source_present") is not True:
            unknowns.append("screen_selection_missing")
        for field, predicate in (
            ("num_cards", lambda value: type(value) is int and value >= 0),
            ("any_number", lambda value: type(value) is bool),
            ("confirm_up", lambda value: type(value) is bool),
        ):
            if not predicate(screen.get(field)):
                unknowns.append(f"grid_{field}_missing_or_invalid")
    elif mechanism in {
        "gambling_chip_redraw", "armaments_upgrade",
        "discard_selected_card", "exhaust_selected_card", "put_on_deck",
    }:
        if snapshot["screen_selection"].get("source_present") is not True:
            unknowns.append("screen_selection_missing")
        max_cards = screen.get("max_cards")
        can_pick_zero = screen.get("can_pick_zero")
        if type(max_cards) is not int or max_cards < 0:
            unknowns.append("hand_select_max_cards_missing_or_invalid")
        elif len(frame.get("selected") or {}) > max_cards:
            issues.append("hand_select_selected_count_exceeds_max")
        if type(can_pick_zero) is not bool:
            unknowns.append("hand_select_can_pick_zero_missing")
        elif (
            str(command or "").casefold() == "proceed"
            and not frame.get("selected") and can_pick_zero is not True
        ):
            issues.append("hand_select_illegal_zero_confirm")
    return sorted(set(unknowns)), sorted(set(issues))


def _oracle_combat_choice_target(record, frame, mechanism):
    """Bind an accepted command to one authoritative visible UUID."""

    result = {"uuid": None, "card": None, "issues": [], "unknowns": []}
    command = str(record.get("action") or "").casefold()
    if command == "return":
        if mechanism != "discovery_to_hand":
            result["issues"].append("unsupported_command_for_combat_choice")
            return result
        for field in ("requested_target_id", "resolved_target_id"):
            if record.get(field) != "action:return":
                result["issues"].append(f"return_{field}_mismatch")
        for field in ("selected_choice_ids", "final_choice_ids"):
            if record.get(field) != ["action:return"]:
                result["issues"].append(f"return_{field}_mismatch")
        legal = record.get("legal_choices_before")
        if not isinstance(legal, list):
            result["unknowns"].append("return_legal_choice_missing")
            return result
        matches = [
            row for row in legal
            if isinstance(row, dict)
            and row.get("choice_id") == "action:return"
            and str(row.get("action") or "").casefold() == "return"
            and row.get("legal") is True
            and row.get("visible") is True
        ]
        if len(matches) != 1:
            result["issues"].append("return_legal_choice_not_unique")
            return result
        result["cancelled"] = True
        return result
    if command == "proceed":
        selected = record.get("selected_choice_ids")
        # A confirmation is itself the selected protocol action.  In a
        # HAND_SELECT chain the preceding card selections are independently
        # reconstructed from the authoritative screen; ``action:proceed`` is
        # therefore correct metadata here, not a selected card masquerading
        # as the command.
        if selected not in (None, [], ["action:proceed"]):
            result["issues"].append("proceed_has_selected_choice")
        for field in ("requested_target_id", "resolved_target_id"):
            value = record.get(field)
            if value not in (None, "", "action:proceed"):
                result["issues"].append(f"proceed_{field}_mismatch")
        return result
    if command != "choose":
        result["issues"].append("unsupported_command_for_combat_choice")
        return result

    requested = record.get("requested_target_id")
    resolved = record.get("resolved_target_id")
    if not isinstance(requested, str) or not requested:
        result["unknowns"].append("requested_target_id_missing")
    if not isinstance(resolved, str) or not resolved:
        result["unknowns"].append("resolved_target_id_missing")
    if requested and resolved and requested != resolved:
        result["issues"].append("requested_resolved_target_mismatch")
    wanted = resolved or requested

    options = record.get("available_options_before")
    if not isinstance(options, list):
        result["unknowns"].append("available_options_before_missing")
        return result
    matches = [
        option for option in options
        if isinstance(option, dict) and str(option.get("option_id")) == str(wanted)
    ]
    if wanted is not None and len(matches) != 1:
        result["issues"].append("accepted_option_binding_not_unique")
        return result
    if not matches:
        result["unknowns"].append("accepted_option_missing")
        return result
    option = matches[0]
    selected = record.get("selected_choice_ids")
    final = record.get("final_choice_ids")
    for field, value in (("selected_choice_ids", selected), ("final_choice_ids", final)):
        if value is None:
            result["unknowns"].append(f"{field}_missing")
        elif value != [wanted]:
            result["issues"].append(f"{field}_mismatch")
    if type(option.get("choice_index")) is not int:
        result["unknowns"].append("choice_index_missing")

    target = option.get("target")
    if not isinstance(target, dict):
        result["unknowns"].append("target_missing")
        return result
    if str(target.get("kind") or "").casefold() != "card":
        result["issues"].append("target_kind_mismatch")
        return result
    target_card = target.get("card")
    target_card = target_card if isinstance(target_card, dict) else None
    uuid = target.get("card_instance_id")
    nested_uuid = (
        target_card.get("card_instance_id") if target_card is not None else None
    )
    if not isinstance(uuid, str) or not uuid or target_card is None:
        result["unknowns"].append("target_card_uuid_or_facts_missing")
        return result
    if nested_uuid != uuid:
        result["issues"].append("target_nested_card_uuid_mismatch")
        return result
    visible_snapshot = (frame.get("snapshot") or {}).get(
        "screen_visible_cards"
    ) or {}
    if visible_snapshot.get("source_present") is not True:
        result["unknowns"].append("screen_visible_cards_missing")
        return result
    visible = frame.get("visible") or {}
    if uuid not in visible:
        result["issues"].append("target_uuid_not_visible")
        return result
    if not _oracle_choice_card_binding_matches(target_card, visible[uuid]):
        result["issues"].append("target_card_facts_mismatch")
        return result

    expected_pile = {
        "discard_to_hand": "discard_pile",
        "discard_to_top": "discard_pile",
        "skill_from_deck_to_hand": "draw_pile",
        "gambling_chip_redraw": "hand",
        "armaments_upgrade": "hand",
        "discard_selected_card": "hand",
        "exhaust_selected_card": "hand",
        "put_on_deck": "hand",
    }.get(mechanism)
    if expected_pile is not None:
        pile_snapshot = ((frame.get("snapshot") or {}).get("piles") or {}).get(
            expected_pile
        ) or {}
        if pile_snapshot.get("source_present") is not True:
            result["unknowns"].append(f"{expected_pile}_missing")
            return result
        pile = (frame.get("piles") or {}).get(expected_pile) or {}
        if uuid not in pile:
            result["issues"].append(f"target_uuid_not_in_{expected_pile}")
            return result
        if not _oracle_choice_card_binding_matches(target_card, pile[uuid]):
            result["issues"].append("target_pile_card_facts_mismatch")
            return result
    elif uuid in (frame.get("all_pile_cards") or {}):
        result["issues"].append("discovery_target_already_in_combat_piles")
        return result
    result.update({"uuid": uuid, "card": target_card})
    return result


def _oracle_choice_pair(frame):
    snapshot = frame.get("snapshot") if isinstance(frame, dict) else None
    if not isinstance(snapshot, dict):
        return None
    return snapshot.get("phase"), snapshot.get("current_action")


def _oracle_record_projection_v2(record):
    if (
        isinstance(record, dict)
        and type(record.get("audit_projection_version")) is int
        and record.get("audit_projection_version") >= 2
    ):
        return True
    options = (
        record.get("available_options_before")
        if isinstance(record, dict) else None
    )
    return bool(
        isinstance(options, list)
        and any(
            _oracle_projection_v2(option.get("target"))
            for option in options
            if isinstance(option, dict)
        )
    )


_ORACLE_CARD_CHOICE_POTION_IDS = frozenset({
    "attackpotion", "colorlesspotion", "powerpotion", "skillpotion",
})


def _oracle_discovery_potion_origin(records, record_index, expected):
    """Bind a DiscoveryAction opened directly by a card-choice potion.

    The base game reuses DiscoveryAction for Attack/Skill/Power/Colorless
    potions.  In that branch there is no resolving Discovery card to enter
    the exhaust pile: the potion is the source.  Require the immediately
    preceding authoritative decision to consume the exact requested potion
    before permitting that one-card settlement shape.
    """

    previous_index, previous = _previous_decision_record(records, record_index)
    current = records[record_index]
    if (
        previous is None
        or previous.get("after_seq") != current.get("before_seq")
        or str(previous.get("action") or "").casefold() != "potion"
    ):
        return None

    before_state, before_missing, before_mismatches = _state_envelope(
        previous, "before", expected
    )
    after_state, after_missing, after_mismatches = _state_envelope(
        previous, "after", expected
    )
    if before_missing or after_missing or before_mismatches or after_mismatches:
        return {
            "status": "unknown",
            "reason": "discovery_potion_origin_state_missing",
            "previous_record_index": previous_index,
        }

    before_game = _game_from_state(before_state) or {}
    after_game = _game_from_state(after_state) or {}
    before_potions = before_game.get("potions")
    after_potions = after_game.get("potions")
    if not isinstance(before_potions, list) or not isinstance(after_potions, list):
        return {
            "status": "unknown",
            "reason": "discovery_potion_origin_inventory_missing",
            "previous_record_index": previous_index,
        }

    requested = previous.get("resolved_target_id") or previous.get(
        "requested_target_id"
    )
    requested = str(requested) if requested not in (None, "") else None
    sources = [
        potion for potion in before_potions
        if isinstance(potion, dict)
        and str(potion.get("potion_instance_id") or "") == requested
    ]
    if len(sources) != 1:
        return {
            "status": "issues",
            "reason": "discovery_potion_origin_identity_mismatch",
            "previous_record_index": previous_index,
        }
    source = sources[0]
    source_id = _oracle_game_id(source.get("id"))
    if source_id not in _ORACLE_CARD_CHOICE_POTION_IDS:
        return {
            "status": "issues",
            "reason": "discovery_potion_origin_type_mismatch",
            "previous_record_index": previous_index,
            "potion_id": source.get("id"),
        }
    if any(
        isinstance(potion, dict)
        and str(potion.get("potion_instance_id") or "") == requested
        for potion in after_potions
    ):
        return {
            "status": "issues",
            "reason": "discovery_potion_origin_not_consumed",
            "previous_record_index": previous_index,
            "potion_id": source.get("id"),
        }
    return {
        "status": "clear",
        "reason": "discovery_card_choice_potion_consumed",
        "previous_record_index": previous_index,
        "potion_id": source.get("id"),
        "potion_instance_id": requested,
    }


def _oracle_combat_choice_settlement(
    mechanism, initial, final, selected, *, discovery_origin=None,
    discovery_cancelled=False,
):
    """Validate persistent and transient channels for one completed chain."""

    persistent_delta = _oracle_choice_master_delta(
        initial.get("deck"), final.get("deck")
    )
    transient_delta = _oracle_choice_transient_delta(initial, final)
    details = {
        "persistent_delta": persistent_delta,
        "transient_delta": transient_delta,
        "selected_card_instance_ids": sorted(selected),
    }
    if any(persistent_delta.values()):
        return "issues", "combat_choice_master_deck_changed", details
    before_all = initial.get("all_pile_cards") or {}
    after_all = final.get("all_pile_cards") or {}
    before_piles = initial.get("piles") or {}
    after_piles = final.get("piles") or {}

    if mechanism == "gambling_chip_redraw":
        if set(before_all) != set(after_all) or transient_delta["changed"]:
            return "issues", "combat_choice_transient_card_universe_changed", details
    if mechanism == "discard_to_hand":
        added = transient_delta["added"]
        resolving_source = None
        if len(added) == 1:
            resolving_uuid = added[0]
            resolving_entry = after_all.get(resolving_uuid)
            deck_card = (final.get("deck") or {}).get(resolving_uuid)
            if (
                resolving_entry is not None
                and resolving_entry[0] in {
                    "discard_pile", "exhaust_pile",
                }
                and deck_card is not None
                and _oracle_choice_resolving_source_matches(
                    deck_card, resolving_entry[1], "hologram"
                )
                and bool(resolving_entry[1].get("exhausts"))
                == (resolving_entry[0] == "exhaust_pile")
            ):
                resolving_source = {
                    "card_instance_id": resolving_uuid,
                    "pile": resolving_entry[0],
                    "card_id": resolving_entry[1].get("id"),
                }
        if (
            transient_delta["removed"]
            or transient_delta["changed"]
            or (added and resolving_source is None)
        ):
            return (
                "issues",
                "discard_to_hand_resolving_card_delta_mismatch",
                details,
            )
        details["resolving_source"] = resolving_source
        if len(selected) > 1:
            return "issues", "discard_to_hand_selection_count_mismatch", details
        if not selected:
            if initial["snapshot"]["screen_visible_cards"]["count"] != 0:
                return "issues", "discard_to_hand_zero_selection_with_options", details
            if transient_delta["moved"]:
                return "issues", "discard_to_hand_zero_selection_changed_piles", details
            return "clear", "discard_to_hand_empty_proceed_clear", details
        uuid = next(iter(selected))
        if uuid not in (before_piles.get("discard_pile") or {}):
            return "issues", "discard_to_hand_source_mismatch", details
        if uuid not in (after_piles.get("hand") or {}):
            return "issues", "discard_to_hand_destination_mismatch", details
        expected_moves = [{
            "card_instance_id": uuid,
            "from": "discard_pile", "to": "hand",
        }]
        if transient_delta["moved"] != expected_moves:
            return "issues", "discard_to_hand_extra_or_missing_movement", details
        return "clear", "discard_to_hand_settlement_clear", details

    if mechanism == "discard_to_top":
        if len(selected) != 1:
            return "issues", "discard_to_top_selection_count_mismatch", details
        uuid = next(iter(selected))
        initial_discard = before_piles.get("discard_pile") or {}
        final_discard = after_piles.get("discard_pile") or {}
        final_draw = after_piles.get("draw_pile") or {}
        if uuid not in initial_discard:
            return "issues", "discard_to_top_source_mismatch", details
        if uuid not in final_draw or uuid in final_discard:
            return "issues", "discard_to_top_destination_mismatch", details
        if not _oracle_choice_card_binding_matches(
            initial_discard[uuid], final_draw[uuid]
        ):
            return "issues", "discard_to_top_card_facts_changed", details
        draw_ids = (((final.get("snapshot") or {}).get("piles") or {}).get(
            "draw_pile"
        ) or {}).get("card_instance_ids")
        if not isinstance(draw_ids, list) or not draw_ids or draw_ids[-1] != uuid:
            return "issues", "discard_to_top_not_on_draw_pile_top", details
        expected_move = {
            "card_instance_id": uuid,
            "from": "discard_pile", "to": "draw_pile",
        }
        if transient_delta["moved"] != [expected_move]:
            return "issues", "discard_to_top_extra_or_missing_movement", details
        if transient_delta["removed"] or transient_delta["changed"]:
            return "issues", "discard_to_top_unexpected_card_delta", details
        # The Headbutt which opened this screen is resolving from the action
        # queue and normally appears in the discard pile only after the
        # selected card has been placed on top of the draw pile.
        added = transient_delta["added"]
        if len(added) != 1:
            return "issues", "discard_to_top_resolving_card_delta_mismatch", details
        resolving_uuid = added[0]
        resolving_entry = after_all.get(resolving_uuid)
        deck_card = (final.get("deck") or {}).get(resolving_uuid)
        if (
            resolving_entry is None
            or resolving_entry[0] != "discard_pile"
            or _oracle_game_id(resolving_entry[1].get("id")) != "headbutt"
            or deck_card is None
            or not _oracle_choice_card_binding_matches(
                deck_card, resolving_entry[1]
            )
        ):
            return "issues", "discard_to_top_resolving_card_unbound", details
        return "clear", "discard_to_top_settlement_clear", details

    if mechanism == "skill_from_deck_to_hand":
        # Secret Technique resolves from the action queue, so the selected
        # card is an existing draw-pile instance while the resolving source
        # card is only materialized in the exhaust pile at settlement.  Bind
        # both channels explicitly instead of treating this well-known Grid
        # overlay as an unclassified combat action.
        if len(selected) != 1:
            return "issues", "skill_from_deck_to_hand_selection_count_mismatch", details
        uuid = next(iter(selected))
        initial_draw = before_piles.get("draw_pile") or {}
        final_draw = after_piles.get("draw_pile") or {}
        initial_hand = before_piles.get("hand") or {}
        final_hand = after_piles.get("hand") or {}
        if uuid not in initial_draw:
            return "issues", "skill_from_deck_to_hand_source_mismatch", details
        if uuid not in final_hand or uuid in final_draw:
            return "issues", "skill_from_deck_to_hand_destination_mismatch", details
        if not _oracle_choice_card_binding_matches(
            initial_draw[uuid], final_hand[uuid]
        ):
            return "issues", "skill_from_deck_to_hand_card_facts_changed", details
        expected_move = {
            "card_instance_id": uuid,
            "from": "draw_pile", "to": "hand",
        }
        if transient_delta["moved"] != [expected_move]:
            return "issues", "skill_from_deck_to_hand_extra_or_missing_movement", details
        if transient_delta["removed"] or transient_delta["changed"]:
            return "issues", "skill_from_deck_to_hand_unexpected_card_delta", details
        # The source Secret Technique is created in limbo while the overlay is
        # open and is exhausted when the action resolves.  It is deliberately
        # not part of the selected draw-pile card's identity.
        added = transient_delta["added"]
        if len(added) != 1:
            return "issues", "skill_from_deck_to_hand_resolving_card_delta_mismatch", details
        resolving = after_all.get(added[0])
        if (
            resolving is None
            or resolving[0] != "exhaust_pile"
            or _oracle_game_id(resolving[1].get("id")) != "secrettechnique"
        ):
            return "issues", "skill_from_deck_to_hand_resolving_card_unbound", details
        if set(final_hand) != set(initial_hand) | {uuid}:
            return "issues", "skill_from_deck_to_hand_hand_delta_mismatch", details
        if set(final_draw) != set(initial_draw) - {uuid}:
            return "issues", "skill_from_deck_to_hand_draw_delta_mismatch", details
        return "clear", "skill_from_deck_to_hand_settlement_clear", details

    if mechanism == "discovery_to_hand":
        if len(selected) > 1:
            return "issues", "discovery_selection_count_mismatch", details
        if not selected:
            cancellation_proven = bool(
                discovery_cancelled
                and isinstance(discovery_origin, dict)
                and discovery_origin.get("status") == "clear"
            )
            if (
                initial["snapshot"]["screen_visible_cards"]["count"] != 0
                and not cancellation_proven
            ):
                return "issues", "discovery_zero_selection_with_options", details
            if any(transient_delta.values()):
                return "issues", "discovery_zero_selection_changed_piles", details
            details.update({
                "cancelled": cancellation_proven,
                "origin": discovery_origin,
            })
            return (
                "clear",
                (
                    "discovery_potion_cancel_settlement_clear"
                    if cancellation_proven
                    else "discovery_empty_proceed_clear"
                ),
                details,
            )
        preview_uuid = next(iter(selected))
        offered = (initial.get("visible") or {}).get(preview_uuid)
        added = transient_delta["added"]
        potion_origin = bool(
            isinstance(discovery_origin, dict)
            and discovery_origin.get("status") == "clear"
        )
        if len(added) != (1 if potion_origin else 2):
            return "issues", "discovery_transient_gain_identity_mismatch", details
        created = [
            uuid for uuid in added
            if after_all.get(uuid, (None,))[0] == "hand"
            and (final.get("deck") or {}).get(uuid) is None
        ]
        resolving = [
            uuid for uuid in added
            if after_all.get(uuid, (None,))[0] == "exhaust_pile"
            and _oracle_game_id(
                (after_all.get(uuid, (None, {}))[1] or {}).get("id")
            ) == "discovery"
        ]
        if len(created) != 1 or len(resolving) != (0 if potion_origin else 1):
            return "issues", "discovery_transient_gain_identity_mismatch", details
        created_uuid = created[0]
        resolving_uuid = resolving[0] if resolving else None
        created_card = after_all[created_uuid][1]
        if not potion_origin:
            resolving_card = after_all[resolving_uuid][1]
            deck_source = (final.get("deck") or {}).get(resolving_uuid)
            source_semantics_match = bool(
                _oracle_game_id(resolving_card.get("id")) == "discovery"
                and str(resolving_card.get("type") or "").upper() == "SKILL"
                and str(resolving_card.get("rarity") or "").upper() == "UNCOMMON"
                and type(resolving_card.get("upgrades")) is int
                and resolving_card.get("upgrades") >= 0
                and resolving_card.get("exhausts") is True
            )
            deck_bound_source = bool(
                source_semantics_match
                and deck_source is not None
                and _oracle_choice_card_binding_matches(
                    deck_source, resolving_card
                )
            )
            transient_source = bool(
                source_semantics_match
                and deck_source is None
            )
        else:
            deck_bound_source = transient_source = True
        if (
            created_uuid in {preview_uuid, resolving_uuid}
            or (resolving_uuid is not None and resolving_uuid == preview_uuid)
            or not (deck_bound_source or transient_source)
        ):
            return "issues", "discovery_resolving_card_unbound", details
        if (
            transient_delta["removed"] or transient_delta["changed"]
            or transient_delta["moved"]
        ):
            return "issues", "discovery_has_extra_transient_delta", details
        if not _oracle_choice_card_template_matches(
            offered, created_card
        ):
            return "issues", "discovery_card_facts_changed", details
        if created_card.get("cost") != 0:
            return "issues", "discovery_card_not_temporary_zero_cost", details
        initial_hand = before_piles.get("hand") or {}
        final_hand = after_piles.get("hand") or {}
        if set(final_hand) != set(initial_hand) | {created_uuid}:
            return "issues", "discovery_hand_delta_mismatch", details
        for name in ("draw_pile", "discard_pile", "limbo"):
            if not _oracle_choice_maps_equal(
                before_piles.get(name), after_piles.get(name)
            ):
                return "issues", f"discovery_{name}_changed", details
        initial_exhaust = before_piles.get("exhaust_pile") or {}
        final_exhaust = after_piles.get("exhaust_pile") or {}
        expected_exhaust = (
            set(initial_exhaust)
            if potion_origin
            else set(initial_exhaust) | {resolving_uuid}
        )
        if set(final_exhaust) != expected_exhaust:
            return "issues", "discovery_exhaust_delta_mismatch", details
        details.update({
            "created_card_instance_id": created_uuid,
            "resolving_card_instance_id": resolving_uuid,
            "origin": discovery_origin,
        })
        return "clear", "discovery_settlement_clear", details

    if mechanism == "armaments_upgrade":
        if len(selected) != 1:
            return "issues", "armaments_selection_count_mismatch", details
        uuid = next(iter(selected))
        initial_hand = before_piles.get("hand") or {}
        final_hand = after_piles.get("hand") or {}
        if uuid not in initial_hand or uuid not in final_hand:
            return "issues", "armaments_target_not_restored_to_hand", details
        before_card = initial_hand[uuid]
        after_card = final_hand[uuid]
        before_upgrades = before_card.get("upgrades")
        after_upgrades = after_card.get("upgrades")
        if not (
            type(before_upgrades) is int
            and type(after_upgrades) is int
            and after_upgrades == before_upgrades + 1
            and _oracle_game_id(before_card.get("id"))
            == _oracle_game_id(after_card.get("id"))
        ):
            return "issues", "armaments_upgrade_delta_mismatch", details
        if transient_delta["removed"]:
            return "issues", "armaments_removed_transient_card", details
        if transient_delta["changed"] != [uuid]:
            return "issues", "armaments_changed_wrong_transient_card", details
        # Cards already executing in the action queue may be absent from the
        # initial pile snapshot and reappear after Armaments resolves.  Every
        # such addition must be an unchanged master-deck instance.
        for added_uuid in transient_delta["added"]:
            deck_card = (final.get("deck") or {}).get(added_uuid)
            pile_entry = after_all.get(added_uuid)
            if (
                deck_card is None or pile_entry is None
                or not _oracle_choice_card_binding_matches(
                    deck_card, pile_entry[1]
                )
            ):
                return "issues", "armaments_unbound_transient_addition", details
        return "clear", "armaments_upgrade_settlement_clear", details

    if mechanism == "put_on_deck":
        if len(selected) != 1:
            return "issues", "put_on_deck_selection_count_mismatch", details
        uuid = next(iter(selected))
        initial_hand = before_piles.get("hand") or {}
        final_hand = after_piles.get("hand") or {}
        initial_draw = before_piles.get("draw_pile") or {}
        final_draw = after_piles.get("draw_pile") or {}
        if uuid not in initial_hand:
            return "issues", "put_on_deck_source_mismatch", details
        if uuid not in final_draw or uuid in final_hand:
            return "issues", "put_on_deck_destination_mismatch", details
        if not _oracle_choice_card_binding_matches(
            initial_hand[uuid], final_draw[uuid]
        ):
            return "issues", "put_on_deck_card_facts_changed", details
        draw_ids = (((final.get("snapshot") or {}).get("piles") or {}).get(
            "draw_pile"
        ) or {}).get("card_instance_ids")
        if not isinstance(draw_ids, list) or not draw_ids or draw_ids[-1] != uuid:
            return "issues", "put_on_deck_not_on_draw_pile_top", details
        expected_move = {
            "card_instance_id": uuid,
            "from": "hand", "to": "draw_pile",
        }
        if transient_delta["moved"] != [expected_move]:
            return "issues", "put_on_deck_extra_or_missing_movement", details
        if transient_delta["removed"] or transient_delta["changed"]:
            return "issues", "put_on_deck_unexpected_card_delta", details
        added = transient_delta["added"]
        resolving_sources = []
        generated_dazed = []
        for added_uuid in added:
            pile_entry = after_all.get(added_uuid)
            if pile_entry is None:
                return "issues", "put_on_deck_unexpected_card_delta", details
            pile_name, runtime_card = pile_entry
            deck_card = (final.get("deck") or {}).get(added_uuid)
            source_id = _oracle_game_id(runtime_card.get("id"))
            source_rarity = {
                "warcry": "COMMON",
                "thinkingahead": "RARE",
            }.get(source_id)
            source_semantics_match = bool(
                added_uuid != uuid
                and pile_name == "exhaust_pile"
                and source_rarity is not None
                and str(runtime_card.get("type") or "").upper() == "SKILL"
                and str(runtime_card.get("rarity") or "").upper()
                == source_rarity
                and type(runtime_card.get("upgrades")) is int
                and runtime_card.get("upgrades") >= 0
                and runtime_card.get("exhausts") is True
            )
            source_identity_bound = bool(
                source_semantics_match
                and (
                    deck_card is None
                    or _oracle_choice_card_binding_matches(
                        deck_card, runtime_card
                    )
                )
            )
            if source_identity_bound:
                resolving_sources.append(added_uuid)
                continue
            if (
                deck_card is None
                and pile_name == "draw_pile"
                and _oracle_game_id(runtime_card.get("id")) == "dazed"
                and runtime_card.get("cost") == -2
                and str(runtime_card.get("type") or "").upper() == "STATUS"
                and str(runtime_card.get("rarity") or "").upper() == "COMMON"
                and runtime_card.get("upgrades") == 0
                and runtime_card.get("exhausts") is False
            ):
                generated_dazed.append(added_uuid)
                continue
            return "issues", "put_on_deck_unexpected_card_delta", details
        if len(resolving_sources) != 1:
            return "issues", "put_on_deck_resolving_card_delta_mismatch", details
        resolving_uuid = resolving_sources[0]
        resolving_card = after_all[resolving_uuid][1]
        before_game = initial.get("game") or {}
        player = ((before_game.get("combat_state") or {}).get("player") or {})
        hex_active = any(
            isinstance(power, dict)
            and _oracle_game_id(power.get("id") or power.get("name"))
            in {"hex", "hexpower"}
            and type(power.get("amount")) is int
            and power.get("amount") > 0
            for power in player.get("powers") or []
        )
        source_is_non_attack = (
            str(resolving_card.get("type") or "").upper() != "ATTACK"
        )
        expected_dazed_count = 1 if hex_active and source_is_non_attack else 0
        if len(generated_dazed) != expected_dazed_count:
            return "issues", "put_on_deck_hex_dazed_delta_mismatch", details
        if len(added) != 1 + expected_dazed_count:
            return "issues", "put_on_deck_resolving_card_delta_mismatch", details
        if set(final_hand) != set(initial_hand) - {uuid}:
            return "issues", "put_on_deck_hand_delta_mismatch", details
        expected_draw = set(initial_draw) | {uuid} | set(generated_dazed)
        if set(final_draw) != expected_draw:
            return "issues", "put_on_deck_draw_delta_mismatch", details
        for name in ("discard_pile", "limbo"):
            if not _oracle_choice_maps_equal(
                before_piles.get(name), after_piles.get(name)
            ):
                return "issues", f"put_on_deck_{name}_changed", details
        initial_exhaust = before_piles.get("exhaust_pile") or {}
        final_exhaust = after_piles.get("exhaust_pile") or {}
        if set(final_exhaust) != set(initial_exhaust) | {resolving_uuid}:
            return "issues", "put_on_deck_exhaust_delta_mismatch", details
        if generated_dazed:
            details["generated_hex_dazed"] = generated_dazed
        return "clear", "put_on_deck_settlement_clear", details

    if mechanism == "discard_selected_card":
        screen = (initial.get("game") or {}).get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        max_cards = screen.get("max_cards")
        can_pick_zero = screen.get("can_pick_zero")
        if type(max_cards) is not int or max_cards < 0:
            return "unknown", "discard_selection_limit_missing", details
        if len(selected) > max_cards:
            return "issues", "discard_selection_count_mismatch", details
        if not selected and can_pick_zero is not True:
            return "issues", "discard_zero_selection_not_allowed", details
        initial_hand = before_piles.get("hand") or {}
        final_hand = after_piles.get("hand") or {}
        final_discard = after_piles.get("discard_pile") or {}
        expected_moves = []
        for uuid in sorted(selected):
            if uuid not in initial_hand or uuid not in final_discard:
                return (
                    "issues", "discard_selected_card_destination_mismatch",
                    details,
                )
            if not _oracle_choice_card_binding_matches(
                initial_hand[uuid], final_discard[uuid]
            ):
                return "issues", "discard_selected_card_facts_changed", details
            expected_moves.append({
                "card_instance_id": uuid,
                "from": "hand", "to": "discard_pile",
            })
        if transient_delta["moved"] != expected_moves:
            return "issues", "discard_action_unexpected_pile_movement", details
        if transient_delta["removed"] or transient_delta["changed"]:
            return "issues", "discard_action_unexpected_card_delta", details
        for added_uuid in transient_delta["added"]:
            deck_card = (final.get("deck") or {}).get(added_uuid)
            pile_entry = after_all.get(added_uuid)
            if (
                deck_card is None or pile_entry is None
                or pile_entry[0] != "discard_pile"
                or not _oracle_choice_card_binding_matches(
                    deck_card, pile_entry[1]
                )
            ):
                return "issues", "discard_action_unbound_source_card", details
        if set(final_hand) != set(initial_hand) - set(selected):
            return "issues", "discard_action_hand_delta_mismatch", details
        if set(final_discard) != (
            set(before_piles.get("discard_pile") or {})
            | set(selected)
            | set(transient_delta["added"])
        ):
            return "issues", "discard_action_discard_delta_mismatch", details
        for name in ("draw_pile", "exhaust_pile", "limbo"):
            if not _oracle_choice_maps_equal(
                before_piles.get(name), after_piles.get(name)
            ):
                return "issues", f"discard_action_{name}_changed", details
        return "clear", "discard_selected_card_settlement_clear", details


    if mechanism == "exhaust_selected_card":
        screen = (initial.get("game") or {}).get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        max_cards = screen.get("max_cards")
        can_pick_zero = screen.get("can_pick_zero")
        if type(max_cards) is not int or max_cards < 0:
            return "unknown", "exhaust_selection_limit_missing", details
        if len(selected) > max_cards:
            return "issues", "exhaust_selection_count_mismatch", details
        if not selected and can_pick_zero is not True:
            return "issues", "exhaust_zero_selection_not_allowed", details
        initial_hand = before_piles.get("hand") or {}
        final_exhaust = after_piles.get("exhaust_pile") or {}
        selected_moves = []
        for uuid in sorted(selected):
            if uuid not in initial_hand or uuid not in final_exhaust:
                return (
                    "issues", "exhaust_selected_card_destination_mismatch",
                    details,
                )
            if not _oracle_choice_card_binding_matches(
                initial_hand[uuid], final_exhaust[uuid]
            ):
                return "issues", "exhaust_selected_card_facts_changed", details
            selected_move = {
                "card_instance_id": uuid,
                "from": "hand", "to": "exhaust_pile",
            }
            if selected_move not in transient_delta["moved"]:
                return "issues", "exhaust_selected_card_move_missing", details
            selected_moves.append(selected_move)
        if transient_delta["removed"] or transient_delta["changed"]:
            return "issues", "exhaust_action_unexpected_card_delta", details
        # The card which opened this overlay is resolving from the action
        # queue and may re-enter a normal pile after confirmation. Bind every
        # such addition to the unchanged master deck instead of treating the
        # queue boundary as a created card.
        resolving_sources = []
        generated_hex_dazed = []
        for added_uuid in transient_delta["added"]:
            deck_card = (final.get("deck") or {}).get(added_uuid)
            pile_entry = after_all.get(added_uuid)
            if pile_entry is None:
                return "issues", "exhaust_action_unbound_transient_addition", details
            runtime_card = pile_entry[1]
            if deck_card is not None and (
                _oracle_choice_card_binding_matches(deck_card, runtime_card)
                or _oracle_choice_resolving_source_matches(
                    deck_card, runtime_card, "burningpact"
                )
            ):
                resolving_sources.append((added_uuid, runtime_card))
                continue
            if (
                deck_card is None
                and pile_entry[0] == "draw_pile"
                and _oracle_game_id(runtime_card.get("id")) == "dazed"
            ):
                generated_hex_dazed.append(added_uuid)
                continue
            return "issues", "exhaust_action_unbound_transient_addition", details
        other_moves = [
            move for move in transient_delta["moved"]
            if move not in selected_moves
        ]
        dark_embrace = 0
        before_game = initial.get("game") or {}
        player = ((before_game.get("combat_state") or {}).get("player") or {})
        for power in player.get("powers") or []:
            if (
                isinstance(power, dict)
                and _oracle_game_id(power.get("id") or power.get("name"))
                in {"darkembrace", "darkembracepower"}
            ):
                amount = power.get("amount")
                if type(amount) is int and amount > 0:
                    dark_embrace += amount
        # ``ExhaustAction`` is only the selection overlay.  Once the exact
        # selected card is confirmed, the card which opened the overlay keeps
        # resolving.  Burning Pact therefore draws two (three when upgraded)
        # before the next authoritative decision state.  The source card is
        # absent from the initial pile snapshot while it is resolving, then
        # reappears as a deck-bound transient addition.  Authorize only the
        # draw count carried by that exact source card; unrelated additions,
        # changed facts, destinations, or excess movement still fail closed.
        source_draw = 0
        burning_pact_sources = []
        for source_uuid, runtime_card in resolving_sources:
            if _oracle_game_id(runtime_card.get("id")) == "burningpact":
                burning_pact_sources.append(source_uuid)
                magic_number = runtime_card.get("magic_number")
                if type(magic_number) is int and magic_number > 0:
                    source_draw += magic_number
        hex_active = any(
            isinstance(power, dict)
            and _oracle_game_id(power.get("id") or power.get("name"))
            in {"hex", "hexpower"}
            and type(power.get("amount")) is int
            and power.get("amount") > 0
            for power in player.get("powers") or []
        )
        if generated_hex_dazed and not (
            hex_active
            and len(generated_hex_dazed) == 1
            and len(burning_pact_sources) == 1
        ):
            return "issues", "exhaust_action_unbound_transient_addition", details
        if generated_hex_dazed:
            details["generated_hex_dazed"] = generated_hex_dazed
        # Dark Embrace draws once per exhausted card.  A multi-select source
        # such as Elixir can therefore cause several exact draw-pile-to-hand
        # moves during the one confirmation settlement.
        allowed_draws = dark_embrace * len(selected) + source_draw
        initial_draw = before_piles.get("draw_pile") or {}
        final_draw = after_piles.get("draw_pile") or {}
        initial_discard = before_piles.get("discard_pile") or {}
        final_discard = after_piles.get("discard_pile") or {}
        final_hand = after_piles.get("hand") or {}
        initial_exhaust = before_piles.get("exhaust_pile") or {}
        final_limbo = after_piles.get("limbo") or {}
        initial_limbo = before_piles.get("limbo") or {}
        added_by_pile = {
            pile: {
                uuid for uuid in transient_delta["added"]
                if after_all.get(uuid, (None,))[0] == pile
            }
            for pile in (
                "hand", "draw_pile", "discard_pile", "exhaust_pile",
                "limbo",
            )
        }
        # A resolving selection source may only settle into a non-hand pile.
        # Generated cards (Dead Branch, Discovery, etc.) are not master-deck
        # additions and were already rejected by the exact binding above.
        if added_by_pile["hand"]:
            return "issues", "exhaust_action_source_returned_to_hand", details
        kept_hand = set(initial_hand) - set(selected)
        drawn = set(final_hand) - kept_hand
        capacity = max(0, 10 - len(kept_hand))
        draw_count = min(
            allowed_draws, capacity,
            len(initial_draw) + len(initial_discard),
        )
        permitted_moves = {
            ("draw_pile", "hand"),
            ("discard_pile", "hand"),
            ("discard_pile", "draw_pile"),
        }
        if (
            not kept_hand <= set(final_hand)
            or len(drawn) != draw_count
            or any(
                (move.get("from"), move.get("to")) not in permitted_moves
                for move in other_moves
            )
            or set(final.get("deck") or {}) != set(initial.get("deck") or {})
        ):
            return "issues", "exhaust_action_unexpected_pile_movement", details
        final_draw_core = set(final_draw) - added_by_pile["draw_pile"]
        final_discard_core = (
            set(final_discard) - added_by_pile["discard_pile"]
        )
        final_exhaust_core = (
            set(final_exhaust) - added_by_pile["exhaust_pile"]
        )
        final_limbo_core = set(final_limbo) - added_by_pile["limbo"]
        if (
            final_exhaust_core != set(initial_exhaust) | set(selected)
            or final_limbo_core != set(initial_limbo)
        ):
            return "issues", "exhaust_action_unexpected_pile_movement", details
        if len(initial_draw) >= draw_count:
            if (
                not drawn <= set(initial_draw)
                or final_draw_core != set(initial_draw) - drawn
                or final_discard_core != set(initial_discard)
            ):
                return (
                    "issues", "exhaust_action_unexpected_pile_movement",
                    details,
                )
            details["draw_branch"] = "draw_pile_only"
        else:
            discard_drawn = drawn - set(initial_draw)
            if (
                not set(initial_draw) <= drawn
                or not discard_drawn <= set(initial_discard)
                or final_draw_core != set(initial_discard) - discard_drawn
                or final_discard_core
            ):
                return (
                    "issues", "exhaust_action_unexpected_pile_movement",
                    details,
                )
            details["draw_branch"] = "discard_reshuffle"
        details["draw_count"] = draw_count
        return "clear", "exhaust_selected_card_settlement_clear", details

    if mechanism != "gambling_chip_redraw":
        return "unknown", "combat_choice_action_unclassified", details

    initial_hand = before_piles.get("hand") or {}
    final_hand = after_piles.get("hand") or {}
    initial_draw = before_piles.get("draw_pile") or {}
    final_draw = after_piles.get("draw_pile") or {}
    initial_discard = before_piles.get("discard_pile") or {}
    final_discard = after_piles.get("discard_pile") or {}
    if not set(selected) <= set(initial_hand):
        return "issues", "gambling_chip_selected_card_not_in_hand", details
    for name in ("exhaust_pile", "limbo"):
        if not _oracle_choice_maps_equal(
            before_piles.get(name), after_piles.get(name)
        ):
            return "issues", f"gambling_chip_{name}_changed", details
    kept = set(initial_hand) - set(selected)
    if not kept <= set(final_hand):
        return "issues", "gambling_chip_unselected_hand_card_removed", details
    count = len(selected)
    if count == 0:
        if any(transient_delta.values()):
            return "issues", "gambling_chip_zero_confirm_changed_piles", details
        return "clear", "gambling_chip_zero_confirm_clear", details

    player = (((initial.get("game") or {}).get("combat_state") or {}).get(
        "player"
    ) or {})
    no_draw = any(
        isinstance(power, dict)
        and _oracle_game_id(power.get("id") or power.get("name"))
        in {"nodraw", "nodrawpower"}
        for power in player.get("powers") or []
    )
    if no_draw:
        if not (
            set(final_hand) == kept
            and _oracle_choice_maps_equal(initial_draw, final_draw)
            and set(final_discard) == set(initial_discard) | set(selected)
        ):
            return "issues", "gambling_chip_no_draw_settlement_mismatch", details
        details["redraw_branch"] = "blocked_by_no_draw"
        return "clear", "gambling_chip_no_draw_discard_only_clear", details

    if len(final_hand) != len(initial_hand):
        return "issues", "gambling_chip_hand_size_not_restored", details
    gained_hand = set(final_hand) - kept
    if len(initial_draw) >= count:
        if not (
            len(gained_hand) == count
            and gained_hand <= set(initial_draw)
            and set(final_draw) == set(initial_draw) - gained_hand
            and set(final_discard) == set(initial_discard) | set(selected)
        ):
            return "issues", "gambling_chip_no_shuffle_redraw_mismatch", details
        details["redraw_branch"] = "draw_pile_sufficient"
    else:
        shuffled = set(initial_discard) | set(selected)
        shuffled_drawn = gained_hand - set(initial_draw)
        if not (
            set(initial_draw) <= gained_hand
            and len(shuffled_drawn) == count - len(initial_draw)
            and shuffled_drawn <= shuffled
            and set(final_draw) == shuffled - shuffled_drawn
            and not final_discard
        ):
            return "issues", "gambling_chip_reshuffle_redraw_mismatch", details
        details["redraw_branch"] = "discard_reshuffle"
    return "clear", "gambling_chip_settlement_clear", details


def _audit_combat_choice_transitions(records, expected):
    """Audit every combat overlay record and every contiguous action chain."""

    report = {
        "transitions": [], "settlements": [], "issues": [], "unknowns": [],
    }
    eligible = []
    transition_by_index = {}

    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("record_type") != "decision":
            continue
        phase = str(record.get("phase") or "").upper()
        raw_before = record.get("authoritative_state_before")
        raw_game = _game_from_state(raw_before)
        if (
            phase not in _ORACLE_COMBAT_CHOICE_PHASES
            or not isinstance(raw_game, dict)
            or str(raw_game.get("room_phase") or "").upper() != "COMBAT"
        ):
            continue
        eligible.append(index)
        before_state, before_missing, before_mismatches = _state_envelope(
            record, "before", expected
        )
        after_state, after_missing, after_mismatches = _state_envelope(
            record, "after", expected
        )
        before = _oracle_combat_choice_frame(before_state)
        after = _oracle_combat_choice_frame(after_state)
        observation = {
            "record_index": index,
            "before_seq": record.get("before_seq"),
            "after_seq": record.get("after_seq"),
            "phase": phase,
            "current_action": (before.get("snapshot") or {}).get(
                "current_action"
            ),
        }
        reasons = []
        issue_reasons = []
        if before_missing or after_missing or before_mismatches or after_mismatches:
            reasons.append("authoritative_state_envelope_missing_or_mismatched")
        if before.get("errors") or after.get("errors"):
            reasons.append("combat_choice_authoritative_evidence_missing")
            observation["evidence_errors"] = {
                "before": before.get("errors") or [],
                "after": after.get("errors") or [],
            }

        claim = (
            (record.get("decision_outcome") or {}).get(
                "combat_choice_transition"
            ) if isinstance(record.get("decision_outcome"), dict) else None
        )
        if claim is None:
            reasons.append("combat_choice_transition_claim_missing")
        elif before.get("snapshot") is not None and after.get("snapshot") is not None:
            claim_mismatches = _oracle_combat_choice_claim_mismatches(
                before["snapshot"], after["snapshot"], claim
            )
            if claim_mismatches:
                issue_reasons.append("combat_choice_transition_claim_mismatch")
                observation["claim_mismatches"] = claim_mismatches

        pair = _oracle_choice_pair(before)
        mechanism = _ORACLE_COMBAT_CHOICE_ACTIONS.get(pair)
        if (
            mechanism == "discard_to_hand"
            and not _oracle_record_projection_v2(record)
        ):
            mechanism = None
        observation["mechanism"] = mechanism
        if mechanism is None:
            reasons.append("combat_choice_action_unclassified")
        else:
            surface_unknowns, surface_issues = (
                _oracle_combat_choice_surface_review(
                    before, mechanism, record.get("action")
                )
            )
            reasons.extend(surface_unknowns)
            issue_reasons.extend(surface_issues)
            if surface_unknowns:
                before["errors"] = sorted(set(
                    (before.get("errors") or []) + surface_unknowns
                ))
        target = (
            _oracle_combat_choice_target(record, before, mechanism)
            if mechanism is not None and before.get("snapshot") is not None
            else {"uuid": None, "issues": [], "unknowns": []}
        )
        issue_reasons.extend(target.get("issues") or [])
        reasons.extend(target.get("unknowns") or [])
        observation["selected_card_instance_id"] = target.get("uuid")

        after_same_action = bool(
            mechanism is not None
            and _ORACLE_COMBAT_CHOICE_ACTIONS.get(_oracle_choice_pair(after))
            == mechanism
        )
        observation["transition_kind"] = (
            "selection" if after_same_action else "settlement"
        )
        if after_same_action:
            after_surface_unknowns, after_surface_issues = (
                _oracle_combat_choice_surface_review(
                    after, mechanism, None
                )
            )
            reasons.extend(after_surface_unknowns)
            issue_reasons.extend(after_surface_issues)
            if after_surface_unknowns:
                after["errors"] = sorted(set(
                    (after.get("errors") or []) + after_surface_unknowns
                ))
        if after_same_action and not (before.get("errors") or after.get("errors")):
            persistent = _oracle_choice_master_delta(
                before.get("deck"), after.get("deck")
            )
            transient = _oracle_choice_transient_delta(before, after)
            observation.update({
                "persistent_delta": persistent,
                "transient_delta": transient,
            })
            allowed_gambling_selection_delta = False
            if mechanism in {
                "gambling_chip_redraw", "armaments_upgrade",
                "discard_selected_card", "exhaust_selected_card", "put_on_deck",
            }:
                before_selected = set(before.get("selected") or {})
                after_selected = set(after.get("selected") or {})
                newly_selected = after_selected - before_selected
                # During the selection UI, Slay the Spire removes each chosen
                # card from the combat hand and exposes the same UUID in the
                # screen's selected set.  That is a UI transition, not a
                # discarded or destroyed card.  Permit exactly that one
                # hand-to-selection alias and retain strict rejection of any
                # additional pile mutation.
                allowed_gambling_selection_delta = bool(
                    not any(persistent.values())
                    and transient == {
                        "added": [],
                        "removed": sorted(newly_selected),
                        "changed": [],
                        "moved": [],
                    }
                )
            if (
                any(persistent.values())
                or (any(transient.values()) and not allowed_gambling_selection_delta)
            ):
                issue_reasons.append("pending_selection_changed_card_state")
            if mechanism in {
                "discard_to_hand", "discard_to_top", "gambling_chip_redraw",
                "armaments_upgrade", "discard_selected_card",
                "exhaust_selected_card", "put_on_deck",
            }:
                before_selected = set(before.get("selected") or {})
                after_selected = set(after.get("selected") or {})
                expected_selected = set(before_selected)
                if target.get("uuid") is not None:
                    if target["uuid"] in before_selected:
                        issue_reasons.append("selected_uuid_reused")
                    expected_selected.add(target["uuid"])
                if after_selected != expected_selected:
                    issue_reasons.append("screen_selection_delta_mismatch")

        if issue_reasons:
            observation.update({
                "status": "issues", "reason_codes": sorted(set(issue_reasons)),
            })
            report["issues"].append({
                "kind": "combat_choice_transition_mismatch",
                "record_index": index,
                "reason_codes": observation["reason_codes"],
                "claim_mismatches": observation.get("claim_mismatches", []),
            })
        elif reasons:
            observation.update({
                "status": "inconclusive", "reason_codes": sorted(set(reasons)),
            })
            report["unknowns"].append({
                "kind": (
                    "combat_choice_transition_claim_missing"
                    if "combat_choice_transition_claim_missing" in reasons
                    else "combat_choice_action_unclassified"
                    if "combat_choice_action_unclassified" in reasons
                    else "combat_choice_transition_evidence_missing"
                ),
                "record_index": index,
                "reason_codes": observation["reason_codes"],
            })
        else:
            observation.update({"status": "clear", "reason_codes": []})
        observation["_before"] = before
        observation["_after"] = after
        observation["_target"] = target
        transition_by_index[index] = observation
        report["transitions"].append(observation)

    consumed = set()
    for index in eligible:
        if index in consumed:
            continue
        first = transition_by_index[index]
        mechanism = first.get("mechanism")
        action = first.get("current_action")
        phase = first.get("phase")
        chain = [index]
        consumed.add(index)
        current = index
        while True:
            current_observation = transition_by_index[current]
            if _oracle_choice_pair(current_observation["_after"]) != (phase, action):
                break
            next_index, next_record = _next_decision_record(records, current)
            if (
                next_record is None
                or next_record.get("before_seq") != records[current].get("after_seq")
                or next_index not in transition_by_index
                or transition_by_index[next_index].get("phase") != phase
                or transition_by_index[next_index].get("current_action") != action
            ):
                break
            chain.append(next_index)
            consumed.add(next_index)
            current = next_index

        final_observation = transition_by_index[chain[-1]]
        still_pending = _oracle_choice_pair(final_observation["_after"]) == (
            phase, action
        )
        settlement = {
            "record_index": chain[0],
            "record_indices": chain,
            "before_seq": records[chain[0]].get("before_seq"),
            "after_seq": records[chain[-1]].get("after_seq"),
            "phase": phase,
            "current_action": action,
            "mechanism": mechanism,
        }
        if mechanism is None:
            settlement.update({
                "status": "inconclusive",
                "reason": "combat_choice_action_unclassified",
            })
            report["unknowns"].append({
                "kind": "combat_choice_settlement_unclassified",
                "record_index": chain[0], "record_indices": chain,
            })
            report["settlements"].append(settlement)
            continue
        if still_pending:
            settlement.update({
                "status": "inconclusive",
                "reason": "combat_choice_settlement_pending",
            })
            report["unknowns"].append({
                "kind": "combat_choice_settlement_pending",
                "record_index": chain[0], "record_indices": chain,
            })
            report["settlements"].append(settlement)
            continue

        initial = first["_before"]
        final = final_observation["_after"]
        discovery_origin = (
            _oracle_discovery_potion_origin(records, chain[0], expected)
            if mechanism == "discovery_to_hand"
            else None
        )
        evidence_errors = sorted(set(
            (initial.get("errors") or []) + (final.get("errors") or [])
        ))
        if evidence_errors:
            settlement.update({
                "status": "inconclusive",
                "reason": "combat_choice_settlement_evidence_missing",
                "evidence_errors": evidence_errors,
            })
            report["unknowns"].append({
                "kind": "combat_choice_settlement_evidence_missing",
                "record_index": chain[0], "record_indices": chain,
                "evidence_errors": evidence_errors,
            })
            report["settlements"].append(settlement)
            continue

        selected = set(initial.get("selected") or {})
        discovery_cancelled = False
        started_midstream = bool(selected)
        chain_issue_reasons = []
        chain_unknown_reasons = []
        if isinstance(discovery_origin, dict):
            if discovery_origin.get("status") == "issues":
                chain_issue_reasons.append(discovery_origin.get("reason"))
            elif discovery_origin.get("status") == "unknown":
                chain_unknown_reasons.append(discovery_origin.get("reason"))
        for offset, record_index in enumerate(chain):
            observation = transition_by_index[record_index]
            before_selected = set(observation["_before"].get("selected") or {})
            if mechanism in {
                "discard_to_hand", "discard_to_top", "gambling_chip_redraw",
                "discard_selected_card", "exhaust_selected_card", "put_on_deck",
            }:
                if before_selected != selected:
                    if offset == 0:
                        started_midstream = True
                    else:
                        chain_issue_reasons.append(
                            "combat_choice_selection_chain_mismatch"
                        )
            target = observation.get("_target") or {}
            discovery_cancelled = bool(
                discovery_cancelled or target.get("cancelled")
            )
            chain_issue_reasons.extend(target.get("issues") or [])
            chain_unknown_reasons.extend(target.get("unknowns") or [])
            if target.get("uuid") is not None:
                if target["uuid"] in selected:
                    chain_issue_reasons.append("selected_uuid_reused")
                selected.add(target["uuid"])
        if started_midstream:
            chain_unknown_reasons.append("combat_choice_chain_started_mid_selection")
        if chain_issue_reasons:
            status, reason, details = (
                "issues", "combat_choice_target_or_chain_mismatch", {
                    "reason_codes": sorted(set(chain_issue_reasons)),
                    "selected_card_instance_ids": sorted(selected),
                }
            )
        elif chain_unknown_reasons:
            status, reason, details = (
                "unknown", "combat_choice_settlement_evidence_missing", {
                    "reason_codes": sorted(set(chain_unknown_reasons)),
                    "selected_card_instance_ids": sorted(selected),
                }
            )
        else:
            status, reason, details = _oracle_combat_choice_settlement(
                mechanism, initial, final, selected,
                discovery_origin=discovery_origin,
                discovery_cancelled=discovery_cancelled,
            )
        settlement.update({
            "status": "inconclusive" if status == "unknown" else status,
            "reason": reason, **details,
        })
        if status == "issues":
            report["issues"].append({
                "kind": "combat_choice_settlement_mismatch",
                "record_index": chain[0], "record_indices": chain,
                "reason": reason,
                "reason_codes": details.get("reason_codes", [reason]),
            })
        elif status == "unknown":
            report["unknowns"].append({
                "kind": "combat_choice_settlement_evidence_missing",
                "record_index": chain[0], "record_indices": chain,
                "reason": reason,
                "reason_codes": details.get("reason_codes", [reason]),
            })
        report["settlements"].append(settlement)

    # Private frames make implementation reuse cheap but never enter JSON.
    for observation in report["transitions"]:
        for field in ("_before", "_after", "_target"):
            observation.pop(field, None)
    return report


def _oracle_monsters_by_instance(game):
    combat = (game or {}).get("combat_state")
    monsters = combat.get("monsters") if isinstance(combat, dict) else None
    if not isinstance(monsters, list):
        return None
    result = {}
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        identity = monster.get("enemy_instance_id")
        if identity is None or str(identity) in result:
            return None
        result[str(identity)] = monster
    return result


def _oracle_stasis_power(monster):
    matches = [
        power for power in ((monster or {}).get("powers") or [])
        if isinstance(power, dict)
        and _oracle_game_id(power.get("id") or power.get("name"))
        in _ORACLE_STASIS_POWER_IDS
    ]
    if len(matches) > 1:
        return "duplicate"
    return matches[0] if matches else None


def _oracle_monster_alive(monster):
    hp = _finite_number((monster or {}).get("current_hp"))
    return bool(
        isinstance(monster, dict)
        and hp is not None and hp > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    )


def _audit_base_game_mechanisms(records):
    """Recompute active base-game mechanisms from protocol facts only."""

    report = {
        "contract_version": BASE_GAME_MECHANISM_CONTRACT_VERSION,
        "eligible": 0, "evaluated": 0,
        "issues": [], "unknowns": [], "observations": [],
    }

    def eligible(mechanism, index, **details):
        report["eligible"] += 1
        observation = {
            "mechanism": mechanism, "record_index": index, **details,
        }
        report["observations"].append(observation)
        return observation

    def clear(observation, **details):
        report["evaluated"] += 1
        observation.update({"status": "clear", **details})

    def fail(observation, kind, **details):
        observation.update({"status": "issues", **details})
        report["issues"].append({
            "kind": kind, "record_index": observation["record_index"],
            "mechanism": observation["mechanism"], **details,
        })

    def unresolved(observation, kind, **details):
        observation.update({"status": "inconclusive", **details})
        report["unknowns"].append({
            "kind": kind, "record_index": observation["record_index"],
            "mechanism": observation["mechanism"], **details,
        })

    black_star_encounters = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("record_type") != "decision":
            continue
        before_game = _game_from_state(record.get("authoritative_state_before"))
        after_game = _game_from_state(record.get("authoritative_state_after"))
        if not isinstance(before_game, dict) or not isinstance(after_game, dict):
            continue

        # Black Star: an elite reward group starts with the normal elite relic
        # plus exactly one additional relic.  Audit only the initial bound
        # COMBAT_REWARD frame; later frames have already removed selected
        # rewards and therefore cannot prove the original cardinality.  Raw
        # screen rewards and raw protocol options must bind by immutable
        # choice_index + relic id, independent of container order.
        phase = str(record.get("phase") or "").upper()
        black_star_relics = [
            relic for relic in (before_game.get("relics") or [])
            if isinstance(relic, dict)
            and _oracle_game_id(relic.get("id") or relic.get("name"))
            in _ORACLE_BLACK_STAR_IDS
        ]
        if phase == "COMBAT_REWARD" and black_star_relics:
            encounter_key = _oracle_black_star_encounter_key(
                record, before_game
            )
            origin, origin_details = _oracle_black_star_reward_origin(
                records, index, before_game
            )
            already_seen = encounter_key in black_star_encounters
            if origin == "not_applicable":
                pass
            elif origin == "continued" and already_seen:
                pass
            else:
                observation = eligible(
                    "black_star_elite_relic_rewards", index,
                    encounter_key=list(encounter_key),
                )
                black_star_encounters.add(encounter_key)
                if origin != "initial":
                    unresolved(
                        observation, "black_star_elite_source_unproven",
                        **origin_details,
                    )
                elif already_seen:
                    fail(
                        observation, "black_star_elite_reward_mismatch",
                        reason="duplicate_initial_reward_group",
                        **origin_details,
                    )
                elif len(black_star_relics) != 1:
                    fail(
                        observation, "black_star_elite_reward_mismatch",
                        reason="duplicate_black_star_inventory_binding",
                        black_star_count=len(black_star_relics),
                        **origin_details,
                    )
                else:
                    screen = before_game.get("screen_state")
                    rewards = (
                        screen.get("rewards")
                        if isinstance(screen, dict) else None
                    )
                    if not isinstance(rewards, list):
                        unresolved(
                            observation, "black_star_reward_screen_missing",
                            reason="authoritative_reward_list_missing",
                            **origin_details,
                        )
                    else:
                        screen_relics = [
                            reward for reward in rewards
                            if isinstance(reward, dict)
                            and str(
                                reward.get("reward_type") or ""
                            ).upper() == "RELIC"
                        ]
                        if len(screen_relics) != 2:
                            fail(
                                observation,
                                "black_star_elite_reward_mismatch",
                                reason="relic_reward_count_mismatch",
                                expected_relic_count=2,
                                screen_relic_count=len(screen_relics),
                                **origin_details,
                            )
                        else:
                            options = record.get("available_options_before")
                            if not isinstance(options, list):
                                unresolved(
                                    observation,
                                    "black_star_reward_screen_missing",
                                    reason="protocol_reward_options_missing",
                                    **origin_details,
                                )
                            else:
                                option_relics = []
                                for option in options:
                                    if not isinstance(option, dict):
                                        continue
                                    target = option.get("target")
                                    target = (
                                        target
                                        if isinstance(target, dict) else {}
                                    )
                                    reward = target.get("reward")
                                    if (
                                        isinstance(reward, dict)
                                        and str(
                                            reward.get("reward_type") or ""
                                        ).upper() == "RELIC"
                                    ):
                                        option_relics.append((
                                            option, target, reward,
                                        ))
                                if len(option_relics) != 2:
                                    fail(
                                        observation,
                                        "black_star_elite_reward_mismatch",
                                        reason=(
                                            "protocol_relic_option_count_mismatch"
                                        ),
                                        expected_relic_count=2,
                                        screen_relic_count=2,
                                        protocol_relic_option_count=(
                                            len(option_relics)
                                        ),
                                        **origin_details,
                                    )
                                else:
                                    screen_bindings = [
                                        _oracle_relic_reward_binding(reward)
                                        for reward in screen_relics
                                    ]
                                    option_bindings = [
                                        _oracle_relic_reward_binding(reward)
                                        for _option, _target, reward
                                        in option_relics
                                    ]
                                    option_ids = [
                                        option.get("option_id")
                                        for option, _target, _reward
                                        in option_relics
                                    ]
                                    option_indexes = [
                                        option.get("choice_index")
                                        for option, _target, _reward
                                        in option_relics
                                    ]
                                    target_kinds = [
                                        str(target.get("kind") or "").casefold()
                                        for _option, target, _reward
                                        in option_relics
                                    ]
                                    stable_options = all(
                                        binding is not None
                                        and type(option_index) is int
                                        and option_index == binding[0]
                                        and option_id not in (None, "")
                                        and target_kind == "reward"
                                        for binding, option_index, option_id,
                                        target_kind in zip(
                                            option_bindings, option_indexes,
                                            option_ids, target_kinds,
                                        )
                                    )
                                    stable_screen = all(
                                        binding is not None
                                        for binding in screen_bindings
                                    )
                                    if not stable_screen or not stable_options:
                                        fail(
                                            observation,
                                            "black_star_elite_reward_mismatch",
                                            reason=(
                                                "unstable_relic_reward_binding"
                                            ),
                                            screen_bindings=screen_bindings,
                                            option_bindings=option_bindings,
                                            option_ids=option_ids,
                                            option_indexes=option_indexes,
                                            target_kinds=target_kinds,
                                            **origin_details,
                                        )
                                    else:
                                        duplicate_binding = bool(
                                            len(set(screen_bindings)) != 2
                                            or len({
                                                binding[0]
                                                for binding in screen_bindings
                                            }) != 2
                                            or len({
                                                binding[1]
                                                for binding in screen_bindings
                                            }) != 2
                                            or len(set(option_bindings)) != 2
                                            or len(set(option_ids)) != 2
                                            or len(set(option_indexes)) != 2
                                        )
                                        if duplicate_binding:
                                            fail(
                                                observation,
                                                "black_star_elite_reward_mismatch",
                                                reason=(
                                                    "duplicate_relic_reward_binding"
                                                ),
                                                screen_bindings=screen_bindings,
                                                option_bindings=option_bindings,
                                                option_ids=option_ids,
                                                **origin_details,
                                            )
                                        elif Counter(
                                            screen_bindings
                                        ) != Counter(option_bindings):
                                            fail(
                                                observation,
                                                "black_star_elite_reward_mismatch",
                                                reason=(
                                                    "protocol_relic_binding_mismatch"
                                                ),
                                                screen_bindings=screen_bindings,
                                                option_bindings=option_bindings,
                                                **origin_details,
                                            )
                                        else:
                                            clear(
                                                observation,
                                                observed_relic_count=2,
                                                stable_bindings=sorted(
                                                    screen_bindings
                                                ),
                                                **origin_details,
                                            )

        # Busted Crown: both getRewardCards bytecode paths start at three and
        # apply every relic modifier.  Only bytecode-covered standard or
        # colorless sources are eligible for an exact cardinality assertion.
        if (
            str(record.get("phase") or "").upper() == "CARD_REWARD"
            and _oracle_unique_relic(before_game, "BustedCrown") is not None
        ):
            observation = eligible("busted_crown_card_reward_count", index)
            source = _oracle_card_reward_source(records, index)
            if source is None:
                unresolved(
                    observation, "busted_crown_reward_source_unproven",
                    reason="fixed reward lists do not necessarily call the relic modifier",
                )
            else:
                screen = before_game.get("screen_state")
                screen_cards = (
                    screen.get("cards") if isinstance(screen, dict) else None
                )
                options = record.get("available_options_before")
                if not isinstance(screen_cards, list) or not isinstance(options, list):
                    unresolved(
                        observation, "busted_crown_reward_cards_missing",
                        source=source,
                    )
                else:
                    option_cards = [
                        option for option in options
                        if isinstance(option, dict)
                        and isinstance(option.get("target"), dict)
                        and str(option["target"].get("kind") or "").casefold()
                        == "card"
                    ]
                    question_card = (
                        _oracle_unique_relic(before_game, "QuestionCard")
                        is not None
                    )
                    expected_count = 3 + (1 if question_card else 0) - 2
                    observed_count = len(screen_cards)
                    screen_binding_values = [
                        _oracle_card_binding(card) for card in screen_cards
                    ]
                    option_binding_values = [
                        _oracle_card_binding(option["target"].get("card"))
                        for option in option_cards
                    ]
                    screen_bindings = Counter(screen_binding_values)
                    option_bindings = Counter(option_binding_values)
                    binding_missing = any(
                        not binding or binding[0] is None
                        for binding in screen_binding_values
                        + option_binding_values
                    )
                    if (
                        observed_count != expected_count
                        or len(option_cards) != observed_count
                        or binding_missing
                        or screen_bindings != option_bindings
                    ):
                        fail(
                            observation,
                            "busted_crown_reward_count_mismatch",
                            source=source, expected_card_count=expected_count,
                            screen_card_count=observed_count,
                            protocol_card_option_count=len(option_cards),
                            screen_option_bindings_match=(
                                screen_bindings == option_bindings
                            ),
                            question_card=question_card,
                        )
                    else:
                        clear(
                            observation, source=source,
                            expected_card_count=expected_count,
                            observed_card_count=observed_count,
                            question_card=question_card,
                        )

        # The Mausoleum: the selected raw event option establishes the static
        # A0/A15 risk contract, while only the two bound authoritative frames
        # establish the realized relic/curse settlement.
        mausoleum = mausoleum_realized_settlement(record)
        if mausoleum.get("status") != "not_applicable":
            observation = eligible(
                "mausoleum_expected_curse_for_random_relic", index,
                expected_trade_contract=mausoleum.get(
                    "expected_trade_contract"
                ),
            )
            if mausoleum.get("status") == "clear":
                clear(
                    observation,
                    realized_settlement=mausoleum.get(
                        "realized_settlement"
                    ),
                    authority="authoritative_protocol_delta",
                )
            elif mausoleum.get("status") == "issues":
                fail(
                    observation, "mausoleum_trade_settlement_mismatch",
                    reason=mausoleum.get("reason"),
                    details={
                        key: _oracle_clone(value)
                        for key, value in mausoleum.items()
                        if key not in {
                            "expected_trade_contract",
                            "realized_settlement",
                        }
                    },
                )
            else:
                unresolved(
                    observation, "mausoleum_trade_settlement_unproven",
                    reason=mausoleum.get("reason"),
                )

        # Omamori: a positive counter prevents each Curse acquisition and
        # consumes exactly one charge.  Counter and master-deck deltas must
        # agree; neither producer consequence text nor score is consulted.
        before_omamori = _oracle_unique_relic(before_game, "Omamori")
        after_omamori = _oracle_unique_relic(after_game, "Omamori")
        deck_delta = _observable_collection_delta(
            before_game.get("deck"), after_game.get("deck")
        )
        added_curses = (
            [card for card in deck_delta["added"] if _oracle_is_curse(card)]
            if isinstance(deck_delta, dict) else None
        )
        attempted, attempt_error, attempt_contradiction = (
            _oracle_known_curse_attempt(record)
        )
        before_counter = (
            before_omamori.get("counter")
            if isinstance(before_omamori, dict) else None
        )
        after_counter = (
            after_omamori.get("counter")
            if isinstance(after_omamori, dict) else None
        )
        counter_changed = bool(
            type(before_counter) is int and type(after_counter) is int
            and before_counter != after_counter
        )
        acquired = before_omamori is None and after_omamori is not None
        omamori_relevant = bool(
            acquired or before_omamori is not None and (
                counter_changed or (attempted or 0) > 0
                or isinstance(added_curses, list) and added_curses
                or attempt_error is not None and (
                    counter_changed
                    or isinstance(added_curses, list) and added_curses
                )
            )
        )
        if omamori_relevant:
            observation = eligible("omamori_curse_prevention", index)
            if added_curses is None:
                unresolved(
                    observation, "omamori_counter_or_deck_missing",
                    before_counter=before_counter, after_counter=after_counter,
                )
            elif acquired and type(after_counter) is not int:
                unresolved(
                    observation, "omamori_counter_or_deck_missing",
                    before_counter=None, after_counter=after_counter,
                )
            elif acquired and attempt_error is not None and not added_curses:
                if after_counter == 2:
                    clear(
                        observation, transition="acquired",
                        attempted_curses=0, charges_before=0,
                        charges_after=2,
                    )
                else:
                    fail(
                        observation, "omamori_transition_mismatch",
                        reason="acquisition_counter_not_initialized_to_two",
                        after_counter=after_counter,
                    )
            elif attempt_error is not None:
                method = fail if attempt_contradiction else unresolved
                method(
                    observation,
                    "omamori_transition_mismatch" if attempt_contradiction
                    else "omamori_curse_attempt_unproven",
                    reason=attempt_error,
                )
            elif after_omamori is None or (
                not acquired and type(before_counter) is not int
            ) or type(after_counter) is not int:
                unresolved(
                    observation, "omamori_counter_or_deck_missing",
                    before_counter=before_counter, after_counter=after_counter,
                )
            elif not (
                0 <= (2 if acquired else before_counter) <= 2
                and 0 <= after_counter <= 2
            ):
                fail(
                    observation, "omamori_transition_mismatch",
                    reason="counter_out_of_base_game_range",
                    before_counter=before_counter, after_counter=after_counter,
                )
            elif type(attempted) is int:
                charges_before = 2 if acquired else before_counter
                expected_transition = _oracle_omamori_expected(
                    charges_before, attempted
                )
                if (
                    expected_transition is None
                    or after_counter != expected_transition["counter_after"]
                    or len(added_curses)
                    != expected_transition["curses_added"]
                ):
                    fail(
                        observation, "omamori_transition_mismatch",
                        attempted_curses=attempted,
                        acquired=acquired,
                        before_counter=charges_before,
                        after_counter=after_counter,
                        added_curse_count=len(added_curses),
                        expected=expected_transition,
                    )
                else:
                    clear(
                        observation, attempted_curses=attempted,
                        acquired=acquired,
                        blocked_curses=expected_transition[
                            "charges_consumed"
                        ],
                        curses_added=expected_transition["curses_added"],
                        charges_before=charges_before,
                        charges_after=after_counter,
                    )

        # Stasis: the serialized power.card is the authority for the exact
        # held UUID.  The base game removes that card from every player pile;
        # CommunicationMod may expose it in ``limbo`` on some frames, but live
        # Bronze Orb traces also legitimately keep limbo empty.  Treat a
        # present limbo copy as corroboration, never as a prerequisite. Owner
        # death returns the same UUID to hand (or, when only that destination
        # is observable, discard remains conservatively unresolved).
        before_monsters = _oracle_monsters_by_instance(before_game)
        after_monsters = _oracle_monsters_by_instance(after_game)
        if before_monsters is None or after_monsters is None:
            visible = before_monsters or after_monsters or {}
            for owner_id, monster in visible.items():
                if _oracle_stasis_power(monster) is None:
                    continue
                observation = eligible(
                    "bronze_orb_stasis", index,
                    owner_instance_id=owner_id,
                )
                unresolved(
                    observation, "stasis_authoritative_combat_state_missing",
                    missing_side=(
                        "before" if before_monsters is None else "after"
                    ),
                )
            continue
        before_piles, before_pile_error = _oracle_combat_piles(before_game)
        after_piles, after_pile_error = _oracle_combat_piles(after_game)
        owner_ids = set(before_monsters) | set(after_monsters)
        acquisition_owner_ids = []
        acquisition_cards = []
        for owner_id in sorted(owner_ids):
            before_power = _oracle_stasis_power(
                before_monsters.get(owner_id)
            )
            after_power = _oracle_stasis_power(after_monsters.get(owner_id))
            if before_power is None and isinstance(after_power, dict):
                acquisition_owner_ids.append(owner_id)
                card = after_power.get("card")
                if _oracle_card_uuid(card) is not None:
                    acquisition_cards.append(card)
        batch_priority = None
        if (
            len(acquisition_owner_ids) > 1
            and len(acquisition_cards) == len(acquisition_owner_ids)
            and not before_pile_error
            and not after_pile_error
        ):
            draw = before_piles["draw_pile"]
            selected_uuids = [
                _oracle_card_uuid(card) for card in acquisition_cards
            ]
            if (
                len(draw) >= len(acquisition_cards)
                and all(uuid in draw for uuid in selected_uuids)
            ):
                expected_priorities = sorted(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in draw.values()
                    ),
                    reverse=True,
                )[:len(acquisition_cards)]
                selected_priorities = sorted(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in acquisition_cards
                    ),
                    reverse=True,
                )
                batch_priority = {
                    "priority_status": (
                        "clear"
                        if len(set(selected_uuids)) == len(selected_uuids)
                        and selected_priorities == expected_priorities
                        else "issues"
                    ),
                    "source_pile": "draw_pile",
                    "batch_size": len(acquisition_cards),
                    "selected_priorities": selected_priorities,
                    "expected_priorities": expected_priorities,
                }
            else:
                # At END, an empty draw pile may be replenished from the
                # pre-END discard plus unplayed hand.  Their exact shuffle
                # order is hidden, but rarity priority is order-independent:
                # selecting the top-k priorities from this conservative
                # superset is still a complete proof.  A non-top selection
                # remains unresolved because some hand cards may have left
                # before the shuffle; it is not evidence of a violation.
                post_end_source = {
                    **before_piles["draw_pile"],
                    **before_piles["discard_pile"],
                    **before_piles["hand"],
                }
                selected_priorities = sorted(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in acquisition_cards
                    ),
                    reverse=True,
                )
                expected_priorities = sorted(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in post_end_source.values()
                    ),
                    reverse=True,
                )[:len(acquisition_cards)]
                superset_proves_priority = bool(
                    len(set(selected_uuids)) == len(selected_uuids)
                    and all(uuid in post_end_source for uuid in selected_uuids)
                    and selected_priorities == expected_priorities
                )
                batch_priority = {
                    "priority_status": (
                        "clear" if superset_proves_priority else "unresolved"
                    ),
                    "source_pile": "draw_or_post_end_discard_superset",
                    "batch_size": len(acquisition_cards),
                    "selected_priorities": selected_priorities,
                    "expected_priorities": expected_priorities,
                }
        for owner_id in sorted(owner_ids):
            before_monster = before_monsters.get(owner_id)
            after_monster = after_monsters.get(owner_id)
            before_power = _oracle_stasis_power(before_monster)
            after_power = _oracle_stasis_power(after_monster)
            if before_power is None and after_power is None:
                continue
            observation = eligible(
                "bronze_orb_stasis", index, owner_instance_id=owner_id,
            )
            if before_power == "duplicate" or after_power == "duplicate":
                fail(
                    observation, "stasis_transition_mismatch",
                    reason="duplicate_stasis_power",
                )
                continue
            if before_pile_error or after_pile_error:
                unresolved(
                    observation, "stasis_complete_piles_missing",
                    before_error=before_pile_error,
                    after_error=after_pile_error,
                )
                continue
            before_card = (
                before_power.get("card")
                if isinstance(before_power, dict) else None
            )
            after_card = (
                after_power.get("card")
                if isinstance(after_power, dict) else None
            )
            if (
                isinstance(before_power, dict)
                and _oracle_card_uuid(before_card) is None
            ) or (
                isinstance(after_power, dict)
                and _oracle_card_uuid(after_card) is None
            ):
                unresolved(
                    observation, "stasis_power_card_evidence_missing",
                    before_card_observable=_oracle_card_uuid(before_card) is not None,
                    after_card_observable=_oracle_card_uuid(after_card) is not None,
                )
                continue
            if before_power is None and isinstance(after_power, dict):
                held_uuid = _oracle_card_uuid(after_card)
                end_discard_reconstruction = bool(
                    not before_piles["draw_pile"]
                    and str(record.get("action") or "").casefold() == "end"
                )
                if before_piles["draw_pile"]:
                    source_name = "draw_pile"
                    source = before_piles[source_name]
                    definite_source = source
                elif end_discard_reconstruction:
                    source_name = "post_end_discard_reconstruction"
                    source = {
                        **before_piles["discard_pile"],
                        **before_piles["hand"],
                    }
                    definite_source = before_piles["discard_pile"]
                else:
                    source_name = "discard_pile"
                    source = before_piles[source_name]
                    definite_source = source
                source_card = source.get(held_uuid)
                if held_uuid is None or source_card is None:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="held_card_not_removed_from_required_source",
                        required_source=source_name, held_uuid=held_uuid,
                    )
                    continue
                selected_priority = _ORACLE_STASIS_RARITY_PRIORITY.get(
                    str(source_card.get("rarity") or "").upper(), 0
                )
                maximum_priority = max(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in source.values()
                    ),
                    default=0,
                )
                definite_priority = max(
                    (
                        _ORACLE_STASIS_RARITY_PRIORITY.get(
                            str(card.get("rarity") or "").upper(), 0
                        )
                        for card in definite_source.values()
                    ),
                    default=0,
                )
                if (
                    batch_priority
                    and batch_priority["priority_status"] == "issues"
                ):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="batch_rarity_priority_violated",
                        **batch_priority,
                    )
                    continue
                if (
                    batch_priority
                    and batch_priority["priority_status"] == "unresolved"
                ):
                    unresolved(
                        observation, "stasis_batch_source_order_unproven",
                        **batch_priority,
                    )
                    continue
                if batch_priority is None and selected_priority < definite_priority:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="rarity_priority_violated",
                        selected_rarity=source_card.get("rarity"),
                        definite_priority=definite_priority,
                    )
                elif batch_priority is None and selected_priority < maximum_priority:
                    unresolved(
                        observation, "stasis_end_discard_priority_unproven",
                        held_uuid=held_uuid,
                        selected_rarity=source_card.get("rarity"),
                        possible_priority=maximum_priority,
                        reason="before-hand cards may be retained or exhausted before Stasis",
                    )
                elif any(
                    held_uuid in after_piles[name]
                    for name in (
                        "hand", "draw_pile", "discard_pile", "exhaust_pile"
                    )
                ):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="held_card_remained_in_player_pile",
                        held_uuid=held_uuid,
                    )
                elif _oracle_card_binding(source_card) != _oracle_card_binding(
                    after_card
                ) or (
                    held_uuid in after_piles["limbo"]
                    and _oracle_card_binding(
                        after_piles["limbo"][held_uuid]
                    ) != _oracle_card_binding(after_card)
                ):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="held_card_binding_mismatch",
                        held_uuid=held_uuid,
                    )
                else:
                    clear(
                        observation, transition="acquired", held_uuid=held_uuid,
                        source_pile=source_name,
                        stasis_batch_source_pile=(
                            batch_priority.get("source_pile")
                            if batch_priority else source_name
                        ),
                        selected_rarity=source_card.get("rarity"),
                        limbo_observed=held_uuid in after_piles["limbo"],
                        stasis_batch_size=(
                            batch_priority.get("batch_size")
                            if batch_priority else 1
                        ),
                    )
                continue
            before_alive = _oracle_monster_alive(before_monster)
            after_alive = _oracle_monster_alive(after_monster)
            if isinstance(before_power, dict) and not before_alive:
                if (
                    isinstance(after_power, dict)
                    and _oracle_card_binding(after_card)
                    != _oracle_card_binding(before_card)
                ):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="post_death_linger_binding_mismatch",
                        held_uuid=_oracle_card_uuid(before_card),
                    )
                elif after_alive:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="dead_stasis_owner_became_alive",
                        held_uuid=_oracle_card_uuid(before_card),
                    )
                else:
                    clear(
                        observation, transition="post_death_linger",
                        held_uuid=_oracle_card_uuid(before_card),
                        lingering_power=isinstance(after_power, dict),
                    )
                continue
            if (
                isinstance(before_power, dict)
                and before_alive and not after_alive
            ):
                held_uuid = _oracle_card_uuid(before_card)
                returned_to = [
                    name for name in ("hand", "discard_pile")
                    if held_uuid is not None and held_uuid in after_piles[name]
                ]
                lingering_binding_invalid = bool(
                    isinstance(after_power, dict)
                    and _oracle_card_binding(after_power.get("card"))
                    != _oracle_card_binding(before_card)
                )
                limbo_binding_invalid = bool(
                    held_uuid in after_piles["limbo"]
                    and _oracle_card_binding(after_piles["limbo"][held_uuid])
                    != _oracle_card_binding(before_card)
                )
                if lingering_binding_invalid:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="lingering_power_card_binding_mismatch",
                        held_uuid=held_uuid,
                    )
                elif len(returned_to) != 1:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="held_card_not_returned_exactly_once",
                        held_uuid=held_uuid, returned_to=returned_to,
                    )
                elif _oracle_card_binding(
                    after_piles[returned_to[0]][held_uuid]
                ) != _oracle_card_binding(before_card):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="returned_card_binding_mismatch",
                        held_uuid=held_uuid,
                        returned_to=returned_to[0],
                    )
                elif any(
                    held_uuid in after_piles[name]
                    for name in ("draw_pile", "exhaust_pile")
                ) or limbo_binding_invalid:
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="returned_card_binding_present_in_wrong_pile",
                        held_uuid=held_uuid,
                        limbo_binding_invalid=limbo_binding_invalid,
                    )
                elif returned_to[0] == "discard_pile":
                    unresolved(
                        observation, "stasis_death_hand_size_unproven",
                        held_uuid=held_uuid,
                        returned_to="discard_pile",
                        observed_after_hand_size=len(after_piles["hand"]),
                        reason=(
                            "the settled frame does not expose hand size at "
                            "the exact StasisPower.onDeath instant"
                        ),
                    )
                else:
                    clear(
                        observation, transition="owner_death_return",
                        held_uuid=held_uuid, returned_to=returned_to[0],
                        lingering_power=isinstance(after_power, dict),
                        lingering_limbo=held_uuid in after_piles["limbo"],
                    )
                continue
            if isinstance(before_power, dict) and isinstance(after_power, dict):
                held_uuid = _oracle_card_uuid(before_card)
                if (
                    held_uuid is None
                    or _oracle_card_binding(before_card)
                    != _oracle_card_binding(after_card)
                    or (
                        held_uuid in before_piles["limbo"]
                        and _oracle_card_binding(
                            before_piles["limbo"][held_uuid]
                        ) != _oracle_card_binding(before_card)
                    )
                    or (
                        held_uuid in after_piles["limbo"]
                        and _oracle_card_binding(
                            after_piles["limbo"][held_uuid]
                        ) != _oracle_card_binding(after_card)
                    )
                    or any(
                        held_uuid in piles[name]
                        for piles in (before_piles, after_piles)
                        for name in (
                            "hand", "draw_pile", "discard_pile", "exhaust_pile"
                        )
                    )
                ):
                    fail(
                        observation, "stasis_transition_mismatch",
                        reason="active_stasis_binding_not_preserved",
                        held_uuid=held_uuid,
                    )
                else:
                    clear(
                        observation, transition="held", held_uuid=held_uuid,
                        limbo_observed=bool(
                            held_uuid in before_piles["limbo"]
                            or held_uuid in after_piles["limbo"]
                        ),
                    )
                continue
            if isinstance(before_power, dict) and after_power is None:
                held_uuid = _oracle_card_uuid(before_card)
                fail(
                    observation, "stasis_transition_mismatch",
                    reason="stasis_removed_while_owner_alive",
                    held_uuid=held_uuid,
                )

    report["issue_count"] = len(report["issues"])
    report["eligible_unknown_count"] = len(report["unknowns"])
    report["status"] = (
        "issues" if report["issue_count"]
        else "inconclusive" if report["eligible_unknown_count"]
        else "clear" if report["eligible"]
        else "not_applicable"
    )
    return report


def _living_from_monsters(monsters):
    if not isinstance(monsters, list):
        return None
    return [
        monster for monster in monsters
        if isinstance(monster, dict)
        and _finite_number(monster.get("current_hp")) is not None
        and float(monster.get("current_hp")) > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]


def _authoritative_combat_after(record, expected):
    state, _missing, _mismatches = _state_envelope(record, "after", expected)
    authoritative = state is not None
    game = _game_from_state(state) or {}
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    monsters = combat.get("monsters")
    room_phase = game.get("room_phase")
    screen_type = game.get("screen_type")
    player = combat.get("player")
    if not isinstance(player, dict):
        player = {}
    if player.get("current_hp") is None and game.get("current_hp") is not None:
        player = {**player, "current_hp": game.get("current_hp")}
    return (
        _living_from_monsters(monsters) if authoritative else None,
        room_phase if authoritative else None,
        screen_type if authoritative else None,
        player if authoritative and isinstance(player, dict) else None,
        monsters if authoritative and isinstance(monsters, list) else None,
        authoritative,
    )


def _oracle_exact_postcombat_hp_loss(
    before_game, after_game, before_player, before_hp, after_hp,
):
    """Independently recover loss hidden by exact settlement healing."""

    if not all(isinstance(value, dict) for value in (
        before_game, after_game, before_player,
    )):
        return None
    if str(after_game.get("room_phase") or "").upper() == "COMBAT":
        return None
    if str(after_game.get("screen_type") or "").upper() == "GAME_OVER":
        return None
    max_hp = _finite_number(
        before_player.get("max_hp")
        if before_player.get("max_hp") is not None
        else before_game.get("max_hp")
    )
    if not (
        before_hp is not None and after_hp is not None and max_hp is not None
        and before_hp > 0 and after_hp > 0 and after_hp < max_hp
    ):
        return None
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in before_game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return None
    has_meat_on_the_bone = "meatonthebone" in relic_ids
    if "blackblood" in relic_ids:
        healing = 12
    elif "burningblood" in relic_ids:
        healing = 6
    else:
        return None
    powers = [
        power for power in before_player.get("powers") or []
        if isinstance(power, dict)
    ]
    power_ids = {
        _oracle_game_id(power.get("id") or power.get("name"))
        for power in powers
    }
    if "selfrepair" in power_ids:
        return None
    regeneration = []
    for power in powers:
        if _oracle_game_id(power.get("id") or power.get("name")) not in {
            "regeneration", "regenerationpower",
        }:
            continue
        amount = _finite_number(power.get("amount"))
        if amount is None or amount <= 0 or not amount.is_integer():
            return None
        regeneration.append(int(amount))
    if len(regeneration) > 1:
        return None
    healing += sum(regeneration)
    if "magicflower" in relic_ids:
        healing = (healing * 3 + 1) // 2
    gross_loss = before_hp + healing - after_hp
    if gross_loss < 0 or gross_loss > before_hp:
        return None
    if has_meat_on_the_bone and (before_hp - gross_loss) * 2 <= max_hp:
        return None
    return gross_loss


def _oracle_exact_regeneration_hp_loss(
    record, before_game, after_game, before_player, before_hp, after_hp,
):
    """Recover gross END damage hidden by deterministic Regeneration.

    Regeneration heals at the end of the player's turn before the enemy
    attack.  When the player has enough missing HP to receive the whole heal,
    the two authoritative HP frames therefore prove gross damage exactly:
    ``before HP + realized heal - after HP``.  Near the HP cap, earlier
    end-of-turn damage could change how much healing was realized, so that
    case deliberately remains unknown instead of trusting producer telemetry.
    """

    if not isinstance(record, dict) or record.get("action") != "end":
        return None
    if not all(isinstance(value, dict) for value in (
        before_game, after_game, before_player,
    )):
        return None
    if (
        str(before_game.get("room_phase") or "").upper() != "COMBAT"
        or str(after_game.get("room_phase") or "").upper() != "COMBAT"
        or str(after_game.get("screen_type") or "").upper() == "GAME_OVER"
    ):
        return None
    max_hp = _finite_number(
        before_player.get("max_hp")
        if before_player.get("max_hp") is not None
        else before_game.get("max_hp")
    )
    if not (
        before_hp is not None and after_hp is not None and max_hp is not None
        and before_hp > 0 and after_hp > 0 and max_hp >= before_hp
    ):
        return None
    regeneration = []
    for power in before_player.get("powers") or []:
        if not isinstance(power, dict):
            continue
        if _oracle_game_id(power.get("id") or power.get("name")) not in {
            "regeneration", "regenerationpower",
        }:
            continue
        amount = _finite_number(power.get("amount"))
        if amount is None or amount <= 0 or not amount.is_integer():
            return None
        regeneration.append(int(amount))
    if len(regeneration) != 1:
        return None

    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in before_game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        healing = 0
    else:
        healing = regeneration[0]
        if "magicflower" in relic_ids:
            healing = (healing * 3 + 1) // 2
        # Full realization is independently proven only when the pre-END
        # frame already has enough missing HP.  Any earlier self-damage can
        # only create more room and therefore cannot change this value.
        if max_hp - before_hp < healing:
            return None

    gross_loss = before_hp + healing - after_hp
    if gross_loss < 0 or gross_loss > before_hp + healing:
        return None
    return gross_loss


def _oracle_exact_fairy_revival_hp_loss(
    before_game, after_game, before_player, before_hp, after_hp,
):
    """Independently recover damage hidden by a consumed Fairy Potion."""

    if not all(isinstance(value, dict) for value in (
        before_game, after_game, before_player,
    )):
        return None
    if (
        str(before_game.get("room_phase") or "").upper() != "COMBAT"
        or str(after_game.get("room_phase") or "").upper() != "COMBAT"
        or before_hp is None or after_hp is None
        or before_hp <= 0 or after_hp <= 0
    ):
        return None
    before_fairies = {
        potion.get("potion_instance_id")
        for potion in before_game.get("potions") or []
        if isinstance(potion, dict)
        and _oracle_game_id(potion.get("id")) in {
            "fairypotion", "fairyinabottle", "fairy",
        }
        and potion.get("potion_instance_id")
    }
    after_fairies = {
        potion.get("potion_instance_id")
        for potion in after_game.get("potions") or []
        if isinstance(potion, dict)
        and _oracle_game_id(potion.get("id")) in {
            "fairypotion", "fairyinabottle", "fairy",
        }
        and potion.get("potion_instance_id")
    }
    if not before_fairies or not (before_fairies - after_fairies):
        return None
    max_hp = _finite_number(
        before_player.get("max_hp")
        if before_player.get("max_hp") is not None
        else before_game.get("max_hp")
    )
    if max_hp is None or max_hp <= 0 or not max_hp.is_integer():
        return None
    relic_ids = {
        _oracle_game_id(relic.get("id") or relic.get("name"))
        for relic in before_game.get("relics") or []
        if isinstance(relic, dict)
    }
    if "markofthebloom" in relic_ids:
        return None
    if any(
        _oracle_game_id(power.get("id") or power.get("name"))
        in {"regeneration", "regenerationpower"}
        and (_finite_number(power.get("amount")) or 0) > 0
        for power in before_player.get("powers") or []
        if isinstance(power, dict)
    ):
        return None
    potency = 60 if "sacredbark" in relic_ids else 30
    revive_healing = max(1, int(max_hp) * potency // 100)
    if "magicflower" in relic_ids:
        revive_healing = (revive_healing * 3 + 1) // 2
    # Fairy resolves from the death hook rather than a potion-use action, so
    # Toy Ornithopter does not add its ordinary five-point potion heal.
    revive_healing = min(int(max_hp), revive_healing)
    gross_loss = before_hp + revive_healing - after_hp
    if gross_loss < 0:
        return None
    return gross_loss


def _authoritative_damage_observed(record, expected):
    (
        after_living, room_phase, screen_type, player_after,
        monsters_after, authoritative,
    ) = _authoritative_combat_after(record, expected)
    if not authoritative:
        return None
    before_state, _missing, _mismatches = _state_envelope(
        record, "before", expected
    )
    after_state, _missing, _mismatches = _state_envelope(
        record, "after", expected
    )
    before_game = _game_from_state(before_state) or {}
    after_game = _game_from_state(after_state) or {}
    before_combat = before_game.get("combat_state")
    before_combat = before_combat if isinstance(before_combat, dict) else {}
    before_player = before_combat.get("player")
    before_monsters = before_combat.get("monsters")
    if not isinstance(before_player, dict) or not isinstance(before_monsters, list):
        return None
    before_hp = _finite_number(before_player.get("current_hp"))
    after_hp = _finite_number((player_after or {}).get("current_hp"))
    player_loss = (
        None if before_hp is None or after_hp is None
        else max(0.0, before_hp - after_hp)
    )
    gross_postcombat_loss = _oracle_exact_postcombat_hp_loss(
        before_game, after_game, before_player, before_hp, after_hp,
    )
    gross_regeneration_loss = _oracle_exact_regeneration_hp_loss(
        record, before_game, after_game, before_player, before_hp, after_hp,
    )
    gross_fairy_loss = _oracle_exact_fairy_revival_hp_loss(
        before_game, after_game, before_player, before_hp, after_hp,
    )
    gross_before_healing = (
        gross_fairy_loss
        if gross_fairy_loss is not None
        else gross_postcombat_loss
        if gross_postcombat_loss is not None
        else gross_regeneration_loss
    )

    after_by_id = {
        monster.get("enemy_instance_id"): monster
        for monster in monsters_after or []
        if isinstance(monster, dict) and monster.get("enemy_instance_id")
    }
    exited = bool(
        (room_phase is not None and str(room_phase).upper() != "COMBAT")
        or str(screen_type or "").upper() == "GAME_OVER"
    )
    monster_loss = 0.0
    bound_target_loss = None
    resolved_target_id = record.get("resolved_target_id")
    observed_any = False
    departed_minion_hp = 0.0
    departed_non_minion_hp = 0.0
    for monster in before_monsters:
        if not isinstance(monster, dict):
            return None
        identity = monster.get("enemy_instance_id")
        left = _finite_number(monster.get("current_hp"))
        if not identity or left is None:
            return None
        after_monster = after_by_id.get(identity)
        right = _finite_number(
            after_monster.get("current_hp") if isinstance(after_monster, dict) else None
        )
        if (
            right is None
            and exited
            and (after_living == [] or monsters_after is None)
        ):
            powers = monster.get("powers")
            powers = powers if isinstance(powers, list) else []
            is_minion = any(
                isinstance(power, dict)
                and _oracle_game_id(power.get("id") or power.get("name"))
                in {"minion", "minionpower"}
                for power in powers
            )
            if left > 0:
                if is_minion:
                    departed_minion_hp += left
                else:
                    departed_non_minion_hp += left
            right = 0.0
        if right is None:
            continue
        observed_any = True
        hp_loss = max(0.0, left - right)
        monster_loss += hp_loss
        if identity == resolved_target_id:
            bound_target_loss = hp_loss
    return {
        "hero_to_monsters": monster_loss if observed_any else None,
        "hero_to_monsters_bound_target": bound_target_loss,
        "terminal_minion_departure_ambiguous": bool(
            exited
            and departed_minion_hp > 0
            and departed_non_minion_hp > 0
        ),
        "hero_to_monsters_direct_loss_min": (
            monster_loss - departed_minion_hp
            if observed_any and departed_minion_hp > 0
            else None
        ),
        "hero_to_monsters_direct_loss_max": (
            monster_loss
            if observed_any and departed_minion_hp > 0
            else None
        ),
        "monsters_to_hero": player_loss,
        "monsters_to_hero_before_healing": gross_before_healing,
        "monsters_to_hero_before_postcombat_healing": (
            gross_postcombat_loss
        ),
        "monsters_to_hero_before_regeneration": gross_regeneration_loss,
        "monsters_to_hero_before_fairy_revival": gross_fairy_loss,
        "player_hp_before": before_hp,
        "player_hp_after": after_hp,
        "screen_type_after": screen_type,
    }


def _deferred_hand_select_end_damage(
    records, record_index, record, expected, *, max_following=5,
):
    """Recover an END settlement deferred through a HAND_SELECT overlay.

    Well-Laid Plans acknowledges the bound END command before RetainCardsAction
    is complete. The END receipt therefore has a real zero immediate HP delta,
    while the contiguous HAND_SELECT proceed receipt owns the enemy-turn
    damage. Join only that exact protocol chain; an unrelated later combat
    frame must never validate the producer's prediction.
    """

    if not (
        isinstance(record, dict)
        and record.get("record_type") == "decision"
        and record.get("action") == "end"
        and type(record.get("after_seq")) is int
    ):
        return None
    after_state, missing, mismatches = _state_envelope(
        record, "after", expected
    )
    if missing or mismatches:
        return None
    after_game = _game_from_state(after_state) or {}
    if not (
        str(after_state.get("phase") or "").upper() == "HAND_SELECT"
        and str(after_game.get("screen_type") or "").upper()
        == "HAND_SELECT"
        and str(after_game.get("room_phase") or "").upper() == "COMBAT"
    ):
        return None
    combat = after_game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    player = combat.get("player")
    player = player if isinstance(player, dict) else {}
    end_hp = _finite_number(player.get("current_hp"))
    if end_hp is None:
        end_hp = _finite_number(after_game.get("current_hp"))
    if end_hp is None:
        return None

    expected_before_seq = record.get("after_seq")
    for offset, candidate in enumerate(
        records[record_index + 1:record_index + 1 + max_following],
        start=1,
    ):
        if not (
            isinstance(candidate, dict)
            and candidate.get("record_type") == "decision"
            and candidate.get("phase") == "HAND_SELECT"
            and candidate.get("action") in {"choose", "proceed"}
            and candidate.get("before_seq") == expected_before_seq
        ):
            return None
        before_state, before_missing, before_mismatches = _state_envelope(
            candidate, "before", expected
        )
        next_state, next_missing, next_mismatches = _state_envelope(
            candidate, "after", expected
        )
        if (
            before_missing or before_mismatches
            or next_missing or next_mismatches
        ):
            return None
        before_game = _game_from_state(before_state) or {}
        before_combat = before_game.get("combat_state")
        before_combat = (
            before_combat if isinstance(before_combat, dict) else {}
        )
        before_player = before_combat.get("player")
        before_player = (
            before_player if isinstance(before_player, dict) else {}
        )
        before_hp = _finite_number(before_player.get("current_hp"))
        if before_hp is None:
            before_hp = _finite_number(before_game.get("current_hp"))
        if before_hp != end_hp:
            return None

        expected_before_seq = candidate.get("after_seq")
        next_game = _game_from_state(next_state) or {}
        if (
            str(next_state.get("phase") or "").upper() == "HAND_SELECT"
            and str(next_game.get("screen_type") or "").upper()
            == "HAND_SELECT"
        ):
            continue
        observed = _authoritative_damage_observed(candidate, expected)
        if not isinstance(observed, dict):
            return None
        loss = _finite_number(observed.get("monsters_to_hero"))
        if loss is None:
            return None
        return {
            "record_index": record_index + offset,
            "observed_damage": observed,
            "settlement_before_seq": candidate.get("before_seq"),
            "settlement_after_seq": candidate.get("after_seq"),
        }
    return None


def _deferred_card_selection_damage(
    records, record_index, record, expected, *, max_following=5,
):
    """Recover card damage queued behind a GRID/HAND_SELECT overlay.

    Cards such as Survivor can trigger Letter Opener before their discard
    selection is complete. The PLAY receipt therefore has a correct zero
    immediate delta even though the card's already-projected damage settles
    when the contiguous selection chain closes. Join only chained authority
    envelopes and compare the original pre-play state with that final frame.
    """

    if not (
        isinstance(record, dict)
        and record.get("record_type") == "decision"
        and record.get("action") == "play"
        and type(record.get("after_seq")) is int
    ):
        return None
    after_state, missing, mismatches = _state_envelope(
        record, "after", expected
    )
    if missing or mismatches:
        return None
    after_game = _game_from_state(after_state) or {}
    overlay_phase = str(after_state.get("phase") or "").upper()
    overlay_screen = str(after_game.get("screen_type") or "").upper()
    if not (
        overlay_phase in {"GRID", "HAND_SELECT"}
        and overlay_screen in {"GRID", "HAND_SELECT"}
        and str(after_game.get("room_phase") or "").upper() == "COMBAT"
    ):
        return None

    expected_before_seq = record.get("after_seq")
    for offset, candidate in enumerate(
        records[record_index + 1:record_index + 1 + max_following],
        start=1,
    ):
        if not (
            isinstance(candidate, dict)
            and candidate.get("record_type") == "decision"
            and str(candidate.get("phase") or "").upper()
            in {"GRID", "HAND_SELECT"}
            and candidate.get("action") in {"choose", "proceed"}
            and candidate.get("before_seq") == expected_before_seq
        ):
            return None
        _before, before_missing, before_mismatches = _state_envelope(
            candidate, "before", expected
        )
        next_state, next_missing, next_mismatches = _state_envelope(
            candidate, "after", expected
        )
        if (
            before_missing or before_mismatches
            or next_missing or next_mismatches
        ):
            return None
        expected_before_seq = candidate.get("after_seq")
        next_game = _game_from_state(next_state) or {}
        if (
            str(next_state.get("phase") or "").upper()
            in {"GRID", "HAND_SELECT"}
            and str(next_game.get("screen_type") or "").upper()
            in {"GRID", "HAND_SELECT"}
        ):
            continue
        if str(next_game.get("room_phase") or "").upper() != "COMBAT":
            return None
        synthetic = copy.deepcopy(record)
        synthetic["after_seq"] = candidate.get("after_seq")
        synthetic["authoritative_state_after"] = copy.deepcopy(next_state)
        observed = _authoritative_damage_observed(synthetic, expected)
        if not isinstance(observed, dict):
            return None
        return {
            "record_index": record_index + offset,
            "observed_damage": observed,
            "settlement_before_seq": candidate.get("before_seq"),
            "settlement_after_seq": candidate.get("after_seq"),
        }
    return None


def _oracle_juggernaut_block_gain_contract(record):
    """Independently reconstruct the narrow Juggernaut trigger contract."""

    game = _oracle_before_game(record)
    combat = game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    player = combat.get("player")
    player = player if isinstance(player, dict) else {}

    def power_amount(*ids):
        wanted = {_oracle_game_id(value) for value in ids}
        return sum(
            max(0, int(power.get("amount") or 0))
            for power in player.get("powers") or []
            if isinstance(power, dict)
            and _oracle_game_id(power.get("id") or power.get("name")) in wanted
        )

    juggernaut = power_amount("Juggernaut", "JuggernautPower")
    if juggernaut <= 0:
        return None
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    card_uuid = record.get("card_instance_id") or record.get(
        "requested_target_id"
    )
    card_id = record.get("card_id") or decision.get("card_id")
    hand = [card for card in combat.get("hand") or [] if isinstance(card, dict)]
    card = next((
        value for value in hand
        if card_uuid and value.get("card_instance_id") == card_uuid
    ), None)
    if card is None and card_id:
        wanted = _oracle_game_id(card_id)
        matches = [
            value for value in hand
            if _oracle_game_id(value.get("id") or value.get("name")) == wanted
        ]
        card = matches[0] if len(matches) == 1 else None
    if card is None:
        return None
    search = decision.get("search")
    search = search if isinstance(search, dict) else {}
    resolution_count = search.get("first_action_resolution_count", 1)
    if (
        type(resolution_count) is not int
        or resolution_count < 1
        or resolution_count > 3
    ):
        return None
    card_token = _oracle_game_id(card.get("id") or card.get("name"))
    if card_token in {"secondwind", "fiendfire", "seversoul"}:
        return None
    attack = str(card.get("type") or "").upper() == "ATTACK"
    triggers = 0
    sources = []
    if int(card.get("base_block") or -1) >= 0 or card_token == "entrench":
        triggers += resolution_count
        sources.append({"source": "card_block", "count": resolution_count})
    if attack and power_amount("Rage", "RagePower") > 0:
        triggers += resolution_count
        sources.append({"source": "rage", "count": resolution_count})
    if power_amount("After Image", "AfterImagePower") > 0:
        triggers += 1
        sources.append({"source": "after_image", "count": 1})
    if card.get("exhausts") is True and power_amount(
        "Feel No Pain", "FeelNoPainPower"
    ) > 0:
        triggers += 1
        sources.append({"source": "feel_no_pain", "count": 1})
    fan = next((
        relic for relic in game.get("relics") or []
        if isinstance(relic, dict)
        and _oracle_game_id(relic.get("id") or relic.get("name"))
        == "ornamentalfan"
    ), None)
    if attack and fan is not None:
        counter = fan.get("counter")
        if type(counter) is not int or counter not in {0, 1, 2}:
            return None
        fan_triggers = (counter + resolution_count) // 3
        if fan_triggers:
            triggers += fan_triggers
            sources.append({"source": "ornamental_fan", "count": fan_triggers})
    return {
        "damage_per_trigger": juggernaut,
        "trigger_count": triggers,
        "sources": sources,
        "card_instance_id": card.get("card_instance_id"),
    }


def _damage_claim_with_authoritative_lethal_cap(
    value, observed_field, observed_damage,
):
    """Normalize only the player-HP truncation proven by a GAME_OVER frame.

    Slay the Spire serializes observed HP loss only down to zero.  A planner
    can therefore correctly predict 32 incoming damage against 28 HP while
    the authoritative delta is necessarily 28.  Do not generalize this to
    nonfatal damage or enemy overkill: both remain exact comparisons.
    """

    if observed_field != "monsters_to_hero" or not isinstance(
        observed_damage, dict
    ):
        return value
    hp_before = _finite_number(observed_damage.get("player_hp_before"))
    hp_after = _finite_number(observed_damage.get("player_hp_after"))
    observed = _finite_number(observed_damage.get(observed_field))
    if not (
        hp_before is not None and hp_before >= 0
        and hp_after == 0
        and observed == hp_before
        and str(observed_damage.get("screen_type_after") or "").upper()
        == "GAME_OVER"
    ):
        return value
    return min(value, hp_before)


_LEGACY_DUPLICATION_PREDICTION_HASHES = frozenset({
    "d1e4bd06a646bb24",
})


def _duplicated_attack_prediction_alias(
    record, model, predicted, observed_predicted,
):
    """Recognize the bounded legacy Sword Boomerang duplication omission.

    This is intentionally limited to controller hashes that predate the live
    duplication fix.  Current hashes must emit the repeated damage directly
    and can never use this compatibility path to hide a regression.
    """

    if not isinstance(record, dict) or not isinstance(model, dict):
        return False
    if (
        record.get("decision_hash")
        not in _LEGACY_DUPLICATION_PREDICTION_HASHES
        or record.get("action") != "play"
        or record.get("target_id") is not None
        or isinstance(record.get("target_before"), dict)
    ):
        return False
    if str(model.get("hero_to_monsters_prediction_basis") or "") != (
        "current_card_final_target_hp_projection"
    ):
        return False
    card_instance_id = record.get("card_instance_id")
    hand = record.get("hand_before")
    hand = hand if isinstance(hand, list) else []
    matching_cards = [
        card for card in hand
        if isinstance(card, dict)
        and isinstance(card_instance_id, str)
        and card.get("card_instance_id") == card_instance_id
    ]
    if (
        len(matching_cards) != 1
        or str(matching_cards[0].get("type") or "").upper() != "ATTACK"
        or _oracle_game_id(matching_cards[0].get("id")) != "swordboomerang"
        or _oracle_game_id(record.get("card_id")) != "swordboomerang"
    ):
        return False
    player = record.get("player_before")
    player = player if isinstance(player, dict) else {}
    powers = player.get("powers")
    powers = powers if isinstance(powers, list) else []
    duplicate_amount = None
    power_ids = set()
    for power in powers:
        if not isinstance(power, dict):
            continue
        power_id = str(power.get("id") or "").casefold()
        power_ids.add(power_id)
        if power_id in {"duplicationpower", "duplication"}:
            amount = _finite_number(power.get("amount"))
            if amount is not None and amount >= 1:
                duplicate_amount = int(amount)
    if power_ids & {
        "doubletappower", "double tap", "doubletap",
        "echoformpower", "echo form", "echoform",
    }:
        return False
    living = [
        monster for monster in (record.get("monsters_before") or [])
        if isinstance(monster, dict)
        and not monster.get("is_gone")
        and not monster.get("half_dead")
        and (_finite_number(monster.get("current_hp")) or 0) > 0
    ]
    if len(living) != 1:
        return False
    target_hp = _finite_number(living[0].get("current_hp"))
    base_prediction = _finite_number(predicted)
    observed = _finite_number(observed_predicted)
    if (
        duplicate_amount is None
        or target_hp is None
        or target_hp <= 0
        or base_prediction is None
        or base_prediction <= 0
        or observed is None
    ):
        return False
    # DuplicationPower.amount counts remaining eligible cards.  The current
    # card receives exactly one additional resolution even when amount > 1.
    expected = min(target_hp, base_prediction * 2)
    return abs(expected - observed) <= 1e-9


def _terminal_minion_departure_prediction_alias(
    record, model, predicted, observed_predicted, observed_damage,
):
    """Compare an exact attack prediction with terminal Minion departure.

    Killing Gremlin Leader, Reptomancer, or The Collector removes their
    surviving Minion monsters as part of the combat transition. Once the
    bridge has left COMBAT, those dismissed monsters no longer have an after
    frame, so their remaining HP is indistinguishable from card damage. The
    complete pre-combat HP delta is therefore an upper bound, not an exact
    observation of the played card's damage.

    For an untargeted Attack, accept only an exact current-card projection
    within the independently observed direct-loss range. For a targeted
    Attack, accept only the stricter proof that its target was the sole living
    non-Minion, the prediction exactly equals that target's starting HP, and
    every other positive-HP departure was a Minion. This preserves a
    contradiction for genuine under/over-predictions and unrelated exits.
    """

    if not all(isinstance(value, dict) for value in (
        record, model, observed_damage,
    )):
        return False
    if (
        record.get("action") != "play"
        or str(model.get("hero_to_monsters_prediction_basis") or "")
        != "current_card_final_target_hp_projection"
        or str(observed_damage.get("screen_type_after") or "").upper()
        != "COMBAT_REWARD"
        or (_finite_number(observed_damage.get("player_hp_after")) or 0)
        <= 0
    ):
        return False
    prediction = _finite_number(predicted)
    observed = _finite_number(observed_predicted)
    minimum = _finite_number(
        observed_damage.get("hero_to_monsters_direct_loss_min")
    )
    maximum = _finite_number(
        observed_damage.get("hero_to_monsters_direct_loss_max")
    )
    if not (
        observed_damage.get("terminal_minion_departure_ambiguous") is True
        and prediction is not None
        and observed is not None
        and minimum is not None
        and maximum is not None
        and observed == maximum
        and minimum <= prediction <= maximum
    ):
        return False

    before_game = _oracle_before_game(record)
    combat = before_game.get("combat_state")
    combat = combat if isinstance(combat, dict) else {}
    hand = combat.get("hand")
    if not isinstance(hand, list):
        return False
    card_instance_id = record.get("card_instance_id")
    matches = [
        card for card in hand
        if isinstance(card, dict)
        and card.get("card_instance_id") == card_instance_id
    ]
    if len(matches) != 1:
        return False
    card = matches[0]
    if str(card.get("type") or "").upper() != "ATTACK":
        return False
    target_id = record.get("enemy_instance_id")
    if card.get("has_target") is False:
        return target_id is None
    if (
        card.get("has_target") is not True
        or not isinstance(target_id, str)
        or not target_id
    ):
        return False

    monsters = combat.get("monsters")
    monsters = monsters if isinstance(monsters, list) else []

    def is_minion(monster):
        return any(
            isinstance(power, dict)
            and _oracle_game_id(power.get("id") or power.get("name"))
            in {"minion", "minionpower"}
            for power in (monster.get("powers") or [])
        )

    living = [
        monster for monster in monsters
        if isinstance(monster, dict)
        and (_finite_number(monster.get("current_hp")) or 0) > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]
    identities = [monster.get("enemy_instance_id") for monster in living]
    if (
        not living
        or any(not isinstance(identity, str) or not identity
               for identity in identities)
        or len(set(identities)) != len(identities)
    ):
        return False
    target_matches = [
        monster for monster in living
        if monster.get("enemy_instance_id") == target_id
    ]
    if len(target_matches) != 1 or is_minion(target_matches[0]):
        return False
    other_living = [
        monster for monster in living
        if monster.get("enemy_instance_id") != target_id
    ]
    if (
        not other_living
        or not all(is_minion(monster) for monster in other_living)
    ):
        return False
    target_hp = _finite_number(target_matches[0].get("current_hp"))
    departed_minion_hp = sum(
        _finite_number(monster.get("current_hp")) or 0
        for monster in other_living
    )
    return bool(
        target_hp is not None
        and target_hp > 0
        and departed_minion_hp > 0
        and abs(prediction - target_hp) <= 1e-9
        and abs(minimum - target_hp) <= 1e-9
        and abs(maximum - (target_hp + departed_minion_hp)) <= 1e-9
    )


def _living_monsters(record):
    monsters = record.get("monsters_before")
    if not isinstance(monsters, list):
        return None
    return [
        monster for monster in monsters
        if isinstance(monster, dict)
        and _finite_number(monster.get("current_hp")) is not None
        and float(monster.get("current_hp")) > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]


def _true_combat_end(record):
    # ``decision.search.true_combat_end`` is a whole-plan property.  Treating
    # it as a promise that the current action ends combat creates false
    # contradictions for valid multi-card lethal lines.  Only the explicit
    # action-level trace claim is eligible for this oracle check.  The legacy
    # top-level name remains accepted for old independently-authored fixtures.
    return (
        record.get("action_true_combat_end_predicted") is True
        or record.get("true_combat_end") is True
    )


def _terminal_is_authoritative(terminal, artifacts):
    """Accept terminal authority only from the immutable state artifact.

    ``authoritative_game_over`` and ``screen_type`` on the terminal trace are
    producer claims.  They are compared later, but can never establish their
    own authority.
    """

    state = (artifacts or {}).get("state") if isinstance(artifacts, dict) else None
    if not isinstance(state, dict) or state.get("protocol_version") != 2:
        return False
    terminal_seq = terminal.get("terminal_state_seq", terminal.get("state_seq"))
    if type(terminal_seq) is not int or state.get("state_seq") != terminal_seq:
        return False
    if terminal.get("termination_kind") == "operational_error":
        # An operational failure is authoritative when the immutable artifact
        # is the exact last state.  It deliberately does not assert GAME_OVER.
        return True
    if terminal.get("termination_kind") != "game_over":
        return False
    game = _game_from_state(state) or {}
    return str(game.get("screen_type") or "").upper() == "GAME_OVER"


def _terminal_game(artifacts):
    state = (artifacts or {}).get("state") if isinstance(artifacts, dict) else None
    return _game_from_state(state)


def _terminal_victory_flags(game):
    if not isinstance(game, dict):
        return None
    screen = game.get("screen_state")
    screen = screen if isinstance(screen, dict) else {}
    values = []
    if "run_victory" in game:
        values.append(game.get("run_victory"))
    if "victory" in screen:
        values.append(screen.get("victory"))
    if not values or any(type(value) is not bool for value in values):
        return None
    if len(set(values)) != 1:
        return "conflict"
    return values[0]


def audit_records(records, decision_hash, attempt_id, artifacts=None):
    """Audit one complete attempt without importing production policy code."""

    records = list(records or [])
    issues = []
    unknowns = []
    blind_reviews = []
    coverage = {
        "protocol_binding": _coverage_bucket(),
        "authoritative_state_envelopes": _coverage_bucket(),
        "canonical_choice_completeness": _coverage_bucket(),
        "candidate_consequences": _coverage_bucket(),
        "choice_selection": _coverage_bucket(),
        "strategy_conflict_review": _coverage_bucket(),
        "observable_state_deltas": _coverage_bucket(),
        "combat_choice_transitions": _coverage_bucket(),
        "combat_choice_settlements": _coverage_bucket(),
        "damage_consistency": _coverage_bucket(),
        "true_combat_end": _coverage_bucket(),
        "terminal_authority": _coverage_bucket(),
        "act4_authority": _coverage_bucket(),
        "heart_defeat_authority": _coverage_bucket(),
    }

    def issue(kind, index=None, **details):
        item = {"kind": kind, "severity": "P1", **details}
        if index is not None:
            item["record_index"] = index
            if 0 <= index < len(records):
                record = records[index]
                item.setdefault("before_seq", record.get("before_seq"))
                item.setdefault("phase", record.get("phase"))
        issues.append(item)
        return item

    def unknown(kind, index=None, **details):
        item = {"kind": kind, **details}
        if index is not None:
            item["record_index"] = index
            if 0 <= index < len(records):
                item.setdefault("before_seq", records[index].get("before_seq"))
        unknowns.append(item)
        return item

    starts = [
        (index, record) for index, record in enumerate(records)
        if isinstance(record, dict) and record.get("record_type") == "controller_start"
    ]
    terminals = [
        (index, record) for index, record in enumerate(records)
        if isinstance(record, dict) and record.get("record_type") == "terminal_result"
    ]
    if len(starts) != 1:
        issue("controller_start_count_mismatch", observed=len(starts), expected=1)
    if len(terminals) != 1:
        issue("terminal_result_count_mismatch", observed=len(terminals), expected=1)

    start = starts[0][1] if len(starts) == 1 else {}
    terminal = terminals[0][1] if len(terminals) == 1 else {}
    artifact_result = (
        artifacts.get("run_result") if isinstance(artifacts, dict) else None
    )
    artifact_result = artifact_result if isinstance(artifact_result, dict) else {}
    expected = {
        "attempt_id": attempt_id,
        "decision_hash": decision_hash,
        **{
            field: artifact_result.get(
                field,
                start.get(field, terminal.get(field)),
            )
            for field in ATTEMPT_BINDING_FIELDS
            if field not in {"attempt_id", "decision_hash"}
        },
    }

    combat_choice_audit = _audit_combat_choice_transitions(records, expected)
    for name, observations in (
        ("combat_choice_transitions", combat_choice_audit["transitions"]),
        ("combat_choice_settlements", combat_choice_audit["settlements"]),
    ):
        bucket = coverage[name]
        for observation in observations:
            bucket["eligible"] += 1
            status = observation.get("status")
            if status == "clear":
                bucket["evaluated"] += 1
            elif status == "issues":
                bucket["issues"] += 1
            else:
                bucket["unknown"] += 1
    for finding in combat_choice_audit["issues"]:
        details = dict(finding)
        kind = details.pop("kind")
        index = details.pop("record_index", None)
        issue(kind, index, **details)
    for finding in combat_choice_audit["unknowns"]:
        details = dict(finding)
        kind = details.pop("kind")
        index = details.pop("record_index", None)
        unknown(kind, index, **details)

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issue("record_not_object", index)
            continue
        record_type = record.get("record_type")
        if record_type not in KNOWN_RECORD_TYPES:
            issue("unknown_record_type", index, record_type=record_type)
        binding = coverage["protocol_binding"]
        for field in ATTEMPT_BINDING_FIELDS:
            binding["eligible"] += 1
            value = record.get(field)
            wanted = expected.get(field)
            if value is None or wanted is None:
                binding["unknown"] += 1
                unknown("binding_field_missing", index, field=field)
            elif type(value) is not type(wanted) or value != wanted:
                binding["issues"] += 1
                issue(
                    "binding_mismatch", index, field=field,
                    expected=wanted, observed=value,
                )
            else:
                binding["evaluated"] += 1
        binding["eligible"] += 1
        if record_type == "decision":
            sequence_values = (record.get("before_seq"), record.get("after_seq"))
            sequence_valid = bool(
                all(type(value) is int for value in sequence_values)
                and sequence_values[1] > sequence_values[0]
            )
        elif record_type == "terminal_result":
            sequence_values = (
                record.get("state_seq"), record.get("terminal_state_seq")
            )
            sequence_valid = bool(
                all(type(value) is int for value in sequence_values)
                and sequence_values[0] == sequence_values[1]
            )
        else:
            sequence_values = (
                record.get("state_seq", record.get("before_seq")),
            )
            sequence_valid = type(sequence_values[0]) is int
        if sequence_valid:
            binding["evaluated"] += 1
        else:
            binding["unknown"] += 1
            unknown(
                "binding_state_sequence_missing_or_invalid", index,
                observed=list(sequence_values),
            )

    if starts and starts[0][0] != 0:
        issue("controller_start_not_first", starts_at=starts[0][0])
    if terminals and terminals[0][0] != len(records) - 1:
        issue("terminal_result_not_last", terminal_at=terminals[0][0])

    terminal_coverage = coverage["terminal_authority"]
    terminal_coverage["eligible"] += 1
    if len(terminals) != 1:
        terminal_coverage["unknown"] += 1
        unknown("terminal_authority_unavailable")
    elif terminal.get("schema_version") != 2:
        terminal_coverage["issues"] += 1
        issue("terminal_schema_mismatch", observed=terminal.get("schema_version"))
    elif terminal.get("termination_kind") not in {"game_over", "operational_error"}:
        terminal_coverage["issues"] += 1
        issue(
            "terminal_kind_invalid",
            observed=terminal.get("termination_kind"),
        )
    elif not _terminal_is_authoritative(terminal, artifacts):
        terminal_coverage["unknown"] += 1
        unknown("terminal_authority_unproven")
    else:
        terminal_coverage["evaluated"] += 1

    terminal_expected = {
        "schema_version": 2,
        "policy_version": expected.get("policy_version"),
        "attempt_id": attempt_id,
        "run_id": expected.get("run_id"),
        "seed": expected.get("seed"),
        "character": expected.get("character"),
        "ascension_level": expected.get("ascension_level"),
        "run_type": expected.get("run_type"),
        "decision_hash": decision_hash,
        "controller_hash": expected.get("controller_hash"),
        "selection_id": expected.get("selection_id"),
        "selection_digest": expected.get("selection_digest"),
        "terminal_state_seq": (
            (artifacts.get("state") or {}).get("state_seq")
            if isinstance(artifacts, dict)
            and isinstance(artifacts.get("state"), dict)
            else None
        ),
    }
    if len(starts) == 1 and len(terminals) == 1:
        for field in (
            "policy_version", "attempt_id", "run_id", "seed", "character",
            "ascension_level", "run_type", "decision_hash",
            "controller_hash", "selection_id", "selection_digest",
        ):
            wanted = terminal_expected.get(field)
            start_value = start.get(field)
            terminal_value = terminal.get(
                field,
                terminal.get("class") if field == "character" else None,
            )
            if wanted is None or start_value is None or terminal_value is None:
                unknown("terminal_binding_field_missing", field=field)
            elif (
                type(start_value) is not type(wanted)
                or type(terminal_value) is not type(wanted)
                or start_value != wanted
                or terminal_value != wanted
            ):
                issue(
                    "terminal_binding_mismatch",
                    field=field,
                    expected=wanted,
                    controller_start=start_value,
                    terminal_result=terminal_value,
                )

    pending_combat_end = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("record_type") != "decision":
            continue
        phase = str(record.get("phase") or "")
        combat_id = record.get("combat_id")
        authoritative_states = {}
        state_coverage = coverage["authoritative_state_envelopes"]
        for side in ("before", "after"):
            state_coverage["eligible"] += 1
            envelope, missing_fields, mismatches = _state_envelope(
                record, side, expected, start
            )
            authoritative_states[side] = envelope
            if missing_fields:
                state_coverage["unknown"] += 1
                unknown(
                    "authoritative_state_envelope_incomplete", index,
                    side=side, missing_fields=missing_fields,
                )
            if mismatches:
                state_coverage["issues"] += 1
                issue(
                    "authoritative_state_binding_mismatch", index,
                    side=side, mismatches=mismatches,
                )
            if envelope is not None:
                state_coverage["evaluated"] += 1

        # A later authoritative decision surface belonging to no combat (or a
        # different combat) proves the previously asserted combat transition.
        for pending_id, pending in list(pending_combat_end.items()):
            if pending_id == combat_id:
                continue
            if index > pending["record_index"]:
                coverage["true_combat_end"]["evaluated"] += 1
                pending_combat_end.pop(pending_id, None)

        if _record_has_strategic_surface(record):
            candidate_coverage = coverage["canonical_choice_completeness"]
            consequence_coverage = coverage["candidate_consequences"]
            selection_coverage = coverage["choice_selection"]
            delta_coverage = coverage["observable_state_deltas"]
            for bucket in (candidate_coverage, selection_coverage, delta_coverage):
                bucket["eligible"] += 1

            expected_choices, missing_inputs = reconstruct_legal_choices(record)
            canonical = _strategic_canonical_rows(
                record, record.get("legal_choices_before")
            )
            selected_consequences = []
            if missing_inputs or not isinstance(canonical, list):
                candidate_coverage["unknown"] += 1
                selection_coverage["unknown"] += 1
                unknown(
                    "canonical_choice_inputs_missing", index,
                    missing_fields=missing_inputs + (
                        [] if isinstance(canonical, list)
                        else ["legal_choices_before"]
                    ),
                )
                canonical = []
            else:
                malformed_expected = [
                    row for row in expected_choices
                    if row.get("invalid") or not row.get("choice_id")
                ]
                malformed_canonical = [
                    row for row in canonical
                    if not isinstance(row, dict)
                    or any(field not in row for field in CANONICAL_CHOICE_FIELDS)
                    or row.get("choice_schema_version")
                    != CANONICAL_CHOICE_SCHEMA_VERSION
                    or not str(row.get("choice_id") or "")
                ]
                expected_ids = [row.get("choice_id") for row in expected_choices]
                actual_ids = [
                    str(row.get("choice_id")) for row in canonical
                    if isinstance(row, dict) and row.get("choice_id") is not None
                ]
                expected_counts = Counter(expected_ids)
                actual_counts = Counter(actual_ids)
                complete = True
                if malformed_expected:
                    complete = False
                    candidate_coverage["issues"] += 1
                    issue(
                        "raw_legal_choice_invalid", index,
                        invalid_count=len(malformed_expected),
                    )
                if malformed_canonical:
                    complete = False
                    candidate_coverage["unknown"] += 1
                    unknown(
                        "canonical_choice_schema_incomplete", index,
                        invalid_count=len(malformed_canonical),
                    )
                duplicate_ids = sorted(
                    choice_id for choice_id, count in actual_counts.items()
                    if count > 1
                )
                if duplicate_ids:
                    complete = False
                    candidate_coverage["issues"] += 1
                    issue(
                        "candidate_duplicate", index,
                        choice_ids=duplicate_ids,
                    )
                missing_ids = sorted(
                    (expected_counts - actual_counts).elements()
                )
                extra_ids = sorted((actual_counts - expected_counts).elements())
                if missing_ids:
                    complete = False
                    candidate_coverage["issues"] += 1
                    issue("candidate_missing", index, choice_ids=missing_ids)
                if extra_ids:
                    complete = False
                    candidate_coverage["issues"] += 1
                    issue("candidate_extra", index, choice_ids=extra_ids)

                expected_by_id = {
                    row["choice_id"]: row for row in expected_choices
                    if row.get("choice_id")
                    and expected_counts[row["choice_id"]] == 1
                }
                canonical_by_id = {
                    str(row["choice_id"]): row for row in canonical
                    if isinstance(row, dict) and row.get("choice_id") is not None
                    and actual_counts[str(row["choice_id"])] == 1
                }
                for choice_id in sorted(expected_by_id.keys() & canonical_by_id.keys()):
                    raw = expected_by_id[choice_id]
                    row = canonical_by_id[choice_id]
                    consequence_coverage["eligible"] += 1
                    if row.get("choice_index") != raw.get("choice_index"):
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_choice_index_mismatch", index,
                            choice_id=choice_id,
                            expected=raw.get("choice_index"),
                            observed=row.get("choice_index"),
                        )
                    if str(row.get("action") or "").lower() != raw.get("action"):
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_semantic_binding_mismatch", index,
                            choice_id=choice_id,
                            expected_action=raw.get("action"),
                            observed_action=row.get("action"),
                        )
                    if (
                        (str(row.get("operation") or "").lower() or None)
                        != (str(raw.get("operation") or "").lower() or None)
                    ):
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_semantic_binding_mismatch", index,
                            choice_id=choice_id,
                            expected_operation=raw.get("operation"),
                            observed_operation=row.get("operation"),
                        )
                    if not (
                        isinstance(row.get("local_reason"), str)
                        and row["local_reason"].strip()
                        and isinstance(row.get("reason_codes"), list)
                        and row["reason_codes"]
                        and all(
                            isinstance(code, str) and code.strip()
                            for code in row["reason_codes"]
                        )
                    ):
                        complete = False
                        candidate_coverage["unknown"] += 1
                        unknown(
                            "candidate_local_rationale_missing", index,
                            choice_id=choice_id,
                        )
                    score_contradictions, score_unknowns = (
                        _review_score_evidence(row)
                    )
                    if score_contradictions:
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "producer_score_evidence_contradicted", index,
                            choice_id=choice_id,
                            contradictions=score_contradictions,
                        )
                    if score_unknowns:
                        complete = False
                        candidate_coverage["unknown"] += 1
                        unknown(
                            "candidate_score_evidence_missing", index,
                            choice_id=choice_id,
                            fields=score_unknowns,
                        )
                    if row.get("candidate_binding") != "unique" or not (
                        isinstance(row.get("candidate_ids"), list)
                        and row["candidate_ids"]
                    ):
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_producer_binding_invalid", index,
                            choice_id=choice_id,
                        )
                    raw_candidate = row.get("producer_candidate_raw")
                    raw_facts = (
                        raw_candidate.get("facts")
                        if isinstance(raw_candidate, dict)
                        and isinstance(raw_candidate.get("facts"), dict)
                        else {}
                    )
                    raw_candidate_id = (
                        raw_candidate.get(
                            "candidate_id",
                            raw_candidate.get("choice_id", raw_candidate.get("id")),
                        )
                        if isinstance(raw_candidate, dict) else None
                    )
                    raw_choice_index_present = bool(
                        isinstance(raw_candidate, dict)
                        and (
                            "choice_index" in raw_candidate
                            or "choice_index" in raw_facts
                        )
                    )
                    raw_action_present = bool(
                        isinstance(raw_candidate, dict)
                        and ("action" in raw_candidate or "action" in raw_facts)
                    )
                    raw_choice_index = (
                        raw_candidate.get("choice_index", raw_facts.get("choice_index"))
                        if isinstance(raw_candidate, dict) else None
                    )
                    raw_action = (
                        raw_candidate.get("action", raw_facts.get("action"))
                        if isinstance(raw_candidate, dict) else None
                    )
                    raw_operation_present = bool(
                        isinstance(raw_candidate, dict)
                        and (
                            "operation" in raw_candidate
                            or "operation" in raw_facts
                        )
                    )
                    raw_operation = (
                        raw_candidate.get("operation", raw_facts.get("operation"))
                        if isinstance(raw_candidate, dict) else None
                    )
                    raw_score = (
                        raw_candidate.get("local_score", raw_candidate.get("score"))
                        if isinstance(raw_candidate, dict) else None
                    )
                    producer_binding_valid = bool(
                        isinstance(raw_candidate, dict)
                        and raw_candidate_id is not None
                        and str(raw_candidate_id) in {
                            str(value) for value in row.get("candidate_ids") or []
                        }
                        and raw_choice_index_present
                        and raw_choice_index == raw.get("choice_index")
                        and raw_action_present
                        and str(raw_action or "").strip().lower() == raw.get("action")
                        and (
                            raw.get("action") != "potion"
                            or raw_operation_present
                            and str(raw_operation or "").strip().lower()
                            == str(raw.get("operation") or "").strip().lower()
                        )
                        and _finite_number(raw_score) == _finite_number(
                            row.get("local_score")
                        )
                    )
                    if not producer_binding_valid:
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_producer_binding_invalid", index,
                            choice_id=choice_id,
                            reason="typed_raw_candidate_binding_mismatch",
                        )
                    if not (
                        isinstance(row.get("model_evidence_status"), str)
                        and row["model_evidence_status"].strip()
                        and isinstance(row.get("model_evidence_reason"), str)
                        and row["model_evidence_reason"].strip()
                    ):
                        complete = False
                        candidate_coverage["unknown"] += 1
                        unknown(
                            "candidate_model_evidence_missing", index,
                            choice_id=choice_id,
                        )
                    semantic_mismatches = {}
                    for field in ("semantic_id", "label", "raw_text", "target"):
                        if row.get(field) != raw.get(field):
                            semantic_mismatches[field] = {
                                "expected": raw.get(field),
                                "observed": row.get(field),
                            }
                    if semantic_mismatches:
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_semantic_binding_mismatch", index,
                            choice_id=choice_id,
                            mismatches=semantic_mismatches,
                        )
                    if row.get("legal") is not True or row.get("visible") is not True:
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_not_legal_visible", index,
                            choice_id=choice_id,
                        )
                    consequence_record = dict(record)
                    consequence_record["_raw_target"] = raw.get("target")
                    consequence_record["_producer_consequence_claim"] = (
                        row.get("producer_consequence_claim")
                    )
                    consequence_ok, consequence_mismatches = (
                        _consequence_matches_visible(
                            row.get("consequences"),
                            _expected_visible_consequence(phase, raw, record),
                            consequence_record,
                        )
                    )
                    consequences = row.get("consequences")
                    if isinstance(consequences, dict):
                        expected_uncertainty = consequences.get("uncertainty")
                        expected_uncertainty = (
                            "; ".join(str(value) for value in expected_uncertainty)
                            if isinstance(expected_uncertainty, list)
                            and expected_uncertainty else None
                        )
                        if row.get("probability_outcomes") != consequences.get(
                            "probabilistic_outcomes"
                        ):
                            consequence_ok = False
                            consequence_mismatches.append(
                                "probabilistic_outcomes_binding"
                            )
                        if row.get("uncertainty") != expected_uncertainty:
                            consequence_ok = False
                            consequence_mismatches.append("uncertainty_binding")
                    if not consequence_ok:
                        complete = False
                        candidate_coverage["issues"] += 1
                        issue(
                            "candidate_consequence_binding_mismatch", index,
                            choice_id=choice_id,
                            fields=consequence_mismatches,
                        )
                        consequence_coverage["issues"] += 1
                    consequence_unknown_fields = []
                    independently_expected = _expected_visible_consequence(
                        phase, raw, record
                    )
                    legacy_deferred_projection = bool(
                        _legacy_grid_bottle_projection(
                            phase, raw, consequences, independently_expected
                        )
                        or _legacy_golden_event_projection(
                            phase, raw, consequences, independently_expected
                        )
                        or _legacy_golden_wing_terminal_projection(
                            phase, raw, consequences, independently_expected,
                            record,
                        )
                    )
                    target_claim_review = independently_expected.get(
                        "_target_claim_review"
                    )
                    target_claim_review = (
                        target_claim_review
                        if isinstance(target_claim_review, dict) else {}
                    )
                    if target_claim_review.get("contradictions"):
                        complete = False
                        candidate_coverage["issues"] += 1
                        consequence_coverage["issues"] += 1
                        issue(
                            "target_consequence_claim_contradicted", index,
                            choice_id=choice_id,
                            contradictions=target_claim_review[
                                "contradictions"
                            ],
                        )
                    if target_claim_review.get("unclassified"):
                        complete = False
                        candidate_coverage["unknown"] += 1
                        consequence_coverage["unknown"] += 1
                        unknown(
                            "target_consequence_claim_unclassified", index,
                            choice_id=choice_id,
                            fields=sorted(set(
                                target_claim_review["unclassified"]
                            )),
                        )
                    settlement_observation = (
                        _authoritative_settlement_observation(
                            record, choice_id, independently_expected
                        ) if row.get("selected") is True else None
                    )
                    if isinstance(consequences, dict):
                        knowledge = consequences.get("field_knowledge")
                        knowledge = knowledge if isinstance(knowledge, dict) else {}
                        classification = consequences.get(
                            "uncertainty_classification"
                        )
                        classification_status = (
                            classification.get("status")
                            if isinstance(classification, dict) else None
                        )
                        for field in _ORACLE_CONSEQUENCE_FIELDS:
                            field_knowledge = knowledge.get(field)
                            status = (
                                field_knowledge.get("status")
                                if isinstance(field_knowledge, dict) else None
                            )
                            if status in {
                                "known", "known_domain", "not_applicable",
                            }:
                                # ``not_applicable`` is intentionally encoded
                                # as null at queued protocol boundaries.  Only
                                # known numeric claims need a finite value (or
                                # an explicit domain); treating the null as an
                                # unknown makes deterministic HAND_SELECT
                                # confirmations fail the release gate.
                                if (
                                    field in {
                                        "hp_delta", "max_hp_delta",
                                        "gold_delta",
                                    }
                                    and status != "not_applicable"
                                ):
                                    numeric = consequences.get(field)
                                    numeric_domain = bool(
                                        status == "known_domain"
                                        and _numeric_domain_bounds(numeric)
                                        is not None
                                    )
                                    if (
                                        _finite_number(numeric) is None
                                        and not numeric_domain
                                    ):
                                        consequence_unknown_fields.append(field)
                                if field == "current_cost":
                                    cost = consequences.get(field)
                                    if not isinstance(cost, dict) or any(
                                        _finite_number(cost.get(name)) is None
                                        for name in ("gold", "hp", "max_hp")
                                    ):
                                        consequence_unknown_fields.append(field)
                                continue
                            if (
                                status == "not_observable"
                                and classification_status == "protocol_hidden"
                                and (
                                    classification.get("homogeneity_proven") is True
                                    or (
                                        field == "card_changes"
                                        and _oracle_game_id(
                                            consequences.get("event_id")
                                        ) in {"matchandkeep", "matchkeep"}
                                        and field_knowledge.get("authority")
                                        == "protocol_hidden_event_state"
                                    )
                                )
                            ):
                                continue
                            settled, _value, _authority = _expected_or_settled_value(
                                field, independently_expected,
                                settlement_observation,
                            )
                            if not settled:
                                consequence_unknown_fields.append(field)
                        if (
                            consequences.get("uncertainty")
                            and not legacy_deferred_projection
                            and not _classified_protocol_uncertainty(
                                phase, raw, consequences, record
                            )
                        ):
                            consequence_unknown_fields.append("uncertainty")
                        if legacy_deferred_projection:
                            consequence_unknown_fields = []
                    else:
                        consequence_unknown_fields.append("consequences")
                    claim_contradictions, claim_unknowns = (
                        _review_producer_consequence_claim(
                            record, raw, row, independently_expected,
                            settlement_observation,
                        )
                    )
                    producer_evidence = (
                        consequences.get("producer_evidence")
                        if isinstance(consequences, dict) else None
                    )
                    if isinstance(producer_evidence, dict) and producer_evidence:
                        complete = False
                        candidate_coverage["unknown"] += 1
                        consequence_coverage["unknown"] += 1
                        unknown(
                            "candidate_consequence_independence_unproven",
                            index,
                            choice_id=choice_id,
                        )
                    if claim_contradictions:
                        complete = False
                        consequence_coverage["issues"] += 1
                        candidate_coverage["issues"] += 1
                        issue(
                            "producer_consequence_claim_contradicted", index,
                            choice_id=choice_id,
                            contradictions=claim_contradictions,
                        )
                    if claim_unknowns:
                        complete = False
                        consequence_coverage["unknown"] += 1
                        candidate_coverage["unknown"] += 1
                        unknown(
                            "producer_consequence_claim_unclassified", index,
                            choice_id=choice_id,
                            fields=claim_unknowns,
                        )
                    if consequence_unknown_fields:
                        consequence_coverage["unknown"] += 1
                        unknown(
                            "candidate_consequence_fields_unverified", index,
                            choice_id=choice_id,
                            fields=sorted(set(consequence_unknown_fields)),
                        )
                    elif consequence_ok:
                        consequence_coverage["evaluated"] += 1
                if complete:
                    candidate_coverage["evaluated"] += 1

                selected_flags = [
                    str(row.get("choice_id")) for row in canonical
                    if isinstance(row, dict) and row.get("selected") is True
                ]
                selected_ids = record.get("selected_choice_ids")
                if not isinstance(selected_ids, list):
                    selected_id = record.get("selected_choice_id")
                    selected_ids = [selected_id] if selected_id is not None else selected_flags
                selected_ids = [str(value) for value in selected_ids if value is not None]
                selected_counts = Counter(selected_ids)
                selection_valid = bool(selected_ids)
                if not selected_ids:
                    selection_coverage["unknown"] += 1
                    unknown("selected_choice_missing", index)
                if selected_counts != Counter(selected_flags):
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "selected_choice_binding_mismatch", index,
                        declared=selected_ids, flagged=selected_flags,
                    )
                if any(choice_id not in expected_counts for choice_id in selected_ids):
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "selected_choice_binding_mismatch", index,
                        selected_choice_ids=selected_ids,
                    )
                requested = record.get("requested_target_id")
                resolved = record.get("resolved_target_id")
                selected_receipt_target = (
                    selected_ids[0] if len(selected_ids) == 1 else None
                )
                selected_operation = None
                if len(selected_ids) == 1:
                    selected_row = canonical_by_id.get(selected_ids[0])
                    if (
                        isinstance(selected_row, dict)
                        and selected_row.get("action") == "potion"
                    ):
                        selected_receipt_target = (
                            (selected_row.get("target") or {}).get(
                                "potion_instance_id"
                            )
                        )
                        selected_operation = selected_row.get("operation")
                if len(selected_ids) == 1 and (
                    str(requested or "") != str(selected_receipt_target or "")
                    or str(resolved or "")
                    != str(selected_receipt_target or "")
                    or (
                        selected_operation is not None
                        and str(record.get("potion_operation") or "").lower()
                        != str(selected_operation).lower()
                    )
                ):
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "selected_choice_binding_mismatch", index,
                        selected_choice_id=selected_ids[0],
                        selected_receipt_target=selected_receipt_target,
                        selected_operation=selected_operation,
                        requested_target_id=requested,
                        resolved_target_id=resolved,
                        observed_operation=record.get("potion_operation"),
                    )
                elif len(selected_ids) > 1 and (
                    requested not in selected_ids or resolved not in selected_ids
                ):
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "selected_choice_binding_mismatch", index,
                        selected_choice_ids=selected_ids,
                        requested_target_id=requested,
                        resolved_target_id=resolved,
                    )

                for selected_id in selected_ids:
                    row = canonical_by_id.get(selected_id)
                    if not isinstance(row, dict):
                        continue
                    consequences = row.get("consequences")
                    if isinstance(consequences, dict):
                        selected_consequences.append((selected_id, row, consequences))

                decision = record.get("decision")
                decision = decision if isinstance(decision, dict) else {}
                advice = decision.get("model_advice")
                advice = advice if isinstance(advice, dict) else {}
                final_ids = record.get("final_choice_ids")
                if not isinstance(final_ids, list):
                    final_ids = advice.get("final_choice_ids")
                final_ids = (
                    [str(value) for value in final_ids]
                    if isinstance(final_ids, list) else []
                )
                if final_ids != selected_ids:
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "final_choice_binding_mismatch", index,
                        final_choice_ids=final_ids,
                        selected_choice_ids=selected_ids,
                    )

                blind_review = _blind_consequence_review(
                    record, expected_by_id, canonical_by_id
                )
                blind_reviews.append(blind_review)
                blind_recommendation = blind_review.get(
                    "recommended_choice_id"
                )
                if (
                    blind_review.get("status") == "clear"
                    and len(selected_ids) == 1
                    and blind_recommendation != selected_ids[0]
                ):
                    selection_valid = False
                    selection_coverage["issues"] += 1
                    issue(
                        "independent_blind_review_disagreement", index,
                        selected_choice_id=selected_ids[0],
                        independently_recommended_choice_id=(
                            blind_recommendation
                        ),
                        review_reason=blind_review.get("reason"),
                    )

                # Every raw protocol-visible option remains audit-eligible.
                # Producer veto/selection_eligible flags are policy claims and
                # cannot make a legal choice disappear from this comparison.
                eligible_rows = [
                    row for row in canonical
                    if isinstance(row, dict)
                    and row.get("legal") is True
                    and row.get("visible") is True
                    and str(row.get("choice_id")) in expected_counts
                    and _oracle_immediately_actionable_choice(
                        record,
                        expected_by_id.get(str(row.get("choice_id"))),
                    )
                ]
                scored_rows = [
                    row for row in eligible_rows
                    if _finite_number(row.get("local_score")) is not None
                ]
                if len(scored_rows) != len(eligible_rows) or not scored_rows:
                    selection_valid = False
                    selection_coverage["unknown"] += 1
                    unknown(
                        "local_scores_incomplete", index,
                        eligible=len(eligible_rows), scored=len(scored_rows),
                    )
                elif len(selected_ids) == 1 and selected_ids[0] in canonical_by_id:
                    best_score = max(float(row["local_score"]) for row in scored_rows)
                    local_best = {
                        str(row["choice_id"]) for row in scored_rows
                        if abs(float(row["local_score"]) - best_score) <= 1e-9
                    }
                    selected_id = selected_ids[0]
                    selected_row = canonical_by_id[selected_id]
                    override = selected_row.get("override")
                    override = override if isinstance(override, dict) else {}
                    model_choice = _canonical_advice_choice_id(
                        advice.get("model_choice_id"), canonical_by_id
                    )
                    override_consistent = bool(
                        advice.get("applied") is True
                        and override.get("applied") is True
                        and override.get("gate_passed") is True
                        and str(model_choice or "") == selected_id
                        and final_ids == [selected_id]
                        and _finite_number(selected_row.get("model_score")) is not None
                        and _finite_number(selected_row.get("model_confidence")) is not None
                    )
                    if advice.get("applied") is True and not override_consistent:
                        selection_valid = False
                        selection_coverage["issues"] += 1
                        issue("invalid_model_override", index)
                    sequential_collection_only = (
                        _sapphire_sequential_collection_only(
                            record, expected_by_id, selected_id, local_best
                        )
                    )
                    if (
                        blind_review.get("status") == "clear"
                        and blind_recommendation not in local_best
                        and not override_consistent
                    ):
                        selection_valid = False
                        selection_coverage["issues"] += 1
                        issue(
                            "producer_score_order_contradicts_independent_consequences",
                            index,
                            independently_recommended_choice_id=(
                                blind_recommendation
                            ),
                            producer_local_best_choice_ids=sorted(local_best),
                        )
                    if (
                        selected_id not in local_best
                        and not sequential_collection_only
                    ):
                        review_supports_selected = bool(
                            blind_review.get("status") == "clear"
                            and blind_recommendation == selected_id
                        )
                        if not review_supports_selected:
                            selection_valid = False
                            confidence = _finite_number(
                                selected_row.get("model_confidence")
                            )
                            if confidence is None:
                                confidence = _finite_number(advice.get("confidence"))
                            candidate_contract = decision.get(
                                "candidate_contract"
                            )
                            candidate_contract = (
                                candidate_contract
                                if isinstance(candidate_contract, dict) else {}
                            )
                            explicit_local_argmax_contract = bool(
                                candidate_contract.get(
                                    "strategy_quality_auditable"
                                ) is True
                                and candidate_contract.get(
                                    "all_visible_options_scored"
                                ) is True
                                and not override_consistent
                                and advice.get("applied") is not True
                            )
                            if blind_review.get("status") == "clear":
                                pass
                            elif explicit_local_argmax_contract:
                                selection_coverage["issues"] += 1
                                issue(
                                    "local_argmax_missed",
                                    index,
                                    selected_choice_id=selected_id,
                                    local_best_choice_ids=sorted(local_best),
                                    score_source=candidate_contract.get(
                                        "score_source"
                                    ),
                                )
                            elif confidence is not None and confidence >= 0.8:
                                selection_coverage["issues"] += 1
                                issue(
                                    "high_confidence_unresolved_strategy_disagreement",
                                    index,
                                    selected_choice_id=selected_id,
                                    local_best_choice_ids=sorted(local_best),
                                    model_confidence=confidence,
                                )
                            else:
                                selection_coverage["unknown"] += 1
                                unknown(
                                    "nonlocal_selection_without_independent_blind_review",
                                    index,
                                    selected_choice_id=selected_id,
                                    local_best_choice_ids=sorted(local_best),
                                )
                    advised_choice = _canonical_advice_choice_id(
                        advice.get("model_choice_id"), canonical_by_id
                    )
                    advised_confidence = _finite_number(advice.get("confidence"))
                    if advised_confidence is None and advised_choice in canonical_by_id:
                        advised_confidence = _finite_number(
                            canonical_by_id[advised_choice].get("model_confidence")
                        )
                    if (
                        advised_choice
                        and advised_choice != selected_id
                        and advised_confidence is not None
                        and advised_confidence >= 0.8
                        and blind_review.get("status") != "clear"
                        and not (
                            blind_review.get("status") == "clear"
                            and blind_recommendation == selected_id
                        )
                    ):
                        selection_valid = False
                        selection_coverage["issues"] += 1
                        issue(
                            "high_confidence_unresolved_strategy_disagreement",
                            index,
                            selected_choice_id=selected_id,
                            model_choice_id=advised_choice,
                            model_confidence=advised_confidence,
                        )
                    rule_choice = _canonical_advice_choice_id(
                        advice.get("rule_choice_id"), canonical_by_id
                    )
                    claimed_conflict = bool(
                        advised_choice
                        and (
                            (rule_choice and advised_choice != rule_choice)
                            or advised_choice != selected_id
                            or (
                                selected_id not in local_best
                                and not sequential_collection_only
                            )
                        )
                    )
                    if claimed_conflict:
                        conflict_coverage = coverage[
                            "strategy_conflict_review"
                        ]
                        conflict_coverage["eligible"] += 1
                        if (
                            blind_review.get("status") == "clear"
                            and blind_recommendation == selected_id
                        ):
                            conflict_coverage["evaluated"] += 1
                        elif blind_review.get("status") == "clear":
                            conflict_coverage["issues"] += 1
                        elif (
                            advised_confidence is not None
                            and advised_confidence >= 0.8
                        ):
                            conflict_coverage["issues"] += 1
                        else:
                            conflict_coverage["unknown"] += 1
                            unknown(
                                "strategy_conflict_without_independent_review",
                                index,
                                selected_choice_id=selected_id,
                                local_best_choice_ids=sorted(local_best),
                                model_choice_id=advised_choice or None,
                                blind_review_reason=blind_review.get("reason"),
                            )
                if selection_valid:
                    selection_coverage["evaluated"] += 1

                for selected_id, row, consequences in selected_consequences:
                    if row.get("probability_outcomes") or row.get("uncertainty"):
                        raw = expected_by_id.get(selected_id)
                        selected_expected = (
                            _expected_visible_consequence(phase, raw, record)
                            if isinstance(raw, dict) else {}
                        )
                        classified_deferred = bool(
                            isinstance(raw, dict)
                            and _classified_protocol_uncertainty(
                                phase, raw, consequences, record
                            )
                        )
                        if (
                            not classified_deferred
                            and not _authoritative_settlement(
                                record, selected_id, selected_expected
                            )
                        ):
                            delta_coverage["unknown"] += 1
                            unknown(
                                "uncertain_consequence_settlement_unproven", index,
                                selected_choice_id=selected_id,
                            )

            before = _observable_from_authoritative_state(
                authoritative_states.get("before")
            )
            after = _observable_from_authoritative_state(
                authoritative_states.get("after")
            )
            if not isinstance(before, dict) or not isinstance(after, dict):
                delta_coverage["unknown"] += 1
                unknown("authoritative_observable_state_missing", index)
            else:
                delta_valid = True
                for side, observed in (
                    ("before", record.get("observable_state_before")),
                    ("after", record.get("observable_state_after")),
                ):
                    expected_observable = before if side == "before" else after
                    if not isinstance(observed, dict):
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "producer_observable_state_missing", index,
                            side=side,
                        )
                    elif _freeze(observed) != _freeze(expected_observable):
                        delta_valid = False
                        delta_coverage["issues"] += 1
                        issue(
                            "producer_observable_state_mismatch", index,
                            side=side,
                            observed=observed,
                            independently_reconstructed=expected_observable,
                        )
                outcome = record.get("decision_outcome")
                outcome = outcome if isinstance(outcome, dict) else {}
                numeric_claims = (
                    ("current_hp", ("current_hp_delta", "hp_delta")),
                    ("max_hp", ("max_hp_delta",)),
                    ("gold", ("gold_delta",)),
                    ("block", ("block_delta",)),
                )
                for state_field, claim_names in numeric_claims:
                    before_value = _finite_number(_state_value(before, state_field))
                    after_value = _finite_number(_state_value(after, state_field))
                    if before_value is None or after_value is None:
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "observable_delta_input_missing", index,
                            field=state_field,
                        )
                        continue
                    present = [name for name in claim_names if name in outcome]
                    if not present:
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "observable_delta_claim_missing", index,
                            field=state_field,
                        )
                        continue
                    claimed_values = [
                        (name, _finite_number(outcome.get(name))) for name in present
                    ]
                    if any(value is None for _name, value in claimed_values):
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "observable_delta_claim_unparseable", index,
                            field=state_field,
                            claims=present,
                        )
                        continue
                    distinct = {value for _name, value in claimed_values}
                    observed_delta = after_value - before_value
                    if len(distinct) != 1 or any(
                        abs(observed_delta - value) > 1e-9
                        for _name, value in claimed_values
                    ):
                        delta_valid = False
                        delta_coverage["issues"] += 1
                        issue(
                            "observable_delta_mismatch", index,
                            field=state_field,
                            claimed={name: value for name, value in claimed_values},
                            observed=observed_delta,
                        )

                observed_collections = {}
                for state_field in ("deck", "relics", "potions"):
                    observed_delta = _observable_collection_delta(
                        _state_value(before, state_field),
                        _state_value(after, state_field),
                    )
                    observed_collections[state_field] = observed_delta
                    if observed_delta is None:
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "observable_delta_input_missing", index,
                            field=state_field,
                        )
                    elif state_field not in outcome:
                        delta_valid = False
                        delta_coverage["unknown"] += 1
                        unknown(
                            "observable_delta_claim_missing", index,
                            field=state_field,
                        )
                    elif not _structured_delta_equal(
                        outcome.get(state_field), observed_delta
                    ):
                        delta_valid = False
                        delta_coverage["issues"] += 1
                        issue(
                            "observable_delta_mismatch", index,
                            field=state_field,
                            claimed=outcome.get(state_field),
                            observed=observed_delta,
                        )

                before_keys = before.get("keys")
                after_keys = after.get("keys")
                if not isinstance(before_keys, dict) or not isinstance(after_keys, dict):
                    delta_valid = False
                    delta_coverage["unknown"] += 1
                    unknown("observable_delta_input_missing", index, field="keys")
                elif "keys_before" not in outcome or "keys_after" not in outcome:
                    delta_valid = False
                    delta_coverage["unknown"] += 1
                    unknown("observable_delta_claim_missing", index, field="keys")
                elif outcome.get("keys_before") != before_keys or outcome.get("keys_after") != after_keys:
                    delta_valid = False
                    delta_coverage["issues"] += 1
                    issue(
                        "observable_delta_mismatch", index, field="keys",
                        claimed={
                            "before": outcome.get("keys_before"),
                            "after": outcome.get("keys_after"),
                        },
                        observed={"before": before_keys, "after": after_keys},
                    )

                for _selected_id, _row, consequences in selected_consequences:
                    mausoleum_evidence = (
                        mausoleum_realized_settlement(record)
                        if consequences.get("operation")
                        == "mausoleum_open_coffin" else None
                    )
                    legacy_gap = _legacy_deferred_consequence_gap(
                        record, consequences
                    )
                    deferred_fields = (
                        legacy_gap.get("deferred_fields", frozenset())
                        if isinstance(legacy_gap, dict) else frozenset()
                    )
                    for claim, state_field in _NUMERIC_DELTA_FIELDS.items():
                        claimed = _finite_number(consequences.get(claim))
                        if claimed is None:
                            continue
                        if (
                            isinstance(mausoleum_evidence, dict)
                            and mausoleum_evidence.get("status") == "clear"
                        ):
                            continue
                        if claim in deferred_fields:
                            delta_valid = False
                            delta_coverage["unknown"] += 1
                            unknown(
                                "historical_deferred_consequence_contract_incomplete",
                                index,
                                field=f"selected_consequence.{claim}",
                                contract_kind=legacy_gap.get("kind"),
                                reason=legacy_gap.get("reason"),
                            )
                            continue
                        before_value = _finite_number(
                            _state_value(before, state_field)
                        )
                        after_value = _finite_number(
                            _state_value(after, state_field)
                        )
                        if before_value is None or after_value is None:
                            delta_valid = False
                            delta_coverage["unknown"] += 1
                            unknown(
                                "observable_delta_input_missing", index,
                                field=state_field,
                            )
                            continue
                        observed_consequence_delta = after_value - before_value
                        if claim == "hp_delta":
                            background_potion_heal = (
                                _oracle_combat_choice_background_potion_heal(
                                    phase,
                                    before,
                                    after,
                                    outcome,
                                    observed_collections.get("potions"),
                                )
                            )
                            observed_consequence_delta -= (
                                background_potion_heal
                            )
                            event_entry_heal = (
                                _oracle_event_downstream_combat_entry_hp_delta(
                                    record
                                )
                            )
                            if event_entry_heal is not None:
                                observed_consequence_delta -= event_entry_heal
                            downstream_entry_heal = (
                                _oracle_random_route_downstream_entry_hp_delta(
                                    record,
                                    (_row.get("target") or {})
                                    if isinstance(_row, dict) else {},
                                )
                            )
                            if (
                                downstream_entry_heal is not None
                                and abs(
                                    observed_consequence_delta
                                    - downstream_entry_heal
                                ) <= 1e-9
                            ):
                                observed_consequence_delta = 0
                        if abs(
                            observed_consequence_delta - claimed
                        ) > 1e-9:
                            delta_valid = False
                            delta_coverage["issues"] += 1
                            issue(
                                "observable_delta_mismatch", index,
                                field=f"selected_consequence.{claim}",
                                claimed=claimed,
                                observed=observed_consequence_delta,
                            )
                    for state_field, consequence_field in (
                        ("deck", "card_changes"),
                        ("relics", "relic_changes"),
                        ("potions", "potion_changes"),
                    ):
                        changes = consequences.get(consequence_field)
                        if not isinstance(changes, dict):
                            continue
                        observed_delta = observed_collections.get(state_field)
                        if not isinstance(observed_delta, dict):
                            continue
                        for claim_field, observed_field in (
                            ("gain", "added"), ("remove", "removed"),
                        ):
                            claimed_items = changes.get(claim_field)
                            if not isinstance(claimed_items, list) or not claimed_items:
                                continue
                            deferred_field = (
                                f"{consequence_field}.{claim_field}"
                            )
                            if deferred_field in deferred_fields:
                                delta_valid = False
                                delta_coverage["unknown"] += 1
                                unknown(
                                    "historical_deferred_consequence_contract_incomplete",
                                    index,
                                    field=(
                                        "selected_consequence."
                                        f"{deferred_field}"
                                    ),
                                    contract_kind=legacy_gap.get("kind"),
                                    reason=legacy_gap.get("reason"),
                                )
                                continue
                            if not _effect_item_multiset_equal(
                                consequence_field,
                                claimed_items,
                                observed_delta[observed_field],
                                allow_new_instance=(
                                    consequence_field == "card_changes"
                                    and claim_field == "gain"
                                ),
                            ):
                                delta_valid = False
                                delta_coverage["issues"] += 1
                                issue(
                                    "observable_delta_mismatch", index,
                                    field=(
                                        f"selected_consequence.{consequence_field}."
                                        f"{claim_field}"
                                    ),
                                    claimed=claimed_items,
                                    observed=observed_delta[observed_field],
                                )
                if delta_valid:
                    delta_coverage["evaluated"] += 1

        model = record.get("damage_model")
        if isinstance(model, dict):
            damage_coverage = coverage["damage_consistency"]
            observed_damage = _authoritative_damage_observed(record, expected)
            if (
                "hero_to_monsters_predicted_min" in model
                or "hero_to_monsters_predicted_max" in model
            ):
                damage_coverage["eligible"] += 1
                minimum = _finite_number(
                    model.get("hero_to_monsters_predicted_min")
                )
                maximum = _finite_number(
                    model.get("hero_to_monsters_predicted_max")
                )
                actual = _finite_number(
                    model.get("hero_to_monsters_actual")
                )
                observed = (
                    _finite_number(observed_damage.get("hero_to_monsters"))
                    if isinstance(observed_damage, dict) else None
                )
                missing = []
                if minimum is None:
                    missing.append("minimum_missing")
                if maximum is None:
                    missing.append("maximum_missing")
                if actual is None:
                    missing.append("actual_missing")
                if observed is None:
                    missing.append("authoritative_observation_missing")
                if missing:
                    damage_coverage["unknown"] += 1
                    unknown(
                        "damage_prediction_bounds_not_comparable", index,
                        minimum=minimum, maximum=maximum, actual=actual,
                        observed=observed, reason_codes=missing,
                    )
                else:
                    bounds_valid = True
                    if minimum > maximum:
                        bounds_valid = False
                        damage_coverage["issues"] += 1
                        issue(
                            "damage_prediction_bounds_invalid", index,
                            minimum=minimum, maximum=maximum,
                        )
                    if model.get("hero_to_monsters_prediction_basis") == (
                        "current_card_with_juggernaut_block_gain_bounds"
                    ):
                        independent_contract = (
                            _oracle_juggernaut_block_gain_contract(record)
                        )
                        published_contract = model.get(
                            "juggernaut_block_gain_contract"
                        )
                        base_minimum = _finite_number(
                            model.get("juggernaut_base_predicted_min")
                        )
                        base_maximum = _finite_number(
                            model.get("juggernaut_base_predicted_max")
                        )
                        before_game = _oracle_before_game(record)
                        living_hp = sum(
                            max(0, int(monster.get("current_hp") or 0))
                            for monster in (
                                before_game.get("combat_state") or {}
                            ).get("monsters") or []
                            if isinstance(monster, dict)
                            and monster.get("is_gone") is not True
                            and monster.get("half_dead") is not True
                        )
                        expected_minimum = (
                            min(living_hp, base_minimum)
                            if base_minimum is not None else None
                        )
                        expected_maximum = (
                            min(
                                living_hp,
                                base_maximum
                                + independent_contract["trigger_count"]
                                * independent_contract["damage_per_trigger"],
                            )
                            if base_maximum is not None
                            and isinstance(independent_contract, dict)
                            else None
                        )
                        if not (
                            isinstance(independent_contract, dict)
                            and _freeze(published_contract)
                            == _freeze(independent_contract)
                            and base_minimum is not None
                            and base_maximum is not None
                            and base_minimum <= base_maximum
                            and minimum == expected_minimum
                            and maximum == expected_maximum
                        ):
                            bounds_valid = False
                            damage_coverage["issues"] += 1
                            issue(
                                "juggernaut_damage_bound_contract_mismatch",
                                index,
                                published_contract=published_contract,
                                independently_expected=independent_contract,
                                published_base_minimum=base_minimum,
                                published_base_maximum=base_maximum,
                                expected_minimum=expected_minimum,
                                expected_maximum=expected_maximum,
                                published_minimum=minimum,
                                published_maximum=maximum,
                            )
                    if abs(actual - observed) > 1e-9:
                        bounds_valid = False
                        damage_coverage["issues"] += 1
                        issue(
                            "damage_observed_claim_mismatch", index,
                            actual_field="hero_to_monsters_actual",
                            claimed_actual=actual,
                            independently_observed=observed,
                        )
                    if not (minimum <= observed <= maximum):
                        bounds_valid = False
                        damage_coverage["issues"] += 1
                        issue(
                            "damage_prediction_outside_bounds", index,
                            minimum=minimum, maximum=maximum,
                            independently_observed=observed,
                        )
                    if bounds_valid:
                        damage_coverage["evaluated"] += 1
            pairs = (
                (
                    "hero_to_monsters_predicted",
                    "hero_to_monsters_actual",
                    "hero_to_monsters",
                ),
                (
                    "monsters_to_hero_predicted",
                    "monsters_to_hero_actual",
                    "monsters_to_hero",
                ),
            )
            for predicted_field, actual_field, observed_field in pairs:
                if (
                    observed_field == "hero_to_monsters"
                    and (
                        "hero_to_monsters_predicted_min" in model
                        or "hero_to_monsters_predicted_max" in model
                    )
                ):
                    continue
                if predicted_field not in model and actual_field not in model:
                    continue
                if (
                    model.get(predicted_field) is None
                    and model.get(actual_field) is None
                ):
                    # Older traces serialized both irrelevant directions as
                    # explicit nulls.  Null/null is no prediction claim and
                    # therefore has nothing to audit.
                    continue
                damage_coverage["eligible"] += 1
                predicted = _finite_number(model.get(predicted_field))
                actual = _finite_number(model.get(actual_field))
                observed = (
                    _finite_number(observed_damage.get(observed_field))
                    if isinstance(observed_damage, dict) else None
                )
                observed_actual = observed
                observed_predicted = observed
                prediction_observation = observed_damage
                deferred_settlement = None
                if (
                    observed_field == "hero_to_monsters"
                    and isinstance(observed_damage, dict)
                    and str(model.get(
                        "hero_to_monsters_prediction_basis"
                    ) or "") == "current_card_final_target_hp_projection"
                ):
                    bound_target_observed = _finite_number(
                        observed_damage.get(
                            "hero_to_monsters_bound_target"
                        )
                    )
                    if bound_target_observed is not None:
                        observed_predicted = bound_target_observed
                if (
                    observed_field == "hero_to_monsters"
                    and observed == 0
                    and record.get("action") == "play"
                ):
                    deferred_settlement = _deferred_card_selection_damage(
                        records, index, record, expected
                    )
                    if isinstance(deferred_settlement, dict):
                        deferred_observed = deferred_settlement.get(
                            "observed_damage"
                        )
                        deferred_observed = (
                            deferred_observed
                            if isinstance(deferred_observed, dict)
                            else {}
                        )
                        deferred_damage = _finite_number(
                            deferred_observed.get("hero_to_monsters")
                        )
                        if deferred_damage is not None:
                            # The producer's actual field correctly describes
                            # the immediate PLAY receipt. Only its prediction
                            # is paired with the later queued-effect delta.
                            observed_predicted = deferred_damage
                            prediction_observation = deferred_observed
                if observed_field == "monsters_to_hero" and isinstance(
                    observed_damage, dict
                ):
                    gross_before_healing = _finite_number(
                        observed_damage.get(
                            "monsters_to_hero_before_healing"
                        )
                    )
                    basis = str(
                        model.get("monsters_to_hero_basis") or ""
                    )
                    if (
                        basis == "end_turn_player_hp_delta"
                        and gross_before_healing is not None
                    ):
                        # Legacy telemetry paired a gross prediction with the
                        # stable net-HP actual.  Both are independently
                        # comparable when the exact intervening heal is known.
                        observed_predicted = gross_before_healing
                    elif basis in {
                        "end_turn_total_hp_loss_before_postcombat_healing",
                        "end_turn_total_hp_loss_before_fairy_revival",
                    }:
                        observed_actual = gross_before_healing
                        observed_predicted = gross_before_healing
                    if (
                        record.get("action") == "end"
                        and observed == 0
                        and basis == "end_turn_player_hp_delta"
                    ):
                        deferred_settlement = (
                            _deferred_hand_select_end_damage(
                                records, index, record, expected
                            )
                        )
                        if isinstance(deferred_settlement, dict):
                            deferred_observed = deferred_settlement.get(
                                "observed_damage"
                            )
                            deferred_observed = (
                                deferred_observed
                                if isinstance(deferred_observed, dict)
                                else {}
                            )
                            deferred_loss = _finite_number(
                                deferred_observed.get("monsters_to_hero")
                            )
                            if deferred_loss is not None:
                                # The producer's actual field describes the
                                # immediate END receipt and correctly remains
                                # zero. Only the prediction settles against
                                # the later enemy-turn delta.
                                observed_predicted = deferred_loss
                                prediction_observation = deferred_observed
                uncertain = (
                    model.get("deterministic") is False
                    or str(model.get("prediction_certainty") or "").lower()
                    in {"uncertain", "heuristic", "unknown"}
                )
                reason_codes = []
                if predicted is None:
                    reason_codes.append("predicted_missing")
                if actual is None:
                    reason_codes.append("actual_missing")
                if observed_actual is None:
                    reason_codes.append("authoritative_actual_missing")
                if observed_predicted is None:
                    reason_codes.append("authoritative_predicted_missing")
                if uncertain:
                    reason_codes.append("uncertain")
                if reason_codes:
                    prediction_basis = model.get(
                        f"{observed_field}_prediction_basis"
                    )
                    if prediction_basis is None:
                        prediction_basis = model.get(
                            f"{observed_field}_basis"
                        )
                    damage_coverage["unknown"] += 1
                    unknown(
                        "damage_prediction_not_comparable", index,
                        predicted_field=predicted_field,
                        actual_field=actual_field,
                        observed_field=observed_field,
                        phase=record.get("phase"),
                        action=record.get("action"),
                        prediction_basis=prediction_basis,
                        predicted=predicted,
                        actual=actual,
                        observed=observed,
                        observed_actual=observed_actual,
                        observed_predicted=observed_predicted,
                        reason_codes=reason_codes,
                    )
                else:
                    pair_valid = True
                    comparable_actual = _damage_claim_with_authoritative_lethal_cap(
                        actual, observed_field, observed_damage
                    )
                    comparable_predicted = (
                        _damage_claim_with_authoritative_lethal_cap(
                            predicted, observed_field,
                            prediction_observation,
                        )
                    )
                    if abs(comparable_actual - observed_actual) > 1e-9:
                        pair_valid = False
                        damage_coverage["issues"] += 1
                        issue(
                            "damage_observed_claim_mismatch", index,
                            actual_field=actual_field,
                            claimed_actual=actual,
                            independently_observed=observed_actual,
                        )
                    if (
                        abs(comparable_predicted - observed_predicted) > 1e-9
                        and not _duplicated_attack_prediction_alias(
                            record, model, predicted, observed_predicted
                        )
                        and not _terminal_minion_departure_prediction_alias(
                            record,
                            model,
                            predicted,
                            observed_predicted,
                            observed_damage,
                        )
                    ):
                        pair_valid = False
                        damage_coverage["issues"] += 1
                        issue(
                            "damage_prediction_mismatch", index,
                            predicted_field=predicted_field,
                            predicted=predicted,
                            independently_observed=observed_predicted,
                            deferred_settlement_record_index=(
                                deferred_settlement.get("record_index")
                                if isinstance(deferred_settlement, dict)
                                else None
                            ),
                        )
                    if pair_valid:
                        damage_coverage["evaluated"] += 1

        if combat_id in pending_combat_end:
            pending = pending_combat_end[combat_id]
            claim_index = pending["record_index"]
            claim_turn = pending["turn"]
            living = _living_monsters(record)
            later_turn = (
                _finite_number(record.get("turn")) is not None
                and _finite_number(claim_turn) is not None
                and float(record.get("turn")) > float(claim_turn)
            )
            later_active_action = record.get("action") in {"play", "potion"}
            if living and (later_turn or later_active_action):
                coverage["true_combat_end"]["issues"] += 1
                issue(
                    "true_combat_end_contradicted", index,
                    asserted_record_index=claim_index,
                    combat_id=combat_id,
                    living_enemy_count=len(living),
                )
                pending_combat_end.pop(combat_id, None)
            elif living == [] and index > claim_index:
                coverage["true_combat_end"]["evaluated"] += 1
                pending_combat_end.pop(combat_id, None)

        if combat_id and _true_combat_end(record):
            end_coverage = coverage["true_combat_end"]
            end_coverage["eligible"] += 1
            (
                after_living, room_phase_after, screen_type_after,
                _player_after, _monsters_after, after_authoritative,
            ) = (
                _authoritative_combat_after(record, expected)
            )
            exited_combat = bool(
                (room_phase_after is not None and str(room_phase_after).upper() != "COMBAT")
                or str(screen_type_after or "").upper() == "GAME_OVER"
            )
            if after_authoritative and after_living:
                end_coverage["issues"] += 1
                issue(
                    "true_combat_end_contradicted", index,
                    combat_id=combat_id,
                    living_enemy_count=len(after_living),
                    evidence="authoritative_after_state",
                )
            elif after_authoritative and after_living == [] and exited_combat:
                end_coverage["evaluated"] += 1
            else:
                pending_combat_end.setdefault(combat_id, {
                    "record_index": index,
                    "turn": record.get("turn"),
                    "after_living": after_living,
                    "room_phase_after": room_phase_after,
                    "screen_type_after": screen_type_after,
                })

    # Independently consume the explicit base-game observable handlers.
    # Capability labels alone never clear a mechanism: every eligible record
    # above must have a raw reward/counter/card lifecycle proof here.
    mechanism_audit = _audit_base_game_mechanisms(records)
    mechanism_bucket = coverage["observable_state_deltas"]
    mechanism_bucket["eligible"] += mechanism_audit["eligible"]
    mechanism_bucket["evaluated"] += mechanism_audit["evaluated"]
    mechanism_bucket["issues"] += len(mechanism_audit["issues"])
    mechanism_bucket["unknown"] += len(mechanism_audit["unknowns"])
    for finding in mechanism_audit["issues"]:
        details = dict(finding)
        kind = details.pop("kind")
        record_index = details.pop("record_index")
        issue(kind, record_index, **details)
    for finding in mechanism_audit["unknowns"]:
        details = dict(finding)
        kind = details.pop("kind")
        record_index = details.pop("record_index")
        unknown(kind, record_index, **details)

    # Multi-stage protocol choices are not complete at their first receipt.
    # Reconstruct the later confirmation from authoritative state frames so a
    # producer cannot declare a purge/upgrade/transform successful by merely
    # labelling it as a classified future effect.
    deferred_grid = {}
    deferred_shop = []
    legacy_deferred = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("record_type") != "decision":
            continue
        selected_row, effects = _selected_deferred_effects(record)
        selected_consequences = (
            selected_row.get("consequences")
            if isinstance(selected_row, dict) else None
        )
        legacy_gap = _legacy_deferred_consequence_gap(
            record, selected_consequences
        )
        if legacy_gap is not None:
            legacy_deferred[index] = legacy_gap
        for effect in effects:
            kind = effect.get("kind")
            if kind == "grid_confirmation_effect":
                deferred_grid[index] = effect
            elif kind == "shop_purge_grid_selection":
                deferred_shop.append((index, record, effect))

    delta_coverage = coverage["observable_state_deltas"]
    shop_parent_by_grid = {}
    for shop_index, shop_record, shop_effect in deferred_shop:
        delta_coverage["eligible"] += 1
        grid_index, grid_record = _next_decision_record(records, shop_index)
        _grid_row, grid_effects = _selected_deferred_effects(grid_record)
        matching_grid = [
            effect for effect in grid_effects
            if effect.get("kind") == "grid_confirmation_effect"
            and effect.get("operation") == "grid_purge"
        ]
        price = _finite_number(shop_effect.get("gold_cost"))
        legacy_gap = legacy_deferred.get(shop_index)
        legacy_grid_gap = legacy_deferred.get(grid_index)
        legacy_pair = bool(
            isinstance(legacy_gap, dict)
            and legacy_gap.get("kind")
            == "legacy_shop_purge_commit_contract"
            and isinstance(legacy_grid_gap, dict)
            and legacy_grid_gap.get("kind")
            == "legacy_grid_purge_commit_contract"
        )
        structural_match = bool(
            grid_record is not None
            and str(grid_record.get("phase") or "").upper() == "GRID"
            and grid_record.get("before_seq") == shop_record.get("after_seq")
            and (len(matching_grid) == 1 or legacy_pair)
        )
        if grid_record is None:
            delta_coverage["unknown"] += 1
            unknown(
                "deferred_choice_settlement_missing", shop_index,
                effect_kind="shop_purge_grid_selection",
                reason="subsequent_grid_decision_missing",
            )
        elif not structural_match:
            delta_coverage["issues"] += 1
            issue(
                "deferred_choice_settlement_mismatch", shop_index,
                effect_kind="shop_purge_grid_selection",
                reason="shop_purge_to_grid_binding_mismatch",
                grid_record_index=grid_index,
            )
        elif price is None or price < 0:
            if (
                isinstance(legacy_gap, dict)
                and legacy_gap.get("kind")
                == "legacy_shop_purge_commit_contract"
            ):
                delta_coverage["unknown"] += 1
                unknown(
                    "deferred_choice_settlement_unproven", shop_index,
                    effect_kind="shop_purge_grid_selection",
                    reason=legacy_gap.get("reason"),
                    grid_record_index=grid_index,
                )
            else:
                delta_coverage["issues"] += 1
                issue(
                    "deferred_choice_settlement_mismatch", shop_index,
                    effect_kind="shop_purge_grid_selection",
                    reason="shop_purge_cost_binding_missing_or_invalid",
                    grid_record_index=grid_index,
                )
        elif grid_index in shop_parent_by_grid:
            delta_coverage["issues"] += 1
            issue(
                "deferred_choice_settlement_mismatch", shop_index,
                effect_kind="shop_purge_grid_selection",
                reason="grid_has_multiple_shop_purge_parents",
                grid_record_index=grid_index,
            )
        else:
            shop_parent_by_grid[grid_index] = {
                "shop_record_index": shop_index,
                "gold_cost": price,
            }

    for grid_index, legacy_gap in legacy_deferred.items():
        if legacy_gap.get("kind") != "legacy_grid_purge_commit_contract":
            continue
        delta_coverage["eligible"] += 1
        delta_coverage["unknown"] += 1
        unknown(
            "deferred_choice_settlement_unproven", grid_index,
            effect_kind="grid_confirmation_effect",
            reason=legacy_gap.get("reason"),
        )

    settled_grid_indices = set()
    for grid_index, grid_effect in deferred_grid.items():
        if grid_index in settled_grid_indices:
            continue
        delta_coverage["eligible"] += 1
        grid_record = records[grid_index]
        settlement_status, details = _grid_deferred_settlement(
            records, grid_index, grid_record, grid_effect, expected
        )
        settled_grid_indices.update(
            index for index in details.get(
                "settled_grid_record_indices", [grid_index]
            )
            if type(index) is int
        )
        parent = shop_parent_by_grid.get(grid_index)
        expected_gold_delta = -parent["gold_cost"] if parent is not None else 0
        if settlement_status == "clear" and abs(
            float(details.get("gold_delta")) - float(expected_gold_delta)
        ) > 1e-9:
            settlement_status = "issue"
            details = {
                **details,
                "reason": "grid_confirmation_gold_delta_mismatch",
                "expected_gold_delta": expected_gold_delta,
            }
        if settlement_status == "clear":
            delta_coverage["evaluated"] += 1
            if parent is not None:
                delta_coverage["evaluated"] += 1
        elif settlement_status == "issue":
            delta_coverage["issues"] += 1
            issue(
                "deferred_choice_settlement_mismatch", grid_index,
                effect_kind="grid_confirmation_effect", **details,
            )
            if parent is not None:
                delta_coverage["issues"] += 1
                issue(
                    "deferred_choice_settlement_mismatch",
                    parent["shop_record_index"],
                    effect_kind="shop_purge_grid_selection",
                    downstream_grid_record_index=grid_index,
                    reason="downstream_grid_confirmation_failed",
                )
        else:
            delta_coverage["unknown"] += 1
            unknown(
                "deferred_choice_settlement_unproven", grid_index,
                effect_kind="grid_confirmation_effect", **details,
            )
            if parent is not None:
                delta_coverage["unknown"] += 1
                unknown(
                    "deferred_choice_settlement_unproven",
                    parent["shop_record_index"],
                    effect_kind="shop_purge_grid_selection",
                    downstream_grid_record_index=grid_index,
                    reason="downstream_grid_confirmation_unproven",
                )

    for combat_id, pending in pending_combat_end.items():
        terminal_after_claim = bool(
            len(terminals) == 1
            and terminals[0][0] > pending["record_index"]
            and _terminal_is_authoritative(terminal, artifacts)
            and terminal.get("victory") is True
        )
        if terminal_after_claim:
            coverage["true_combat_end"]["evaluated"] += 1
        else:
            coverage["true_combat_end"]["unknown"] += 1
            unknown(
                "true_combat_end_unproven",
                pending["record_index"],
                combat_id=combat_id,
            )

    # All four immutable terminal snapshots must carry the same ten-field
    # binding.  Missing artifacts are explicit unknowns, never an optional
    # integration path.
    artifact_binding = coverage["protocol_binding"]
    artifact_names = ("run_context", "run_result", "state", "selection")
    for artifact_name in artifact_names:
        artifact = (
            artifacts.get(artifact_name)
            if isinstance(artifacts, dict) else None
        )
        if not isinstance(artifact, dict):
            artifact_binding["eligible"] += 1
            artifact_binding["unknown"] += 1
            unknown("artifact_missing_or_invalid", artifact=artifact_name)
            continue
        fields = dict(terminal_expected)
        if artifact_name == "state":
            fields.pop("schema_version", None)
            artifact_binding["eligible"] += 1
            if artifact.get("protocol_version") != 2:
                artifact_binding["issues"] += 1
                issue(
                    "artifact_protocol_version_mismatch",
                    artifact=artifact_name,
                    expected=2,
                    observed=artifact.get("protocol_version"),
                )
            else:
                artifact_binding["evaluated"] += 1
        for field, wanted in fields.items():
            artifact_binding["eligible"] += 1
            observed = artifact.get(field)
            if observed is None or wanted is None:
                artifact_binding["unknown"] += 1
                unknown(
                    "artifact_binding_field_missing",
                    artifact=artifact_name,
                    field=field,
                )
            elif type(observed) is not type(wanted) or observed != wanted:
                artifact_binding["issues"] += 1
                issue(
                    "artifact_binding_mismatch", artifact=artifact_name,
                    field=field, expected=wanted,
                    observed=observed,
                )
            else:
                artifact_binding["evaluated"] += 1

    terminal_game = _terminal_game(artifacts)
    authoritative_acts = []
    if isinstance(terminal_game, dict) and type(terminal_game.get("act")) is int:
        authoritative_acts.append(terminal_game["act"])
    for record in records:
        if not isinstance(record, dict) or record.get("record_type") != "decision":
            continue
        for side in ("before", "after"):
            envelope, _missing, _mismatches = _state_envelope(
                record, side, expected
            )
            game = _game_from_state(envelope)
            if isinstance(game, dict) and type(game.get("act")) is int:
                authoritative_acts.append(game["act"])

    act4_coverage = coverage["act4_authority"]
    act4_coverage["eligible"] += 1
    claimed_max_act = terminal.get("observed_max_act", terminal.get("act"))
    if type(claimed_max_act) is not int or not authoritative_acts:
        act4_coverage["unknown"] += 1
        unknown(
            "act4_observation_unproven",
            claimed_observed_max_act=claimed_max_act,
        )
    else:
        authoritative_max_act = max(authoritative_acts)
        if claimed_max_act != authoritative_max_act:
            act4_coverage["issues"] += 1
            issue(
                "act4_observation_mismatch",
                claimed_observed_max_act=claimed_max_act,
                authoritative_observed_max_act=authoritative_max_act,
            )
        else:
            act4_coverage["evaluated"] += 1

    heart_coverage = coverage["heart_defeat_authority"]
    heart_coverage["eligible"] += 1
    claimed_heart = terminal.get("heart_defeated")
    authoritative_heart = (
        terminal_game.get("heart_defeated")
        if isinstance(terminal_game, dict) else None
    )
    authoritative_victory = _terminal_victory_flags(terminal_game)
    claimed_victory = terminal.get("victory")
    if (
        type(claimed_heart) is not bool
        or type(authoritative_heart) is not bool
        or type(claimed_victory) is not bool
        or authoritative_victory is None
    ):
        heart_coverage["unknown"] += 1
        unknown(
            "heart_terminal_outcome_unproven",
            claimed_heart_defeated=claimed_heart,
            authoritative_heart_defeated=authoritative_heart,
            claimed_victory=claimed_victory,
            authoritative_victory=authoritative_victory,
        )
    elif authoritative_victory == "conflict":
        heart_coverage["issues"] += 1
        issue("authoritative_victory_flags_conflict")
    elif (
        claimed_heart != authoritative_heart
        or claimed_victory != authoritative_victory
    ):
        heart_coverage["issues"] += 1
        issue(
            "heart_terminal_outcome_mismatch",
            claimed={
                "heart_defeated": claimed_heart,
                "victory": claimed_victory,
            },
            authoritative={
                "heart_defeated": authoritative_heart,
                "victory": authoritative_victory,
            },
        )
    elif claimed_heart and (
        not authoritative_acts or max(authoritative_acts) < 4
    ):
        heart_coverage["issues"] += 1
        issue("heart_defeat_without_authoritative_act4")
    elif claimed_heart and not claimed_victory:
        heart_coverage["issues"] += 1
        issue("heart_defeat_without_authoritative_victory")
    else:
        heart_coverage["evaluated"] += 1

    for bucket in coverage.values():
        if bucket["issues"]:
            bucket["status"] = "issues"
        elif bucket["unknown"]:
            bucket["status"] = "inconclusive"
        elif bucket["eligible"]:
            bucket["status"] = "clear"
        else:
            bucket["status"] = "not_applicable"

    disagreement_count = sum(
        1 for item in issues if item.get("kind") in DISAGREEMENT_KINDS
    )
    status = "issues" if issues else "inconclusive" if unknowns else "clear"
    return {
        "oracle_version": ORACLE_VERSION,
        "coverage_contract_version": ORACLE_COVERAGE_CONTRACT_VERSION,
        "binding_fields": list(ATTEMPT_BINDING_FIELDS) + [
            "terminal_state_seq"
        ],
        "required_coverage_keys": sorted(REQUIRED_COVERAGE_KEYS),
        "disagreement_kinds": sorted(DISAGREEMENT_KINDS),
        "decision_hash": decision_hash,
        "attempt_id": attempt_id,
        "status": status,
        "issue_count": len(issues),
        "eligible_unknown_count": len(unknowns),
        "disagreement_count": disagreement_count,
        "coverage": coverage,
        "issues": issues,
        "unknowns": unknowns,
        "blind_reviews": blind_reviews,
        "base_game_mechanisms": mechanism_audit,
        "combat_choice_transitions": combat_choice_audit["transitions"],
        "combat_choice_settlements": combat_choice_audit["settlements"],
    }
