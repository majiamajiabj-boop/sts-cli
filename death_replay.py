"""Fail-closed death replay extraction from authoritative trace records.

The extractor is intentionally simulation-free.  It never calls the
production planner and labels every unchosen-action claim by evidence level.
"""

from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import tempfile


DEATH_REPLAY_SCHEMA_VERSION = 1
EVIDENCE_LEVELS = {
    "authoritative",
    "independent_oracle",
    "production_planner",
    "model_suggestion",
    "unable_to_determine",
}

_SIMPLE_ATTACK_CARD_IDS = {
    "strikeb", "strikeg", "strikep", "striker",
}
_SIMPLE_BLOCK_CARD_IDS = {
    "defendb", "defendg", "defendp", "defendr",
}
_HARMFUL_END_CARD_IDS = {
    "burn", "decay", "doubt", "normality", "pain", "regret",
}
_SAFE_PLAYER_POWER_IDS = {
    "artifact", "dexterity", "draw", "drawcard", "drawcardnextturn",
    "drawcardnextturnpower", "drawreduction", "drawreductionpower",
    "envenom", "envenompower", "equilibrium", "equilibriumpower",
    "flex", "frail", "strength",
    "vulnerable", "weak",
}
_EXACT_PLAYER_POWER_IDS = set(_SAFE_PLAYER_POWER_IDS) - {
    # Ignoring these effects cannot invent a survival line, but it can miss
    # damage/hand value. Keep the oracle safe for "survival exists" without
    # letting it make the stronger "no survival exists" claim.
    "draw", "drawcard", "drawcardnextturn", "drawcardnextturnpower",
    "drawreduction", "drawreductionpower", "envenom", "envenompower",
    "equilibrium", "equilibriumpower",
}
_SAFE_MONSTER_POWER_IDS = {
    "artifact", "barricade", "metallicize", "regenerate", "ritual",
    "strength", "vulnerable", "weak",
}
_EXACT_MONSTER_POWER_IDS = {
    "artifact", "barricade", "metallicize", "regenerate", "ritual",
    "strength", "weak",
}
_EXACT_NONREACTIVE_RELIC_IDS = {
    "anchor", "ancientteaset", "blackblood", "burningblood", "calipers",
    "clockworksouvenir", "coffeedripper", "frozenegg2", "fusionhammer",
    "juzubracelet", "meatonthebone", "purewater", "ringofthesnake",
}
_SURVIVAL_UNSAFE_RELIC_IDS = {"velvetchoker"}
_ZERO_ENERGY_X_NONREACTIVE_PLAYER_POWER_IDS = (
    _SAFE_PLAYER_POWER_IDS | {"corruption", "weakened"}
)
_ZERO_ENERGY_X_NONREACTIVE_MONSTER_POWER_IDS = (
    _SAFE_MONSTER_POWER_IDS | {"malleable", "vulnerable"}
)
_ZERO_ENERGY_X_NONREACTIVE_RELIC_IDS = _EXACT_NONREACTIVE_RELIC_IDS | {
    "pear", "preservedinsect", "redmask", "regalpillow", "shovel",
    "tinyhouse",
}
_SECOND_WIND_ZERO_EXHAUST_PLAYER_POWER_IDS = _SAFE_PLAYER_POWER_IDS | {
    "nodraw",
}
_SECOND_WIND_ZERO_EXHAUST_MONSTER_POWER_IDS = _SAFE_MONSTER_POWER_IDS | {
    "minion",
}
_SECOND_WIND_ZERO_EXHAUST_RELIC_IDS = _EXACT_NONREACTIVE_RELIC_IDS | {
    "eternalfeather", "horncleat", "pandorasbox", "pantograph", "vajra",
}
# These relics either have no effect during the already-serialized enemy
# attack or have an exact, state-local effect that cannot rescue an end-turn
# branch after all cards are unplayable.  Keep this allowlist narrow: an
# unknown potion-triggered heal, revival, buffer, or timing relic must leave
# the counterfactual inconclusive.
_ESSENCE_OF_STEEL_END_TURN_RELIC_IDS = _EXACT_NONREACTIVE_RELIC_IDS | {
    "ancientteaset", "bloodvial", "burningblood", "horncleat",
    "incenseburner", "kunai", "meatonthebone", "mercuryhourglass",
    "oddlysmoothstone", "runicpyramid", "strawberry", "threadandneedle",
    "tinychest", "toxicegg2", "turnip", "velvetchoker",
    "whitebeaststatue", "sundial", "pear", "championbelt",
    "bagofpreparation",
}
_NO_BLOCK_DEFEND_NONRESCUE_RELIC_IDS = _EXACT_NONREACTIVE_RELIC_IDS | {
    "bagofmarbles", "bagofpreparation", "goldenidol", "redmask",
    "runiccube", "strawberry", "tungstenrod",
}

_REQUIRED_ACTION_FIELDS = (
    "before_seq",
    "after_seq",
    "turn",
    "hp_after",
    "player_before",
    "monsters_before",
    "hand_before",
    "draw_pile_before",
    "discard_pile_before",
    "exhaust_pile_before",
    "potions_before",
    "relics_before",
    "legal_actions_before",
    "damage_model",
    "decision_outcome",
)


def _is_death(terminal):
    if not isinstance(terminal, dict):
        return False
    if terminal.get("record_type") != "terminal_result":
        return False
    if type(terminal.get("schema_version")) is not int or terminal.get(
        "schema_version"
    ) != 2:
        return False
    if terminal.get("termination_kind") != "game_over":
        return False
    if terminal.get("victory") is not False:
        return False
    hp = terminal.get("current_hp")
    return (
        isinstance(hp, (int, float))
        and not isinstance(hp, bool)
        and hp == 0
    )


def _authoritative_game_over(terminal):
    """Require both independent terminal authority markers."""

    return bool(
        isinstance(terminal, dict)
        and terminal.get("authoritative_game_over") is True
        and terminal.get("screen_type") == "GAME_OVER"
    )


def _death_signal(terminal):
    """Recognize a claimed death that still needs strict validation.

    This deliberately accepts an incomplete terminal contract so the replay
    fails closed as inconclusive instead of silently becoming not-applicable.
    """

    if not isinstance(terminal, dict):
        return False
    hp = terminal.get("current_hp")
    return bool(
        terminal.get("termination_kind") == "game_over"
        and isinstance(hp, (int, float))
        and not isinstance(hp, bool)
        and hp <= 0
    )


def _observed_act4(records, terminal):
    values = []
    if isinstance(terminal, dict):
        values.extend((terminal.get("observed_max_act"), terminal.get("act")))
        if terminal.get("heart_defeated") is True:
            return True
    for record in records:
        if not isinstance(record, dict):
            continue
        values.extend((record.get("observed_max_act"), record.get("act")))
        phase = str(record.get("phase") or "").casefold()
        if "heart" in phase or "act4" in phase or "act_4" in phase:
            return True
        for monster in record.get("monsters_before") or []:
            if isinstance(monster, dict) and "heart" in str(
                monster.get("id", monster.get("name", ""))
            ).casefold():
                return True
    return any(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 4
        for value in values
    )


def _binding(record):
    return (
        record.get("attempt_id"),
        record.get("run_id"),
        record.get("seed"),
        record.get("character", record.get("class")),
        record.get("ascension_level"),
        record.get("run_type"),
        record.get("decision_hash"),
        record.get("controller_hash"),
        record.get("policy_version"),
        record.get("selection_id"),
        record.get("selection_digest"),
    )


def _same_binding(left, right):
    left_binding = _binding(left)
    right_binding = _binding(right)
    return all(
        type(observed) is type(expected) and observed == expected
        for observed, expected in zip(left_binding, right_binding)
    )


def _candidate_rows(record):
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    rows = record.get("candidates_before")
    if not isinstance(rows, list):
        rows = decision.get("candidates")
    return rows if isinstance(rows, list) else None


def _model_advice(record):
    decision = record.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    if "model_advice" in decision:
        return decision.get("model_advice")
    return record.get("model_advice")


def _counterfactual(candidate):
    if not isinstance(candidate, dict) or candidate.get("selected") is True:
        return None
    claim = candidate.get("counterfactual")
    claim = claim if isinstance(claim, dict) else {}
    level = str(claim.get("evidence_level") or "unable_to_determine")
    if level not in EVIDENCE_LEVELS:
        level = "unable_to_determine"
    return {
        "choice_id": candidate.get("choice_id", candidate.get("id")),
        "evidence_level": level,
        "predicted_survives": claim.get("predicted_survives"),
        "predicted_hp_loss": claim.get("predicted_hp_loss"),
        "basis": claim.get("basis"),
    }


def _living_monsters(record):
    monsters = record.get("monsters_before")
    if not isinstance(monsters, list):
        return None
    return [
        monster for monster in monsters
        if isinstance(monster, dict)
        and isinstance(monster.get("current_hp"), (int, float))
        and not isinstance(monster.get("current_hp"), bool)
        and monster.get("current_hp") > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]


def _reconstruct_legal_action_ids(record):
    """Rebuild combat legality from authoritative hand/energy/targets."""

    problems = []
    player = record.get("player_before")
    hand = record.get("hand_before")
    potions = record.get("potions_before")
    living = _living_monsters(record)
    if not isinstance(player, dict) or not isinstance(hand, list) or not isinstance(potions, list):
        return None, ["combat_legality_inputs_missing"]
    energy = player.get("energy")
    if not isinstance(energy, int) or isinstance(energy, bool) or energy < 0:
        return None, ["player_before.energy:invalid"]
    if living is None:
        return None, ["monsters_before:invalid"]

    ids = ["action:end"]
    for index, card in enumerate(hand):
        if not isinstance(card, dict):
            problems.append(f"hand_before[{index}]:not_object")
            continue
        card_instance_id = card.get("card_instance_id")
        if not card_instance_id:
            problems.append(f"hand_before[{index}].card_instance_id")
            continue
        if "is_playable" not in card:
            problems.append(f"hand_before[{index}].is_playable")
            continue
        if card.get("is_playable") is not True:
            continue
        if "has_target" not in card:
            problems.append(f"hand_before[{index}].has_target")
            continue
        cost = card.get("cost")
        if not isinstance(cost, int) or isinstance(cost, bool):
            problems.append(f"hand_before[{index}].cost")
            continue
        effective_cost = energy if cost == -1 else max(0, cost)
        if effective_cost > energy:
            continue
        if card.get("has_target") is True:
            ids.extend(
                f"play:{card_instance_id}:{monster.get('enemy_instance_id')}"
                for monster in living
                if monster.get("enemy_instance_id")
            )
        else:
            ids.append(f"play:{card_instance_id}")

    for index, potion in enumerate(potions):
        if not isinstance(potion, dict):
            problems.append(f"potions_before[{index}]:not_object")
            continue
        if potion.get("can_use") is not True:
            continue
        potion_instance_id = potion.get("potion_instance_id")
        if not potion_instance_id:
            problems.append(f"potions_before[{index}].potion_instance_id")
            continue
        if "requires_target" not in potion:
            problems.append(f"potions_before[{index}].requires_target")
            continue
        if potion.get("requires_target") is True:
            ids.extend(
                f"potion:{potion_instance_id}:{monster.get('enemy_instance_id')}"
                for monster in living
                if monster.get("enemy_instance_id")
            )
        else:
            ids.append(f"potion:{potion_instance_id}")
    return ids, problems


def _selected_action_id(record):
    action = str(record.get("action") or "").casefold()
    if action == "end":
        return "action:end"
    if action == "play":
        card_id = record.get("card_instance_id")
        if not card_id:
            return None
        target_id = record.get("enemy_instance_id")
        return f"play:{card_id}:{target_id}" if target_id else f"play:{card_id}"
    if action == "potion":
        potion_id = record.get("potion_instance_id")
        if not potion_id:
            return None
        target_id = record.get("enemy_instance_id")
        return (
            f"potion:{potion_id}:{target_id}"
            if target_id else f"potion:{potion_id}"
        )
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _token(value):
    return "".join(
        character for character in str(value or "").casefold()
        if character.isalnum()
    )


def _counterfactual_choice_id(candidate):
    if not isinstance(candidate, dict):
        return None
    choice_id = candidate.get("choice_id", candidate.get("id"))
    return str(choice_id) if choice_id else None


def _incoming_attack(monsters):
    """Return exact next attack total for the currently living enemies."""

    total = 0.0
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        hp = _number(monster.get("current_hp"))
        if hp is None:
            return None
        if hp <= 0 or monster.get("is_gone") is True or monster.get("half_dead") is True:
            continue
        damage = _number(monster.get("move_adjusted_damage"))
        hits = _number(monster.get("move_hits"))
        if damage is None or hits is None or hits < 0 or int(hits) != hits:
            return None
        total += max(0.0, damage) * int(hits)
    return total


def _simple_card_effect(card):
    """Classify the small exact card subset used by the independent oracle."""

    if not isinstance(card, dict):
        return None
    identity = _token(card.get("id"))
    cost = card.get("cost")
    if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
        return None
    if identity in _SIMPLE_ATTACK_CARD_IDS:
        damage = _number(card.get("damage"))
        if damage is None or damage < 0 or card.get("has_target") is not True:
            return None
        return {"kind": "attack", "cost": cost, "amount": damage}
    if identity in _SIMPLE_BLOCK_CARD_IDS:
        block = _number(card.get("block"))
        if block is None or block < 0 or card.get("has_target") is not False:
            return None
        return {"kind": "block", "cost": cost, "amount": block}
    return None


def _safe_reactive_environment(record):
    player = record.get("player_before")
    if not isinstance(player, dict):
        return False, ["player_before"]
    unsafe_reasons = []
    exactness_reasons = []
    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    unsupported_player_powers = sorted(
        power for power in player_power_ids
        if power not in _SAFE_PLAYER_POWER_IDS
    )
    if unsupported_player_powers:
        unsafe_reasons.append("player_powers")
        exactness_reasons.append("player_powers")
    elif any(
        power not in _EXACT_PLAYER_POWER_IDS for power in player_power_ids
    ):
        exactness_reasons.append("player_power_card_math")
    if player.get("orbs"):
        exactness_reasons.append("player_orbs")
    relic_ids = {
        _token(relic.get("id", relic.get("name")))
        for relic in record.get("relics_before") or []
        if isinstance(relic, dict)
    }
    if relic_ids & _SURVIVAL_UNSAFE_RELIC_IDS:
        unsafe_reasons.append("card_play_restricting_relic")
    # Shuriken is exact when the visible legal hand cannot reach its next
    # trigger.  Its existing Strength is already folded into serialized card
    # damage, and a no-trigger turn has no other survival interaction.
    shuriken_exact = False
    if "shuriken" in relic_ids:
        shuriken = next((
            relic for relic in record.get("relics_before") or []
            if isinstance(relic, dict)
            and _token(relic.get("id", relic.get("name"))) == "shuriken"
        ), None)
        counter = shuriken.get("counter") if isinstance(shuriken, dict) else None
        playable_attacks = sum(
            1 for card in record.get("hand_before") or []
            if isinstance(card, dict)
            and card.get("is_playable") is True
            and str(card.get("type") or "").upper() == "ATTACK"
        )
        shuriken_exact = bool(
            type(counter) is int and 0 <= counter <= 2
            and playable_attacks < 3 - counter
        )
    if any(
        relic_id not in _EXACT_NONREACTIVE_RELIC_IDS
        and not (relic_id == "shuriken" and shuriken_exact)
        for relic_id in relic_ids
    ):
        exactness_reasons.append("unmodeled_beneficial_relic")
    for monster in record.get("monsters_before") or []:
        if not isinstance(monster, dict):
            continue
        monster_power_ids = {
            _token(power.get("id", power.get("name")))
            for power in monster.get("powers") or []
            if isinstance(power, dict)
        }
        unsupported = {
            power for power in monster_power_ids
            if power not in _SAFE_MONSTER_POWER_IDS
        }
        if unsupported:
            unsafe_reasons.append("monster_reactive_powers")
            exactness_reasons.append("monster_reactive_powers")
            break
        if any(
            power not in _EXACT_MONSTER_POWER_IDS
            for power in monster_power_ids
        ):
            exactness_reasons.append("monster_power_card_math")
    for card in record.get("hand_before") or []:
        if isinstance(card, dict) and _token(card.get("id")) in _HARMFUL_END_CARD_IDS:
            unsafe_reasons.append("harmful_end_card")
            exactness_reasons.append("harmful_end_card")
            break
    return (
        not unsafe_reasons,
        not exactness_reasons,
        sorted(set(unsafe_reasons + exactness_reasons)),
    )


def _apply_simple_card(card, effect, monsters, block, energy, target_id):
    if effect["cost"] > energy:
        return None
    updated = [dict(monster) for monster in monsters]
    if effect["kind"] == "block":
        return updated, block + effect["amount"], energy - effect["cost"]
    target_index = next(
        (
            index for index, monster in enumerate(updated)
            if monster.get("enemy_instance_id") == target_id
            and _number(monster.get("current_hp")) is not None
            and _number(monster.get("current_hp")) > 0
            and monster.get("is_gone") is not True
            and monster.get("half_dead") is not True
        ),
        None,
    )
    if target_index is None:
        return None
    target = updated[target_index]
    remaining = effect["amount"]
    target_block = max(0.0, _number(target.get("block")) or 0.0)
    absorbed = min(target_block, remaining)
    target["block"] = target_block - absorbed
    remaining -= absorbed
    target["current_hp"] = max(
        0.0, (_number(target.get("current_hp")) or 0.0) - remaining
    )
    if target["current_hp"] <= 0:
        target["is_gone"] = True
    return updated, block, energy - effect["cost"]


def _zero_energy_x_attack_nonrescue(record, first_choice_id):
    """Prove a zero-energy Whirlwind cannot change a fatal END.

    This is intentionally narrow.  It applies only when every visible power,
    relic, monster power, remaining card, and potion proves that playing the
    zero-hit attack cannot create block, energy, damage, or another action.
    """

    if record.get("action") != "end" or not first_choice_id.startswith("play:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    monsters = record.get("monsters_before")
    potions = record.get("potions_before")
    relics = record.get("relics_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (monsters, list), (potions, list),
        (relics, list),
    )):
        return None
    if player.get("energy") != 0 or player.get("orbs"):
        return None
    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    if hp is None or block is None:
        return None

    instance_id = first_choice_id[len("play:"):]
    if ":" in instance_id:
        return None
    matching = [
        card for card in hand
        if isinstance(card, dict)
        and str(card.get("card_instance_id") or "") == instance_id
    ]
    if len(matching) != 1:
        return None
    card = matching[0]
    if not (
        _token(card.get("id")) == "whirlwind"
        and card.get("cost") == -1
        and card.get("is_playable") is True
        and card.get("has_target") is False
        and (_number(card.get("damage")) or 0) >= 0
    ):
        return None
    if any(
        other is not card
        and isinstance(other, dict)
        and other.get("is_playable") is True
        for other in hand
    ) or any(
        isinstance(potion, dict) and potion.get("can_use") is True
        for potion in potions
    ):
        return None

    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    relic_ids = {
        _token(relic.get("id", relic.get("name")))
        for relic in relics if isinstance(relic, dict)
    }
    if (
        not player_power_ids <= _ZERO_ENERGY_X_NONREACTIVE_PLAYER_POWER_IDS
        or not relic_ids <= _ZERO_ENERGY_X_NONREACTIVE_RELIC_IDS
    ):
        return None
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        monster_power_ids = {
            _token(power.get("id", power.get("name")))
            for power in monster.get("powers") or []
            if isinstance(power, dict)
        }
        if not (
            monster_power_ids
            <= _ZERO_ENERGY_X_NONREACTIVE_MONSTER_POWER_IDS
        ):
            return None

    incoming = _incoming_attack(monsters)
    if incoming is None:
        return None
    hp_loss = min(max(0.0, hp), max(0.0, incoming - block))
    return {
        "status": "proven",
        "predicted_survives": hp - hp_loss > 0,
        "predicted_hp_loss": hp_loss,
        "basis": (
            "independent exact zero-energy Whirlwind is state-equivalent "
            "to END in a nonreactive environment"
        ),
    }


def _second_wind_zero_exhaust_nonrescue(record, first_choice_id):
    """Prove a zero-exhaust Second Wind cannot improve a fatal END.

    Second Wind gains block once per *other* non-Attack card exhausted from
    hand.  If every other card is an unplayable Attack, the card gains exactly
    zero block.  Keep this proof deliberately narrow: no other action may
    remain, and every visible power/relic must be nonreactive to playing this
    Skill so that the branch is state-equivalent to the observed END for
    one-turn survival.
    """

    if record.get("action") != "end" or not first_choice_id.startswith("play:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    monsters = record.get("monsters_before")
    potions = record.get("potions_before")
    relics = record.get("relics_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (monsters, list), (potions, list),
        (relics, list),
    )):
        return None
    energy = player.get("energy")
    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    if (
        not isinstance(energy, int) or isinstance(energy, bool) or energy < 0
        or hp is None or block is None or player.get("orbs")
    ):
        return None

    matching = [
        card for card in hand
        if isinstance(card, dict)
        and first_choice_id == f'play:{card.get("card_instance_id")}'
    ]
    if len(matching) != 1:
        return None
    card = matching[0]
    cost = card.get("cost")
    if not (
        _token(card.get("id")) == "secondwind"
        and str(card.get("type") or "").upper() == "SKILL"
        and isinstance(cost, int) and not isinstance(cost, bool)
        and 0 <= cost <= energy
        and card.get("is_playable") is True
        and card.get("has_target") is False
        and card.get("exhausts") is False
    ):
        return None
    other_cards = [other for other in hand if other is not card]
    if any(
        not isinstance(other, dict)
        or str(other.get("type") or "").upper() != "ATTACK"
        or other.get("is_playable") is not False
        for other in other_cards
    ):
        return None
    if any(
        not isinstance(potion, dict) or potion.get("can_use") is not False
        for potion in potions
    ):
        return None

    player_powers = player.get("powers")
    if not isinstance(player_powers, list) or any(
        not isinstance(power, dict)
        or not _token(power.get("id", power.get("name")))
        for power in player_powers
    ):
        return None
    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player_powers
    }
    if not player_power_ids <= _SECOND_WIND_ZERO_EXHAUST_PLAYER_POWER_IDS:
        return None

    if any(
        not isinstance(relic, dict)
        or not _token(relic.get("id", relic.get("name")))
        for relic in relics
    ):
        return None
    relic_ids = {
        _token(relic.get("id", relic.get("name"))) for relic in relics
    }
    if not relic_ids <= _SECOND_WIND_ZERO_EXHAUST_RELIC_IDS:
        return None

    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        powers = monster.get("powers")
        if not isinstance(powers, list) or any(
            not isinstance(power, dict)
            or not _token(power.get("id", power.get("name")))
            for power in powers
        ):
            return None
        monster_power_ids = {
            _token(power.get("id", power.get("name"))) for power in powers
        }
        if not (
            monster_power_ids
            <= _SECOND_WIND_ZERO_EXHAUST_MONSTER_POWER_IDS
        ):
            return None

    incoming = _incoming_attack(monsters)
    if incoming is None:
        return None
    hp_loss = min(max(0.0, hp), max(0.0, incoming - block))
    return {
        "status": "proven",
        "predicted_survives": hp - hp_loss > 0,
        "predicted_hp_loss": hp_loss,
        "basis": (
            "independent exact Second Wind exhausts zero non-Attack cards "
            "and is state-equivalent to END in a nonreactive environment"
        ),
    }


def _artifact_blocked_debuff_potion_nonrescue(record, first_choice_id):
    """Prove a narrow fatal-END potion branch cannot change survival.

    Weak/Fear/Poison potions are fully consumed by a positive Artifact stack.
    When the player has no remaining playable card or other usable potion, the
    targeted potion followed by END is state-equivalent for incoming damage to
    the observed fatal END.  This proof deliberately does not generalize to
    other potion effects or to turns with another continuation available.
    """

    if record.get("action") != "end" or not first_choice_id.startswith("potion:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    potions = record.get("potions_before")
    monsters = record.get("monsters_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (potions, list), (monsters, list),
    )):
        return None
    if player.get("energy") != 0:
        return None
    if any(
        isinstance(card, dict) and card.get("is_playable") is True
        for card in hand
    ):
        return None

    matching_potions = []
    for potion in potions:
        if not isinstance(potion, dict):
            continue
        potion_instance_id = str(potion.get("potion_instance_id") or "")
        prefix = f"potion:{potion_instance_id}:"
        if (
            potion_instance_id
            and first_choice_id.startswith(prefix)
            and potion.get("can_use") is True
            and potion.get("requires_target") is True
        ):
            matching_potions.append((
                potion, first_choice_id[len(prefix):]
            ))
    if len(matching_potions) != 1:
        return None
    potion, target_id = matching_potions[0]
    if _token(potion.get("id")) not in {
        "fearpotion", "poisonpotion", "weakpotion",
    }:
        return None
    if any(
        isinstance(other, dict)
        and other.get("can_use") is True
        and other is not potion
        for other in potions
    ):
        return None

    matching_targets = [
        monster for monster in monsters
        if isinstance(monster, dict)
        and str(monster.get("enemy_instance_id") or "") == target_id
        and (_number(monster.get("current_hp")) or 0) > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]
    if len(matching_targets) != 1:
        return None
    artifact_amounts = [
        _number(power.get("amount"))
        for power in matching_targets[0].get("powers") or []
        if isinstance(power, dict)
        and _token(power.get("id", power.get("name"))) == "artifact"
    ]
    if not any(amount is not None and amount > 0 for amount in artifact_amounts):
        return None

    hp_before = _number(player.get("current_hp"))
    hp_after = _number(record.get("hp_after"))
    if hp_after is None:
        after = record.get("authoritative_state_after")
        game_state = after.get("game_state") if isinstance(after, dict) else None
        hp_after = _number(
            game_state.get("current_hp") if isinstance(game_state, dict) else None
        )
    if hp_before is None or hp_after != 0:
        return None
    return {
        "status": "proven",
        "predicted_survives": False,
        "predicted_hp_loss": hp_before,
        "basis": (
            "independent exact Artifact consumption: targeted debuff is "
            "blocked and no continuation remains before the observed fatal END"
        ),
    }


def _liquid_bronze_single_hit_nonrescue(record, first_choice_id):
    """Prove Liquid Bronze cannot prevent one already-serialized lethal hit.

    Thorns damage is reactive: the attacker first deals its hit, then receives
    Thorns damage.  With exactly one living, single-hit attacker, no playable
    cards or other usable potions, and no Toy Ornithopter heal, drinking Liquid
    Bronze cannot change whether that first hit kills the player.
    """

    if record.get("action") != "end" or not first_choice_id.startswith("potion:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    potions = record.get("potions_before")
    monsters = record.get("monsters_before")
    relics = record.get("relics_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (potions, list),
        (monsters, list), (relics, list),
    )):
        return None
    if player.get("energy") != 0 or any(
        isinstance(card, dict) and card.get("is_playable") is True
        for card in hand
    ):
        return None
    if any(not isinstance(relic, dict) for relic in relics):
        return None
    if any(
        _token(relic.get("id", relic.get("name"))) == "toyornithopter"
        for relic in relics
    ):
        return None

    usable_potions = [
        potion for potion in potions
        if isinstance(potion, dict) and potion.get("can_use") is True
    ]
    if len(usable_potions) != 1:
        return None
    potion = usable_potions[0]
    potion_instance_id = str(potion.get("potion_instance_id") or "")
    if (
        not potion_instance_id
        or first_choice_id != f"potion:{potion_instance_id}"
        or potion.get("requires_target") is not False
        or _token(potion.get("id", potion.get("name"))) != "liquidbronze"
    ):
        return None

    living = []
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        hp = _number(monster.get("current_hp"))
        if hp is None:
            return None
        if (
            hp > 0
            and monster.get("is_gone") is not True
            and monster.get("half_dead") is not True
        ):
            living.append(monster)
    if len(living) != 1:
        return None
    attacker = living[0]
    hits = _number(attacker.get("move_hits"))
    intent = str(attacker.get("intent") or "").upper()
    if hits != 1 or "ATTACK" not in intent:
        return None

    hp_before = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    incoming = _incoming_attack(monsters)
    hp_after = _number(record.get("hp_after"))
    if hp_after is None:
        after = record.get("authoritative_state_after")
        game_state = after.get("game_state") if isinstance(after, dict) else None
        hp_after = _number(
            game_state.get("current_hp") if isinstance(game_state, dict) else None
        )
    if (
        hp_before is None or hp_before <= 0 or block is None or block < 0
        or incoming is None or hp_after != 0
        or hp_before - max(0.0, incoming - block) > 0
    ):
        return None
    return {
        "status": "proven",
        "predicted_survives": False,
        "predicted_hp_loss": min(hp_before, max(0.0, incoming - block)),
        "basis": (
            "independent exact Liquid Bronze ordering: the sole single-hit "
            "attacker deals the observed lethal hit before taking Thorns damage"
        ),
    }


def _zero_energy_nonrescue_potion_set(record, first_choice_id):
    """Prove a closed zero-energy potion belt cannot beat one lethal hit.

    Elixir has no defensive effect without an on-exhaust power, Liquid Bronze
    reacts only after the attacker hits, and Swift cannot expose a playable
    line when its entire authoritative draw suffix costs positive energy.  In
    that narrow shape even drinking every remaining bottle before END leaves
    the already-serialized first attack lethal.
    """

    if record.get("action") != "end" or not str(
        first_choice_id or ""
    ).startswith("potion:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    draw_pile = record.get("draw_pile_before")
    discard_pile = record.get("discard_pile_before")
    potions = record.get("potions_before")
    monsters = record.get("monsters_before")
    relics = record.get("relics_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (draw_pile, list),
        (discard_pile, list), (potions, list), (monsters, list),
        (relics, list),
    )):
        return None
    if player.get("energy") != 0 or player.get("orbs"):
        return None
    if any(
        isinstance(card, dict) and card.get("is_playable") is True
        for card in hand
    ):
        return None

    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    if not player_power_ids <= {"hex", "vulnerable", "weak", "weakened"}:
        return None
    relic_ids = {
        _token(relic.get("id", relic.get("name")))
        for relic in relics if isinstance(relic, dict)
    }
    if relic_ids & {"charonsashes", "sacredbark", "toyornithopter"}:
        return None

    usable = [
        potion for potion in potions
        if isinstance(potion, dict) and potion.get("can_use") is True
    ]
    matching = [
        potion for potion in usable
        if str(potion.get("potion_instance_id") or "")
        and first_choice_id
        == f"potion:{potion.get('potion_instance_id')}"
    ]
    if len(matching) != 1:
        return None
    usable_ids = {
        _token(potion.get("id", potion.get("name"))) for potion in usable
    }
    if not usable_ids or not usable_ids <= {
        "elixir", "elixirpotion", "liquidbronze", "swiftpotion",
    }:
        return None
    if "swiftpotion" in usable_ids:
        # Three visible cards are drawn before any reshuffle.  Negative costs
        # here are status/curse sentinels, not playable zero-energy cards.
        if len(draw_pile) < 3 or any(
            not isinstance(card, dict)
            or type(card.get("cost")) is not int
            or card.get("cost") == 0
            for card in draw_pile[:3]
        ):
            return None

    living = []
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        monster_hp = _number(monster.get("current_hp"))
        if monster_hp is None:
            return None
        if (
            monster_hp > 0
            and monster.get("is_gone") is not True
            and monster.get("half_dead") is not True
        ):
            living.append(monster)
    if len(living) != 1:
        return None
    attacker = living[0]
    if (
        _number(attacker.get("move_hits")) != 1
        or "ATTACK" not in str(attacker.get("intent") or "").upper()
    ):
        return None

    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    incoming = _incoming_attack(monsters)
    hp_after = _number(record.get("hp_after"))
    if (
        hp is None or hp <= 0 or block is None or block < 0
        or incoming is None or hp_after != 0
    ):
        return None
    loss = max(0.0, incoming - block)
    if hp - loss > 0:
        return None
    return {
        "status": "proven",
        "predicted_survives": False,
        "predicted_hp_loss": min(hp, loss),
        "basis": (
            "independent exact zero-energy potion-set bound: Elixir has no "
            "on-exhaust defense, Swift draws no zero-cost card, and Liquid "
            "Bronze reacts only after the sole lethal hit"
        ),
    }


def _fatal_zero_energy_potion_nonrescue(record, first_choice_id):
    """Prove the best Weak/Forge combination still cannot prevent death.

    This intentionally narrow proof is evaluated before the generic reactive
    environment gate.  Transient/Fading and similar powers do not change the
    already serialized attack value.  At zero energy with no playable card,
    an all-upgraded hand makes Blessing of the Forge a no-op; Weak Potion can
    then be bounded exactly from the visible target attack and Paper Krane.
    """

    if record.get("action") != "end" or not first_choice_id.startswith(
        "potion:"
    ):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    potions = record.get("potions_before")
    monsters = record.get("monsters_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (potions, list), (monsters, list),
    )):
        return None
    if player.get("energy") != 0 or any(
        isinstance(card, dict) and card.get("is_playable") is True
        for card in hand
    ):
        return None

    usable = [
        potion for potion in potions
        if isinstance(potion, dict) and potion.get("can_use") is True
    ]
    matching = [
        potion for potion in usable
        if str(potion.get("potion_instance_id") or "")
        and (
            first_choice_id
            == f"potion:{potion.get('potion_instance_id')}"
            or first_choice_id.startswith(
                f"potion:{potion.get('potion_instance_id')}:"
            )
        )
    ]
    if len(matching) != 1:
        return None
    usable_ids = {_token(potion.get("id")) for potion in usable}
    if not usable_ids or not usable_ids <= {
        "weakpotion", "blessingoftheforge",
    }:
        return None

    if "blessingoftheforge" in usable_ids:
        if not hand or any(
            not isinstance(card, dict)
            or not isinstance(card.get("upgrades"), int)
            or isinstance(card.get("upgrades"), bool)
            or card.get("upgrades") <= 0
            for card in hand
        ):
            return None

    incoming = _incoming_attack(monsters)
    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    if incoming is None or hp is None or block is None:
        return None
    hp_after = _number(record.get("hp_after"))
    if hp_after is None:
        after = record.get("authoritative_state_after")
        game_state = after.get("game_state") if isinstance(after, dict) else None
        hp_after = _number(
            game_state.get("current_hp") if isinstance(game_state, dict) else None
        )
    if hp_after != 0:
        return None

    best_incoming = incoming
    if "weakpotion" in usable_ids:
        relic_ids = {
            _token(relic.get("id", relic.get("name")))
            for relic in record.get("relics_before") or []
            if isinstance(relic, dict)
        }
        weak_multiplier = (
            0.60
            if relic_ids & {"papercrane", "paperkrane"}
            else 0.75
        )
        for monster in monsters:
            if not isinstance(monster, dict):
                return None
            monster_hp = _number(monster.get("current_hp"))
            damage = _number(monster.get("move_adjusted_damage"))
            hits = _number(monster.get("move_hits"))
            if (
                monster_hp is None or damage is None or hits is None
                or hits < 0 or int(hits) != hits
            ):
                return None
            if (
                monster_hp <= 0 or monster.get("is_gone") is True
                or monster.get("half_dead") is True
            ):
                continue
            artifact = any(
                isinstance(power, dict)
                and _token(power.get("id", power.get("name"))) == "artifact"
                and (_number(power.get("amount")) or 0) > 0
                for power in monster.get("powers") or []
            )
            reduced_per_hit = (
                damage if artifact
                else math.floor(max(0.0, damage) * weak_multiplier)
            )
            target_before = max(0.0, damage) * int(hits)
            target_after = reduced_per_hit * int(hits)
            best_incoming = min(
                best_incoming,
                incoming - target_before + target_after,
            )

    best_hp_after = hp - max(0.0, best_incoming - block)
    if best_hp_after > 0:
        return None
    return {
        "status": "proven",
        "predicted_survives": False,
        "predicted_hp_loss": hp,
        "basis": (
            "independent exact zero-energy potion bound: Forge cannot "
            "upgrade the hand and even the best Weak target remains lethal"
        ),
    }


def _essence_of_steel_end_turn_nonrescue(record, first_choice_id):
    """Prove a fatal Essence of Steel branch when the end-turn math is closed.

    Essence of Steel adds four Plated Armor, which becomes four block at the
    end of the player's turn before the serialized enemy attack.  This helper
    is intentionally limited to the one-potion, zero-energy, no-playable-card
    shape.  It accepts only player/monster powers and relics whose current
    attack interaction is already exact; revival, buffer, potion-heal, and
    other timing-dependent effects remain unknown.
    """

    if record.get("action") != "end" or first_choice_id != str(
        first_choice_id or ""
    ) or not first_choice_id.startswith("potion:"):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    potions = record.get("potions_before")
    monsters = record.get("monsters_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (potions, list), (monsters, list),
    )):
        return None
    if player.get("energy") != 0 or player.get("orbs"):
        return None
    if any(
        isinstance(card, dict) and card.get("is_playable") is True
        for card in hand
    ):
        return None

    usable = [
        potion for potion in potions
        if isinstance(potion, dict) and potion.get("can_use") is True
    ]
    if len(usable) != 1:
        return None
    potion = usable[0]
    potion_instance_id = str(potion.get("potion_instance_id") or "")
    if (
        not potion_instance_id
        or first_choice_id != f"potion:{potion_instance_id}"
        or potion.get("requires_target") is not False
        or _token(potion.get("id", potion.get("name")))
        != "essenceofsteel"
    ):
        return None

    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    if not player_power_ids <= _SAFE_PLAYER_POWER_IDS:
        return None
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        monster_power_ids = {
            _token(power.get("id", power.get("name")))
            for power in monster.get("powers") or []
            if isinstance(power, dict)
        }
        if not monster_power_ids <= _SAFE_MONSTER_POWER_IDS:
            return None

    relic_ids = set()
    for relic in record.get("relics_before") or []:
        if not isinstance(relic, dict):
            return None
        relic_id = _token(relic.get("id", relic.get("name")))
        if relic_id == "incenseburner":
            counter = relic.get("counter")
            # Counter 0..5 is not triggered until the next turn starts; a
            # malformed or already-triggered counter is not an exact branch.
            if type(counter) is not int or not 0 <= counter <= 5:
                return None
        elif relic_id not in _ESSENCE_OF_STEEL_END_TURN_RELIC_IDS:
            return None
        relic_ids.add(relic_id)

    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    incoming = _incoming_attack(monsters)
    if hp is None or hp <= 0 or block is None or block < 0 or incoming is None:
        return None
    hp_after = _number(record.get("hp_after"))
    if hp_after is None:
        after = record.get("authoritative_state_after")
        game_state = after.get("game_state") if isinstance(after, dict) else None
        hp_after = _number(
            game_state.get("current_hp") if isinstance(game_state, dict) else None
        )
    if hp_after != 0:
        return None

    remaining_damage = max(0.0, incoming - block - 4.0)
    if hp - remaining_damage > 0:
        return None
    return {
        "status": "proven",
        "predicted_survives": False,
        "predicted_hp_loss": min(hp, remaining_damage),
        "basis": (
            "independent exact Essence of Steel ordering: four Plated Armor "
            "block still leaves the serialized end-turn attack lethal"
        ),
    }


def _writhing_mass_non_guaranteed_branch(record, first_choice_id):
    """Classify a fatal Writhing Mass reroll without inventing an outcome.

    A nonlethal hit against Compulsive replaces the visible intent with a
    random one.  Such a hit can save the player, but it is not a guaranteed
    escape.  The death gate should therefore distinguish it from an unknown
    deterministic rescue while still detecting an exact lethal first hit.
    This proof is deliberately restricted to the zero-energy Anger plus
    Explosive Potion surface observed in base-game traces.
    """

    if record.get("action") != "end":
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    monsters = record.get("monsters_before")
    potions = record.get("potions_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (monsters, list), (potions, list),
    )):
        return None
    if player.get("energy") != 0 or player.get("orbs"):
        return None
    living = [
        monster for monster in monsters
        if isinstance(monster, dict)
        and (_number(monster.get("current_hp")) or 0) > 0
        and monster.get("is_gone") is not True
        and monster.get("half_dead") is not True
    ]
    if len(living) != 1 or _token(living[0].get("id")) != "writhingmass":
        return None
    target = living[0]
    power_ids = {
        _token(power.get("id", power.get("name")))
        for power in target.get("powers") or []
        if isinstance(power, dict)
    }
    if "compulsive" not in power_ids and "compulsivepower" not in power_ids:
        return None
    if not power_ids <= {
        "compulsive", "compulsivepower", "malleable", "malleablepower",
        "strength", "vulnerable", "weak",
    }:
        return None
    player_power_ids = {
        _token(power.get("id", power.get("name")))
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    if not player_power_ids <= {
        "artifact", "flex", "strength", "vulnerable", "weak", "weakened",
    }:
        return None

    playable_angers = []
    for card in hand:
        if not isinstance(card, dict) or card.get("is_playable") is not True:
            continue
        damage = _number(card.get("damage"))
        if (
            _token(card.get("id")) != "anger"
            or card.get("cost") != 0
            or card.get("has_target") is not True
            or damage is None or damage < 0
            or not card.get("card_instance_id")
        ):
            return None
        playable_angers.append((str(card["card_instance_id"]), damage))

    usable_explosives = []
    for potion in potions:
        if not isinstance(potion, dict) or potion.get("can_use") is not True:
            continue
        if (
            _token(potion.get("id")) != "explosivepotion"
            or not potion.get("potion_instance_id")
        ):
            return None
        usable_explosives.append(str(potion["potion_instance_id"]))

    relics = [
        relic for relic in record.get("relics_before") or []
        if isinstance(relic, dict)
    ]
    relic_ids = {
        _token(relic.get("id", relic.get("name"))) for relic in relics
    }
    if "sacredbark" in relic_ids:
        return None
    fan = next((
        relic for relic in relics
        if _token(relic.get("id", relic.get("name"))) == "ornamentalfan"
    ), None)
    if fan is not None and fan.get("counter") not in {0, 1}:
        return None

    target_id = str(target.get("enemy_instance_id") or "")
    selected_damage = None
    if first_choice_id.startswith("play:"):
        matches = [
            damage for instance_id, damage in playable_angers
            if first_choice_id == f"play:{instance_id}:{target_id}"
        ]
        if len(matches) == 1:
            selected_damage = matches[0]
    elif first_choice_id.startswith("potion:"):
        matches = [
            instance_id for instance_id in usable_explosives
            if first_choice_id in {
                f"potion:{instance_id}",
                f"potion:{instance_id}:{target_id}",
            }
        ]
        if len(matches) == 1:
            selected_damage = 10.0
    if selected_damage is None:
        return None

    hp = _number(target.get("current_hp"))
    target_block = _number(target.get("block"))
    player_hp = _number(player.get("current_hp"))
    player_block = _number(player.get("block"))
    incoming = _incoming_attack(living)
    if None in {hp, target_block, player_hp, player_block, incoming}:
        return None
    lethal_threshold = max(0.0, hp) + max(0.0, target_block)
    if selected_damage >= lethal_threshold:
        return {
            "status": "proven",
            "predicted_survives": True,
            "predicted_hp_loss": 0.0,
            "basis": "independent exact lethal hit before Compulsive reroll",
        }
    if player_hp - max(0.0, incoming - player_block) > 0:
        return None
    # Ignore Malleable's additional block to obtain an upper bound on all
    # damage still available this turn.  If even that bound cannot kill, the
    # only possible rescue is the random intent reroll itself.
    total_damage_upper_bound = sum(
        damage for _instance_id, damage in playable_angers
    ) + 10.0 * len(usable_explosives)
    if total_damage_upper_bound >= lethal_threshold:
        return None
    return {
        "status": "classified_non_guaranteed",
        "predicted_survives": None,
        "predicted_hp_loss": None,
        "reason": "non_guaranteed_compulsive_intent_reroll",
        "basis": (
            "independent exact nonlethal damage bound: only a random "
            "Compulsive intent reroll could rescue the turn"
        ),
    }


def _no_block_defend_nonrescue(record, first_choice_id):
    """Prove a serialized zero-Block Defend cannot rescue the death turn.

    Panic Button's No Block power leaves Defends legally playable while their
    authoritative Block value is zero.  The general Strike/Defend oracle
    deliberately rejects active powers and reactive relics; this narrow proof
    handles the exact no-attack surface and Tungsten Rod's per-hit reduction.
    Runic Cube may draw during the enemy attack, but cards drawn after ending
    the turn cannot interrupt that already-serialized attack.
    """

    if record.get("action") != "end" or not first_choice_id.startswith(
        "play:"
    ):
        return None
    player = record.get("player_before")
    hand = record.get("hand_before")
    monsters = record.get("monsters_before")
    potions = record.get("potions_before")
    relics = record.get("relics_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (monsters, list), (potions, list),
        (relics, list),
    )):
        return None
    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    energy = player.get("energy")
    if (
        hp is None or hp <= 0 or block != 0
        or type(energy) is not int or energy <= 0 or player.get("orbs")
    ):
        return None

    powers = {
        _token(power.get("id", power.get("name"))): power.get("amount")
        for power in player.get("powers") or []
        if isinstance(power, dict)
    }
    if not set(powers) <= {"berserk", "noblockpower", "strength"}:
        return None
    if not isinstance(powers.get("noblockpower"), (int, float)) or (
        powers["noblockpower"] <= 0
    ):
        return None

    playable = [
        card for card in hand
        if isinstance(card, dict) and card.get("is_playable") is True
    ]
    if not playable or any(
        _token(card.get("id")) not in _SIMPLE_BLOCK_CARD_IDS
        or _number(card.get("block")) != 0
        or card.get("has_target") is not False
        or type(card.get("cost")) is not int
        or card.get("cost") < 0
        or not card.get("card_instance_id")
        for card in playable
    ):
        return None
    instance_id = first_choice_id[len("play:"):]
    if ":" in instance_id or sum(
        str(card.get("card_instance_id")) == instance_id
        for card in playable
    ) != 1:
        return None
    if any(
        isinstance(potion, dict) and potion.get("can_use") is True
        for potion in potions
    ):
        return None

    relic_ids = {
        _token(relic.get("id", relic.get("name")))
        for relic in relics if isinstance(relic, dict)
    }
    if not relic_ids <= _NO_BLOCK_DEFEND_NONRESCUE_RELIC_IDS:
        return None
    tungsten = "tungstenrod" in relic_ids
    incoming = 0.0
    for monster in monsters:
        if not isinstance(monster, dict):
            return None
        monster_hp = _number(monster.get("current_hp"))
        if monster_hp is None:
            return None
        if (
            monster_hp <= 0 or monster.get("is_gone") is True
            or monster.get("half_dead") is True
        ):
            continue
        monster_powers = {
            _token(power.get("id", power.get("name")))
            for power in monster.get("powers") or []
            if isinstance(power, dict)
        }
        if not monster_powers <= {
            "artifact", "minion", "strength", "vulnerable", "weak",
        }:
            return None
        damage = _number(monster.get("move_adjusted_damage"))
        hits = monster.get("move_hits")
        if (
            damage is None or damage < 0 or type(hits) is not int
            or hits < 0
        ):
            return None
        per_hit = max(0.0, damage - (1.0 if tungsten else 0.0))
        incoming += per_hit * hits

    hp_loss = min(hp, incoming)
    return {
        "status": "proven",
        "predicted_survives": hp - hp_loss > 0,
        "predicted_hp_loss": hp_loss,
        "basis": (
            "independent exact No Block Defend is state-equivalent to END; "
            "Tungsten Rod is applied once per serialized attack hit"
        ),
    }


def _best_simple_turn_survival(record, first_choice_id):
    """Conservatively prove one-turn survival for a selected first action.

    A false result is returned only when every playable card is in the exact
    Strike/Defend subset and no unmodeled potion/reactive/end-turn mechanism
    is active.  A true result may also come from a partial card surface: the
    concrete simple line itself is sufficient proof of survival.
    """

    artifact_blocked = _artifact_blocked_debuff_potion_nonrescue(
        record, first_choice_id
    )
    if artifact_blocked is not None:
        return artifact_blocked

    liquid_bronze = _liquid_bronze_single_hit_nonrescue(
        record, first_choice_id
    )
    if liquid_bronze is not None:
        return liquid_bronze

    closed_potion_set = _zero_energy_nonrescue_potion_set(
        record, first_choice_id
    )
    if closed_potion_set is not None:
        return closed_potion_set

    fatal_potion_bound = _fatal_zero_energy_potion_nonrescue(
        record, first_choice_id
    )
    if fatal_potion_bound is not None:
        return fatal_potion_bound

    essence_of_steel = _essence_of_steel_end_turn_nonrescue(
        record, first_choice_id
    )
    if essence_of_steel is not None:
        return essence_of_steel

    writhing_mass = _writhing_mass_non_guaranteed_branch(
        record, first_choice_id
    )
    if writhing_mass is not None:
        return writhing_mass

    no_block_defend = _no_block_defend_nonrescue(record, first_choice_id)
    if no_block_defend is not None:
        return no_block_defend

    zero_energy_x = _zero_energy_x_attack_nonrescue(record, first_choice_id)
    if zero_energy_x is not None:
        return zero_energy_x

    second_wind = _second_wind_zero_exhaust_nonrescue(
        record, first_choice_id
    )
    if second_wind is not None:
        return second_wind

    player = record.get("player_before")
    hand = record.get("hand_before")
    monsters = record.get("monsters_before")
    potions = record.get("potions_before")
    if not all(isinstance(value, expected) for value, expected in (
        (player, dict), (hand, list), (monsters, list), (potions, list),
    )):
        return {"status": "unknown", "reason": "state_inputs_missing"}
    hp = _number(player.get("current_hp"))
    block = _number(player.get("block"))
    energy = player.get("energy")
    if (
        hp is None or block is None or not isinstance(energy, int)
        or isinstance(energy, bool) or energy < 0
    ):
        return {"status": "unknown", "reason": "player_state_invalid"}
    safe_environment, exact_environment, unsafe_reasons = (
        _safe_reactive_environment(record)
    )
    if not safe_environment:
        return {
            "status": "unknown",
            "reason": "reactive_environment_unsupported",
            "details": unsafe_reasons,
        }
    if _incoming_attack(monsters) is None:
        return {"status": "unknown", "reason": "enemy_intent_damage_missing"}

    cards = {}
    unsupported_playable = []
    for card in hand:
        if not isinstance(card, dict) or card.get("is_playable") is not True:
            continue
        instance_id = card.get("card_instance_id")
        effect = _simple_card_effect(card)
        if not instance_id or effect is None:
            unsupported_playable.append(instance_id or "missing-card-id")
            continue
        cards[str(instance_id)] = (card, effect)
    usable_potions = [
        potion for potion in potions
        if isinstance(potion, dict) and potion.get("can_use") is True
    ]

    states = []
    if first_choice_id == "action:end":
        states = [(monsters, block, energy, frozenset(cards))]
        allow_continuation = False
    elif first_choice_id.startswith("play:"):
        matching_cards = [
            instance_id for instance_id in cards
            if first_choice_id == f"play:{instance_id}"
            or first_choice_id.startswith(f"play:{instance_id}:")
        ]
        if len(matching_cards) != 1:
            return {"status": "unknown", "reason": "first_card_unsupported"}
        instance_id = matching_cards[0]
        card, effect = cards[instance_id]
        prefix = f"play:{instance_id}"
        target_id = (
            first_choice_id[len(prefix) + 1:]
            if first_choice_id.startswith(f"{prefix}:") else None
        )
        applied = _apply_simple_card(
            card, effect, monsters, block, energy, target_id
        )
        if applied is None:
            return {"status": "unknown", "reason": "first_card_effect_unresolved"}
        next_monsters, next_block, next_energy = applied
        states = [(
            next_monsters, next_block, next_energy,
            frozenset(set(cards) - {instance_id}),
        )]
        allow_continuation = True
    elif first_choice_id.startswith("potion:"):
        return {"status": "unknown", "reason": "potion_effect_unsupported"}
    else:
        return {"status": "unknown", "reason": "first_action_unsupported"}

    best_hp_after = float("-inf")
    visited = set()
    while states:
        current_monsters, current_block, current_energy, remaining = states.pop()
        state_key = (
            tuple(
                (
                    monster.get("enemy_instance_id"),
                    _number(monster.get("current_hp")),
                    _number(monster.get("block")),
                    monster.get("is_gone"),
                )
                for monster in current_monsters
            ),
            current_block,
            current_energy,
            tuple(sorted(remaining)),
        )
        if state_key in visited:
            continue
        visited.add(state_key)
        incoming = _incoming_attack(current_monsters)
        if incoming is None:
            return {"status": "unknown", "reason": "enemy_transition_unresolved"}
        hp_after = hp - max(0.0, incoming - current_block)
        best_hp_after = max(best_hp_after, hp_after)
        if not allow_continuation:
            continue
        for instance_id in remaining:
            card, effect = cards[instance_id]
            if effect["cost"] > current_energy:
                continue
            targets = [None]
            if effect["kind"] == "attack":
                targets = [
                    monster.get("enemy_instance_id")
                    for monster in current_monsters
                    if _number(monster.get("current_hp")) is not None
                    and _number(monster.get("current_hp")) > 0
                    and monster.get("is_gone") is not True
                    and monster.get("half_dead") is not True
                ]
            for target_id in targets:
                applied = _apply_simple_card(
                    card, effect, current_monsters, current_block,
                    current_energy, target_id,
                )
                if applied is None:
                    continue
                next_monsters, next_block, next_energy = applied
                states.append((
                    next_monsters, next_block, next_energy,
                    frozenset(set(remaining) - {instance_id}),
                ))

    if best_hp_after > 0:
        return {
            "status": "proven",
            "predicted_survives": True,
            "predicted_hp_loss": max(0.0, hp - best_hp_after),
            "basis": "independent exact Strike/Defend one-turn line",
        }
    surface_complete = (
        exact_environment
        and (
            not allow_continuation
            or (not unsupported_playable and not usable_potions)
        )
    )
    if surface_complete:
        return {
            "status": "proven",
            "predicted_survives": False,
            "predicted_hp_loss": max(0.0, hp - best_hp_after),
            "basis": "independent exhaustive exact Strike/Defend one-turn search",
        }
    return {
        "status": "unknown",
        "reason": "legal_surface_contains_unsupported_effects",
        "unsupported_playable_card_ids": unsupported_playable,
        "usable_potion_ids": [potion.get("id") for potion in usable_potions],
    }


def _structure_problems(record, candidates):
    """Return missing/malformed replay paths for one authoritative action."""

    problems = []
    for field in _REQUIRED_ACTION_FIELDS:
        if field not in record or record.get(field) is None:
            problems.append(field)
    for field in (
        "hand_before", "draw_pile_before", "discard_pile_before",
        "exhaust_pile_before", "potions_before", "relics_before",
        "legal_actions_before", "monsters_before",
    ):
        if field in record and not isinstance(record.get(field), list):
            problems.append(f"{field}:not_list")

    player = record.get("player_before")
    if isinstance(player, dict):
        for field in (
            "current_hp", "max_hp", "block", "energy", "powers", "orbs",
        ):
            if field not in player:
                problems.append(f"player_before.{field}")
    elif "player_before" in record:
        problems.append("player_before:not_object")

    for monster_index, monster in enumerate(record.get("monsters_before") or []):
        if not isinstance(monster, dict):
            problems.append(f"monsters_before[{monster_index}]:not_object")
            continue
        for field in (
            "enemy_instance_id", "current_hp", "block", "intent", "powers",
        ):
            if field not in monster:
                problems.append(f"monsters_before[{monster_index}].{field}")

    for potion_index, potion in enumerate(record.get("potions_before") or []):
        if not isinstance(potion, dict):
            problems.append(f"potions_before[{potion_index}]:not_object")
            continue
        for field in ("potion_instance_id", "id", "can_use"):
            if field not in potion:
                problems.append(f"potions_before[{potion_index}].{field}")

    for relic_index, relic in enumerate(record.get("relics_before") or []):
        if not isinstance(relic, dict):
            problems.append(f"relics_before[{relic_index}]:not_object")
            continue
        for field in ("id", "counter"):
            if field not in relic:
                problems.append(f"relics_before[{relic_index}].{field}")

    legal_actions = record.get("legal_actions_before")
    legal_ids = []
    if isinstance(legal_actions, list):
        if not legal_actions:
            problems.append("legal_actions_before:empty")
        for action_index, action in enumerate(legal_actions):
            if not isinstance(action, dict):
                problems.append(
                    f"legal_actions_before[{action_index}]:not_object"
                )
                continue
            choice_id = action.get("choice_id")
            if not choice_id:
                problems.append(
                    f"legal_actions_before[{action_index}].choice_id"
                )
            else:
                legal_ids.append(str(choice_id))
            if action.get("legal") is not True:
                problems.append(
                    f"legal_actions_before[{action_index}].legal"
                )

    candidate_ids = []
    if isinstance(candidates, list) and not candidates:
        problems.append("decision.candidates:empty")
    for candidate_index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            problems.append(
                f"decision.candidates[{candidate_index}]:not_object"
            )
            continue
        choice_id = candidate.get("choice_id", candidate.get("id"))
        if not choice_id:
            problems.append(
                f"decision.candidates[{candidate_index}].choice_id"
            )
        else:
            candidate_ids.append(str(choice_id))
    if isinstance(legal_actions, list) and isinstance(candidates, list):
        if Counter(legal_ids) != Counter(candidate_ids):
            problems.append("legal_action_candidate_bijection")
        if any(count > 1 for count in Counter(candidate_ids).values()):
            problems.append("decision.candidates:duplicate_choice_id")

    if "damage_model" in record and not isinstance(
        record.get("damage_model"), dict
    ):
        problems.append("damage_model:not_object")
    elif isinstance(record.get("damage_model"), dict) and not record[
        "damage_model"
    ]:
        problems.append("damage_model:empty")
    if "decision_outcome" in record and not isinstance(
        record.get("decision_outcome"), dict
    ):
        problems.append("decision_outcome:not_object")
    elif isinstance(record.get("decision_outcome"), dict) and not record[
        "decision_outcome"
    ]:
        problems.append("decision_outcome:empty")
    if isinstance(record.get("monsters_before"), list) and not record[
        "monsters_before"
    ]:
        problems.append("monsters_before:empty")
    return sorted(set(problems))


def _deferred_hand_select_terminal(records, terminal_cause, *, max_preceding=5):
    """Bind a fatal HAND_SELECT receipt back to its contiguous combat END."""

    if not (
        isinstance(terminal_cause, dict)
        and str(terminal_cause.get("phase") or "").upper() == "HAND_SELECT"
        and terminal_cause.get("action") in {"choose", "proceed"}
        and type(terminal_cause.get("before_seq")) is int
    ):
        return None
    before_state = terminal_cause.get("authoritative_state_before")
    after_state = terminal_cause.get("authoritative_state_after")
    if not isinstance(before_state, dict) or not isinstance(after_state, dict):
        return None
    before_game = before_state.get("game_state")
    after_game = after_state.get("game_state")
    if not isinstance(before_game, dict) or not isinstance(after_game, dict):
        return None
    if not (
        str(before_game.get("room_phase") or "").upper() == "COMBAT"
        and str(before_game.get("screen_type") or "").upper()
        == "HAND_SELECT"
        and str(after_state.get("phase") or "").upper() == "GAME_OVER"
        and str(after_game.get("screen_type") or "").upper() == "GAME_OVER"
    ):
        return None

    cursor = terminal_cause.get("before_seq")
    settlement_chain = [terminal_cause]
    for _ in range(max_preceding):
        predecessors = [
            record for record in records
            if isinstance(record, dict)
            and record.get("record_type") == "decision"
            and record.get("after_seq") == cursor
            and _same_binding(record, terminal_cause)
        ]
        if len(predecessors) != 1:
            return None
        predecessor = predecessors[0]
        phase = str(predecessor.get("phase") or "").upper()
        action = predecessor.get("action")
        if phase == "HAND_SELECT" and action in {"choose", "proceed"}:
            settlement_chain.append(predecessor)
            cursor = predecessor.get("before_seq")
            if type(cursor) is not int:
                return None
            continue
        if not (
            phase.startswith("COMBAT_TURN_")
            and action == "end"
            and predecessor.get("combat_id")
        ):
            return None
        end_after = predecessor.get("authoritative_state_after")
        end_after = end_after if isinstance(end_after, dict) else {}
        end_after_game = end_after.get("game_state")
        end_after_game = (
            end_after_game if isinstance(end_after_game, dict) else {}
        )
        if not (
            str(end_after.get("phase") or "").upper() == "HAND_SELECT"
            and str(end_after_game.get("screen_type") or "").upper()
            == "HAND_SELECT"
        ):
            return None

        player_before = predecessor.get("player_before")
        player_before = (
            player_before if isinstance(player_before, dict) else {}
        )
        hp_before = _number(player_before.get("current_hp"))
        hp_after = _number(terminal_cause.get("hp_after"))
        if hp_after is None:
            hp_after = _number(after_game.get("current_hp"))
        if hp_before is None or hp_after is None or hp_after > hp_before:
            return None
        hp_loss = hp_before - hp_after

        merged = dict(predecessor)
        merged.update({
            "after_seq": terminal_cause.get("after_seq"),
            "hp_after": hp_after,
            "authoritative_state_after": after_state,
            "deferred_hand_select_settlement": {
                "source_end_before_seq": predecessor.get("before_seq"),
                "source_end_after_seq": predecessor.get("after_seq"),
                "terminal_before_seq": terminal_cause.get("before_seq"),
                "terminal_after_seq": terminal_cause.get("after_seq"),
                "intermediate_before_seqs": sorted(
                    row.get("before_seq")
                    for row in settlement_chain
                    if type(row.get("before_seq")) is int
                ),
            },
        })
        outcome = dict(predecessor.get("decision_outcome") or {})
        terminal_outcome = terminal_cause.get("decision_outcome")
        terminal_outcome = (
            terminal_outcome if isinstance(terminal_outcome, dict) else {}
        )
        for key in (
            "phase_after", "room_phase_after", "room_type_after",
            "screen_type_after", "act_after", "floor_after",
        ):
            if key in terminal_outcome:
                outcome[key] = terminal_outcome[key]
        outcome.update({
            "player_hp_loss": hp_loss,
            "player_hp_delta": hp_after - hp_before,
            "hp_delta": hp_after - hp_before,
        })
        merged["decision_outcome"] = outcome
        damage_model = dict(predecessor.get("damage_model") or {})
        if "monsters_to_hero_actual" in damage_model:
            damage_model["monsters_to_hero_actual"] = hp_loss
            damage_model["monsters_to_hero_basis"] = (
                "deferred_hand_select_player_hp_delta"
            )
        merged["damage_model"] = damage_model
        return {
            "source_record": predecessor,
            "merged_record": merged,
            "terminal_record": terminal_cause,
        }
    return None


def _proven_heart_victory_transition(records, final_record, terminal):
    """Accept the explicit COMPLETE -> PROCEED -> GAME_OVER victory bridge.

    This does not exempt any combat prediction or terminal binding checks.
    It only identifies the authoritative after-state to validate when winning
    the Heart ends combat one decision before the victory screen.
    """
    if not (
        _authoritative_game_over(terminal)
        and terminal.get("victory") is True
        and terminal.get("heart_defeated") is True
        and terminal.get("act") == 4
        and all(value is not None for value in _binding(terminal))
        and _same_binding(final_record, terminal)
    ):
        return None
    def mapping(value):
        return value if isinstance(value, dict) else {}

    complete = mapping(final_record.get("authoritative_state_after"))
    complete_game = mapping(complete.get("game_state"))
    start = final_record.get("after_seq")
    finish = terminal.get("terminal_state_seq")
    if not (
        type(start) is int and type(finish) is int and finish > start
        and complete.get("state_seq") == start
        and complete.get("phase") == "COMPLETE"
        and complete_game.get("screen_type") == "COMPLETE"
        and _same_binding(complete, terminal)
    ):
        return None
    candidates = [
        record for record in records
        if isinstance(record, dict) and record.get("record_type") == "decision"
        and type(record.get("before_seq")) is int
        and start <= record["before_seq"] < finish
    ]
    if len(candidates) != 1:
        return None
    transition = candidates[0]
    before = mapping(transition.get("authoritative_state_before"))
    after = mapping(transition.get("authoritative_state_after"))
    before_game = mapping(before.get("game_state"))
    after_game = mapping(after.get("game_state"))
    if not (
        transition.get("action") == "proceed"
        and transition.get("before_seq") == start
        and transition.get("after_seq") == finish
        and _same_binding(transition, terminal)
        and _same_binding(before, terminal) and _same_binding(after, terminal)
        and before.get("state_seq") == start and after.get("state_seq") == finish
        and transition.get("phase") == before.get("phase") == "COMPLETE"
        and before_game.get("screen_type") == "COMPLETE"
        and after.get("phase") == after_game.get("screen_type") == "GAME_OVER"
    ):
        return None
    # Compact decision snapshots may omit display screen_state; the schema-2
    # authoritative terminal result owns victory in that case. A serialized
    # contradictory victory flag must still fail.
    screen_state = mapping(after_game.get("screen_state"))
    if "victory" in screen_state and screen_state["victory"] is not True:
        return None
    for key in ("seed", "class", "ascension_level", "act", "floor", "current_hp", "max_hp"):
        if key not in complete_game or (
            type(complete_game[key]) is not type(before_game.get(key))
            or complete_game[key] != before_game.get(key)
        ):
            return None
    if not (
        type(complete_game.get("floor")) is int
        and after_game.get("floor") == complete_game["floor"] + 1
        and complete_game.get("act") == after_game.get("act") == 4
        and _number(complete_game.get("current_hp")) is not None
        and complete_game["current_hp"] > 0
        and final_record.get("hp_after") == transition.get("hp_before")
        == transition.get("hp_after") == complete_game["current_hp"]
        == after_game.get("current_hp") == terminal.get("current_hp")
        and complete_game["max_hp"] == after_game.get("max_hp")
    ):
        return None
    return transition


def build_death_replay(records, terminal=None, *, last_turns=3):
    """Build a replay for the final death combat.

    Missing data makes the replay ``inconclusive`` rather than fabricating a
    counterfactual.  A proven unchosen survival line is an ``issues`` result.
    """

    records = list(records or [])
    if terminal is None:
        terminals = [
            record for record in records
            if isinstance(record, dict)
            and record.get("record_type") == "terminal_result"
        ]
        terminal = terminals[-1] if len(terminals) == 1 else None

    issues = []
    unknowns = []
    if not isinstance(last_turns, int) or isinstance(last_turns, bool) or last_turns < 1:
        raise ValueError("last_turns must be a positive integer")
    if not isinstance(terminal, dict):
        unknowns.append({"kind": "terminal_missing"})
    death_contract_proven = _is_death(terminal)
    death_signal_observed = _death_signal(terminal)
    act4_observed = _observed_act4(records, terminal)
    game_over_authority_proven = _authoritative_game_over(terminal)
    replay_is_death = death_contract_proven and game_over_authority_proven
    replay_is_act4 = act4_observed and game_over_authority_proven
    if death_signal_observed and not death_contract_proven:
        unknowns.append({"kind": "terminal_death_contract_invalid"})
    if (
        isinstance(terminal, dict)
        and not death_signal_observed
        and not act4_observed
    ):
        return {
            "schema_version": DEATH_REPLAY_SCHEMA_VERSION,
            "attempt_id": terminal.get("attempt_id"),
            "run_id": terminal.get("run_id"),
            "seed": terminal.get("seed"),
            "character": terminal.get("character", terminal.get("class")),
            "ascension_level": terminal.get("ascension_level"),
            "run_type": terminal.get("run_type"),
            "decision_hash": terminal.get("decision_hash"),
            "controller_hash": terminal.get("controller_hash"),
            "policy_version": terminal.get("policy_version"),
            "selection_id": terminal.get("selection_id"),
            "selection_digest": terminal.get("selection_digest"),
            "terminal_state_seq": terminal.get(
                "terminal_state_seq", terminal.get("state_seq")
            ),
            "replay_kind": "not_applicable",
            "status": "not_applicable",
            "issue_count": 0,
            "eligible_unknown_count": 0,
            "death_observed": False,
            "issues": [],
            "unknowns": [],
            "turns": [],
            "counterfactuals": [],
        }

    terminal = terminal or {}
    terminal_binding = _binding(terminal)
    if not (
        terminal.get("record_type") == "terminal_result"
        and type(terminal.get("schema_version")) is int
        and terminal.get("schema_version") == 2
        and all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                terminal_binding[0], terminal_binding[1],
                terminal_binding[3], terminal_binding[5],
                terminal_binding[6], terminal_binding[7],
                terminal_binding[8], terminal_binding[9],
            )
        )
        and (type(terminal_binding[2]) is int or (
            isinstance(terminal_binding[2], str)
            and bool(terminal_binding[2].strip())
        ))
        and type(terminal_binding[4]) is int
        and terminal_binding[4] == 0
        and terminal_binding[5] == "standard"
    ):
        unknowns.append({"kind": "terminal_binding_incomplete"})
    if terminal and not game_over_authority_proven:
        unknowns.append({
            "kind": "authoritative_game_over_unproven",
            "authoritative_game_over": terminal.get(
                "authoritative_game_over"
            ),
            "screen_type": terminal.get("screen_type"),
        })
    terminal_seq = terminal.get(
        "terminal_state_seq", terminal.get("state_seq")
    )
    terminal_decisions = [
        record for record in records
        if isinstance(record, dict)
        and record.get("record_type") == "decision"
        and type(record.get("after_seq")) is int
        and type(terminal_seq) is int
        and record.get("after_seq") == terminal_seq
    ]
    terminal_causes = [
        record for record in terminal_decisions
        if isinstance(record.get("authoritative_state_after"), dict)
        and record["authoritative_state_after"].get("phase") == "GAME_OVER"
        and isinstance(
            record["authoritative_state_after"].get("game_state"), dict
        )
        and record["authoritative_state_after"]["game_state"].get(
            "screen_type"
        ) == "GAME_OVER"
    ]
    if len(terminal_decisions) > 1:
        issues.append({
            "kind": "death_terminal_decision_ambiguous",
            "terminal_state_seq": terminal_seq,
            "before_seqs": sorted(
                record.get("before_seq") for record in terminal_decisions
                if type(record.get("before_seq")) is int
            ),
        })
    if terminal_decisions and len(terminal_causes) != 1:
        unknowns.append({
            "kind": "death_terminal_decision_cause_unproven",
            "terminal_state_seq": terminal_seq,
        })
    terminal_cause = terminal_causes[0] if len(terminal_causes) == 1 else None
    deferred_terminal = _deferred_hand_select_terminal(
        records, terminal_cause
    )
    noncombat_terminal_cause = bool(
        replay_is_death
        and isinstance(terminal_cause, dict)
        and deferred_terminal is None
        and not terminal_cause.get("combat_id")
        and str(terminal_cause.get("phase") or "").upper() != "COMBAT"
        and not str(terminal_cause.get("phase") or "").upper().startswith(
            "COMBAT_TURN_"
        )
    )
    if noncombat_terminal_cause:
        unknowns.append({
            "kind": "death_combat_not_applicable_noncombat_cause",
            "phase": terminal_cause.get("phase"),
            "before_seq": terminal_cause.get("before_seq"),
            "after_seq": terminal_cause.get("after_seq"),
        })

    combat = []
    for record in records:
        if not (
            isinstance(record, dict)
            and record.get("record_type") == "decision"
            and record.get("combat_id")
        ):
            continue
        if (
            isinstance(deferred_terminal, dict)
            and record is deferred_terminal.get("source_record")
        ):
            combat.append(deferred_terminal["merged_record"])
        else:
            combat.append(record)
    if noncombat_terminal_cause:
        final_combat = []
        combat_id = None
    elif not combat:
        unknowns.append({"kind": "death_combat_missing"})
        final_combat = []
        combat_id = None
    else:
        previous_after_seq = None
        sequenced_combat = []
        for record_index, record in enumerate(combat):
            before_seq = record.get("before_seq")
            after_seq = record.get("after_seq")
            if not (
                type(before_seq) is int and type(after_seq) is int
            ):
                unknowns.append({
                    "kind": "combat_trace_sequence_unavailable",
                    "record_index": record_index,
                    "combat_id": record.get("combat_id"),
                    "before_seq": before_seq,
                    "after_seq": after_seq,
                })
                continue
            if after_seq <= before_seq:
                issues.append({
                    "kind": "combat_action_sequence_invalid",
                    "record_index": record_index,
                    "combat_id": record.get("combat_id"),
                    "before_seq": before_seq,
                    "after_seq": after_seq,
                })
            if (
                previous_after_seq is not None
                and before_seq < previous_after_seq
            ):
                issues.append({
                    "kind": "combat_trace_sequence_regression",
                    "record_index": record_index,
                    "combat_id": record.get("combat_id"),
                    "before_seq": before_seq,
                    "previous_after_seq": previous_after_seq,
                })
            previous_after_seq = after_seq
            sequenced_combat.append(record)

        if sequenced_combat:
            latest_after_seq = max(
                record["after_seq"] for record in sequenced_combat
            )
            latest_ids = {
                record.get("combat_id") for record in sequenced_combat
                if record.get("after_seq") == latest_after_seq
            }
            if len(latest_ids) != 1:
                issues.append({
                    "kind": "final_combat_identity_ambiguous",
                    "after_seq": latest_after_seq,
                    "combat_ids": sorted(str(value) for value in latest_ids),
                })
            combat_id = next(
                record.get("combat_id") for record in reversed(sequenced_combat)
                if record.get("after_seq") == latest_after_seq
            )
        else:
            combat_id = combat[-1].get("combat_id")
        final_combat = [
            record for record in combat if record.get("combat_id") == combat_id
        ]
        final_combat.sort(key=lambda record: (
            record.get("before_seq")
            if type(record.get("before_seq")) is int else -1,
            record.get("after_seq")
            if type(record.get("after_seq")) is int else -1,
        ))

    exact_heart_rows = [
        monster
        for record in final_combat
        for monster in record.get("monsters_before") or []
        if isinstance(monster, dict)
        and _token(monster.get("id", monster.get("name"))) == "corruptheart"
    ]
    if terminal.get("heart_defeated") is True and final_combat:
        final_monster_ids = [
            str(monster.get("id", monster.get("name", ""))).casefold()
            for record in final_combat
            for monster in record.get("monsters_before") or []
            if isinstance(monster, dict)
        ]
        if not exact_heart_rows:
            issues.append({
                "kind": "heart_terminal_combat_identity_mismatch",
                "combat_id": combat_id,
                "observed_monster_ids": sorted(set(final_monster_ids)),
            })

    if exact_heart_rows:
        heart_row_power_sets = []
        for monster in exact_heart_rows:
            heart_row_power_sets.append({
                _token(power.get("id", power.get("name")))
                for power in monster.get("powers") or []
                if isinstance(power, dict)
            })
        heart_mechanics_proven_in_one_row = any(
            any("beatofdeath" in token for token in powers)
            and any("invincible" in token for token in powers)
            for powers in heart_row_power_sets
        )
        if not heart_mechanics_proven_in_one_row:
            unknowns.append({
                "kind": "heart_active_mechanism_evidence_missing",
                "combat_id": combat_id,
                "required_same_monster_row": [
                    "BeatOfDeath", "Invincible",
                ],
                "observed_power_ids_by_row": [
                    sorted(powers) for powers in heart_row_power_sets
                ],
            })

    for index, record in enumerate(final_combat):
        if not _same_binding(record, terminal):
            issues.append({
                "kind": "death_replay_binding_mismatch",
                "record_index": index,
                "before_seq": record.get("before_seq"),
            })

    turn_numbers = sorted({
        record.get("turn") for record in final_combat
        if isinstance(record.get("turn"), int)
        and not isinstance(record.get("turn"), bool)
    })
    selected_turns = turn_numbers[-last_turns:]
    if not noncombat_terminal_cause and len(selected_turns) < last_turns:
        complete_short_combat = bool(
            selected_turns
            and selected_turns[0] == 1
            and selected_turns == list(range(1, selected_turns[-1] + 1))
        )
        if not complete_short_combat:
            unknowns.append({
                "kind": "insufficient_complete_turns",
                "required": last_turns,
                "observed": len(selected_turns),
            })

    final_record = max(
        final_combat,
        key=lambda record: (
            record.get("after_seq")
            if type(record.get("after_seq")) is int else -1,
            record.get("before_seq")
            if type(record.get("before_seq")) is int else -1,
        ),
    ) if final_combat else None
    replay_turns = []
    counterfactuals = []
    for turn in selected_turns:
        actions = [
            record for record in final_combat if record.get("turn") == turn
        ]
        actions.sort(key=lambda record: (
            record.get("before_seq")
            if isinstance(record.get("before_seq"), int) else -1
        ))
        complete = bool(actions)
        action_rows = []
        for action_index, record in enumerate(actions):
            candidates = _candidate_rows(record)
            missing = []
            if candidates is None:
                missing.append("decision.candidates")
                candidates = []
            advice = _model_advice(record)
            if not isinstance(advice, dict):
                missing.append("decision.model_advice")
                advice = None
            elif not str(advice.get("status") or ""):
                missing.append("decision.model_advice.status")
            missing.extend(_structure_problems(record, candidates))
            if missing:
                complete = False
                unknowns.append({
                    "kind": "death_replay_fields_missing",
                    "turn": turn,
                    "action_index": action_index,
                    "before_seq": record.get("before_seq"),
                    "fields": sorted(set(missing)),
                })

            reconstructed_ids, legality_problems = (
                _reconstruct_legal_action_ids(record)
            )
            if legality_problems:
                complete = False
                unknowns.append({
                    "kind": "death_replay_legality_inputs_incomplete",
                    "turn": turn,
                    "action_index": action_index,
                    "before_seq": record.get("before_seq"),
                    "fields": sorted(set(legality_problems)),
                })
            declared_ids = [
                str(action.get("choice_id"))
                for action in record.get("legal_actions_before") or []
                if isinstance(action, dict) and action.get("choice_id")
                and action.get("legal") is True
            ]
            selected_action_id = _selected_action_id(record)
            candidate_selected_ids = [
                str(candidate.get("choice_id", candidate.get("id")))
                for candidate in candidates
                if isinstance(candidate, dict)
                and candidate.get("selected") is True
                and candidate.get("choice_id", candidate.get("id"))
            ]
            if selected_action_id is None:
                complete = False
                unknowns.append({
                    "kind": "selected_action_identity_unavailable",
                    "turn": turn,
                    "before_seq": record.get("before_seq"),
                    "action": record.get("action"),
                })
            elif (
                selected_action_id not in declared_ids
                or candidate_selected_ids != [selected_action_id]
            ):
                complete = False
                issues.append({
                    "kind": "selected_action_binding_mismatch",
                    "turn": turn,
                    "before_seq": record.get("before_seq"),
                    "selected_action_id": selected_action_id,
                    "declared_legal_action_ids": declared_ids,
                    "candidate_selected_ids": candidate_selected_ids,
                })
            if (
                reconstructed_ids is not None
                and not legality_problems
                and Counter(declared_ids) != Counter(
                reconstructed_ids
                )
            ):
                complete = False
                issues.append({
                    "kind": "legal_action_reconstruction_mismatch",
                    "turn": turn,
                    "before_seq": record.get("before_seq"),
                    "declared_only": sorted(
                        (Counter(declared_ids) - Counter(reconstructed_ids)).elements()
                    ),
                    "reconstructed_only": sorted(
                        (Counter(reconstructed_ids) - Counter(declared_ids)).elements()
                    ),
                })

            player_before = record.get("player_before")
            player_before = player_before if isinstance(player_before, dict) else {}
            hp_before = _number(player_before.get("current_hp"))
            hp_after = _number(record.get("hp_after"))
            outcome = record.get("decision_outcome")
            outcome = outcome if isinstance(outcome, dict) else {}
            delta_claim = _number(
                outcome.get("player_hp_delta", outcome.get("hp_delta"))
            )
            if hp_before is None or hp_after is None or delta_claim is None:
                complete = False
                unknowns.append({
                    "kind": "death_replay_player_delta_unavailable",
                    "turn": turn,
                    "before_seq": record.get("before_seq"),
                })
            elif abs((hp_after - hp_before) - delta_claim) > 1e-9:
                complete = False
                issues.append({
                    "kind": "death_replay_player_delta_mismatch",
                    "turn": turn,
                    "before_seq": record.get("before_seq"),
                    "claimed_delta": delta_claim,
                    "observed_delta": hp_after - hp_before,
                })

            damage_model = record.get("damage_model")
            damage_model = damage_model if isinstance(damage_model, dict) else {}
            if record.get("action") == "end" and hp_before is not None and hp_after is not None:
                observed_loss = max(0.0, hp_before - hp_after)
                for field in (
                    "monsters_to_hero_actual", "monsters_to_hero_predicted",
                ):
                    if field not in damage_model:
                        continue
                    claim = _number(damage_model.get(field))
                    if claim is None:
                        complete = False
                        unknowns.append({
                            "kind": "death_replay_damage_claim_unparseable",
                            "turn": turn,
                            "before_seq": record.get("before_seq"),
                            "field": field,
                        })
                    else:
                        comparable_claim = claim
                        if (
                            field == "monsters_to_hero_predicted"
                            and hp_after == 0
                        ):
                            comparable_claim = min(claim, hp_before)
                    if (
                        claim is not None
                        and damage_model.get("deterministic") is True
                        and abs(comparable_claim - observed_loss) > 1e-9
                    ):
                        complete = False
                        issues.append({
                            "kind": "death_replay_damage_prediction_mismatch",
                            "turn": turn,
                            "before_seq": record.get("before_seq"),
                            "field": field,
                            "claimed": claim,
                            "observed": observed_loss,
                        })
            is_fatal_record = bool(
                death_contract_proven
                and final_record is not None
                and record is final_record
                and hp_after is not None
                and hp_after == 0
            )
            for candidate in candidates:
                row = _counterfactual(candidate)
                if row is not None:
                    original_row = dict(row)
                    choice_id = str(row.get("choice_id") or "")
                    independent = _best_simple_turn_survival(
                        record, choice_id
                    )
                    if independent.get("status") == "proven":
                        original_prediction = row.get("predicted_survives")
                        row.update({
                            "evidence_level": "independent_oracle",
                            "predicted_survives": independent.get(
                                "predicted_survives"
                            ),
                            "predicted_hp_loss": independent.get(
                                "predicted_hp_loss"
                            ),
                            "basis": independent.get("basis"),
                            "producer_counterfactual": original_row,
                            "independent_resolution": independent,
                        })
                        if (
                            isinstance(original_prediction, bool)
                            and original_prediction
                            is not independent.get("predicted_survives")
                        ):
                            issues.append({
                                "kind": "counterfactual_oracle_disagreement",
                                "turn": turn,
                                "before_seq": record.get("before_seq"),
                                "choice_id": choice_id,
                                "producer_evidence_level": original_row.get(
                                    "evidence_level"
                                ),
                                "producer_predicted_survives": original_prediction,
                                "independent_predicted_survives": independent.get(
                                    "predicted_survives"
                                ),
                            })
                    elif independent.get("status") == "classified_non_guaranteed":
                        row.update({
                            "evidence_level": "independent_oracle",
                            "predicted_survives": None,
                            "predicted_hp_loss": None,
                            "basis": independent.get("basis"),
                            "producer_counterfactual": original_row,
                            "independent_resolution": independent,
                            "counterfactual_eligibility": (
                                "non_guaranteed_reactive_reroll"
                            ),
                        })
                    else:
                        original_level = row.get("evidence_level")
                        row.update({
                            "evidence_level": "unable_to_determine",
                            "predicted_survives": None,
                            "predicted_hp_loss": None,
                            "basis": "independent one-turn oracle could not prove this branch",
                            "producer_counterfactual": original_row,
                            "independent_resolution": independent,
                        })
                        if is_fatal_record:
                            complete = False
                            unknowns.append({
                                "kind": (
                                    "critical_counterfactual_unresolved"
                                    if original_level == "unable_to_determine"
                                    else "critical_counterfactual_not_independent"
                                ),
                                "turn": turn,
                                "before_seq": record.get("before_seq"),
                                "choice_id": choice_id,
                                "producer_evidence_level": original_level,
                                "reason": independent.get("reason"),
                            })
                        else:
                            row["counterfactual_eligibility"] = (
                                "not_applicable_nonfatal_action"
                            )
                    row.update({
                        "turn": turn,
                        "before_seq": record.get("before_seq"),
                    })
                    counterfactuals.append(row)
                    if (
                        row["evidence_level"]
                        in {"authoritative", "independent_oracle"}
                        and row.get("predicted_survives") is True
                        and is_fatal_record
                    ):
                        issues.append({
                            "kind": "proven_survival_alternative",
                            "turn": turn,
                            "before_seq": record.get("before_seq"),
                            "choice_id": row.get("choice_id"),
                            "evidence_level": row["evidence_level"],
                        })
                    actual_loss = record.get("decision_outcome")
                    actual_loss = (
                        actual_loss.get("player_hp_loss")
                        if isinstance(actual_loss, dict) else None
                    )
                    predicted_loss = row.get("predicted_hp_loss")
                    if (
                        row["evidence_level"] in {"authoritative", "independent_oracle"}
                        and isinstance(actual_loss, (int, float))
                        and not isinstance(actual_loss, bool)
                        and isinstance(predicted_loss, (int, float))
                        and not isinstance(predicted_loss, bool)
                        and predicted_loss < actual_loss
                    ):
                        issues.append({
                            "kind": "proven_lower_loss_alternative",
                            "turn": turn,
                            "before_seq": record.get("before_seq"),
                            "choice_id": row.get("choice_id"),
                            "evidence_level": row["evidence_level"],
                            "actual_hp_loss": actual_loss,
                            "alternative_hp_loss": predicted_loss,
                        })
            action_rows.append({
                "before_seq": record.get("before_seq"),
                "after_seq": record.get("after_seq"),
                "action": record.get("action"),
                "selected_target_id": record.get("resolved_target_id"),
                "player_before": record.get("player_before"),
                "monsters_before": record.get("monsters_before"),
                "hand_before": record.get("hand_before"),
                "draw_pile_before": record.get("draw_pile_before"),
                "discard_pile_before": record.get("discard_pile_before"),
                "exhaust_pile_before": record.get("exhaust_pile_before"),
                "potions_before": record.get("potions_before"),
                "relics_before": record.get("relics_before"),
                "legal_actions_before": record.get("legal_actions_before"),
                "independently_reconstructed_legal_action_ids": reconstructed_ids,
                "production_candidates": candidates,
                "prediction": record.get("damage_model"),
                "actual_outcome": record.get("decision_outcome"),
                "model_advice": advice,
                "final_action": record.get("action"),
            })

        for previous, current in zip(actions, actions[1:]):
            previous_after = previous.get("after_seq")
            current_before = current.get("before_seq")
            if not (
                isinstance(previous_after, int)
                and isinstance(current_before, int)
                and current_before >= previous_after
            ):
                complete = False
                unknowns.append({
                    "kind": "death_replay_state_sequence_not_monotonic",
                    "turn": turn,
                    "previous_after_seq": previous_after,
                    "current_before_seq": current_before,
                })

        replay_turns.append({
            "turn": turn,
            "complete": complete,
            "first_state_seq": actions[0].get("before_seq") if actions else None,
            "last_state_seq": actions[-1].get("after_seq") if actions else None,
            "actions": action_rows,
        })

    # Turn closure is proved by the next authoritative turn transition, not
    # by assuming that every turn's final logged command must be END (Vault,
    # Time Eater, automatic transitions, and terminal actions violate that
    # assumption).
    actions_by_turn = {
        turn: sorted(
            [record for record in final_combat if record.get("turn") == turn],
            key=lambda record: (
                record.get("before_seq")
                if isinstance(record.get("before_seq"), int) else -1
            ),
        )
        for turn in selected_turns
    }
    for position, (previous_turn, current_turn) in enumerate(zip(
        selected_turns, selected_turns[1:]
    )):
        previous_actions = actions_by_turn.get(previous_turn) or []
        current_actions = actions_by_turn.get(current_turn) or []
        previous_after = previous_actions[-1].get("after_seq") if previous_actions else None
        current_before = current_actions[0].get("before_seq") if current_actions else None
        if not (
            current_turn == previous_turn + 1
            and isinstance(previous_after, int)
            and isinstance(current_before, int)
            and current_before >= previous_after
        ):
            if position < len(replay_turns):
                replay_turns[position]["complete"] = False
            unknowns.append({
                "kind": "death_replay_turn_transition_unproven",
                "previous_turn": previous_turn,
                "current_turn": current_turn,
                "previous_after_seq": previous_after,
                "current_before_seq": current_before,
            })

    result_state_seq = terminal.get("state_seq")
    if type(result_state_seq) is not int:
        unknowns.append({"kind": "terminal_result_state_seq_missing"})
    elif type(terminal_seq) is int and result_state_seq != terminal_seq:
        issues.append({
            "kind": "terminal_result_state_seq_mismatch",
            "state_seq": result_state_seq,
            "terminal_state_seq": terminal_seq,
        })
    if final_combat:
        final_after_seq = final_record.get("after_seq")
        if type(final_after_seq) is not int:
            unknowns.append({"kind": "final_combat_state_seq_missing"})
            if replay_turns:
                replay_turns[-1]["complete"] = False
        elif type(terminal_seq) is not int:
            unknowns.append({"kind": "terminal_state_seq_missing"})
            if replay_turns:
                replay_turns[-1]["complete"] = False
        elif final_after_seq > terminal_seq:
            issues.append({
                "kind": "death_terminal_state_seq_mismatch",
                "final_after_seq": final_after_seq,
                "terminal_state_seq": terminal_seq,
                "reason": "terminal_precedes_final_combat_state",
            })
            if replay_turns:
                replay_turns[-1]["complete"] = False

        authoritative_after = final_record.get("authoritative_state_after")
        victory_transition = _proven_heart_victory_transition(
            records, final_record, terminal
        )
        validated_after_seq = final_after_seq
        if victory_transition is not None:
            authoritative_after = victory_transition["authoritative_state_after"]
            validated_after_seq = victory_transition["after_seq"]
        if not isinstance(authoritative_after, dict):
            unknowns.append({
                "kind": "final_authoritative_combat_after_missing",
                "final_after_seq": final_after_seq,
                "terminal_state_seq": terminal_seq,
            })
            if replay_turns:
                replay_turns[-1]["complete"] = False
        else:
            after_game = authoritative_after.get("game_state")
            after_game = after_game if isinstance(after_game, dict) else None
            missing_after = []
            mismatched_after = []
            if authoritative_after.get("state_seq") is None:
                missing_after.append("state_seq")
            elif authoritative_after.get("state_seq") != validated_after_seq:
                mismatched_after.append("state_seq")
            if authoritative_after.get("phase") is None:
                missing_after.append("phase")
            elif authoritative_after.get("phase") != "GAME_OVER":
                mismatched_after.append("phase")
            if after_game is None:
                missing_after.append("game_state")
            else:
                terminal_facts = {
                    "seed": terminal.get("seed"),
                    "class": terminal.get("character", terminal.get("class")),
                    "ascension_level": terminal.get("ascension_level"),
                    "act": terminal.get("act"),
                    "floor": terminal.get("floor"),
                    "current_hp": terminal.get("current_hp"),
                }
                for field, expected_value in terminal_facts.items():
                    if field not in after_game or after_game.get(field) is None:
                        missing_after.append(f"game_state.{field}")
                    elif (
                        type(after_game.get(field)) is not type(expected_value)
                        or after_game.get(field) != expected_value
                    ):
                        mismatched_after.append(f"game_state.{field}")
                if after_game.get("screen_type") is None:
                    missing_after.append("game_state.screen_type")
                elif after_game.get("screen_type") != "GAME_OVER":
                    mismatched_after.append("game_state.screen_type")
                record_hp_after = _number(final_record.get("hp_after"))
                terminal_hp = _number(terminal.get("current_hp"))
                after_hp = _number(after_game.get("current_hp"))
                if (
                    record_hp_after is None
                    or terminal_hp is None
                    or after_hp is None
                ):
                    missing_after.append("terminal_hp_chain")
                elif not (
                    record_hp_after == after_hp == terminal_hp
                ):
                    mismatched_after.append("terminal_hp_chain")
                elif death_contract_proven and terminal_hp != 0:
                    mismatched_after.append("death_hp_not_zero")
            if missing_after:
                unknowns.append({
                    "kind": "final_authoritative_combat_after_incomplete",
                    "fields": sorted(set(missing_after)),
                    "final_after_seq": final_after_seq,
                })
                if replay_turns:
                    replay_turns[-1]["complete"] = False
            if mismatched_after:
                issues.append({
                    "kind": "final_authoritative_combat_after_mismatch",
                    "fields": sorted(set(mismatched_after)),
                    "final_after_seq": final_after_seq,
                    "terminal_state_seq": terminal_seq,
                })
                if replay_turns:
                    replay_turns[-1]["complete"] = False

    evidence_counts = Counter(
        row["evidence_level"] for row in counterfactuals
    )
    status = "issues" if issues else "inconclusive" if unknowns else "clear"
    return {
        "schema_version": DEATH_REPLAY_SCHEMA_VERSION,
        "attempt_id": terminal.get("attempt_id"),
        "run_id": terminal.get("run_id"),
        "seed": terminal.get("seed"),
        "character": terminal.get("character", terminal.get("class")),
        "ascension_level": terminal.get("ascension_level"),
        "run_type": terminal.get("run_type"),
        "decision_hash": terminal.get("decision_hash"),
        "controller_hash": terminal.get("controller_hash"),
        "policy_version": terminal.get("policy_version"),
        "selection_id": terminal.get("selection_id"),
        "selection_digest": terminal.get("selection_digest"),
        "terminal_state_seq": terminal.get(
            "terminal_state_seq", terminal.get("state_seq")
        ),
        "replay_kind": (
            "death" if replay_is_death
            else "act4_terminal" if replay_is_act4 else "unknown"
        ),
        "terminal_cause": (
            {
                "kind": "deferred_combat_end",
                "phase": (
                    deferred_terminal["merged_record"].get("phase")
                ),
                "action": "end",
                "before_seq": (
                    deferred_terminal["merged_record"].get("before_seq")
                ),
                "after_seq": (
                    deferred_terminal["merged_record"].get("after_seq")
                ),
                "settlement_phase": terminal_cause.get("phase"),
                "settlement_before_seq": terminal_cause.get("before_seq"),
                "settlement_after_seq": terminal_cause.get("after_seq"),
            }
            if isinstance(deferred_terminal, dict) else
            {
                "kind": "noncombat_decision",
                "phase": terminal_cause.get("phase"),
                "action": terminal_cause.get("action"),
                "before_seq": terminal_cause.get("before_seq"),
                "after_seq": terminal_cause.get("after_seq"),
                "requested_target_id": terminal_cause.get(
                    "requested_target_id"
                ),
                "resolved_target_id": terminal_cause.get(
                    "resolved_target_id"
                ),
            }
            if noncombat_terminal_cause else None
        ),
        "combat_id": combat_id,
        "requested_turn_count": last_turns,
        "status": status,
        "issue_count": len(issues),
        "eligible_unknown_count": len(unknowns),
        "death_observed": replay_is_death,
        "issues": issues,
        "unknowns": unknowns,
        "evidence_level_counts": dict(evidence_counts),
        "turns": replay_turns,
        "counterfactuals": counterfactuals,
    }


def write_death_replay(path, records, terminal=None, *, last_turns=3):
    """Atomically write and return one death replay JSON artifact."""

    replay = build_death_replay(records, terminal, last_turns=last_turns)
    write_replay(path, replay)
    return replay


def write_replay(path, replay):
    """Atomically persist the exact replay object already used by an audit."""

    if not isinstance(replay, dict):
        raise TypeError("death replay must be an object")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(replay, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return replay
