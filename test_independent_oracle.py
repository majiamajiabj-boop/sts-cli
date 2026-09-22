import copy
import ast
import hashlib
import json
import unittest
from pathlib import Path

import autoplay
import bridge
import decision_cases
import independent_oracle


ATTEMPT = "attempt-oracle"
RUN_ID = "IRONCLAD:0:123"
SEED = 123
DECISION_HASH = "decision-hash"
CONTROLLER_HASH = "controller-hash"
POLICY_VERSION = "fast-policy-v5"
SELECTION_DIGEST = "d" * 64


def knowing_skull_surface(*, costs):
    instance = {
        "potion_cost": costs[0], "gold_cost": costs[1],
        "card_cost": costs[2], "leave_cost": costs[3],
        "gold_reward": 90,
    }
    rows = (
        ("TAKE_POTION", {"hp_loss": costs[0], "reward_count": 1,
                         "reward_kind": "RANDOM_POTION"}),
        ("TAKE_GOLD", {"hp_loss": costs[1], "gold_gain": 90}),
        ("TAKE_CARD", {"hp_loss": costs[2], "reward_count": 1,
                       "reward_color": "COLORLESS",
                       "reward_rarity": "UNCOMMON",
                       "selection_mode": "RANDOM"}),
        ("LEAVE", {"hp_loss": costs[3]}),
    )
    targets = []
    options = []
    for index, (kind, parameters) in enumerate(rows):
        contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "Knowing Skull",
            "event_class": "com.megacrit.cardcrawl.events.city.KnowingSkull",
            "event_stage": "ASK", "original_button_index": index,
            "option_kind": kind,
            "instance_parameters": copy.deepcopy(instance),
            "parameters": copy.deepcopy(parameters),
        }
        encoded = json.dumps(
            contract, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target = {
            "kind": "event_option", "event_id": "Knowing Skull",
            "choice_index": index, "original_button_index": index,
            "event_contract": copy.deepcopy(contract),
            "mechanism_id": (
                "event-mechanism:"
                + hashlib.sha256(encoded).hexdigest()[:20]
            ),
        }
        targets.append(target)
        options.append({
            "choice_index": index, "original_button_index": index,
            "disabled": False, "event_contract": copy.deepcopy(contract),
        })
    return targets, options


def structured_consequence(raw_text, *, event_id=None, gold_delta=0):
    value = {
        "schema_version": 1,
        "scope": "immediate_protocol_transition",
        "hp_delta": 0,
        "max_hp_delta": 0,
        "gold_delta": gold_delta,
        "card_changes": {
            "gain": [], "remove": [], "upgrade": [], "transform": [],
        },
        "relic_changes": {"gain": [], "remove": [], "counter": []},
        "potion_changes": {"gain": [], "remove": [], "replace": []},
        "curse": {
            "gain": [], "remove": [], "probability": 0.0,
            "omamori_applicable": False, "omamori_charges_consumed": 0,
        },
        "probabilistic_outcomes": [],
        "current_cost": {"gold": 0, "hp": 0, "max_hp": 0},
        "future_costs": [],
        "raw_effect_text": raw_text,
        "uncertainty": [],
        "uncertainty_classification": {
            "status": "none",
            "authority": "test_independent_fixture",
            "reason": "typed_gain_gold_event_primitive",
        },
        "event_id": event_id,
    }
    value["field_knowledge"] = {
        field: {
            "status": "known",
            "authority": "test_independent_fixture",
            "reason": "typed_gain_gold_event_primitive",
        }
        for field in (
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
            "probabilistic_outcomes", "current_cost", "future_costs",
        )
    }
    return value


def bound(record_type, **overrides):
    row = {
        "record_type": record_type,
        "attempt_id": ATTEMPT,
        "run_id": RUN_ID,
        "seed": SEED,
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "policy_version": POLICY_VERSION,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "selection_id": "selection-oracle",
        "selection_digest": SELECTION_DIGEST,
    }
    row.update(overrides)
    if record_type == "decision":
        before_seq = row.get("before_seq")
        after_seq = row.get("after_seq")
        if type(before_seq) is int and "authoritative_state_before" not in row:
            observable = row.get("observable_state_before")
            combat = None
            if isinstance(row.get("monsters_before"), list):
                combat = {
                    "player": copy.deepcopy(row.get("player_before") or {}),
                    "monsters": copy.deepcopy(row.get("monsters_before") or []),
                }
            if isinstance(observable, dict) or combat is not None:
                row["authoritative_state_before"] = authoritative_state(
                    before_seq, observable=observable, combat=combat,
                    room_phase=(
                        "COMBAT" if combat is not None else "EVENT"
                    ),
                )
        if type(after_seq) is int and "authoritative_state_after" not in row:
            observable = row.get("observable_state_after")
            if isinstance(observable, dict):
                row["authoritative_state_after"] = authoritative_state(
                    after_seq, observable=observable, room_phase="EVENT",
                )
        for side in ("before", "after"):
            key = f"authoritative_state_{side}"
            if isinstance(row.get(key), dict):
                row[key] = authoritative_state(
                    row.get("before_seq" if side == "before" else "after_seq"),
                    existing=row[key],
                )
    return row


def authoritative_state(
    state_seq, *, observable=None, combat=None, room_phase=None, existing=None
):
    existing = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    supplied_game = existing.get("game_state")
    supplied_game = supplied_game if isinstance(supplied_game, dict) else {}
    observable = observable if isinstance(observable, dict) else {}
    keys = observable.get("keys") or {
        "ruby": False, "emerald": False, "sapphire": False,
    }
    game = {
        "class": "IRONCLAD",
        "ascension_level": 0,
        "seed": SEED,
        "act": 3,
        "floor": 50,
        "current_hp": observable.get("current_hp"),
        "max_hp": observable.get("max_hp"),
        "block": observable.get("block"),
        "gold": observable.get("gold"),
        "room_phase": room_phase,
        "screen_type": "EVENT" if room_phase != "COMBAT" else None,
        "deck": copy.deepcopy(observable.get("deck", [])),
        "relics": copy.deepcopy(observable.get("relics", [])),
        "potions": copy.deepcopy(observable.get("potions", [])),
        "keys": copy.deepcopy(keys),
        "has_ruby_key": keys.get("ruby"),
        "has_emerald_key": keys.get("emerald"),
        "has_sapphire_key": keys.get("sapphire"),
        **supplied_game,
    }
    if combat is not None and "combat_state" not in supplied_game:
        game["combat_state"] = copy.deepcopy(combat)
    result = {
        "protocol_version": 2,
        "attempt_id": ATTEMPT,
        "run_id": RUN_ID,
        "seed": SEED,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "policy_version": POLICY_VERSION,
        "selection_id": "selection-oracle",
        "selection_digest": SELECTION_DIGEST,
        "state_seq": state_seq,
        "game_state": game,
    }
    result.update({key: value for key, value in existing.items() if key != "game_state"})
    result["game_state"] = game
    return result


def choice(choice_id, index, score, *, selected=False, label=None):
    label = label or choice_id
    target = {
        "kind": "event_option",
        "event_id": choice_id,
        "mechanism_id": "gain_gold_exact",
        "amount": 10 if index == 0 else 0,
    }
    return independent_oracle.canonical_choice(
        choice_id,
        choice_index=index,
        label=label,
        raw_text=label,
        semantic_id=f"event:{index}",
        target=target,
        consequences=structured_consequence(
            label,
            event_id=choice_id,
            gold_delta=10 if index == 0 else 0,
        ),
        local_score=score,
        final_source="local",
        selected=selected,
    )


def production_candidate(
    choice_id, index, score, *, action="choose", operation=None,
    consequences=None,
):
    row = {
        "choice_id": choice_id,
        "choice_index": index,
        "action": action,
        "score": score,
        "local_reason": "production integration fixture",
        "reason_codes": ["PRODUCTION_INTEGRATION_FIXTURE"],
        "score_rule_id": "production_integration_sum",
        "score_formula": {"kind": "sum_components_v1"},
        "score_inputs": {"base": score},
        "score_components": [{
            "name": "base", "input": "base", "coefficient": 1,
            "value": score,
        }],
        "consequences": copy.deepcopy(consequences or {}),
    }
    if operation is not None:
        row["operation"] = operation
    return row


def typed_neow_option(
    choice_index, reward_kind="HUNDRED_GOLD", drawback_kind="NONE",
    *, max_hp=80, label="Neow option",
):
    contract = {
        "contract_version": 1,
        "contract_kind": "NEOW_REWARD",
        "reward_kind": reward_kind,
        "drawback_kind": drawback_kind,
        "parameters": {
            "hp_bonus": max_hp // 10,
            "cursed": drawback_kind == "CURSE",
            "drawback_def_kind": (
                None if drawback_kind == "NONE" else drawback_kind
            ),
        },
    }
    encoded = json.dumps(
        contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "option_id": f"neow:{choice_index}",
        "choice_index": choice_index,
        "label": label,
        "target": {
            "kind": "event_option",
            "event_id": "Neow Event",
            "neow_contract": contract,
            "mechanism_id": (
                "neow-mechanism:"
                + hashlib.sha256(encoded).hexdigest()[:20]
            ),
        },
    }


def neow_production_candidate(option, before, score):
    state = {
        "phase": "NEOW", "options": [option],
        "available_commands": ["choose"],
        "game_state": copy.deepcopy(before),
    }
    consequence = autoplay._structured_option_consequence(
        "NEOW", option["target"], option["label"], state=state
    )
    claim = {
        key: copy.deepcopy(value)
        for key, value in consequence.items()
        if key in autoplay._PRODUCER_EFFECT_FIELDS
    }
    return production_candidate(
        option["option_id"], option["choice_index"], score,
        consequences=claim,
    )


def strategic_decision():
    return bound(
        "decision",
        before_seq=10,
        after_seq=11,
        phase="EVENT",
        action="choose",
        available_options_before=[
            {
                "option_id": "option:a", "choice_index": 0,
                "label": "Gain 10 Gold",
                "target": {
                    "kind": "event_option", "event_id": "option:a",
                    "mechanism_id": "gain_gold_exact", "amount": 10,
                },
            },
            {
                "option_id": "option:b", "choice_index": 1,
                "label": "Gain 0 Gold",
                "target": {
                    "kind": "event_option", "event_id": "option:b",
                    "mechanism_id": "gain_gold_exact", "amount": 0,
                },
            },
        ],
        available_commands_before=["choose", "state"],
        legal_choices_before=[
            choice(
                "option:a", 0, 10, selected=True,
                label="Gain 10 Gold",
            ),
            choice("option:b", 1, 5, label="Gain 0 Gold"),
        ],
        selected_choice_ids=["option:a"],
        requested_target_id="option:a",
        resolved_target_id="option:a",
        final_choice_ids=["option:a"],
        observable_state_before={
            "current_hp": 50,
            "max_hp": 80,
            "gold": 100,
            "block": 0,
            "deck": [],
            "relics": [],
            "potions": [],
            "keys": {"ruby": False, "emerald": False, "sapphire": False},
        },
        observable_state_after={
            "current_hp": 50,
            "max_hp": 80,
            "gold": 110,
            "block": 0,
            "deck": [],
            "relics": [],
            "potions": [],
            "keys": {"ruby": False, "emerald": False, "sapphire": False},
        },
        decision_outcome={
            "current_hp_delta": 0,
            "hp_delta": 0,
            "max_hp_delta": 0,
            "gold_delta": 10,
            "block_delta": 0,
            "deck": {"added": [], "removed": [], "changed": []},
            "relics": {"added": [], "removed": [], "changed": []},
            "potions": {"added": [], "removed": [], "changed": []},
            "keys_before": {
                "ruby": False, "emerald": False, "sapphire": False,
            },
            "keys_after": {
                "ruby": False, "emerald": False, "sapphire": False,
            },
        },
        decision={
            "model_advice": {
                "status": "agreed",
                "applied": False,
                "model_choice_id": "option:a",
                "final_choice_ids": ["option:a"],
            }
        },
    )


def controller_start():
    return bound("controller_start", schema_version=2, state_seq=1)


def terminal():
    return bound(
        "terminal_result",
        schema_version=2,
        termination_kind="game_over",
        screen_type="GAME_OVER",
        state_seq=100,
        terminal_state_seq=100,
        victory=False,
        heart_defeated=False,
        current_hp=0,
        act=3,
        observed_max_act=3,
    )


def base_records(decision=None):
    return [controller_start(), decision or strategic_decision(), terminal()]


def combat_choice_expected():
    return {
        "attempt_id": ATTEMPT,
        "run_id": RUN_ID,
        "seed": SEED,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "policy_version": POLICY_VERSION,
        "selection_id": "selection-oracle",
        "selection_digest": SELECTION_DIGEST,
    }


def combat_choice_card(card_id, uuid):
    return {
        "id": card_id,
        "name": card_id,
        "type": "SKILL",
        "rarity": "COMMON",
        "upgrades": 0,
        "card_instance_id": uuid,
    }


def combat_choice_state(
    seq, phase, current_action, *, deck, hand, draw, discard,
    exhaust=None, limbo=None, visible=None, selected=None,
    max_cards=None, powers=None,
):
    if phase == "GRID":
        screen = {
            "cards": copy.deepcopy(visible if visible is not None else []),
            "selected_cards": copy.deepcopy(
                selected if selected is not None else []
            ),
            "num_cards": 1,
            "any_number": False,
            "confirm_up": bool(selected),
        }
    elif phase == "HAND_SELECT":
        screen = {
            "hand": copy.deepcopy(visible if visible is not None else hand),
            "selected": copy.deepcopy(selected if selected is not None else []),
            "max_cards": len(hand) if max_cards is None else max_cards,
            "can_pick_zero": True,
        }
    elif phase == "CARD_REWARD":
        screen = {
            "cards": copy.deepcopy(visible if visible is not None else []),
            "bowl_available": False,
            "skip_available": False,
        }
    else:
        screen = {}
    return authoritative_state(
        seq,
        existing={
            "phase": phase,
            "game_state": {
                "room_phase": "COMBAT",
                "screen_type": phase if phase in {
                    "GRID", "CARD_REWARD", "HAND_SELECT"
                } else "NONE",
                "current_action": current_action,
                "deck": copy.deepcopy(deck),
                "screen_state": screen,
                "combat_state": {
                    "turn": 1,
                    "player": {
                        "current_hp": 50, "max_hp": 80,
                        "block": 0, "energy": 3,
                        "powers": copy.deepcopy(powers or []),
                    },
                    "monsters": [],
                    "hand": copy.deepcopy(hand),
                    "draw_pile": copy.deepcopy(draw),
                    "discard_pile": copy.deepcopy(discard),
                    "exhaust_pile": copy.deepcopy(exhaust or []),
                    "limbo": copy.deepcopy(limbo or []),
                },
            },
        },
    )


def combat_choice_record(before, after, *, card=None, command="choose"):
    option_id = (
        f"card:{card['card_instance_id']}" if card is not None else None
    )
    values = {
        "before_seq": before["state_seq"],
        "after_seq": after["state_seq"],
        "phase": before["phase"],
        "action": command,
        "audit_projection_version": 2,
        "authoritative_state_before": copy.deepcopy(before),
        "authoritative_state_after": copy.deepcopy(after),
        "decision_outcome": {
            "combat_choice_transition": (
                autoplay.combat_choice_transition_claim(before, after)
            ),
        },
    }
    if card is not None:
        values.update({
            "requested_target_id": option_id,
            "resolved_target_id": option_id,
            "selected_choice_ids": [option_id],
            "final_choice_ids": [option_id],
            "available_options_before": [{
                "option_id": option_id,
                "choice_index": 0,
                "label": card["name"],
                "target": {
                    "kind": "card",
                    "card_instance_id": card["card_instance_id"],
                    "card": copy.deepcopy(card),
                },
            }],
        })
    elif command == "proceed":
        values.update({
            "requested_target_id": "action:proceed",
            "resolved_target_id": "action:proceed",
        })
    return bound("decision", **values)


def combat_choice_audit(records):
    return independent_oracle._audit_combat_choice_transitions(
        records, combat_choice_expected()
    )


def regeneration_end_damage_record(
    before_seq, *, regeneration, gross_damage, predicted=None,
):
    before_hp = 40
    net_loss = gross_damage - regeneration
    after_hp = before_hp - net_loss
    monster = {
        "enemy_instance_id": "enemy:regeneration-fixture",
        "id": "Cultist",
        "current_hp": 30,
        "is_gone": False,
        "half_dead": False,
    }
    before_game = {
        "room_phase": "COMBAT",
        "screen_type": "NONE",
        "current_hp": before_hp,
        "max_hp": 80,
        "relics": [],
        "combat_state": {
            "player": {
                "current_hp": before_hp,
                "max_hp": 80,
                "powers": [{
                    "id": "Regeneration", "amount": regeneration,
                }],
            },
            "monsters": [copy.deepcopy(monster)],
        },
    }
    remaining = regeneration - 1
    after_powers = (
        [] if remaining <= 0
        else [{"id": "Regeneration", "amount": remaining}]
    )
    after_game = {
        "room_phase": "COMBAT",
        "screen_type": "NONE",
        "current_hp": after_hp,
        "max_hp": 80,
        "relics": [],
        "combat_state": {
            "player": {
                "current_hp": after_hp,
                "max_hp": 80,
                "powers": after_powers,
            },
            "monsters": [copy.deepcopy(monster)],
        },
    }
    return bound(
        "decision",
        before_seq=before_seq,
        after_seq=before_seq + 1,
        phase="COMBAT_TURN_1",
        action="end",
        authoritative_state_before=authoritative_state(
            before_seq, existing={"game_state": before_game},
        ),
        authoritative_state_after=authoritative_state(
            before_seq + 1, existing={"game_state": after_game},
        ),
        damage_model={
            "deterministic": True,
            "monsters_to_hero_basis": "end_turn_player_hp_delta",
            "monsters_to_hero_predicted": (
                gross_damage if predicted is None else predicted
            ),
            "monsters_to_hero_actual": max(0, net_loss),
        },
    )


def issue_kinds(report):
    return {item["kind"] for item in report["issues"]}


class IndependentOracleTests(unittest.TestCase):
    def test_transition_and_navigation_commands_are_not_strategic_surface(self):
        self.assertFalse(independent_oracle._record_has_strategic_surface({
            "phase": "COMBAT_REWARD", "available_options_before": [],
            "available_commands_before": ["proceed"],
        }))
        self.assertFalse(independent_oracle._record_has_strategic_surface({
            "phase": "EVENT", "action": "wait",
            "available_options_before": [{
                "option_id": "match:remaining", "choice_index": 0,
                "target": {"kind": "event_option"},
            }],
        }))
        record = {
            "phase": "MAP",
            "available_options_before": [
                {"option_id": "map:a", "choice_index": 0,
                 "target": {"kind": "map_node", "x": 1, "y": 2}},
                {"option_id": "map:b", "choice_index": 1,
                 "target": {"kind": "map_node", "x": 2, "y": 2}},
            ],
            "available_commands_before": ["choose", "return"],
        }
        choices, missing = independent_oracle.reconstruct_legal_choices(record)
        self.assertEqual([], missing)
        self.assertNotIn("action:return", {
            row["choice_id"] for row in choices
        })
        canonical = [
            {"choice_id": "map:a", "action": "choose"},
            {"choice_id": "action:return", "action": "return"},
        ]
        self.assertEqual(
            [canonical[0]],
            independent_oracle._strategic_canonical_rows(record, canonical),
        )
        bound_rows = {
            "option:canonical": {
                "candidate_binding": "unique",
                "choice_id": "option:canonical",
                "semantic_id": "grid:uuid-a",
                "candidate_ids": ["grid:uuid-a"],
                "producer_candidate_raw": {
                    "choice_id": "grid:uuid-a", "id": "Bash",
                },
            },
        }
        self.assertEqual(
            "option:canonical",
            independent_oracle._canonical_advice_choice_id(
                "grid:uuid-a", bound_rows
            ),
        )
        ambiguous = copy.deepcopy(bound_rows)
        ambiguous["option:other"] = copy.deepcopy(
            ambiguous["option:canonical"]
        )
        self.assertEqual(
            "grid:uuid-a",
            independent_oracle._canonical_advice_choice_id(
                "grid:uuid-a", ambiguous
            ),
        )

    def test_combat_choice_grid_discard_to_hand_is_uuid_bound_and_order_free(self):
        chosen = combat_choice_card("HologramTarget", "chosen")
        other = combat_choice_card("Defend_R", "other")
        kept = combat_choice_card("Strike_R", "kept")
        before = combat_choice_state(
            10, "GRID", "BetterDiscardPileToHandAction",
            deck=[chosen, other, kept], hand=[kept], draw=[],
            discard=[chosen, other], visible=[other, chosen], selected=[],
        )
        after = combat_choice_state(
            11, "COMBAT_TURN_1", None,
            deck=[kept, chosen, other], hand=[kept, chosen], draw=[],
            discard=[other],
        )
        record = combat_choice_record(before, after, card=chosen)
        # Producer container order is presentation-only; immutable UUIDs bind
        # the same authoritative set.
        record["decision_outcome"]["combat_choice_transition"]["before"][
            "piles"
        ]["discard_pile"]["card_instance_ids"] = ["other", "chosen"]

        report = combat_choice_audit([record])

        self.assertEqual("clear", report["transitions"][0]["status"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            [{
                "card_instance_id": "chosen",
                "from": "discard_pile", "to": "hand",
            }],
            report["settlements"][0]["transient_delta"]["moved"],
        )
        self.assertEqual(
            {"added": [], "removed": [], "changed": []},
            report["settlements"][0]["persistent_delta"],
        )

    def test_combat_choice_hologram_source_settles_after_grid_selection(self):
        chosen = combat_choice_card("Deadly Poison", "chosen")
        kept = combat_choice_card("Defend_G", "kept")
        hologram = combat_choice_card("Hologram", "hologram")
        hologram["exhausts"] = True
        deck = [chosen, kept, hologram]
        before = combat_choice_state(
            20, "GRID", "BetterDiscardPileToHandAction",
            deck=deck, hand=[kept], draw=[], discard=[chosen],
            visible=[chosen], selected=[],
        )
        after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept, chosen], draw=[], discard=[],
            exhaust=[hologram],
        )

        report = combat_choice_audit([
            combat_choice_record(before, after, card=chosen)
        ])

        self.assertEqual("clear", report["transitions"][0]["status"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "hologram",
            report["settlements"][0]["resolving_source"][
                "card_instance_id"
            ],
        )

    def test_combat_choice_headbutt_moves_exact_card_to_draw_pile_top(self):
        chosen = combat_choice_card("Heavy Blade", "chosen")
        headbutt = combat_choice_card("Headbutt", "headbutt")
        headbutt["type"] = "ATTACK"
        kept = combat_choice_card("Strike_R", "kept")
        below = combat_choice_card("Bash", "below")
        before = combat_choice_state(
            10, "GRID", "DiscardPileToTopOfDeckAction",
            deck=[chosen, headbutt, kept, below], hand=[kept], draw=[below],
            discard=[chosen], visible=[chosen], selected=[],
        )
        after = combat_choice_state(
            11, "COMBAT_TURN_1", None,
            deck=[chosen, headbutt, kept, below], hand=[kept],
            draw=[below, chosen], discard=[headbutt],
        )

        report = combat_choice_audit([
            combat_choice_record(before, after, card=chosen)
        ])

        self.assertEqual("clear", report["transitions"][0]["status"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "discard_to_top_settlement_clear",
            report["settlements"][0]["reason"],
        )

        wrong_order = combat_choice_state(
            11, "COMBAT_TURN_1", None,
            deck=[chosen, headbutt, kept, below], hand=[kept],
            draw=[chosen, below], discard=[headbutt],
        )
        forged = combat_choice_audit([
            combat_choice_record(before, wrong_order, card=chosen)
        ])
        self.assertEqual("issues", forged["settlements"][0]["status"])
        self.assertEqual(
            "discard_to_top_not_on_draw_pile_top",
            forged["settlements"][0]["reason"],
        )

    def test_combat_choice_warcry_puts_exact_card_on_draw_pile_top(self):
        chosen = combat_choice_card("Flame Barrier", "chosen")
        kept = combat_choice_card("Strike_R", "kept")
        below = combat_choice_card("Bash", "below")
        warcry = combat_choice_card("Warcry", "warcry")
        warcry["type"] = "SKILL"
        warcry["exhausts"] = True
        before = combat_choice_state(
            20, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below, warcry],
            hand=[chosen, kept], draw=[below], discard=[],
            visible=[chosen, kept], selected=[], max_cards=1,
        )
        before["game_state"]["screen_state"]["can_pick_zero"] = False
        selected = combat_choice_state(
            21, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below], discard=[],
            visible=[kept], selected=[chosen], max_cards=1,
        )
        selected["game_state"]["screen_state"]["can_pick_zero"] = False
        after = combat_choice_state(
            22, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below, chosen], discard=[],
            exhaust=[warcry],
        )

        report = combat_choice_audit([
            combat_choice_record(before, selected, card=chosen),
            combat_choice_record(selected, after, command="proceed"),
        ])

        self.assertEqual("clear", report["transitions"][0]["status"])
        self.assertEqual("clear", report["transitions"][1]["status"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "put_on_deck_settlement_clear",
            report["settlements"][0]["reason"],
        )

        thinking_ahead = combat_choice_card(
            "Thinking Ahead", "thinking-ahead-source"
        )
        thinking_ahead["rarity"] = "RARE"
        thinking_ahead["type"] = "SKILL"
        thinking_ahead["exhausts"] = True
        generated_before = combat_choice_state(
            23, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below], hand=[chosen, kept], draw=[below],
            discard=[], visible=[chosen, kept], selected=[], max_cards=1,
        )
        generated_before["game_state"]["screen_state"][
            "can_pick_zero"
        ] = False
        generated_selected = combat_choice_state(
            24, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below], hand=[kept], draw=[below],
            discard=[], visible=[kept], selected=[chosen], max_cards=1,
        )
        generated_selected["game_state"]["screen_state"][
            "can_pick_zero"
        ] = False
        generated_after = combat_choice_state(
            25, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below], hand=[kept],
            draw=[below, chosen], discard=[], exhaust=[thinking_ahead],
        )
        generated_report = combat_choice_audit([
            combat_choice_record(
                generated_before, generated_selected, card=chosen
            ),
            combat_choice_record(
                generated_selected, generated_after, command="proceed"
            ),
        ])
        self.assertEqual(
            "clear", generated_report["settlements"][0]["status"],
            generated_report["settlements"],
        )
        self.assertEqual(
            "put_on_deck_settlement_clear",
            generated_report["settlements"][0]["reason"],
        )

        dazed = combat_choice_card("Dazed", "hex-dazed")
        dazed["type"] = "STATUS"
        dazed["rarity"] = "COMMON"
        dazed["cost"] = -2
        dazed["exhausts"] = False
        hex_powers = [{"id": "Hex", "amount": 1}]
        hex_before = combat_choice_state(
            30, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below, warcry],
            hand=[chosen, kept], draw=[below], discard=[],
            visible=[chosen, kept], selected=[], max_cards=1,
            powers=hex_powers,
        )
        hex_before["game_state"]["screen_state"]["can_pick_zero"] = False
        hex_selected = combat_choice_state(
            31, "HAND_SELECT", "PutOnDeckAction",
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below], discard=[],
            visible=[kept], selected=[chosen], max_cards=1,
            powers=hex_powers,
        )
        hex_selected["game_state"]["screen_state"]["can_pick_zero"] = False
        hex_after = combat_choice_state(
            32, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below, dazed, chosen], discard=[],
            exhaust=[warcry], powers=hex_powers,
        )
        hex_report = combat_choice_audit([
            combat_choice_record(hex_before, hex_selected, card=chosen),
            combat_choice_record(hex_selected, hex_after, command="proceed"),
        ])
        self.assertEqual(
            "clear", hex_report["settlements"][0]["status"],
            hex_report["settlements"],
        )
        self.assertEqual(
            ["hex-dazed"],
            hex_report["settlements"][0]["generated_hex_dazed"],
        )

        wrong_status = combat_choice_card("Slimed", "wrong-status")
        wrong_status["type"] = "STATUS"
        wrong_status["rarity"] = "SPECIAL"
        wrong_hex_after = combat_choice_state(
            32, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below, wrong_status, chosen], discard=[],
            exhaust=[warcry], powers=hex_powers,
        )
        wrong_hex_report = combat_choice_audit([
            combat_choice_record(hex_before, hex_selected, card=chosen),
            combat_choice_record(
                hex_selected, wrong_hex_after, command="proceed",
            ),
        ])
        self.assertEqual(
            "issues", wrong_hex_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "put_on_deck_unexpected_card_delta",
            wrong_hex_report["settlements"][0]["reason"],
        )

        missing_source_after = combat_choice_state(
            32, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[below, dazed, chosen], discard=[],
            exhaust=[], powers=hex_powers,
        )
        missing_source_report = combat_choice_audit([
            combat_choice_record(hex_before, hex_selected, card=chosen),
            combat_choice_record(
                hex_selected, missing_source_after, command="proceed",
            ),
        ])
        self.assertEqual(
            "issues", missing_source_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "put_on_deck_resolving_card_delta_mismatch",
            missing_source_report["settlements"][0]["reason"],
        )

        extra_dazed = copy.deepcopy(dazed)
        extra_dazed["card_instance_id"] = "extra-hex-dazed"
        extra_hex_after = combat_choice_state(
            32, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept],
            draw=[below, dazed, extra_dazed, chosen],
            discard=[], exhaust=[warcry], powers=hex_powers,
        )
        extra_hex_report = combat_choice_audit([
            combat_choice_record(hex_before, hex_selected, card=chosen),
            combat_choice_record(
                hex_selected, extra_hex_after, command="proceed",
            ),
        ])
        self.assertEqual(
            "issues", extra_hex_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "put_on_deck_hex_dazed_delta_mismatch",
            extra_hex_report["settlements"][0]["reason"],
        )

        wrong_order = combat_choice_state(
            22, "COMBAT_TURN_1", None,
            deck=[chosen, kept, below, warcry],
            hand=[kept], draw=[chosen, below], discard=[],
            exhaust=[warcry],
        )
        forged = combat_choice_audit([
            combat_choice_record(before, selected, card=chosen),
            combat_choice_record(selected, wrong_order, command="proceed"),
        ])
        self.assertEqual("issues", forged["settlements"][0]["status"])
        self.assertEqual(
            "put_on_deck_not_on_draw_pile_top",
            forged["settlements"][0]["reason"],
        )
    def test_combat_choice_duplicate_uuid_is_evidence_unknown(self):
        duplicate_a = combat_choice_card("Defend_R", "duplicate")
        duplicate_b = combat_choice_card("Strike_R", "duplicate")
        before = combat_choice_state(
            10, "GRID", "BetterDiscardPileToHandAction",
            deck=[duplicate_a], hand=[], draw=[],
            discard=[duplicate_a, duplicate_b], visible=[], selected=[],
        )
        after = combat_choice_state(
            11, "COMBAT_TURN_1", None,
            deck=[duplicate_a], hand=[], draw=[],
            discard=[duplicate_a, duplicate_b],
        )
        record = combat_choice_record(before, after, command="proceed")

        report = combat_choice_audit([record])

        self.assertEqual("inconclusive", report["transitions"][0]["status"])
        self.assertEqual("inconclusive", report["settlements"][0]["status"])
        self.assertEqual([], report["issues"])
        self.assertIn(
            "combat_choice_authoritative_evidence_missing",
            report["transitions"][0]["reason_codes"],
        )

    def test_combat_choice_discovery_binds_created_card_and_exhausted_source(self):
        discovery = combat_choice_card("Impervious", "preview")
        discovery["rarity"] = "RARE"
        discovery["cost"] = 2
        created = copy.deepcopy(discovery)
        created["card_instance_id"] = "created"
        created["cost"] = 0
        source = combat_choice_card("Discovery", "source")
        source["rarity"] = "UNCOMMON"
        source["cost"] = 1
        source["exhausts"] = True
        kept = combat_choice_card("Strike_R", "kept")
        before = combat_choice_state(
            20, "CARD_REWARD", "DiscoveryAction",
            deck=[kept], hand=[kept], draw=[], discard=[],
            visible=[discovery],
        )
        after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[created, kept], draw=[], discard=[],
            exhaust=[source],
        )
        record = combat_choice_record(before, after, card=discovery)

        clear = combat_choice_audit([record])

        self.assertEqual("clear", clear["transitions"][0]["status"])
        self.assertEqual("clear", clear["settlements"][0]["status"])
        self.assertEqual(
            ["created", "source"],
            clear["settlements"][0]["transient_delta"]["added"],
        )
        self.assertEqual([], clear["settlements"][0]["persistent_delta"]["added"])
        self.assertEqual(
            "created",
            clear["settlements"][0]["created_card_instance_id"],
        )
        self.assertEqual(
            "source",
            clear["settlements"][0]["resolving_card_instance_id"],
        )

        forged_created = copy.deepcopy(created)
        forged_created["id"] = "Demon Form"
        forged_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[forged_created, kept],
            draw=[], discard=[], exhaust=[source],
        )
        forged_identity = combat_choice_audit([
            combat_choice_record(before, forged_after, card=discovery)
        ])
        self.assertEqual("issues", forged_identity["settlements"][0]["status"])
        self.assertEqual(
            "discovery_card_facts_changed",
            forged_identity["settlements"][0]["reason"],
        )

        paid_created = copy.deepcopy(created)
        paid_created["cost"] = 1
        paid_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[paid_created, kept],
            draw=[], discard=[], exhaust=[source],
        )
        paid_report = combat_choice_audit([
            combat_choice_record(before, paid_after, card=discovery)
        ])
        self.assertEqual("issues", paid_report["settlements"][0]["status"])
        self.assertEqual(
            "discovery_card_not_temporary_zero_cost",
            paid_report["settlements"][0]["reason"],
        )

        missing_source_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[created, kept], draw=[], discard=[],
        )
        missing_source_report = combat_choice_audit([
            combat_choice_record(before, missing_source_after, card=discovery)
        ])
        self.assertEqual(
            "issues", missing_source_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "discovery_transient_gain_identity_mismatch",
            missing_source_report["settlements"][0]["reason"],
        )

        extra = combat_choice_card("Dazed", "extra")
        extra_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[created, kept], draw=[extra], discard=[],
            exhaust=[source],
        )
        extra_report = combat_choice_audit([
            combat_choice_record(before, extra_after, card=discovery)
        ])
        self.assertEqual("issues", extra_report["settlements"][0]["status"])
        self.assertEqual(
            "discovery_transient_gain_identity_mismatch",
            extra_report["settlements"][0]["reason"],
        )

        forged_source = copy.deepcopy(source)
        forged_source["exhausts"] = False
        forged_source_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[created, kept], draw=[], discard=[],
            exhaust=[forged_source],
        )
        forged_source_report = combat_choice_audit([
            combat_choice_record(
                before, forged_source_after, card=discovery,
            )
        ])
        self.assertEqual(
            "issues", forged_source_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "discovery_resolving_card_unbound",
            forged_source_report["settlements"][0]["reason"],
        )

        forged_after = copy.deepcopy(after)
        forged_after["game_state"]["deck"].append(copy.deepcopy(discovery))
        forged = combat_choice_record(before, forged_after, card=discovery)
        forged_report = combat_choice_audit([forged])
        self.assertEqual("issues", forged_report["settlements"][0]["status"])
        self.assertEqual(
            "combat_choice_master_deck_changed",
            forged_report["settlements"][0]["reason"],
        )

    def test_combat_choice_discovery_binds_consumed_card_choice_potion(self):
        preview = combat_choice_card("Shockwave", "preview")
        preview["rarity"] = "UNCOMMON"
        preview["cost"] = 2
        preview["exhausts"] = True
        created = copy.deepcopy(preview)
        created["card_instance_id"] = "created"
        created["cost"] = 0
        kept = combat_choice_card("Strike_R", "kept")
        empty_slot = {
            "id": "Potion Slot", "potion_instance_id": "potion:empty",
            "slot": 0, "can_use": False, "can_discard": False,
        }
        before = combat_choice_state(
            20, "CARD_REWARD", "DiscoveryAction",
            deck=[kept], hand=[kept], draw=[], discard=[],
            visible=[preview],
        )
        before["game_state"]["potions"] = [copy.deepcopy(empty_slot)]
        after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[created, kept], draw=[], discard=[],
        )
        after["game_state"]["potions"] = [copy.deepcopy(empty_slot)]

        potion_before = copy.deepcopy(before)
        potion_before["state_seq"] = 19
        potion_before["phase"] = "COMBAT_TURN_1"
        potion_before["game_state"]["screen_type"] = "NONE"
        potion_before["game_state"]["screen_state"] = {}
        potion_before["game_state"]["current_action"] = None
        source = {
            "id": "SkillPotion", "potion_instance_id": "potion:source",
            "slot": 0, "can_use": True, "can_discard": True,
        }
        potion_before["game_state"]["potions"] = [source]
        potion_record = bound(
            "decision", before_seq=19, after_seq=20,
            phase="COMBAT_TURN_1", action="potion",
            requested_target_id="potion:source",
            resolved_target_id="potion:source",
            authoritative_state_before=potion_before,
            authoritative_state_after=before,
        )
        choice_record = combat_choice_record(before, after, card=preview)

        report = combat_choice_audit([potion_record, choice_record])

        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "discovery_settlement_clear",
            report["settlements"][0]["reason"],
        )
        self.assertEqual(
            ["created"], report["settlements"][0]["transient_delta"]["added"],
        )
        self.assertIsNone(
            report["settlements"][0]["resolving_card_instance_id"],
        )
        self.assertEqual(
            "SkillPotion", report["settlements"][0]["origin"]["potion_id"],
        )

        cancelled_after = combat_choice_state(
            21, "COMBAT_TURN_1", None,
            deck=[kept], hand=[kept], draw=[], discard=[],
        )
        cancelled_after["game_state"]["potions"] = [
            copy.deepcopy(empty_slot)
        ]
        cancel_record = combat_choice_record(
            before, cancelled_after, command="return"
        )
        cancel_record.update({
            "requested_target_id": "action:return",
            "resolved_target_id": "action:return",
            "selected_choice_ids": ["action:return"],
            "final_choice_ids": ["action:return"],
            "legal_choices_before": [{
                "choice_id": "action:return",
                "action": "return",
                "legal": True,
                "visible": True,
            }],
        })

        cancelled = combat_choice_audit([potion_record, cancel_record])

        self.assertEqual("clear", cancelled["transitions"][0]["status"])
        self.assertEqual("clear", cancelled["settlements"][0]["status"])
        self.assertEqual(
            "discovery_potion_cancel_settlement_clear",
            cancelled["settlements"][0]["reason"],
        )

        forged_potion_before = copy.deepcopy(potion_before)
        forged_potion_before["game_state"]["potions"][0]["id"] = "SpeedPotion"
        forged_potion_record = copy.deepcopy(potion_record)
        forged_potion_record["authoritative_state_before"] = forged_potion_before
        forged = combat_choice_audit([forged_potion_record, choice_record])
        self.assertEqual("issues", forged["settlements"][0]["status"])
        self.assertIn(
            "discovery_potion_origin_type_mismatch",
            forged["settlements"][0]["reason_codes"],
        )

    def test_combat_choice_skill_from_deck_to_hand_binds_source_and_generated_secret_technique(self):
        chosen = combat_choice_card("Defend_R", "chosen")
        kept = combat_choice_card("Strike_R", "kept")
        source = combat_choice_card("Secret Technique", "source")
        before = combat_choice_state(
            30, "GRID", "SkillFromDeckToHandAction",
            deck=[chosen, kept], hand=[kept], draw=[chosen], discard=[],
            visible=[chosen], selected=[],
        )
        after = combat_choice_state(
            31, "COMBAT_TURN_1", None,
            deck=[chosen, kept], hand=[kept, chosen], draw=[], discard=[],
            exhaust=[source],
        )

        report = combat_choice_audit([
            combat_choice_record(before, after, card=chosen)
        ])

        self.assertEqual("clear", report["transitions"][0]["status"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "skill_from_deck_to_hand_settlement_clear",
            report["settlements"][0]["reason"],
        )

    def test_combat_choice_gambling_multiselect_confirm_without_shuffle(self):
        a = combat_choice_card("Strike_R", "a")
        b = combat_choice_card("Defend_R", "b")
        kept = combat_choice_card("Neutralize", "kept")
        x = combat_choice_card("Backflip", "x")
        y = combat_choice_card("Dagger Spray", "y")
        z = combat_choice_card("Prepared", "z")
        old_discard = combat_choice_card("Survivor", "old-discard")
        deck = [a, b, kept, x, y, z, old_discard]
        state0 = combat_choice_state(
            30, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[a, b, kept], draw=[x, y, z],
            discard=[old_discard], visible=[a, b, kept], selected=[],
        )
        state1 = combat_choice_state(
            31, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[b, kept], draw=[x, y, z],
            discard=[old_discard], visible=[b, kept], selected=[a],
            max_cards=3,
        )
        state2 = combat_choice_state(
            32, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[kept], draw=[x, y, z],
            discard=[old_discard], visible=[kept], selected=[a, b],
            max_cards=3,
        )
        state3 = combat_choice_state(
            33, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept, x, y], draw=[z],
            discard=[old_discard, b, a],
        )
        proceed = combat_choice_record(state2, state3, command="proceed")
        # The confirm command is the selected protocol action.  It must not
        # be mistaken for an already-selected card in the Gambling Chip chain.
        proceed["selected_choice_ids"] = ["action:proceed"]
        proceed["final_choice_ids"] = ["action:proceed"]
        records = [
            combat_choice_record(state0, state1, card=a),
            combat_choice_record(state1, state2, card=b),
            proceed,
        ]

        report = combat_choice_audit(records)

        self.assertEqual(3, len(report["transitions"]))
        self.assertTrue(all(
            row["status"] == "clear" for row in report["transitions"]
        ), report["transitions"])
        self.assertEqual(1, len(report["settlements"]))
        settlement = report["settlements"][0]
        self.assertEqual("clear", settlement["status"])
        self.assertEqual("draw_pile_sufficient", settlement["redraw_branch"])
        self.assertEqual(["a", "b"], settlement["selected_card_instance_ids"])

        malformed = copy.deepcopy(proceed)
        malformed["selected_choice_ids"] = ["card:a"]
        malformed["final_choice_ids"] = ["card:a"]
        malformed_report = combat_choice_audit([
            combat_choice_record(state0, state1, card=a),
            combat_choice_record(state1, state2, card=b),
            malformed,
        ])
        self.assertEqual("issues", malformed_report["transitions"][-1]["status"])
        self.assertIn(
            "proceed_has_selected_choice",
            malformed_report["transitions"][-1]["reason_codes"],
        )

    def test_combat_choice_gambling_reshuffle_and_zero_confirm(self):
        a = combat_choice_card("Strike_R", "a")
        b = combat_choice_card("Defend_R", "b")
        kept = combat_choice_card("Neutralize", "kept")
        x = combat_choice_card("Backflip", "x")
        q = combat_choice_card("Survivor", "q")
        r = combat_choice_card("Prepared", "r")
        deck = [a, b, kept, x, q, r]
        state0 = combat_choice_state(
            40, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[a, b, kept], draw=[x], discard=[q, r],
            visible=[a, b, kept], selected=[],
        )
        state1 = combat_choice_state(
            41, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[a, b, kept], draw=[x], discard=[q, r],
            visible=[b, kept], selected=[a],
        )
        state2 = combat_choice_state(
            42, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[a, b, kept], draw=[x], discard=[q, r],
            visible=[kept], selected=[a, b],
        )
        state3 = combat_choice_state(
            43, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept, x, q], draw=[r, a, b], discard=[],
        )
        report = combat_choice_audit([
            combat_choice_record(state0, state1, card=a),
            combat_choice_record(state1, state2, card=b),
            combat_choice_record(state2, state3, command="proceed"),
        ])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual("discard_reshuffle", report["settlements"][0]["redraw_branch"])

        zero_before = combat_choice_state(
            50, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[kept], draw=[x], discard=[q],
            visible=[kept], selected=[],
        )
        zero_after = combat_choice_state(
            51, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[x], discard=[q],
        )
        zero = combat_choice_audit([
            combat_choice_record(
                zero_before, zero_after, command="proceed"
            )
        ])
        self.assertEqual("clear", zero["settlements"][0]["status"])
        self.assertEqual(
            "gambling_chip_zero_confirm_clear",
            zero["settlements"][0]["reason"],
        )

        no_draw = [{"id": "No Draw", "amount": -1}]
        blocked0 = combat_choice_state(
            60, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[a, b, kept], draw=[], discard=[q, r],
            visible=[a, b, kept], selected=[], powers=no_draw,
            max_cards=99,
        )
        blocked1 = combat_choice_state(
            61, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[b, kept], draw=[], discard=[q, r],
            visible=[b, kept], selected=[a], powers=no_draw,
            max_cards=99,
        )
        blocked2 = combat_choice_state(
            62, "HAND_SELECT", "GamblingChipAction",
            deck=deck, hand=[kept], draw=[], discard=[q, r],
            visible=[kept], selected=[a, b], powers=no_draw,
            max_cards=99,
        )
        blocked3 = combat_choice_state(
            63, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[], discard=[q, r, a, b],
            powers=no_draw,
        )
        blocked = combat_choice_audit([
            combat_choice_record(blocked0, blocked1, card=a),
            combat_choice_record(blocked1, blocked2, card=b),
            combat_choice_record(blocked2, blocked3, command="proceed"),
        ])
        self.assertEqual(
            "clear", blocked["settlements"][0]["status"], blocked
        )
        self.assertEqual(
            "gambling_chip_no_draw_discard_only_clear",
            blocked["settlements"][0]["reason"],
        )
        self.assertEqual(
            "blocked_by_no_draw",
            blocked["settlements"][0]["redraw_branch"],
        )

    def test_combat_choice_discard_action_binds_selected_card_and_source(self):
        selected = combat_choice_card("Defend_G", "selected")
        kept = combat_choice_card("Strike_G", "kept")
        source = combat_choice_card("Survivor", "source")
        deck = [selected, kept, source]
        state0 = combat_choice_state(
            52, "HAND_SELECT", "DiscardAction",
            deck=deck, hand=[selected, kept], draw=[], discard=[],
            visible=[selected, kept], selected=[], max_cards=1,
        )
        state1 = combat_choice_state(
            53, "HAND_SELECT", "DiscardAction",
            deck=deck, hand=[kept], draw=[], discard=[],
            visible=[kept], selected=[selected], max_cards=1,
        )
        state2 = combat_choice_state(
            54, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[],
            discard=[selected, source],
        )

        report = combat_choice_audit([
            combat_choice_record(state0, state1, card=selected),
            combat_choice_record(state1, state2, command="proceed"),
        ])

        self.assertTrue(all(
            row["status"] == "clear" for row in report["transitions"]
        ), report["transitions"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "discard_selected_card_settlement_clear",
            report["settlements"][0]["reason"],
        )

        wrong_source = combat_choice_card("Prepared", "generated-source")
        wrong_after = combat_choice_state(
            54, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[],
            discard=[selected, wrong_source],
        )
        wrong = combat_choice_audit([
            combat_choice_record(state0, state1, card=selected),
            combat_choice_record(state1, wrong_after, command="proceed"),
        ])
        self.assertEqual("issues", wrong["settlements"][0]["status"])
        self.assertEqual(
            "discard_action_unbound_source_card",
            wrong["settlements"][0]["reason"],
        )

    def test_combat_choice_exhaust_action_binds_selected_card_and_source(self):
        selected = combat_choice_card("Defend_R", "selected")
        kept = combat_choice_card("Strike_R", "kept")
        source = combat_choice_card("True Grit", "source")
        deck = [selected, kept, source]
        state0 = combat_choice_state(
            55, "HAND_SELECT", "ExhaustAction",
            deck=deck, hand=[selected, kept], draw=[], discard=[],
            visible=[selected, kept], selected=[], max_cards=1,
        )
        state1 = combat_choice_state(
            56, "HAND_SELECT", "ExhaustAction",
            deck=deck, hand=[kept], draw=[], discard=[],
            visible=[kept], selected=[selected], max_cards=1,
        )
        state2 = combat_choice_state(
            57, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[], discard=[source],
            exhaust=[selected],
        )

        report = combat_choice_audit([
            combat_choice_record(state0, state1, card=selected),
            combat_choice_record(state1, state2, command="proceed"),
        ])

        self.assertTrue(all(
            row["status"] == "clear" for row in report["transitions"]
        ), report["transitions"])
        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            "exhaust_selected_card_settlement_clear",
            report["settlements"][0]["reason"],
        )

        # Burning Pact continues resolving after the same selection overlay:
        # the exact selected card is exhausted, the source returns to discard,
        # and its deck-bound magic number authorizes exactly two draws.
        draw_a = combat_choice_card("Shrug It Off", "draw-a")
        draw_b = combat_choice_card("Pommel Strike", "draw-b")
        pact = combat_choice_card("Burning Pact", "pact")
        pact["magic_number"] = 2
        pact_deck = [selected, kept, pact, draw_a, draw_b]
        pact_before = combat_choice_state(
            58, "HAND_SELECT", "ExhaustAction",
            deck=pact_deck, hand=[selected, kept], draw=[draw_a, draw_b],
            discard=[], visible=[selected, kept], selected=[], max_cards=1,
        )
        pact_selected = combat_choice_state(
            59, "HAND_SELECT", "ExhaustAction",
            deck=pact_deck, hand=[kept], draw=[draw_a, draw_b],
            discard=[], visible=[kept], selected=[selected], max_cards=1,
        )
        pact_after = combat_choice_state(
            60, "COMBAT_TURN_1", None,
            deck=pact_deck, hand=[kept, draw_a, draw_b], draw=[],
            discard=[pact], exhaust=[selected],
        )
        pact_report = combat_choice_audit([
            combat_choice_record(pact_before, pact_selected, card=selected),
            combat_choice_record(pact_selected, pact_after, command="proceed"),
        ])
        self.assertEqual("clear", pact_report["settlements"][0]["status"])

        # A temporary Armaments upgrade changes only the combat copy.  Its
        # runtime magic number, not the unchanged master deck template,
        # authorizes Burning Pact+'s third draw.
        draw_c = combat_choice_card("Defend_R", "draw-c")
        pact_runtime_plus = copy.deepcopy(pact)
        pact_runtime_plus["upgrades"] = 1
        pact_runtime_plus["magic_number"] = 3
        temp_deck = [selected, kept, pact, draw_a, draw_b, draw_c]
        temp_before = combat_choice_state(
            601, "HAND_SELECT", "ExhaustAction",
            deck=temp_deck, hand=[selected, kept],
            draw=[draw_a, draw_b, draw_c], discard=[],
            visible=[selected, kept], selected=[], max_cards=1,
        )
        temp_selected = combat_choice_state(
            602, "HAND_SELECT", "ExhaustAction",
            deck=temp_deck, hand=[kept], draw=[draw_a, draw_b, draw_c],
            discard=[], visible=[kept], selected=[selected], max_cards=1,
        )
        temp_after = combat_choice_state(
            603, "COMBAT_TURN_1", None,
            deck=temp_deck, hand=[kept, draw_a, draw_b, draw_c], draw=[],
            discard=[pact_runtime_plus], exhaust=[selected],
        )
        temp_report = combat_choice_audit([
            combat_choice_record(temp_before, temp_selected, card=selected),
            combat_choice_record(temp_selected, temp_after, command="proceed"),
        ])
        self.assertEqual(
            "clear", temp_report["settlements"][0]["status"],
            temp_report["settlements"],
        )

        # Chosen's Hex creates one new Dazed when the resolving Skill was
        # played.  That generated card is independently authorized only when
        # both the Hex power and exact Burning Pact source are visible.
        dazed = combat_choice_card("Dazed", "hex-dazed")
        dazed["rarity"] = "SPECIAL"
        hex_powers = [{"id": "Hex", "amount": 1}]
        hex_before = combat_choice_state(
            604, "HAND_SELECT", "ExhaustAction",
            deck=pact_deck, hand=[selected, kept], draw=[draw_a, draw_b],
            discard=[], visible=[selected, kept], selected=[], max_cards=1,
            powers=hex_powers,
        )
        hex_selected = combat_choice_state(
            605, "HAND_SELECT", "ExhaustAction",
            deck=pact_deck, hand=[kept], draw=[draw_a, draw_b], discard=[],
            visible=[kept], selected=[selected], max_cards=1,
            powers=hex_powers,
        )
        hex_after = combat_choice_state(
            606, "COMBAT_TURN_1", None,
            deck=pact_deck, hand=[kept, draw_a, draw_b], draw=[dazed],
            discard=[pact], exhaust=[selected], powers=hex_powers,
        )
        hex_report = combat_choice_audit([
            combat_choice_record(hex_before, hex_selected, card=selected),
            combat_choice_record(hex_selected, hex_after, command="proceed"),
        ])
        self.assertEqual(
            "clear", hex_report["settlements"][0]["status"],
            hex_report["settlements"],
        )

        no_hex_before = copy.deepcopy(hex_before)
        no_hex_before["game_state"]["combat_state"]["player"]["powers"] = []
        no_hex_selected = copy.deepcopy(hex_selected)
        no_hex_selected["game_state"]["combat_state"]["player"]["powers"] = []
        no_hex_report = combat_choice_audit([
            combat_choice_record(
                no_hex_before, no_hex_selected, card=selected,
            ),
            combat_choice_record(no_hex_selected, hex_after, command="proceed"),
        ])
        self.assertEqual(
            "issues", no_hex_report["settlements"][0]["status"],
        )

        # Burning Pact+ draws three. If the draw pile has only one card, the
        # complete discard pile is shuffled into draw before the remaining
        # two draws; the source itself settles to the new discard afterward.
        discard_a = combat_choice_card("Impervious", "discard-a")
        discard_b = combat_choice_card("Shrug It Off", "discard-b")
        discard_c = combat_choice_card("Defend_R", "discard-c")
        pact_plus = combat_choice_card("Burning Pact", "pact-plus")
        pact_plus["magic_number"] = 3
        reshuffle_deck = [
            selected, kept, pact_plus, draw_a,
            discard_a, discard_b, discard_c,
        ]
        reshuffle_before = combat_choice_state(
            61, "HAND_SELECT", "ExhaustAction",
            deck=reshuffle_deck, hand=[selected, kept], draw=[draw_a],
            discard=[discard_a, discard_b, discard_c],
            visible=[selected, kept], selected=[], max_cards=1,
        )
        reshuffle_selected = combat_choice_state(
            62, "HAND_SELECT", "ExhaustAction",
            deck=reshuffle_deck, hand=[kept], draw=[draw_a],
            discard=[discard_a, discard_b, discard_c],
            visible=[kept], selected=[selected], max_cards=1,
        )
        reshuffle_after = combat_choice_state(
            63, "COMBAT_TURN_1", None,
            deck=reshuffle_deck,
            hand=[kept, draw_a, discard_a, discard_b],
            draw=[discard_c], discard=[pact_plus], exhaust=[selected],
        )
        reshuffle_report = combat_choice_audit([
            combat_choice_record(
                reshuffle_before, reshuffle_selected, card=selected,
            ),
            combat_choice_record(
                reshuffle_selected, reshuffle_after, command="proceed",
            ),
        ])
        self.assertEqual(
            "clear", reshuffle_report["settlements"][0]["status"],
            reshuffle_report["settlements"],
        )
        self.assertEqual(
            "discard_reshuffle",
            reshuffle_report["settlements"][0]["draw_branch"],
        )

        wrong_reshuffle_after = combat_choice_state(
            63, "COMBAT_TURN_1", None,
            deck=reshuffle_deck,
            hand=[kept, draw_a, discard_a, discard_b],
            draw=[], discard=[pact_plus, discard_c], exhaust=[selected],
        )
        wrong_reshuffle_report = combat_choice_audit([
            combat_choice_record(
                reshuffle_before, reshuffle_selected, card=selected,
            ),
            combat_choice_record(
                reshuffle_selected, wrong_reshuffle_after,
                command="proceed",
            ),
        ])
        self.assertEqual(
            "issues", wrong_reshuffle_report["settlements"][0]["status"],
        )
        self.assertEqual(
            "exhaust_action_unexpected_pile_movement",
            wrong_reshuffle_report["settlements"][0]["reason"],
        )

        wrong_after = combat_choice_state(
            60, "COMBAT_TURN_1", None,
            deck=pact_deck, hand=[draw_a, draw_b], draw=[],
            discard=[pact, selected], exhaust=[kept],
        )
        wrong_report = combat_choice_audit([
            combat_choice_record(pact_before, pact_selected, card=selected),
            combat_choice_record(
                pact_selected, wrong_after, command="proceed"
            ),
        ])
        self.assertEqual("issues", wrong_report["settlements"][0]["status"])
        self.assertEqual(
            "exhaust_selected_card_destination_mismatch",
            wrong_report["settlements"][0]["reason"],
        )

    def test_combat_choice_exhaust_action_allows_bound_multi_select(self):
        selected = [
            combat_choice_card("Wound", "wound-a"),
            combat_choice_card("Wound", "wound-b"),
            combat_choice_card("Defend_R", "defend-a"),
            combat_choice_card("Defend_R", "defend-b"),
        ]
        kept = combat_choice_card("Fiend Fire", "kept")
        deck = [*selected, kept]
        states = [combat_choice_state(
            70, "HAND_SELECT", "ExhaustAction",
            deck=deck, hand=[*selected, kept], draw=[], discard=[],
            visible=[*selected, kept], selected=[], max_cards=99,
        )]
        remaining = [*selected, kept]
        chosen = []
        for offset, card in enumerate(selected, 1):
            remaining = [item for item in remaining if item is not card]
            chosen.append(card)
            states.append(combat_choice_state(
                70 + offset, "HAND_SELECT", "ExhaustAction",
                deck=deck, hand=remaining, draw=[], discard=[],
                visible=remaining, selected=chosen, max_cards=99,
            ))
        states.append(combat_choice_state(
            75, "COMBAT_TURN_1", None,
            deck=deck, hand=[kept], draw=[], discard=[], exhaust=selected,
        ))
        records = [
            combat_choice_record(states[index], states[index + 1], card=card)
            for index, card in enumerate(selected)
        ]
        records.append(combat_choice_record(
            states[-2], states[-1], command="proceed",
        ))

        report = combat_choice_audit(records)

        self.assertEqual("clear", report["settlements"][0]["status"])
        self.assertEqual(
            sorted(card["card_instance_id"] for card in selected),
            report["settlements"][0]["selected_card_instance_ids"],
        )

    def test_combat_choice_no_option_proceed_pending_and_unknown_action(self):
        deck = [combat_choice_card("Strike_R", "deck-a")]
        empty = combat_choice_state(
            60, "GRID", "BetterDiscardPileToHandAction",
            deck=deck, hand=[], draw=[], discard=[], visible=[], selected=[],
        )
        closed = combat_choice_state(
            61, "COMBAT_TURN_1", None,
            deck=deck, hand=[], draw=[], discard=[],
        )
        no_option = combat_choice_audit([
            combat_choice_record(empty, closed, command="proceed")
        ])
        self.assertEqual("clear", no_option["transitions"][0]["status"])
        self.assertEqual("clear", no_option["settlements"][0]["status"])

        target = combat_choice_card("HologramTarget", "target")
        pending_after = combat_choice_state(
            61, "GRID", "BetterDiscardPileToHandAction",
            deck=[target], hand=[], draw=[], discard=[target],
            visible=[], selected=[target],
        )
        pending_before = combat_choice_state(
            60, "GRID", "BetterDiscardPileToHandAction",
            deck=[target], hand=[], draw=[], discard=[target],
            visible=[target], selected=[],
        )
        pending = combat_choice_audit([
            combat_choice_record(pending_before, pending_after, card=target)
        ])
        self.assertEqual("clear", pending["transitions"][0]["status"])
        self.assertEqual("inconclusive", pending["settlements"][0]["status"])
        self.assertEqual(
            "combat_choice_settlement_pending",
            pending["settlements"][0]["reason"],
        )

        unknown_before = combat_choice_state(
            70, "GRID", "FutureUnknownAction",
            deck=deck, hand=[], draw=[], discard=[], visible=[], selected=[],
        )
        unknown_after = combat_choice_state(
            71, "COMBAT_TURN_1", None,
            deck=deck, hand=[], draw=[], discard=[],
        )
        unknown = combat_choice_audit([
            combat_choice_record(
                unknown_before, unknown_after, command="proceed"
            )
        ])
        self.assertEqual("inconclusive", unknown["transitions"][0]["status"])
        self.assertEqual("inconclusive", unknown["settlements"][0]["status"])
        self.assertIn(
            "combat_choice_action_unclassified",
            unknown["transitions"][0]["reason_codes"],
        )

    def test_combat_choice_forged_claim_raw_target_and_effect_are_disagreements(self):
        chosen = combat_choice_card("HologramTarget", "chosen")
        other = combat_choice_card("Defend_R", "other")
        before = combat_choice_state(
            80, "GRID", "BetterDiscardPileToHandAction",
            deck=[chosen, other], hand=[other], draw=[], discard=[chosen],
            visible=[chosen], selected=[],
        )
        after = combat_choice_state(
            81, "COMBAT_TURN_1", None,
            deck=[chosen, other], hand=[other, chosen], draw=[], discard=[],
        )
        forged_claim = combat_choice_record(before, after, card=chosen)
        forged_claim["decision_outcome"]["combat_choice_transition"][
            "before"
        ]["current_action"] = "ForgedAction"
        claim_report = combat_choice_audit([forged_claim])
        self.assertEqual("issues", claim_report["transitions"][0]["status"])
        self.assertEqual(
            "combat_choice_transition_mismatch",
            claim_report["issues"][0]["kind"],
        )

        raw_wrong = combat_choice_record(before, after, card=chosen)
        raw_wrong["available_options_before"][0]["target"][
            "card_instance_id"
        ] = "other"
        raw_wrong["available_options_before"][0]["target"]["card"] = (
            copy.deepcopy(other)
        )
        raw_report = combat_choice_audit([raw_wrong])
        self.assertEqual("issues", raw_report["transitions"][0]["status"])
        self.assertIn(
            "target_uuid_not_visible",
            raw_report["transitions"][0]["reason_codes"],
        )

        effect_after = combat_choice_state(
            81, "COMBAT_TURN_1", None,
            deck=[chosen, other], hand=[other], draw=[], discard=[chosen],
        )
        effect_report = combat_choice_audit([
            combat_choice_record(before, effect_after, card=chosen)
        ])
        self.assertEqual("issues", effect_report["settlements"][0]["status"])
        self.assertEqual(
            "discard_to_hand_destination_mismatch",
            effect_report["settlements"][0]["reason"],
        )

        self.assertIn(
            "combat_choice_transition_mismatch",
            independent_oracle.DISAGREEMENT_KINDS,
        )
        self.assertIn(
            "combat_choice_settlement_mismatch",
            independent_oracle.DISAGREEMENT_KINDS,
        )

    def test_combat_choice_claim_missing_is_eligible_unknown_and_coverage_is_exposed(self):
        deck = [combat_choice_card("Strike_R", "deck-a")]
        before = combat_choice_state(
            90, "GRID", "BetterDiscardPileToHandAction",
            deck=deck, hand=[], draw=[], discard=[], visible=[], selected=[],
        )
        after = combat_choice_state(
            91, "COMBAT_TURN_1", None,
            deck=deck, hand=[], draw=[], discard=[],
        )
        missing = combat_choice_record(before, after, command="proceed")
        missing["decision_outcome"].pop("combat_choice_transition")
        direct = combat_choice_audit([missing])
        self.assertEqual("inconclusive", direct["transitions"][0]["status"])
        self.assertEqual(
            "combat_choice_transition_claim_missing",
            direct["unknowns"][0]["kind"],
        )

        report = self.audit([controller_start(), missing, terminal()])
        self.assertEqual(
            1, report["coverage"]["combat_choice_transitions"]["eligible"]
        )
        self.assertEqual(
            1, report["coverage"]["combat_choice_transitions"]["unknown"]
        )
        self.assertEqual(
            1, report["coverage"]["combat_choice_settlements"]["evaluated"]
        )
        self.assertIn(
            "combat_choice_transitions",
            report["required_coverage_keys"],
        )
        self.assertIn(
            "combat_choice_settlements",
            report["required_coverage_keys"],
        )
    def test_neow_parent_context_independently_recovers_grid_operation(self):
        contract = typed_neow_option(
            0, "REMOVE_TWO", "NONE", max_hp=80
        )["target"]["neow_contract"]
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "NEOW",
            "source_option_id": "option:remove-two",
            "source_choice_index": 0,
            "mechanism_id": independent_oracle._oracle_neow_mechanism_id(
                contract
            ),
            "neow_contract": contract,
            "operation": "remove",
            "select_count": 2,
        }
        card = {
            "id": "Defend_R", "name": "Defend", "upgrades": 0,
            "card_instance_id": "card:defend",
        }
        record = {
            "authoritative_state_before": {"game_state": {
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [card],
                    "parent_choice_context": parent,
                },
            }},
        }
        target = {
            "kind": "card", "card_instance_id": "card:defend",
            "card": card,
        }

        operation, selected, error = independent_oracle._oracle_grid_target(
            record, target
        )

        self.assertIsNone(error)
        self.assertEqual("grid_remove", operation)
        self.assertEqual("card:defend", selected["card_instance_id"])

    def test_duplicator_parent_context_independently_recovers_grid_operation(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "Duplicator",
            "event_class": (
                "com.megacrit.cardcrawl.events.shrines.Duplicator"
            ),
            "event_stage": "MAIN",
            "original_button_index": 0,
            "option_kind": "DUPLICATE",
            "instance_parameters": {"screen_num": 0},
            "parameters": {
                "duplicate_select_count": 1,
                "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
            },
        }
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "EVENT",
            "source_option_id": "option:duplicate",
            "source_choice_index": 0,
            "mechanism_id": (
                independent_oracle._oracle_event_mechanism_id(contract)
            ),
            "event_contract": contract,
            "operation": "duplicate",
            "select_count": 1,
        }
        card = {
            "id": "Shrug It Off", "name": "Shrug It Off", "upgrades": 0,
            "card_instance_id": "card:shrug",
        }
        record = {
            "authoritative_state_before": {"game_state": {
                "ascension_level": 0, "relics": [],
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [card],
                    "parent_choice_context": parent,
                },
            }},
        }
        target = {
            "kind": "card", "card_instance_id": "card:shrug",
            "card": card,
        }

        operation, selected, error = independent_oracle._oracle_grid_target(
            record, target
        )

        self.assertIsNone(error)
        self.assertEqual("grid_duplicate", operation)
        self.assertEqual("card:shrug", selected["card_instance_id"])

    def test_library_parent_context_independently_recovers_grid_gain(self):
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "EVENT",
            "source_option_id": "option:read",
            "source_choice_index": 0,
            "event_id": "The Library",
            "event_class": (
                "com.megacrit.cardcrawl.events.city.TheLibrary"
            ),
            "original_button_index": 0,
            "operation": "gain",
            "select_count": 1,
            "selection_domain": "library_card_offering",
        }
        mechanism = {
            key: copy.deepcopy(parent[key])
            for key in (
                "event_id", "event_class", "original_button_index",
                "operation", "select_count", "selection_domain",
            )
        }
        parent["mechanism_id"] = bridge.stable_id(
            "event-grid-mechanism", mechanism
        )
        card = {
            "id": "Flame Barrier", "name": "Flame Barrier",
            "type": "SKILL", "upgrades": 0,
            "card_instance_id": "library:flame-barrier",
        }
        record = {
            "phase": "GRID",
            "authoritative_state_before": {"game_state": {
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [copy.deepcopy(card)],
                    "parent_choice_context": parent,
                },
            }},
        }
        target = {
            "kind": "card",
            "card_instance_id": card["card_instance_id"],
            "card": copy.deepcopy(card),
        }

        operation, selected, error = independent_oracle._oracle_grid_target(
            record, target
        )

        self.assertIsNone(error)
        self.assertEqual("grid_gain", operation)
        self.assertEqual(card["card_instance_id"], selected["card_instance_id"])
        self.assertEqual(
            "library_card_offering",
            independent_oracle._oracle_canonical_audit_value(parent)[
                "selection_domain"
            ],
        )

    def test_bottled_parent_context_independently_recovers_grid_operation(self):
        cases = (
            ("Bottled Flame", "bottle_attack", "ATTACK"),
            ("Bottled Lightning", "bottle_skill", "SKILL"),
            ("Bottled Tornado", "bottle_power", "POWER"),
        )
        for relic_id, operation, card_type in cases:
            with self.subTest(relic_id=relic_id):
                card = {
                    "id": f"card:{card_type}", "name": card_type,
                    "type": card_type, "upgrades": 0,
                    "card_instance_id": f"uuid:{card_type}",
                }
                record = {
                    "phase": "GRID",
                    "authoritative_state_before": {"game_state": {
                        "screen_state": {
                            "for_upgrade": False,
                            "for_transform": False,
                            "for_purge": False,
                            "cards": [copy.deepcopy(card)],
                            "parent_choice_context": {
                                "authority": "accepted_protocol_choice",
                                "parent_phase": "COMBAT_REWARD",
                                "relic_id": relic_id,
                                "operation": operation,
                                "card_type": card_type,
                                "select_count": 1,
                            },
                        },
                    }},
                }
                target = {
                    "kind": "card",
                    "card_instance_id": card["card_instance_id"],
                    "card": copy.deepcopy(card),
                    "audit_projection_version": 2,
                }

                actual, selected, error = (
                    independent_oracle._oracle_grid_target(record, target)
                )

                self.assertIsNone(error)
                self.assertEqual(f"grid_{operation}", actual)
                self.assertEqual(card["card_instance_id"], selected["card_instance_id"])

    def test_headbutt_grid_target_is_typed_discard_to_top(self):
        card = combat_choice_card("Shrug It Off", "card:shrug")
        record = {
            "phase": "GRID",
            "authoritative_state_before": {"game_state": {
                "room_phase": "COMBAT",
                "current_action": "DiscardPileToTopOfDeckAction",
                "combat_state": {"discard_pile": [copy.deepcopy(card)]},
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [copy.deepcopy(card)],
                },
            }},
        }
        target = {
            "kind": "card", "card_instance_id": "card:shrug",
            "card": copy.deepcopy(card), "audit_projection_version": 2,
        }

        operation, selected, error = independent_oracle._oracle_grid_target(
            record, target
        )

        self.assertIsNone(error)
        self.assertEqual("grid_combat_discard_to_top", operation)
        self.assertEqual("card:shrug", selected["card_instance_id"])

    def test_explicitly_vetoed_untyped_neow_candidate_is_not_a_claim(self):
        row = {
            "selection_eligible": False,
            "veto_reason": "typed_neow_contract_missing",
            "producer_consequence_claim": {
                "operation": "unclassified_neow_contract",
            },
            "producer_consequence_raw": {
                "present": True,
                "value": {
                    "operation": "unclassified_neow_contract",
                    "uncertainty": (
                        "authoritative typed Neow contract missing"
                    ),
                },
            },
        }
        self.assertTrue(
            independent_oracle._explicit_ineligible_neow_sentinel(
                {"phase": "NEOW"}, row, {}
            )
        )
        row["selection_eligible"] = True
        self.assertFalse(
            independent_oracle._explicit_ineligible_neow_sentinel(
                {"phase": "NEOW"}, row, {}
            )
        )

    def test_neow_remove_grid_purge_alias_requires_parent_context(self):
        contract = typed_neow_option(
            0, "REMOVE_TWO", "NONE", max_hp=80
        )["target"]["neow_contract"]
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "NEOW",
            "mechanism_id": independent_oracle._oracle_neow_mechanism_id(
                contract
            ),
            "neow_contract": contract,
            "operation": "remove",
            "select_count": 2,
        }
        card = {
            "id": "Strike_R", "name": "Strike", "upgrades": 0,
            "card_instance_id": "card:strike",
        }
        record = {
            "phase": "GRID",
            "authoritative_state_before": {"game_state": {
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [card],
                    "parent_choice_context": parent,
                },
            }},
        }
        raw = {
            "target": {
                "kind": "card", "card_instance_id": "card:strike",
                "card": card,
            },
        }
        self.assertTrue(
            independent_oracle._grid_remove_purge_claim_alias(
                record, raw, "grid_purge", "grid_remove"
            )
        )
        expected = [{
            "kind": "grid_confirmation_effect",
            "operation": "grid_remove",
            "selected_card": card,
            "commit_timing": "after_grid_confirmation",
        }]
        claimed = copy.deepcopy(expected)
        claimed[0]["operation"] = "grid_purge"
        self.assertTrue(
            independent_oracle._grid_remove_purge_claim_alias(
                record, raw, claimed, expected
            )
        )
        bad_parent = copy.deepcopy(record)
        bad_parent["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["parent_choice_context"]["operation"] = "upgrade"
        self.assertFalse(
            independent_oracle._grid_remove_purge_claim_alias(
                bad_parent, raw, "grid_purge", "grid_remove"
            )
        )

    def test_hand_select_is_not_misclassified_as_master_deck_gain(self):
        cards = [
            {
                "id": "Defend_R", "name": "Defend", "upgrades": 0,
                "card_instance_id": "hand:defend", "type": "SKILL",
                "cost": 1,
            },
            {
                "id": "Strike_R", "name": "Strike", "upgrades": 0,
                "card_instance_id": "hand:strike", "type": "ATTACK",
                "cost": 1,
            },
        ]
        raw = {
            "option_id": "option:strike", "choice_index": 1,
            "label": "Strike", "target": {
                "kind": "card", "card_instance_id": "hand:strike",
                "card": copy.deepcopy(cards[1]),
            },
        }
        record = {
            "phase": "HAND_SELECT",
            "authoritative_state_before": {"game_state": {
                "current_action": "ArmamentsAction",
                "screen_state": {
                    "hand": copy.deepcopy(cards), "selected": [],
                    "max_cards": 1, "can_pick_zero": False,
                },
            }},
        }

        expected = independent_oracle._expected_visible_consequence(
            "HAND_SELECT", raw, record
        )
        self.assertEqual("hand_select_card", expected["operation"])
        self.assertEqual([], expected["card_changes"]["gain"])
        self.assertEqual(cards[1], expected["selected_card"])
        self.assertEqual(
            "ArmamentsAction", expected["future_costs"][0]["current_action"]
        )

        tampered = copy.deepcopy(raw)
        tampered["target"]["card"]["id"] = "Feed"
        invalid = independent_oracle._expected_visible_consequence(
            "HAND_SELECT", tampered, record
        )
        self.assertIn(
            "hand_select_target_card_facts_mismatch", invalid["uncertainty"]
        )

    def test_map_entry_relic_healing_is_recomputed_from_before_state(self):
        before = {
            "current_hp": 50, "max_hp": 80,
            "deck": [{"id": f"card-{index}"} for index in range(12)],
            "relics": [
                {"id": "Blood Vial"}, {"id": "Meal Ticket"},
                {"id": "Eternal Feather"}, {"id": "Pantograph"},
                {"id": "MawBank", "counter": -1},
            ],
        }
        record = {"authoritative_state_before": {"game_state": before}}
        targets = (
            ({"kind": "map_node", "symbol": "M", "x": 1, "y": 2}, 2),
            ({"kind": "map_node", "symbol": "$", "x": 1, "y": 2}, 15),
            ({"kind": "map_node", "symbol": "R", "x": 1, "y": 2}, 6),
            ({"kind": "map_boss", "act": 1}, 27),
        )
        for target, expected_heal in targets:
            with self.subTest(target=target):
                expected = independent_oracle._expected_visible_consequence(
                    "MAP", {"target": target}, record
                )
                self.assertEqual(expected_heal, expected["hp_delta"])
                self.assertEqual(12, expected["gold_delta"])

        spent = copy.deepcopy(record)
        spent["authoritative_state_before"]["game_state"]["relics"][-1]["counter"] = -2
        expected = independent_oracle._expected_visible_consequence(
            "MAP", {"target": targets[0][0]}, spent
        )
        self.assertEqual(0, expected["gold_delta"])

        missing_counter = copy.deepcopy(record)
        del missing_counter["authoritative_state_before"]["game_state"]["relics"][-1]["counter"]
        expected = independent_oracle._expected_visible_consequence(
            "MAP", {"target": targets[0][0]}, missing_counter
        )
        self.assertEqual("unknown", expected["field_knowledge"]["gold_delta"]["status"])

        capped = copy.deepcopy(record)
        capped["authoritative_state_before"]["game_state"]["current_hp"] = 79
        boss = independent_oracle._expected_visible_consequence(
            "MAP", {"target": {"kind": "map_boss", "act": 1}}, capped
        )
        self.assertEqual(1, boss["hp_delta"])

    def test_mark_of_the_bloom_zeroes_all_independent_healing_paths(self):
        before = {
            "current_hp": 10, "max_hp": 80,
            "deck": [{"id": f"card-{index}"} for index in range(10)],
            "relics": [
                {"id": "Blood Vial"}, {"id": "Eternal Feather"},
                {"id": "Regal Pillow"}, {"id": "Mark of the Bloom"},
            ],
        }
        record = {"authoritative_state_before": {"game_state": before}}

        campfire = independent_oracle._expected_visible_consequence(
            "REST",
            {"target": {"kind": "rest", "rest_option": "REST"}},
            record,
        )
        combat = independent_oracle._expected_visible_consequence(
            "MAP",
            {"target": {"kind": "map_node", "symbol": "M"}},
            record,
        )
        rest_room = independent_oracle._expected_visible_consequence(
            "MAP",
            {"target": {"kind": "map_node", "symbol": "R"}},
            record,
        )
        unknown_route = copy.deepcopy(record)
        unknown_route["decision_outcome"] = {"room_phase_after": "COMBAT"}

        self.assertEqual(0, campfire["hp_delta"])
        self.assertEqual(0, combat["hp_delta"])
        self.assertEqual(0, rest_room["hp_delta"])
        self.assertEqual(
            0,
            independent_oracle._oracle_random_route_downstream_entry_hp_delta(
                unknown_route,
                {"kind": "map_node", "symbol": "?"},
            ),
        )

    def test_unknown_route_separates_downstream_blood_vial_heal(self):
        target = {"kind": "map_node", "symbol": "?", "x": 1, "y": 4}
        record = {
            "authoritative_state_before": {
                "game_state": {
                    "current_hp": 48,
                    "max_hp": 80,
                    "relics": [{"id": "Blood Vial"}],
                },
            },
            "decision_outcome": {"room_phase_after": "COMBAT"},
        }

        self.assertEqual(
            2,
            independent_oracle._oracle_random_route_downstream_entry_hp_delta(
                record, target
            ),
        )
        consequence = independent_oracle._expected_visible_consequence(
            "MAP", {"target": target}, record
        )
        self.assertEqual(0, consequence["hp_delta"])

    def test_event_fight_separates_downstream_blood_vial_heal(self):
        record = {
            "phase": "EVENT",
            "authoritative_state_before": {
                "game_state": {
                    "current_hp": 55,
                    "max_hp": 86,
                    "relics": [{"id": "Blood Vial"}],
                },
            },
            "decision_outcome": {
                "phase_after": "COMBAT_TURN_1",
                "room_phase_before": "EVENT",
                "room_phase_after": "COMBAT",
            },
        }

        self.assertEqual(
            2,
            independent_oracle._oracle_event_downstream_combat_entry_hp_delta(
                record
            ),
        )
        bloom = copy.deepcopy(record)
        bloom["authoritative_state_before"]["game_state"]["relics"].append(
            {"id": "Mark of the Bloom"}
        )
        self.assertEqual(
            0,
            independent_oracle._oracle_event_downstream_combat_entry_hp_delta(
                bloom
            ),
        )
        no_vial = copy.deepcopy(record)
        no_vial["authoritative_state_before"]["game_state"]["relics"] = []
        self.assertIsNone(
            independent_oracle._oracle_event_downstream_combat_entry_hp_delta(
                no_vial
            )
        )

    def test_unknown_route_separates_downstream_meal_ticket_heal(self):
        target = {"kind": "map_node", "symbol": "?", "x": 2, "y": 11}
        record = {
            "authoritative_state_before": {
                "game_state": {
                    "current_hp": 60,
                    "max_hp": 80,
                    "relics": [{"id": "MealTicket"}],
                },
            },
            "decision_outcome": {
                "room_phase_after": "COMPLETE",
                "room_type_after": "ShopRoom",
            },
        }

        self.assertEqual(
            15,
            independent_oracle._oracle_random_route_downstream_entry_hp_delta(
                record, target
            ),
        )
        consequence = independent_oracle._expected_visible_consequence(
            "MAP", {"target": target}, record
        )
        self.assertEqual(0, consequence["hp_delta"])

        capped = copy.deepcopy(record)
        capped["authoritative_state_before"]["game_state"]["current_hp"] = 74
        self.assertEqual(
            6,
            independent_oracle._oracle_random_route_downstream_entry_hp_delta(
                capped, target
            ),
        )

    def test_golden_idol_max_hp_cost_caps_current_hp(self):
        record = {
            "authoritative_state_before": {
                "game_state": {
                    "current_hp": 79,
                    "max_hp": 80,
                    "relics": [],
                    "screen_state": {"options": [{}, {}, {}]},
                },
            },
        }
        raw = {
            "choice_index": 2,
            "target": {
                "kind": "event_option",
                "event_id": "Golden Idol",
                "original_button_index": 2,
            },
        }

        consequence = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )

        self.assertEqual(-5, consequence["hp_delta"])
        self.assertEqual(-6, consequence["max_hp_delta"])

    def test_golden_idol_legacy_branches_are_fully_typed(self):
        record = {
            "authoritative_state_before": {
                "game_state": {
                    "current_hp": 80,
                    "max_hp": 80,
                    "relics": [],
                    "screen_state": {"options": [{}, {}]},
                },
            },
        }
        take = independent_oracle._expected_visible_consequence(
            "EVENT",
            {
                "choice_index": 0,
                "target": {
                    "kind": "event_option",
                    "event_id": "Golden Idol",
                    "original_button_index": 0,
                },
            },
            record,
        )
        leave = independent_oracle._expected_visible_consequence(
            "EVENT",
            {
                "choice_index": 1,
                "target": {
                    "kind": "event_option",
                    "event_id": "Golden Idol",
                    "original_button_index": 1,
                },
            },
            record,
        )
        for consequence in (take, leave):
            self.assertEqual([], consequence["probabilistic_outcomes"])
            self.assertEqual(
                "known",
                consequence["field_knowledge"][
                    "probabilistic_outcomes"
                ]["status"],
            )
            self.assertEqual([], consequence["uncertainty"])
        self.assertEqual("Golden Idol", take["relic_id"])
        self.assertTrue(leave["leave"])

    def test_golden_idol_custom_unknown_prose_defers_to_typed_mechanics(self):
        record = {
            "authoritative_state_before": {"game_state": {
                "current_hp": 80,
                "max_hp": 80,
                "ascension_level": 0,
                "relics": [],
                "screen_state": {"options": [{}, {}]},
            }},
        }
        raw = {
            "choice_index": 0,
            "raw_text": "Take",
            "target": {
                "kind": "event_option",
                "event_id": "Golden Idol",
                "original_button_index": 0,
            },
        }
        legacy = independent_oracle._oracle_empty_consequence(
            "golden idol non-leave outcome settles on the following event surface"
        )
        legacy["uncertainty"] = [
            "golden idol non-leave outcome settles on the following event surface"
        ]
        legacy["uncertainty_classification"] = {
            "status": "unresolved",
            "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        }
        legacy["event_id"] = "Golden Idol"
        legacy["raw_effect_text"] = "Take"

        self.assertTrue(
            independent_oracle._classified_protocol_uncertainty(
                "EVENT", raw, legacy, record,
            )
        )

    def test_forgotten_altar_relic_identity_binds_to_typed_event_table(self):
        target = {
            "kind": "event_option",
            "event_id": "Forgotten Altar",
            "original_button_index": 0,
            "audit_projection_version": 2,
        }
        raw = {"action": "choose", "choice_index": 0, "target": target}
        record = {
            "phase": "EVENT",
            "authoritative_state_before": {"game_state": {
                "current_hp": 70,
                "max_hp": 80,
                "ascension_level": 0,
                "relics": [{"id": "Golden Idol"}],
                "screen_state": {
                    "event_id": "Forgotten Altar",
                    "options": [
                        {"original_button_index": index}
                        for index in range(3)
                    ],
                },
            }},
        }
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record,
        )

        def row(gained="Bloody Idol", removed="Golden Idol"):
            value = {"relic_id": gained, "lost_relic_id": removed}
            return {
                "producer_consequence_raw": {
                    "present": True, "value": copy.deepcopy(value),
                },
                "producer_consequence_claim": {"relic_id": gained},
                "producer_scoring_facts": {},
                "unclassified_producer_fields": ["lost_relic_id"],
            }

        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, row(), expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, row(gained="Circlet"), expected, None,
            )
        )
        self.assertEqual([], unresolved)
        self.assertEqual("relic_id", contradictions[0]["field"])

    def test_golden_idol_unresolved_settlement_binds_lifecycle_metadata(self):
        before_game = {
            "current_hp": 80,
            "max_hp": 80,
            "gold": 100,
            "block": 0,
            "deck": [],
            "relics": [],
            "potions": [],
            "keys": {"ruby": False, "emerald": False, "sapphire": False},
            "room_phase": "EVENT",
            "room_type": "EventRoom",
            "screen_type": "EVENT",
            "act": 1,
            "floor": 10,
        }
        after_game = copy.deepcopy(before_game)
        after_game["relics"] = [{"id": "Golden Idol"}]
        before_state = authoritative_state(
            10,
            observable=before_game,
            room_phase="EVENT",
            existing={"phase": "EVENT", "game_state": before_game},
        )
        after_state = authoritative_state(
            11,
            observable=after_game,
            room_phase="EVENT",
            existing={"phase": "EVENT", "game_state": after_game},
        )
        outcome = {
            "current_hp_delta": 0,
            "hp_delta": 0,
            "max_hp_delta": 0,
            "gold_delta": 0,
            "block_delta": 0,
            "deck": {"added": [], "removed": [], "changed": []},
            "relics": {
                "added": [{"id": "Golden Idol"}],
                "removed": [], "changed": [],
            },
            "potions": {"added": [], "removed": [], "changed": []},
            "keys_before": before_game["keys"],
            "keys_after": after_game["keys"],
            "phase_before": "EVENT", "phase_after": "EVENT",
            "room_phase_before": "EVENT", "room_phase_after": "EVENT",
            "room_type_before": "EventRoom", "room_type_after": "EventRoom",
            "screen_type_before": "EVENT", "screen_type_after": "EVENT",
            "act_before": 1, "act_after": 1,
            "floor_before": 10, "floor_after": 10,
        }
        record = bound(
            "decision",
            before_seq=10,
            after_seq=11,
            phase="EVENT",
            action="choose",
            requested_target_id="golden:0",
            resolved_target_id="golden:0",
            selected_choice_ids=["golden:0"],
            final_choice_ids=["golden:0"],
            decision_outcome=copy.deepcopy(outcome),
            authoritative_state_before=before_state,
            authoritative_state_after=after_state,
        )
        record["authoritative_choice_settlement"] = {
            "status": "unresolved",
            "authority": "protocol_state_delta",
            "fully_observable": False,
            "choice_id": "golden:0",
            "before_seq": 10,
            "after_seq": 11,
            "observed_outcome": copy.deepcopy(outcome),
        }
        observed = independent_oracle._authoritative_settlement_observation(
            record, "golden:0", {"event_id": "Golden Idol"}
        )
        self.assertIsNotNone(observed)
        self.assertEqual("EVENT", observed["phase_before"])
        self.assertEqual("EventRoom", observed["room_type_after"])

    def test_bottled_grid_operation_is_inferred_from_authoritative_delta(self):
        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "bottle:card",
            "in_bottle_flame": False,
            "in_bottle_lightning": False,
            "in_bottle_tornado": False,
        }
        after_card = copy.deepcopy(card)
        after_card["in_bottle_flame"] = True
        record = {
            "authoritative_state_before": {"game_state": {
                "deck": [copy.deepcopy(card)],
                "screen_state": {
                    "for_upgrade": False,
                    "for_transform": False,
                    "for_purge": False,
                    "cards": [copy.deepcopy(card)],
                    "num_cards": 1,
                },
            }},
            "authoritative_state_after": {"game_state": {
                "deck": [after_card],
            }},
        }
        operation, selected, error = independent_oracle._oracle_grid_target(
            record,
            {
                "kind": "card",
                "card_instance_id": card["card_instance_id"],
                "card": copy.deepcopy(card),
            },
        )
        self.assertIsNone(error)
        self.assertEqual("grid_bottle_attack", operation)
        self.assertEqual(card["card_instance_id"], selected["card_instance_id"])

    def test_world_of_goop_typed_options_have_exact_consequences(self):
        def contract(original, option_kind, parameters):
            return {
                "contract_version": 1,
                "contract_kind": "BASE_GAME_EVENT_OPTION",
                "event_id": "World of Goop",
                "event_class": (
                    "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
                ),
                "original_button_index": original,
                "option_kind": option_kind,
                "parameters": parameters,
            }

        def option(original, choice_index, event_contract):
            return {
                "original_button_index": original,
                "choice_index": choice_index,
                "disabled": False,
                "event_contract": event_contract,
            }

        gather_contract = contract(
            0, "GATHER", {"gold_gain": 75, "hp_damage": 11}
        )
        leave_contract = contract(1, "LEAVE", {"gold_loss": 20})
        intro_options = [
            option(0, 0, gather_contract),
            option(1, 1, leave_contract),
        ]

        def target(choice_index, event_contract):
            return {
                "kind": "event_option",
                "event_id": "World of Goop",
                "original_button_index": event_contract[
                    "original_button_index"
                ],
                "event_contract": event_contract,
                "mechanism_id": independent_oracle._oracle_event_mechanism_id(
                    event_contract
                ),
                "choice_index": choice_index,
            }

        record = {
            "authoritative_state_before": {"game_state": {
                "ascension_level": 0,
                "current_hp": 80,
                "max_hp": 80,
                "gold": 44,
                "screen_state": {
                    "event_id": "World of Goop",
                    "options": intro_options,
                },
            }},
        }
        gather = independent_oracle._expected_visible_consequence(
            "EVENT",
            {
                "choice_id": "event:world-of-goop:0",
                "choice_index": 0,
                "target": target(0, gather_contract),
            },
            record,
        )
        self.assertEqual("world_of_goop_gather_gold", gather["operation"])
        self.assertEqual(-11, gather["hp_delta"])
        self.assertEqual(75, gather["gold_delta"])
        self.assertEqual([], gather["uncertainty"])
        self.assertEqual("known", gather["field_knowledge"]["gold_delta"]["status"])

        continue_contract = contract(0, "CONTINUE", {})
        result_record = copy.deepcopy(record)
        result_record["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["options"] = [option(0, 0, continue_contract)]
        advance = independent_oracle._expected_visible_consequence(
            "EVENT",
            {
                "choice_id": "event:world-of-goop:continue",
                "choice_index": 0,
                "target": target(0, continue_contract),
            },
            result_record,
        )
        self.assertEqual(
            "world_of_goop_dialog_advance_noop", advance["operation"]
        )
        self.assertEqual(0, advance["hp_delta"])
        self.assertEqual(0, advance["gold_delta"])
        self.assertEqual([], advance["uncertainty"])

        legacy = independent_oracle._expected_visible_consequence(
            "EVENT",
            {
                "choice_index": 0,
                "target": {
                    "kind": "event_option", "event_id": "World of Goop",
                },
            },
            {"authoritative_state_before": {"game_state": {
                "ascension_level": 0,
            }}},
        )
        self.assertEqual(
            ["event outcome is unclassified without typed protocol mechanics"],
            legacy["uncertainty"],
        )

    def test_the_cleric_typed_options_have_exact_consequences(self):
        instance = {
            "heal_amount": 20,
            "heal_gold_cost": 35,
            "purify_cost": 50,
        }

        def contract(stage, original, option_kind, parameters):
            return {
                "contract_version": 1,
                "contract_kind": "BASE_GAME_EVENT_OPTION",
                "event_id": "The Cleric",
                "event_class": (
                    "com.megacrit.cardcrawl.events.exordium.Cleric"
                ),
                "event_stage": stage,
                "instance_parameters": instance,
                "original_button_index": original,
                "option_kind": option_kind,
                "parameters": parameters,
            }

        def option(original, choice_index, event_contract):
            return {
                "original_button_index": original,
                "choice_index": choice_index,
                "disabled": False,
                "event_contract": event_contract,
            }

        specs = [
            (0, "HEAL", {"gold_cost": 35, "heal_amount": 20}),
            (1, "PURIFY", {
                "gold_cost_if_purgeable": 50,
                "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            (2, "LEAVE", {}),
        ]
        contracts = [
            contract("MAIN", original, kind, parameters)
            for original, kind, parameters in specs
        ]
        options = [
            option(index, index, event_contract)
            for index, event_contract in enumerate(contracts)
        ]
        record = {
            "authoritative_state_before": {"game_state": {
                "ascension_level": 0,
                "current_hp": 70,
                "max_hp": 80,
                "gold": 90,
                "screen_state": {
                    "event_id": "The Cleric",
                    "options": options,
                },
            }},
        }

        def raw(choice_index, event_contract):
            return {
                "choice_id": f"event:cleric:{choice_index}",
                "choice_index": choice_index,
                "target": {
                    "kind": "event_option",
                    "event_id": "The Cleric",
                    "original_button_index": choice_index,
                    "event_contract": event_contract,
                    "mechanism_id": (
                        independent_oracle._oracle_event_mechanism_id(
                            event_contract
                        )
                    ),
                },
            }

        heal = independent_oracle._expected_visible_consequence(
            "EVENT", raw(0, contracts[0]), record
        )
        self.assertEqual("cleric_heal", heal["operation"])
        self.assertEqual(10, heal["hp_delta"])
        self.assertEqual(-35, heal["gold_delta"])
        self.assertEqual({"gold": 35, "hp": 0, "max_hp": 0}, heal["current_cost"])

        purify = independent_oracle._expected_visible_consequence(
            "EVENT", raw(1, contracts[1]), record
        )
        self.assertEqual("cleric_open_purge_grid", purify["operation"])
        self.assertEqual(-50, purify["gold_delta"])
        self.assertEqual("grid_purge", purify["future_costs"][0]["operation"])
        self.assertEqual(1, purify["future_costs"][0]["select_count"])

        leave = independent_oracle._expected_visible_consequence(
            "EVENT", raw(2, contracts[2]), record
        )
        self.assertEqual("cleric_leave", leave["operation"])
        self.assertTrue(leave["leave"])

        continue_contract = contract("RESULT", 0, "CONTINUE", {})
        result_record = copy.deepcopy(record)
        result_record["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["options"] = [option(0, 0, continue_contract)]
        advance = independent_oracle._expected_visible_consequence(
            "EVENT", raw(0, continue_contract), result_record
        )
        self.assertEqual(
            "cleric_dialog_advance_noop", advance["operation"]
        )
        self.assertEqual([], advance["uncertainty"])

        legacy = independent_oracle._expected_visible_consequence(
            "EVENT",
            {"choice_index": 0, "target": {
                "kind": "event_option", "event_id": "The Cleric",
            }},
            {"authoritative_state_before": {"game_state": {
                "ascension_level": 0,
            }}},
        )
        self.assertEqual(
            ["event outcome is unclassified without typed protocol mechanics"],
            legacy["uncertainty"],
        )

    def test_repeatable_and_duplicator_events_are_typed_end_to_end(self):
        def contract(
            event_id, event_class, stage, original, kind, instance, parameters
        ):
            return {
                "contract_version": 1,
                "contract_kind": "BASE_GAME_EVENT_OPTION",
                "event_id": event_id,
                "event_class": event_class,
                "event_stage": stage,
                "original_button_index": original,
                "option_kind": kind,
                "instance_parameters": copy.deepcopy(instance),
                "parameters": copy.deepcopy(parameters),
            }

        def evaluate(event_id, contracts, game_extra, selected_index=0):
            options = [
                {
                    "original_button_index": item["original_button_index"],
                    "choice_index": index,
                    "disabled": False,
                    "event_contract": copy.deepcopy(item),
                }
                for index, item in enumerate(contracts)
            ]
            selected = contracts[selected_index]
            target = {
                "kind": "event_option", "event_id": event_id,
                "choice_index": selected_index,
                "original_button_index": selected["original_button_index"],
                "event_contract": copy.deepcopy(selected),
                "mechanism_id": (
                    independent_oracle._oracle_event_mechanism_id(selected)
                ),
            }
            game = {
                "ascension_level": 0,
                "screen_state": {"event_id": event_id, "options": options},
                **copy.deepcopy(game_extra),
            }
            raw = {
                "choice_index": selected_index, "target": target,
                "raw_text": "localized",
            }
            record = {"authoritative_state_before": {"game_state": game}}
            expected = independent_oracle._expected_visible_consequence(
                "EVENT", raw, record
            )
            produced = autoplay._structured_option_consequence(
                "EVENT", target, "localized", state={"game_state": game}
            )
            matched, mismatches = (
                independent_oracle._consequence_matches_visible(
                    produced, expected
                )
            )
            self.assertTrue(matched, mismatches)
            return expected

        dead_instance = {
            "num_rewards": 0, "encounter_chance_percent": 25,
            "remaining_rewards": ["GOLD", "RELIC", "NOTHING"],
            "enemy_index": 1, "encounter_id": "Gremlin Nob",
        }
        dead_contracts = [
            contract(
                "Dead Adventurer",
                "com.megacrit.cardcrawl.events.exordium.DeadAdventurer",
                "INTRO", 0, "SEARCH", dead_instance, {
                    "roll_min": 0, "roll_max_inclusive": 99,
                    "encounter_roll_lt": 25,
                    "encounter_id": "Gremlin Nob",
                    "encounter_reward_gold_min": 25,
                    "encounter_reward_gold_max": 35,
                    "success_reward_kind": "GOLD",
                    "success_gold_gain": 30,
                    "success_random_relic_count": 0,
                    "success_relic_selection_mode": "NONE",
                },
            ),
            contract(
                "Dead Adventurer",
                "com.megacrit.cardcrawl.events.exordium.DeadAdventurer",
                "INTRO", 1, "LEAVE", dead_instance, {},
            ),
        ]
        dead = evaluate("Dead Adventurer", dead_contracts, {"relics": []})
        self.assertEqual("dead_adventurer_search", dead["operation"])
        self.assertEqual([0, 30], dead["gold_delta"])
        self.assertEqual(
            "exhaustive_probability",
            dead["uncertainty_classification"]["status"],
        )
        dead_target = {
            "kind": "event_option", "event_id": "Dead Adventurer",
            "choice_index": 0, "original_button_index": 0,
            "event_contract": copy.deepcopy(dead_contracts[0]),
            "mechanism_id": independent_oracle._oracle_event_mechanism_id(
                dead_contracts[0]
            ),
            "audit_projection_version": 3,
        }
        dead_raw = {"choice_index": 0, "target": dead_target}
        dead_record = {
            "phase": "EVENT",
            "available_options_before": [dead_raw, {
                "choice_index": 1,
                "target": {
                    "kind": "event_option",
                    "event_id": "Dead Adventurer",
                    "original_button_index": 1,
                    "event_contract": copy.deepcopy(dead_contracts[1]),
                    "mechanism_id": independent_oracle._oracle_event_mechanism_id(
                        dead_contracts[1]
                    ),
                    "audit_projection_version": 3,
                },
            }],
            "authoritative_state_before": {"game_state": {
                "ascension_level": 0, "relics": [],
                "screen_state": {
                    "event_id": "Dead Adventurer",
                    "options": [
                        {
                            "choice_index": index,
                            "original_button_index": contract_value[
                                "original_button_index"
                            ],
                            "disabled": False,
                            "event_contract": copy.deepcopy(contract_value),
                        }
                        for index, contract_value in enumerate(dead_contracts)
                    ],
                },
            }},
        }
        dead_expected = independent_oracle._expected_visible_consequence(
            "EVENT", dead_raw, dead_record
        )
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                dead_record, dead_raw, {
                    "producer_consequence_raw": {
                        "present": True, "value": {},
                    },
                    "producer_consequence_claim": {},
                    "producer_scoring_facts": {},
                    "unclassified_producer_fields": [],
                }, dead_expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        scrap_instance = {
            "relic_chance_percent_displayed": 45, "damage": 5,
            "total_damage_dealt": 7, "screen_num": 0,
        }
        scrap_contracts = [
            contract(
                "Scrap Ooze",
                "com.megacrit.cardcrawl.events.exordium.ScrapOoze",
                "MAIN", 0, "REACH_INSIDE", scrap_instance, {
                    "hp_loss": 5, "roll_min": 0,
                    "roll_max_inclusive": 99,
                    "success_roll_min_inclusive": 54,
                    "success_random_relic_count": 1,
                    "success_relic_selection_mode": (
                        "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                    ),
                },
            ),
            contract(
                "Scrap Ooze",
                "com.megacrit.cardcrawl.events.exordium.ScrapOoze",
                "MAIN", 1, "LEAVE", scrap_instance, {},
            ),
        ]
        scrap = evaluate("Scrap Ooze", scrap_contracts, {"relics": []})
        self.assertEqual(-5, scrap["hp_delta"])
        self.assertEqual(
            0.46, scrap["probabilistic_outcomes"][0]["probability"]
        )

        scrap_result_instance = {
            "relic_chance_percent_displayed": 45, "damage": 5,
            "total_damage_dealt": 12, "screen_num": 1,
        }
        scrap_result_contract = contract(
            "Scrap Ooze",
            "com.megacrit.cardcrawl.events.exordium.ScrapOoze",
            "RESULT", 0, "CONTINUE", scrap_result_instance, {},
        )
        scrap_result_target = {
            "kind": "event_option",
            "event_id": "Scrap Ooze",
            "choice_index": 0,
            "original_button_index": 0,
            "event_contract": copy.deepcopy(scrap_result_contract),
            "mechanism_id": independent_oracle._oracle_event_mechanism_id(
                scrap_result_contract
            ),
        }
        scrap_result = independent_oracle._expected_visible_consequence(
            "EVENT",
            {"choice_index": 0, "target": scrap_result_target},
            {"authoritative_state_before": {"game_state": {
                "ascension_level": 0,
                "screen_state": {
                    "event_id": "Scrap Ooze",
                    "options": [{
                        "original_button_index": 0,
                        "choice_index": 0,
                        "disabled": False,
                        "event_contract": copy.deepcopy(scrap_result_contract),
                    }],
                },
                "relics": [],
            }}},
        )
        self.assertEqual("scrap_ooze_dialog_advance_noop", scrap_result["operation"])
        self.assertEqual([], scrap_result["uncertainty"])
        legacy_scrap_result = {
            "leave": True,
            "event_outcome_id": "leave",
        }
        matched, mismatches = independent_oracle._consequence_matches_visible(
            legacy_scrap_result, scrap_result,
            {"phase": "EVENT"},
        )
        self.assertTrue(matched, mismatches)
        structured_scrap_result = {
            "schema_version": 1,
            "scope": scrap_result["scope"],
            "event_id": "Scrap Ooze",
            "hp_delta": None,
            "max_hp_delta": None,
            "gold_delta": None,
            "card_changes": {
                "gain": [], "remove": [], "upgrade": [], "transform": [],
            },
            "relic_changes": {
                "gain": [], "remove": [], "counter": [],
            },
            "potion_changes": {
                "gain": [], "remove": [], "replace": [],
            },
            "curse": {
                "gain": [], "remove": [], "probability": None,
                "omamori_applicable": None,
                "omamori_charges_consumed": None,
            },
            "probabilistic_outcomes": [],
            "current_cost": {"gold": None, "hp": None, "max_hp": None},
            "future_costs": [],
            "raw_effect_text": "",
            "uncertainty": [
                "typed_event_contract_missing_invalid_or_mismatched",
            ],
            "uncertainty_classification": {
                "status": "unresolved",
            },
            "field_knowledge": {
                field: {"status": "unknown"}
                for field in independent_oracle._ORACLE_CONSEQUENCE_FIELDS
            },
        }
        matched, mismatches = independent_oracle._consequence_matches_visible(
            structured_scrap_result, scrap_result,
            {
                "phase": "EVENT",
                "_producer_consequence_claim": {"leave": True},
            },
        )
        self.assertTrue(matched, mismatches)
        self.assertTrue(
            independent_oracle._classified_protocol_uncertainty(
                "EVENT",
                {"choice_index": 0, "target": scrap_result_target},
                structured_scrap_result,
                {
                    "phase": "EVENT",
                    "authoritative_state_before": {"game_state": {
                        "ascension_level": 0,
                        "screen_state": {
                            "event_id": "Scrap Ooze",
                            "options": [{
                                "original_button_index": 0,
                                "choice_index": 0,
                                "disabled": False,
                                "event_contract": copy.deepcopy(
                                    scrap_result_contract
                                ),
                            }],
                        },
                        "relics": [],
                    }},
                },
            )
        )

        face_pool = [
            "CultistMask", "FaceOfCleric", "GremlinMask", "NlothsMask",
            "SsserpentHead",
        ]
        face_instance = {
            "gold_reward": 75, "damage": 8,
            "random_face_pool": face_pool,
        }
        face_contracts = [
            contract(
                "Face Trader",
                "com.megacrit.cardcrawl.events.shrines.FaceTrader",
                "MAIN", 0, "TOUCH", face_instance,
                {"hp_loss": 8, "gold_gain": 75},
            ),
            contract(
                "Face Trader",
                "com.megacrit.cardcrawl.events.shrines.FaceTrader",
                "MAIN", 1, "TRADE", face_instance, {
                    "random_relic_pool": face_pool,
                    "random_relic_count": 1,
                    "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
                },
            ),
            contract(
                "Face Trader",
                "com.megacrit.cardcrawl.events.shrines.FaceTrader",
                "MAIN", 2, "LEAVE", face_instance, {},
            ),
        ]
        face = evaluate(
            "Face Trader", face_contracts,
            {"max_hp": 80, "relics": []}, selected_index=1,
        )
        self.assertEqual("face_trader_trade", face["operation"])
        self.assertEqual(5, len(face["probabilistic_outcomes"]))
        face_producer = copy.deepcopy(face)
        face_producer["event_id"] = "FaceTrader"
        matched, mismatches = independent_oracle._consequence_matches_visible(
            face_producer, face, {"phase": "EVENT"}
        )
        self.assertTrue(matched, mismatches)
        self.assertTrue(independent_oracle._effect_claim_equal(
            "relic_changes", face["relic_changes"], face["relic_changes"]
        ))

        duplicator_instance = {"screen_num": 0}
        duplicator_contracts = [
            contract(
                "Duplicator",
                "com.megacrit.cardcrawl.events.shrines.Duplicator",
                "MAIN", 0, "DUPLICATE", duplicator_instance, {
                    "duplicate_select_count": 1,
                    "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
                },
            ),
            contract(
                "Duplicator",
                "com.megacrit.cardcrawl.events.shrines.Duplicator",
                "MAIN", 1, "LEAVE", duplicator_instance, {},
            ),
        ]
        duplicator = evaluate(
            "Duplicator", duplicator_contracts, {"relics": []}
        )
        self.assertEqual(
            "duplicator_open_duplicate_grid", duplicator["operation"]
        )
        self.assertEqual(
            "grid_duplicate", duplicator["future_costs"][0]["operation"]
        )

        bonfire_instance = {"card_select": False}
        bonfire_choose = contract(
            "Bonfire Elementals",
            "com.megacrit.cardcrawl.events.shrines.Bonfire",
            "CHOOSE", 0, "OFFER_CARD", bonfire_instance, {
                "offer_select_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
                ),
            },
        )
        bonfire = evaluate(
            "Bonfire Elementals", [bonfire_choose], {"relics": []}
        )
        self.assertEqual("bonfire_open_offer_grid", bonfire["operation"])
        self.assertEqual(
            "grid_offer_card", bonfire["future_costs"][0]["operation"]
        )
        self.assertEqual(
            "classified_future",
            bonfire["uncertainty_classification"]["status"],
        )
        bonfire_target = {
            "kind": "event_option",
            "event_id": "Bonfire Elementals",
            "choice_index": 0,
            "original_button_index": 0,
            "event_contract": copy.deepcopy(bonfire_choose),
            "mechanism_id": (
                independent_oracle._oracle_event_mechanism_id(bonfire_choose)
            ),
            "audit_projection_version": 3,
        }
        bonfire_raw = {"choice_index": 0, "target": bonfire_target}
        bonfire_record = {
            "phase": "EVENT",
            "available_options_before": [bonfire_raw],
            "authoritative_state_before": {"game_state": {
                "ascension_level": 0,
                "screen_state": {
                    "event_id": "Bonfire Elementals",
                    "options": [{
                        "original_button_index": 0,
                        "choice_index": 0,
                        "disabled": False,
                        "event_contract": copy.deepcopy(bonfire_choose),
                    }],
                },
            }},
        }
        raw_classification = {
            "status": "classified_future",
            "authority": "producer_event_contract",
            "reason": "the next GRID binds the offered card",
        }
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                bonfire_record,
                bonfire_raw,
                {
                    "producer_consequence_raw": {
                        "present": True,
                        "value": {
                            "hp_delta": 0,
                            "original_button_index": 0,
                            "uncertainty_classification": raw_classification,
                        },
                    },
                    "producer_consequence_claim": {"hp_delta": 0},
                    "producer_scoring_facts": {},
                    "unclassified_producer_fields": [
                        "original_button_index",
                        "uncertainty_classification",
                    ],
                },
                bonfire,
                None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        for stage in ("INTRO", "COMPLETE"):
            with self.subTest(event="Bonfire Elementals", stage=stage):
                advance = evaluate(
                    "Bonfire Elementals",
                    [contract(
                        "Bonfire Elementals",
                        "com.megacrit.cardcrawl.events.shrines.Bonfire",
                        stage, 0, "CONTINUE", bonfire_instance, {},
                    )],
                    {"relics": []},
                )
                self.assertEqual(
                    "bonfire_dialog_advance_noop", advance["operation"]
                )
                self.assertEqual([], advance["uncertainty"])

    def test_effect_claim_comparison_ignores_presentation_fields_only(self):
        claimed_card = {
            "id": "Metallicize", "card_instance_id": "card:one",
            "upgrades": 0, "name": "金属化", "price": 75,
            "is_playable": False,
        }
        observed_card = {
            "id": "Metallicize", "card_instance_id": "card:one",
            "upgrades": 0, "type": "POWER", "cost": 1,
        }
        claimed_potion = {"id": "SpeedPotion", "price": 50}
        observed_potion = {
            "id": "SpeedPotion", "potion_instance_id": "potion:new",
            "slot": 2,
        }

        self.assertTrue(independent_oracle._effect_claim_equal(
            "card_changes", {"gain": [claimed_card]},
            {"gain": [observed_card]},
        ))
        self.assertTrue(independent_oracle._effect_claim_equal(
            "potion_changes", {"gain": [claimed_potion]},
            {"gain": [observed_potion]},
        ))
        self.assertFalse(independent_oracle._effect_claim_equal(
            "card_changes", {"gain": [claimed_card]},
            {"gain": [{**observed_card, "id": "Demon Form"}]},
        ))
        self.assertFalse(independent_oracle._effect_claim_equal(
            "potion_changes", {"gain": [claimed_potion]},
            {"gain": [{**observed_potion, "id": "WeakPotion"}]},
        ))
        random_card = {
            "kind": "random_card_gain", "count": 1,
            "domain": "base_game_colorless_uncommon_pool",
            "selection_mode": "random",
        }
        random_potion = {
            "kind": "random_potion_gain", "count": 1,
            "domain": "base_game_potion_pool", "selection_mode": "random",
        }
        self.assertTrue(independent_oracle._effect_claim_equal(
            "card_changes",
            {"gain": [random_card], "remove": [], "upgrade": [], "transform": []},
            {"gain": [copy.deepcopy(random_card)], "remove": []},
        ))
        self.assertTrue(independent_oracle._effect_claim_equal(
            "potion_changes",
            {"gain": [random_potion], "remove": [], "replace": []},
            {"gain": [copy.deepcopy(random_potion)], "remove": [], "replace": []},
        ))
        forged_random = {**random_card, "domain": "rare_colorless_pool"}
        self.assertFalse(independent_oracle._effect_claim_equal(
            "card_changes", {"gain": [random_card]},
            {"gain": [forged_random]},
        ))
        deferred_claim = [{
            "kind": "grid_confirmation_effect",
            "operation": "grid_purge",
            "selected_card": claimed_card,
            "commit_timing": "after_grid_confirmation",
        }]
        deferred_expected = [{
            "kind": "grid_confirmation_effect",
            "operation": "grid_purge",
            "selected_card": {
                key: value for key, value in claimed_card.items()
                if key != "is_playable"
            },
            "commit_timing": "after_grid_confirmation",
        }]
        self.assertTrue(independent_oracle._effect_claim_equal(
            "future_costs", deferred_claim, deferred_expected,
        ))
        wrong_uuid = copy.deepcopy(deferred_claim)
        wrong_uuid[0]["selected_card"]["card_instance_id"] = "card:other"
        self.assertFalse(independent_oracle._effect_claim_equal(
            "future_costs", wrong_uuid, deferred_expected,
        ))
        wrong_timing = copy.deepcopy(deferred_claim)
        wrong_timing[0]["commit_timing"] = "immediate"
        self.assertFalse(independent_oracle._effect_claim_equal(
            "future_costs", wrong_timing, deferred_expected,
        ))

    def test_parameterized_global_commands_are_not_choice_candidates(self):
        choices, missing = independent_oracle.reconstruct_legal_choices({
            "available_options_before": [{
                "option_id": "event:0",
                "choice_index": 0,
                "label": "Accept",
                "target": {"kind": "event_option", "event_id": "Event"},
            }],
            "available_commands_before": [
                "choose", "potion", "key", "click", "wait", "state", "skip",
            ],
        })

        self.assertEqual([], missing)
        self.assertEqual(
            {"event:0", "action:return"},
            {row["choice_id"] for row in choices},
        )

    def artifacts(self, records):
        terminal_row = next(
            row for row in records
            if isinstance(row, dict)
            and row.get("record_type") == "terminal_result"
        )
        state = authoritative_state(
            terminal_row.get("terminal_state_seq"),
            existing={
                "terminal_state_seq": terminal_row.get("terminal_state_seq"),
                "game_state": {
                    "screen_type": "GAME_OVER",
                    "screen_state": {
                        "victory": terminal_row.get("victory"),
                    },
                    "run_victory": terminal_row.get("victory"),
                    "heart_defeated": terminal_row.get("heart_defeated"),
                    "act": terminal_row.get("act", 3),
                },
            },
        )
        result = copy.deepcopy(terminal_row)
        context = {
            "schema_version": 2,
            **{field: result.get(field) for field in (
                "attempt_id", "run_id", "seed", "character",
                "ascension_level", "run_type", "decision_hash",
                "controller_hash", "policy_version", "selection_id",
                "selection_digest",
                "terminal_state_seq",
            )},
        }
        selection = copy.deepcopy(context)
        return {
            "run_context": context,
            "run_result": result,
            "state": state,
            "selection": selection,
        }

    def audit(self, records, *, artifacts=None):
        return independent_oracle.audit_records(
            records,
            DECISION_HASH,
            ATTEMPT,
            artifacts=self.artifacts(records) if artifacts is None else artifacts,
        )

    def production_record(
        self, *, phase, options, commands, payload, candidates,
        before=None, after=None,
    ):
        default_observable = {
            "current_hp": 50, "max_hp": 80, "gold": 100, "block": 0,
            "deck": [], "relics": [], "potions": [],
            "keys": {"ruby": False, "emerald": False, "sapphire": False},
        }
        before = {**default_observable, **copy.deepcopy(before or {})}
        after = {**before, **copy.deepcopy(after or {})}
        observable_keys = set(default_observable)
        observable_before = {
            key: copy.deepcopy(value) for key, value in before.items()
            if key in observable_keys
        }
        observable_after = {
            key: copy.deepcopy(value) for key, value in after.items()
            if key in observable_keys
        }
        state = {
            "phase": phase,
            "options": copy.deepcopy(options),
            "available_commands": list(commands),
            "game_state": copy.deepcopy(before),
        }
        decision = {
            "candidates": copy.deepcopy(candidates),
            "model_advice": {"status": "not_consulted", "applied": False},
        }
        canonical = autoplay.canonical_legal_choices(
            state, copy.deepcopy(payload), decision
        )
        selected_ids = [
            str(row["choice_id"]) for row in canonical
            if row.get("selected") is True
        ]
        requested = (
            payload.get("option_id") or payload.get("potion_instance_id")
            or payload.get("target_id")
        )
        record = bound(
            "decision",
            before_seq=10,
            after_seq=11,
            phase=phase,
            action=payload.get("action"),
            potion_operation=(
                payload.get("operation")
                if payload.get("action") == "potion" else None
            ),
            available_options_before=[
                autoplay.compact_option(option) for option in options
            ],
            available_commands_before=list(commands),
            legal_choices_before=canonical,
            selected_choice_ids=selected_ids,
            requested_target_id=requested,
            resolved_target_id=requested,
            final_choice_ids=selected_ids,
            observable_state_before=observable_before,
            observable_state_after=observable_after,
            decision_outcome=autoplay.observable_inventory_delta(
                observable_before, observable_after
            ),
            decision={
                "model_advice": {
                    "status": "not_consulted", "applied": False,
                    "final_choice_ids": selected_ids,
                }
            },
            authoritative_state_before=authoritative_state(
                10, observable=observable_before, room_phase="EVENT",
                existing={"game_state": {
                    "screen_state": copy.deepcopy(before["screen_state"]),
                }} if isinstance(before.get("screen_state"), dict) else None,
            ),
            authoritative_state_after=authoritative_state(
                11, observable=observable_after, room_phase="EVENT",
                existing={"game_state": {
                    "screen_state": copy.deepcopy(after["screen_state"]),
                }} if isinstance(after.get("screen_state"), dict) else None,
            ),
        )
        resource = autoplay._resource_preparation_options(state, payload)
        if resource:
            record["decision_surface_kind"] = "resource_preparation"
            record["parent_choice_surface_pending"] = True
            record["resource_preparation_options_before"] = [
                autoplay.compact_option(option) for option in resource
            ]
        record["authoritative_choice_settlement"] = (
            autoplay.authoritative_choice_settlement(record)
        )
        return record

    def test_indexed_a0_event_consequences_match_independent_oracle(self):
        jax = {
            "id": "J.A.X.", "name": "J.A.X.", "type": "SKILL",
            "rarity": "SPECIAL", "upgrades": 0,
        }
        cases = {
            "Living Wall": [0, 1, 2],
            "Golden Wing": [0, 1, 2],
            "Drug Dealer": [0, 1, 2],
            "Lab": [0],
        }
        for event_id, indexes in cases.items():
            with self.subTest(event_id=event_id):
                options = []
                for index in indexes:
                    target = {
                        "kind": "event_option", "event_id": event_id,
                        "choice_index": index,
                        "original_button_index": index,
                    }
                    if event_id == "Drug Dealer" and index == 0:
                        target["card"] = copy.deepcopy(jax)
                    options.append({
                        "option_id": f"{event_id}:{index}",
                        "choice_index": index,
                        "label": f"choice {index}",
                        "target": target,
                    })
                before = {
                    "ascension_level": 0,
                    "max_hp": 80,
                    "screen_state": {"event_id": event_id},
                }
                state = {
                    "phase": "EVENT", "options": copy.deepcopy(options),
                    "available_commands": ["choose"],
                    "game_state": {
                        "ascension_level": 0,
                        **copy.deepcopy(before),
                    },
                }
                candidates = []
                for option in options:
                    consequence = autoplay._structured_option_consequence(
                        "EVENT", option["target"], option["label"],
                        state=state,
                    )
                    claim = {
                        key: copy.deepcopy(value)
                        for key, value in consequence.items()
                        if key in autoplay._PRODUCER_EFFECT_FIELDS
                    }
                    candidates.append(production_candidate(
                        option["option_id"], option["choice_index"],
                        10 - option["choice_index"], consequences=claim,
                    ))
                record = self.production_record(
                    phase="EVENT", options=options, commands=["choose"],
                    payload={
                        "action": "choose",
                        "option_id": options[0]["option_id"],
                    },
                    candidates=candidates, before=before,
                )
                report = self.audit(base_records(record))
                self.assertNotIn(
                    "candidate_consequence_binding_mismatch",
                    issue_kinds(report),
                    report,
                )
                self.assertNotIn(
                    "producer_consequence_claim_contradicted",
                    issue_kinds(report),
                    report,
                )

    def test_progress_bound_events_match_independent_mechanics(self):
        card = lambda card_id: {
            "id": card_id, "name": card_id, "type": (
                "CURSE" if card_id in {"Parasite", "Writhe", "Pain"}
                else "SKILL"
            ),
            "rarity": (
                "CURSE" if card_id in {"Parasite", "Writhe", "Pain"}
                else "UNCOMMON"
            ),
            "upgrades": 0,
            "card_instance_id": f"preview:{card_id}",
        }
        cases = (
            (
                "Shining Light",
                "com.megacrit.cardcrawl.events.exordium.ShiningLight",
                "event_stage", "INTRO", 2, {},
            ),
            (
                "Mushrooms",
                "com.megacrit.cardcrawl.events.exordium.Mushrooms",
                "screen_num", 0, 2, {1: card("Parasite")},
            ),
            (
                "The Library",
                "com.megacrit.cardcrawl.events.city.TheLibrary",
                "screen_num", 0, 2, {},
            ),
            (
                "Masked Bandits",
                "com.megacrit.cardcrawl.events.city.MaskedBandits",
                "event_stage", "INTRO", 2, {},
            ),
            (
                "Colosseum",
                "com.megacrit.cardcrawl.events.city.Colosseum",
                "event_stage", "POST_COMBAT", 2, {},
            ),
            (
                "Winding Halls",
                "com.megacrit.cardcrawl.events.beyond.WindingHalls",
                "screen_num", 1, 3,
                {0: card("Madness"), 1: card("Writhe")},
            ),
            (
                "SensoryStone",
                "com.megacrit.cardcrawl.events.beyond.SensoryStone",
                "event_stage", "INTRO_2", 3, {},
            ),
            (
                "Accursed Blacksmith",
                "com.megacrit.cardcrawl.events.shrines.AccursedBlacksmith",
                "screen_num", 0, 3, {1: card("Pain")},
            ),
            (
                "Mysterious Sphere",
                "com.megacrit.cardcrawl.events.beyond.MysteriousSphere",
                "event_stage", "INTRO", 2, {},
            ),
            (
                "Spire Heart",
                "com.megacrit.cardcrawl.events.beyond.SpireHeart",
                "event_stage", "INTRO", 1, {},
            ),
        )
        for (
            event_id, event_class, progress_field, progress, count, previews,
        ) in cases:
            with self.subTest(event_id=event_id):
                screen = {
                    "event_id": event_id,
                    "event_class": event_class,
                    progress_field: progress,
                }
                options = []
                for index in range(count):
                    target = {
                        "kind": "event_option",
                        "event_id": event_id,
                        "original_button_index": index,
                        "audit_projection_version": (
                            autoplay.AUDIT_PROJECTION_VERSION
                        ),
                    }
                    if event_id != "The Library":
                        target.update({
                            "event_class": event_class,
                            progress_field: progress,
                        })
                    if index in previews:
                        target["card"] = copy.deepcopy(previews[index])
                    options.append({
                        "option_id": f"{event_id}:{index}",
                        "choice_index": index,
                        "original_button_index": index,
                        "label": f"choice {index}",
                        "target": target,
                    })
                screen["options"] = copy.deepcopy(options)
                game = {
                    "ascension_level": 0,
                    "current_hp": (
                        10 if event_id == "The Library"
                        else 64 if event_id == "Winding Halls"
                        else 60
                    ),
                    "max_hp": (
                        50 if event_id == "The Library"
                        else 90 if event_id == "Winding Halls"
                        else 80
                    ),
                    "gold": 123,
                    "deck": [
                        {
                            "id": "Strike_R", "type": "ATTACK",
                            "upgrades": 0,
                            "card_instance_id": "deck:strike",
                        },
                        {
                            "id": "Defend_R", "type": "SKILL",
                            "upgrades": 0,
                            "card_instance_id": "deck:defend",
                        },
                    ],
                    "relics": [], "potions": [],
                    "screen_state": screen,
                }
                state = {
                    "phase": "EVENT", "options": copy.deepcopy(options),
                    "game_state": copy.deepcopy(game),
                }
                record = {
                    "phase": "EVENT",
                    "available_options_before": copy.deepcopy(options),
                    "authoritative_state_before": (
                        autoplay.authoritative_state_snapshot({
                            "protocol_version": 2,
                            "phase": "EVENT",
                            "game_state": copy.deepcopy(game),
                        })
                    ),
                }
                for option in options:
                    production = autoplay._structured_option_consequence(
                        "EVENT", option["target"], option["label"],
                        state=state,
                    )
                    if (
                        event_id == "The Library"
                        and option["choice_index"] == 1
                    ):
                        self.assertEqual(17, production["hp_delta"])
                    if event_id == "Winding Halls":
                        self.assertEqual(
                            {0: -11, 1: 23, 2: 0}[option["choice_index"]],
                            production["hp_delta"],
                        )
                        self.assertEqual(
                            {0: 0, 1: 0, 2: -5}[option["choice_index"]],
                            production["max_hp_delta"],
                        )
                    expected = independent_oracle._expected_visible_consequence(
                        "EVENT", {
                            "choice_index": option["choice_index"],
                            "raw_text": option["label"],
                            "target": option["target"],
                        }, record,
                    )
                    matched, mismatches = (
                        independent_oracle._consequence_matches_visible(
                            production, expected,
                        )
                    )
                    self.assertTrue(matched, mismatches)
                    self.assertIsNotNone(production.get("operation"))
                    self.assertNotEqual(
                        "unresolved",
                        production["uncertainty_classification"]["status"],
                    )

    def test_nloth_trade_matches_visible_relic_label_independently(self):
        event_id = "N'loth"
        event_class = "com.megacrit.cardcrawl.events.shrines.Nloth"
        relics = [
            {"id": "Whetstone", "name": "Whetstone", "counter": -1},
            {
                "id": "PreservedInsect", "name": "Preserved Insect",
                "counter": -1,
            },
        ]
        rows = [
            (0, "Trade Whetstone"),
            (1, "Trade Preserved Insect"),
            (2, "Leave"),
        ]
        options = []
        for index, label in rows:
            target = {
                "kind": "event_option", "event_id": event_id,
                "original_button_index": index,
                "audit_projection_version": autoplay.AUDIT_PROJECTION_VERSION,
            }
            options.append({
                "option_id": f"nloth:{index}", "choice_index": index,
                "original_button_index": index, "label": label,
                "target": target,
            })
        game = {
            "ascension_level": 0, "current_hp": 37, "max_hp": 80,
            "gold": 411, "deck": [], "relics": relics, "potions": [],
            "screen_state": {
                "event_id": event_id, "event_class": event_class,
                "options": copy.deepcopy(options),
            },
        }
        state = {
            "phase": "EVENT", "options": copy.deepcopy(options),
            "game_state": copy.deepcopy(game),
        }
        record = {
            "phase": "EVENT",
            "available_options_before": copy.deepcopy(options),
            "authoritative_state_before": autoplay.authoritative_state_snapshot({
                "protocol_version": 2, "phase": "EVENT",
                "game_state": copy.deepcopy(game),
            }),
        }

        for option in options:
            production = autoplay._structured_option_consequence(
                "EVENT", option["target"], option["label"], state=state,
            )
            expected = independent_oracle._expected_visible_consequence(
                "EVENT", {
                    "choice_index": option["choice_index"],
                    "raw_text": option["label"],
                    "target": option["target"],
                }, record,
            )
            matched, mismatches = (
                independent_oracle._consequence_matches_visible(
                    production, expected,
                )
            )
            self.assertTrue(matched, mismatches)
            self.assertNotEqual(
                "unresolved",
                production["uncertainty_classification"]["status"],
            )
        self.assertEqual(
            [relics[0]],
            autoplay._structured_option_consequence(
                "EVENT", options[0]["target"], options[0]["label"],
                state=state,
            )["relic_changes"]["remove"],
        )

    def test_moai_head_full_heal_matches_independent_mechanics(self):
        event_id = "The Moai Head"
        event_class = "com.megacrit.cardcrawl.events.beyond.MoaiHead"
        options = []
        for choice_index, original_index, label in (
            (0, 0, "Jump Inside"), (1, 2, "Leave"),
        ):
            target = {
                "kind": "event_option", "event_id": event_id,
                "original_button_index": original_index,
                "audit_projection_version": autoplay.AUDIT_PROJECTION_VERSION,
            }
            options.append({
                "option_id": f"moai:{original_index}",
                "choice_index": choice_index,
                "original_button_index": original_index,
                "label": label, "target": target,
            })
        game = {
            "ascension_level": 0, "current_hp": 39, "max_hp": 94,
            "gold": 383, "deck": [], "relics": [], "potions": [],
            "screen_state": {
                "event_id": event_id, "event_class": event_class,
                "options": copy.deepcopy(options),
            },
        }
        state = {
            "phase": "EVENT", "options": copy.deepcopy(options),
            "game_state": copy.deepcopy(game),
        }
        record = {
            "phase": "EVENT",
            "available_options_before": copy.deepcopy(options),
            "authoritative_state_before": autoplay.authoritative_state_snapshot({
                "protocol_version": 2, "phase": "EVENT",
                "game_state": copy.deepcopy(game),
            }),
        }

        for option in options:
            production = autoplay._structured_option_consequence(
                "EVENT", option["target"], option["label"], state=state,
            )
            expected = independent_oracle._expected_visible_consequence(
                "EVENT", {
                    "choice_index": option["choice_index"],
                    "raw_text": option["label"],
                    "target": option["target"],
                }, record,
            )
            matched, mismatches = (
                independent_oracle._consequence_matches_visible(
                    production, expected,
                )
            )
            self.assertTrue(matched, mismatches)
        jump = autoplay._structured_option_consequence(
            "EVENT", options[0]["target"], options[0]["label"], state=state,
        )
        self.assertEqual(43, jump["hp_delta"])
        self.assertEqual(-12, jump["max_hp_delta"])

    def test_spire_heart_act_four_heal_matches_independent_mechanics(self):
        event_class = "com.megacrit.cardcrawl.events.beyond.SpireHeart"
        target = {
            "kind": "event_option", "event_id": "Spire Heart",
            "original_button_index": 0,
            "event_class": event_class,
            "event_stage": "GO_TO_ENDING",
            "audit_projection_version": autoplay.AUDIT_PROJECTION_VERSION,
        }
        option = {
            "option_id": "spire-heart:0", "choice_index": 0,
            "original_button_index": 0, "label": "Continue",
            "target": copy.deepcopy(target),
        }
        game = {
            "ascension_level": 0, "act": 3,
            "current_hp": 33, "max_hp": 90,
            "has_ruby_key": True,
            "has_emerald_key": True,
            "has_sapphire_key": True,
            "screen_state": {
                "event_id": "Spire Heart", "event_class": event_class,
                "event_stage": "GO_TO_ENDING",
                "options": [copy.deepcopy(option)],
            },
        }
        state = {
            "phase": "EVENT", "options": [copy.deepcopy(option)],
            "game_state": copy.deepcopy(game),
        }
        record = {
            "authoritative_state_before": {"game_state": copy.deepcopy(game)}
        }

        produced = autoplay._structured_option_consequence(
            "EVENT", target, "Continue", state=state,
        )
        expected = independent_oracle._expected_visible_consequence(
            "EVENT",
            {"choice_index": 0, "raw_text": "Continue", "target": target},
            record,
        )
        matched, mismatches = independent_oracle._consequence_matches_visible(
            produced, expected,
        )

        self.assertTrue(matched, mismatches)
        self.assertEqual(57, expected["hp_delta"])
        self.assertEqual("spire_heart_enter_act_four", expected["operation"])

    def test_indexed_event_identity_requires_the_complete_dialog_surface(self):
        event_id = "Golden Wing"
        pray = {
            "option_id": "golden-wing:pray", "choice_index": 0,
            "label": "Pray",
            "target": {
                "kind": "event_option", "event_id": event_id,
                "original_button_index": 0,
            },
        }
        leave = {
            "option_id": "golden-wing:leave", "choice_index": 1,
            "label": "Leave",
            "target": {
                "kind": "event_option", "event_id": event_id,
                "original_button_index": 2,
            },
        }
        state = {
            "phase": "EVENT", "options": [pray, leave],
            "game_state": {
                "ascension_level": 0, "max_hp": 80,
                "screen_state": {
                    "event_id": event_id,
                    "options": [
                        {"original_button_index": 0, "disabled": False},
                        {"original_button_index": 1, "disabled": True},
                        {"original_button_index": 2, "disabled": False},
                    ],
                },
            },
        }
        consequence = autoplay._structured_option_consequence(
            "EVENT", pray["target"], pray["label"], state=state,
        )
        self.assertEqual(-7, consequence["hp_delta"])
        self.assertEqual("golden_wing_pray_remove", consequence["operation"])

        terminal = copy.deepcopy(state)
        terminal["options"] = [copy.deepcopy(pray)]
        terminal["game_state"]["screen_state"]["options"] = [{
            "original_button_index": 0, "disabled": False,
        }]
        consequence = autoplay._structured_option_consequence(
            "EVENT", pray["target"], "Leave", state=terminal,
        )
        self.assertIsNone(consequence["hp_delta"])
        self.assertNotIn("operation", consequence)
        self.assertEqual(
            ["event outcome is unclassified without typed protocol mechanics"],
            consequence["uncertainty"],
        )
        self.assertEqual(
            "unresolved",
            consequence["uncertainty_classification"]["status"],
        )

    def test_golden_wing_terminal_legacy_projection_is_independently_typed(self):
        event_id = "Golden Wing"
        target = {
            "kind": "event_option",
            "event_id": event_id,
            "original_button_index": 0,
            "audit_projection_version": autoplay.AUDIT_PROJECTION_VERSION,
        }
        option = {
            "option_id": "golden-wing:terminal",
            "choice_index": 0,
            "label": "Continue",
            "target": copy.deepcopy(target),
        }
        screen = {
            "event_id": event_id,
            "event_class": "com.megacrit.cardcrawl.events.exordium.GoldenWing",
            "options": [{"original_button_index": 0, "disabled": False}],
        }
        before = {
            "current_hp": 40,
            "max_hp": 80,
            "gold": 100,
            "deck": [],
            "relics": [],
            "potions": [],
            "keys": {"ruby": False, "emerald": False, "sapphire": False},
            "screen_state": screen,
        }
        record = bound(
            "decision",
            before_seq=10,
            after_seq=11,
            phase="EVENT",
            available_options_before=[copy.deepcopy(option)],
            chosen_option_before=copy.deepcopy(option),
            decision_outcome={
                "phase_before": "EVENT",
                "phase_after": "GRID",
            },
            authoritative_state_before=authoritative_state(
                10,
                observable=before,
                room_phase="EVENT",
                existing={"game_state": {"screen_state": screen}},
            ),
        )
        state = {
            "phase": "EVENT",
            "options": [copy.deepcopy(option)],
            "game_state": copy.deepcopy(before),
        }
        legacy = autoplay._structured_option_consequence(
            "EVENT", target, option["label"], state=state,
        )
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", option, record,
        )
        self.assertEqual("goldenwing_dialog_advance_noop", expected["operation"])
        self.assertTrue(expected["leave"])
        matched, mismatches = independent_oracle._consequence_matches_visible(
            legacy, expected, record,
        )
        self.assertTrue(matched, mismatches)
        self.assertTrue(
            independent_oracle._classified_protocol_uncertainty(
                "EVENT", option, legacy, record,
            )
        )

    def test_numeric_domain_accepts_legacy_min_max_spelling(self):
        self.assertEqual(
            (50.0, 80.0),
            independent_oracle._numeric_domain_bounds({"min": 50, "max": 80}),
        )
        self.assertEqual(
            (0.0, 30.0),
            independent_oracle._numeric_domain_bounds([0, 30]),
        )
        self.assertIsNone(
            independent_oracle._numeric_domain_bounds({"min": 80, "max": 50}),
        )
        self.assertIsNone(
            independent_oracle._numeric_domain_bounds([30, 0]),
        )

    def test_neow_dialog_metadata_is_typed_and_legacy_omission_is_accepted(self):
        contract = {
            "contract_kind": "NEOW_DIALOG_ADVANCE",
            "contract_version": 1,
            "resource_effect": "NONE",
            "screen_num": 1,
        }
        target = {
            "kind": "event_option", "event_id": "Neow Event",
            "neow_contract": contract,
            "mechanism_id": autoplay._neow_mechanism_id(contract),
        }
        option = {
            "option_id": "neow:dialog", "choice_index": 0,
            "label": "Talk", "target": target,
        }
        state = {
            "phase": "NEOW", "options": [option],
            "game_state": {
                "ascension_level": 0, "current_hp": 80,
                "max_hp": 80, "gold": 99,
            },
        }
        current = autoplay._structured_option_consequence(
            "NEOW", target, "Talk", state=state,
        )
        self.assertIsNone(current["reward_kind"])
        self.assertEqual("NONE", current["drawback_kind"])
        self.assertEqual(
            {"screen_num": 1, "resource_effect": "NONE"},
            current["parameters"],
        )

        record = {
            "phase": "NEOW",
            "available_options_before": [autoplay.compact_option(option)],
            "authoritative_state_before": authoritative_state(
                10,
                observable={
                    "current_hp": 80, "max_hp": 80, "gold": 99,
                    "deck": [], "relics": [], "potions": [],
                },
                room_phase="EVENT",
            ),
        }
        raw = {
            "choice_id": "neow:dialog", "choice_index": 0,
            "target": copy.deepcopy(target), "selected": True,
            "raw_text": "Talk",
            "consequences": copy.deepcopy(current),
        }
        expected = independent_oracle._expected_visible_consequence(
            "NEOW", raw, record,
        )
        legacy = copy.deepcopy(current)
        for field in ("reward_kind", "drawback_kind", "parameters"):
            legacy.pop(field)
        clear, mismatches = independent_oracle._consequence_matches_visible(
            legacy, expected,
        )
        self.assertTrue(clear, mismatches)

        producer = {
            "producer_consequence_raw": {
                "present": True,
                "value": {
                    "event_id": "Neow Event",
                    "operation": "neow_dialog_advance",
                    "reward_kind": None,
                    "drawback_kind": "NONE",
                    "parameters": {
                        "screen_num": 1, "resource_effect": "NONE",
                    },
                },
            },
            "producer_consequence_claim": {
                "event_id": "Neow Event",
                "operation": "neow_dialog_advance",
                "reward_kind": None,
                "drawback_kind": "NONE",
                "parameters": {
                    "screen_num": 1, "resource_effect": "NONE",
                },
            },
            "producer_scoring_facts": {},
            "unclassified_producer_fields": [],
        }
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, producer, expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        legacy_producer = copy.deepcopy(producer)
        legacy_producer["producer_consequence_raw"]["value"]["event_id"] = (
            "neow"
        )
        legacy_producer["producer_consequence_claim"]["event_id"] = "neow"
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, legacy_producer, expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        non_neow_record = copy.deepcopy(record)
        non_neow_record["phase"] = "EVENT"
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                non_neow_record, raw, legacy_producer, expected, None,
            )
        )
        self.assertEqual([], unresolved)
        self.assertEqual("event_id", contradictions[0]["field"])

    def test_unselected_dollys_mirror_shop_candidate_is_fully_typed(self):
        boot = {
            "id": "Boot", "name": "The Boot", "counter": -1,
            "price": 147,
        }
        mirror = {
            "id": "DollysMirror", "name": "Dolly's Mirror",
            "counter": -1, "price": 157,
        }
        options = [
            {
                "option_id": "shop:boot", "choice_index": 4,
                "label": "The Boot",
                "target": {"kind": "relic", "item": boot},
            },
            {
                "option_id": "shop:mirror", "choice_index": 7,
                "label": "Dolly's Mirror",
                "target": {"kind": "relic", "item": mirror},
            },
        ]
        before = {
            "ascension_level": 0, "gold": 200,
            "relics": [], "screen_state": {},
        }
        state = {
            "phase": "SHOP_SCREEN", "options": copy.deepcopy(options),
            "available_commands": ["choose", "return"],
            "game_state": copy.deepcopy(before),
        }
        candidates = []
        for option, score in zip(options, (20, 10)):
            consequence = autoplay._structured_option_consequence(
                "SHOP_SCREEN", option["target"], option["label"],
                state=state,
            )
            candidates.append(production_candidate(
                option["option_id"], option["choice_index"], score,
                consequences={
                    key: copy.deepcopy(value)
                    for key, value in consequence.items()
                    if key in autoplay._PRODUCER_EFFECT_FIELDS
                },
            ))
        record = self.production_record(
            phase="SHOP_SCREEN", options=options,
            commands=["choose", "return"],
            payload={"action": "choose", "option_id": "shop:boot"},
            candidates=candidates, before=before,
            after={"gold": 53, "relics": [boot]},
        )

        report = self.audit(base_records(record))

        self.assertNotIn(
            "candidate_consequence_binding_mismatch",
            issue_kinds(report),
            report,
        )
        mirror_row = next(
            row for row in record["legal_choices_before"]
            if row["choice_id"] == "shop:mirror"
        )
        followup = mirror_row["consequences"]["future_costs"][0]
        self.assertEqual("duplicate", followup["operation"])
        self.assertEqual(1, followup["select_count"])

    def test_unselected_orrery_shop_candidate_is_independently_typed(self):
        tea_set = {
            "id": "Ancient Tea Set", "name": "Ancient Tea Set",
            "counter": -1, "price": 150,
        }
        orrery = {
            "id": "Orrery", "name": "Orrery", "counter": -1,
            "price": 153,
        }
        options = [
            {
                "option_id": "shop:tea-set", "choice_index": 8,
                "label": "Ancient Tea Set",
                "target": {
                    "kind": "relic", "item": tea_set,
                    "audit_projection_version": 3,
                },
            },
            {
                "option_id": "shop:orrery", "choice_index": 9,
                "label": "Orrery",
                "target": {
                    "kind": "relic", "item": orrery,
                    "audit_projection_version": 3,
                },
            },
        ]
        before = {
            "ascension_level": 0, "gold": 211,
            "relics": [], "screen_state": {},
        }
        state = {
            "phase": "SHOP_SCREEN", "options": copy.deepcopy(options),
            "available_commands": ["choose", "return"],
            "game_state": copy.deepcopy(before),
        }
        candidates = []
        for option, score in zip(options, (20, 10)):
            consequence = autoplay._structured_option_consequence(
                "SHOP_SCREEN", option["target"], option["label"],
                state=state,
            )
            candidates.append(production_candidate(
                option["option_id"], option["choice_index"], score,
                consequences={
                    key: copy.deepcopy(value)
                    for key, value in consequence.items()
                    if key in autoplay._PRODUCER_EFFECT_FIELDS
                },
            ))
        record = self.production_record(
            phase="SHOP_SCREEN", options=options,
            commands=["choose", "return"],
            payload={"action": "choose", "option_id": "shop:tea-set"},
            candidates=candidates, before=before,
            after={"gold": 61, "relics": [tea_set]},
        )

        report = self.audit(base_records(record))

        self.assertEqual(
            [],
            [
                item for item in report["unknowns"]
                if item.get("choice_id") == "shop:orrery"
            ],
            report,
        )
        self.assertEqual(
            [],
            [
                item for item in report["issues"]
                if item.get("choice_id") == "shop:orrery"
            ],
            report,
        )
        orrery_row = next(
            row for row in record["legal_choices_before"]
            if row["choice_id"] == "shop:orrery"
        )
        consequence = orrery_row["consequences"]
        self.assertEqual(-153, consequence["gold_delta"])
        self.assertEqual(153, consequence["current_cost"]["gold"])
        self.assertEqual(
            {
                "kind": "optional_card_reward_sequence",
                "relic_id": "Orrery",
                "card_pool": "CHARACTER",
                "reward_count": 5,
                "candidates_per_reward": 3,
                "select_count_per_reward": 1,
                "can_skip": True,
                "timing": "after_relic_purchase",
            },
            consequence["future_costs"][0],
        )

        modified_game = copy.deepcopy(before)
        modified_game["relics"] = [
            {"id": "QuestionCard"}, {"id": "BustedCrown"},
        ]
        modified_state = copy.deepcopy(state)
        modified_state["game_state"] = copy.deepcopy(modified_game)
        modified_raw = {
            "choice_id": "shop:orrery", "choice_index": 9,
            "raw_text": "Orrery", "target": options[1]["target"],
        }
        modified_production = autoplay._structured_option_consequence(
            "SHOP_SCREEN", options[1]["target"], "Orrery",
            state=modified_state,
        )
        modified_expected = (
            independent_oracle._expected_visible_consequence(
                "SHOP_SCREEN", modified_raw,
                {"authoritative_state_before": {
                    "game_state": modified_game,
                }},
            )
        )
        self.assertEqual(
            2,
            modified_production["future_costs"][0][
                "candidates_per_reward"
            ],
        )
        self.assertEqual(
            2,
            modified_expected["future_costs"][0][
                "candidates_per_reward"
            ],
        )

        legacy = copy.deepcopy(consequence)
        for field in (
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "potion_changes", "curse", "current_cost", "future_costs",
        ):
            legacy["field_knowledge"][field] = {
                "status": "unknown",
                "authority": "protocol_surface",
                "reason": "mechanism_not_classified",
            }
        legacy["hp_delta"] = None
        legacy["max_hp_delta"] = None
        legacy["gold_delta"] = None
        legacy["curse"] = {
            "gain": [], "remove": [], "probability": None,
            "omamori_applicable": None,
            "omamori_charges_consumed": None,
        }
        legacy["current_cost"] = {
            "gold": None, "hp": None, "max_hp": None,
        }
        legacy["future_costs"] = []
        legacy["uncertainty"] = []
        legacy["uncertainty_classification"] = {
            "status": "none",
            "authority": "production_mechanics_projection",
            "reason": "shop_relic_purchase",
        }
        raw = {
            "choice_id": "shop:orrery", "choice_index": 9,
            "raw_text": "Orrery", "target": options[1]["target"],
        }
        expected = independent_oracle._expected_visible_consequence(
            "SHOP_SCREEN", raw, record,
        )
        compatible, mismatches = (
            independent_oracle._consequence_matches_visible(
                legacy, expected, record,
            )
        )
        self.assertTrue(compatible, mismatches)

    @staticmethod
    def resequence_record(record, before_seq, after_seq):
        record["before_seq"] = before_seq
        record["after_seq"] = after_seq
        record["authoritative_state_before"]["state_seq"] = before_seq
        record["authoritative_state_after"]["state_seq"] = after_seq
        settlement = record.get("authoritative_choice_settlement")
        if isinstance(settlement, dict):
            settlement["before_seq"] = before_seq
            settlement["after_seq"] = after_seq
        return record

    def test_oracle_module_has_only_explicit_standard_library_imports(self):
        source = Path(independent_oracle.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                imported.add(str(node.module).split(".", 1)[0])

        self.assertEqual(
            {"collections", "copy", "hashlib", "json", "math", "re"}, imported
        )

    @staticmethod
    def mechanism_record(phase, before_game, after_game, *, options=None,
                         selected_choice_ids=None, action="choose"):
        return {
            "record_type": "decision",
            "phase": phase,
            "action": action,
            "before_seq": 10,
            "after_seq": 11,
            "available_options_before": copy.deepcopy(options or []),
            "selected_choice_ids": list(selected_choice_ids or []),
            "authoritative_state_before": authoritative_state(
                10, existing={"game_state": copy.deepcopy(before_game)},
            ),
            "authoritative_state_after": authoritative_state(
                11, existing={"game_state": copy.deepcopy(after_game)},
            ),
        }

    @staticmethod
    def black_star_reward(choice_index, relic_id):
        return {
            "choice_index": choice_index,
            "reward_type": "RELIC",
            "label": relic_id,
            "relic": {"id": relic_id, "name": relic_id},
        }

    @staticmethod
    def black_star_option(reward, *, option_id=None):
        relic_id = reward.get("relic", {}).get("id")
        choice_index = reward.get("choice_index")
        return {
            "option_id": (
                option_id
                if option_id is not None
                else f"reward:{choice_index}:{relic_id}"
            ),
            "choice_index": choice_index,
            "target": {
                "kind": "reward", "reward": copy.deepcopy(reward),
            },
        }

    def black_star_combat_parent(self, room_type="MonsterRoomElite"):
        game = {
            "room_phase": "COMBAT", "room_type": room_type,
            "relics": [{"id": "BlackStar", "name": "Black Star"}],
            "deck": [], "potions": [],
            "combat_state": {
                "turn": 3,
                "player": {"powers": []},
                "monsters": [{
                    "enemy_instance_id": "enemy:elite",
                    "id": "GremlinNob", "current_hp": 1,
                    "is_gone": False, "half_dead": False, "powers": [],
                }],
            },
        }
        record = self.mechanism_record(
            "COMBAT_TURN_3", game, game, action="play",
        )
        return self.resequence_record(record, 9, 10)

    def black_star_reward_record(
        self, rewards, *, options=None, room_type="MonsterRoomElite",
        before_seq=10, after_seq=11, after_rewards=None,
    ):
        before = {
            "room_phase": "COMPLETE", "room_type": room_type,
            "relics": [{"id": "BlackStar", "name": "Black Star"}],
            "deck": [], "potions": [],
            "screen_state": {"rewards": copy.deepcopy(rewards)},
        }
        after = copy.deepcopy(before)
        if after_rewards is not None:
            after["screen_state"]["rewards"] = copy.deepcopy(after_rewards)
        if options is None:
            options = [self.black_star_option(reward) for reward in rewards]
        record = self.mechanism_record(
            "COMBAT_REWARD", before, after, options=options,
        )
        return self.resequence_record(record, before_seq, after_seq)

    @staticmethod
    def mausoleum_event_contract(
        stage, original_button_index, *, ascension=0
    ):
        percent = 100 if ascension >= 15 else 50
        option_kind = {
            ("INTRO", 0): "OPEN",
            ("INTRO", 1): "LEAVE",
            ("RESULT", 0): "CONTINUE",
        }[(stage, original_button_index)]
        parameters = (
            {
                "random_relic_count": 1,
                "relic_selection_mode": (
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                ),
                "curse_card_id": "Writhe",
                "curse_probability_percent": percent,
            }
            if (stage, original_button_index) == ("INTRO", 0)
            else {}
        )
        return {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "The Mausoleum",
            "event_class": (
                "com.megacrit.cardcrawl.events.city.TheMausoleum"
            ),
            "event_stage": stage,
            "original_button_index": original_button_index,
            "option_kind": option_kind,
            "instance_parameters": {
                "curse_probability_percent": percent,
            },
            "parameters": parameters,
        }

    @staticmethod
    def mausoleum_writhe(uuid="mausoleum-writhe"):
        return {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
            "uuid": uuid, "card_instance_id": uuid,
        }

    def mausoleum_record(
        self, *, ascension=0, choice_index=0, before_deck=None,
        after_deck=None, before_relics=None, after_relics=None,
        before_potions=None, after_potions=None, before_keys=None,
        after_keys=None, before_hp=50, after_hp=50,
        before_max_hp=80, after_max_hp=80,
        before_gold=100, after_gold=100,
    ):
        writhe_preview = {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
        }
        open_contract = self.mausoleum_event_contract(
            "INTRO", 0, ascension=ascension
        )
        leave_contract = self.mausoleum_event_contract(
            "INTRO", 1, ascension=ascension
        )
        options = [
            {
                "option_id": "mausoleum:open", "choice_index": 0,
                "label": "localized text is not authority",
                "target": {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 0, "original_button_index": 0,
                    "event_contract": copy.deepcopy(open_contract),
                    "card": copy.deepcopy(writhe_preview),
                },
            },
            {
                "option_id": "mausoleum:leave", "choice_index": 1,
                "label": "leave",
                "target": {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 1, "original_button_index": 1,
                    "event_contract": copy.deepcopy(leave_contract),
                },
            },
        ]
        selected = options[choice_index]
        before_observable = {
            "current_hp": before_hp, "max_hp": before_max_hp,
            "gold": before_gold, "block": 0,
            "deck": copy.deepcopy(before_deck or []),
            "relics": copy.deepcopy(before_relics or []),
            "potions": copy.deepcopy(before_potions or []),
            "keys": copy.deepcopy(before_keys or {
                "ruby": False, "emerald": False, "sapphire": False,
            }),
        }
        after_observable = {
            "current_hp": after_hp, "max_hp": after_max_hp,
            "gold": after_gold, "block": 0,
            "deck": copy.deepcopy(
                after_deck
                if after_deck is not None else before_observable["deck"]
            ),
            "relics": copy.deepcopy(
                after_relics
                if after_relics is not None else before_observable["relics"]
            ),
            "potions": copy.deepcopy(
                after_potions
                if after_potions is not None else before_observable["potions"]
            ),
            "keys": copy.deepcopy(
                after_keys
                if after_keys is not None else before_observable["keys"]
            ),
        }
        before_state = authoritative_state(
            10, observable=before_observable, room_phase="EVENT",
            existing={"phase": "EVENT", "game_state": {
                "ascension_level": ascension,
                "screen_state": {
                    "event_id": "The Mausoleum",
                    "options": [
                        {
                            "choice_index": 0, "disabled": False,
                            "original_button_index": 0,
                            "event_contract": copy.deepcopy(open_contract),
                            "card": copy.deepcopy(writhe_preview),
                        },
                        {
                            "choice_index": 1, "disabled": False,
                            "original_button_index": 1,
                            "event_contract": copy.deepcopy(leave_contract),
                        },
                    ],
                },
            }},
        )
        after_state = authoritative_state(
            11, observable=after_observable, room_phase="EVENT",
            existing={
                "phase": "EVENT",
                "game_state": {"ascension_level": ascension},
            },
        )
        for state in (before_state, after_state):
            state["ascension_level"] = ascension
        return bound(
            "decision", before_seq=10, after_seq=11,
            phase="EVENT", action="choose", ascension_level=ascension,
            available_options_before=copy.deepcopy(options),
            selected_choice_ids=[selected["option_id"]],
            requested_target_id=selected["option_id"],
            resolved_target_id=selected["option_id"],
            authoritative_state_before=before_state,
            authoritative_state_after=after_state,
        )

    def mausoleum_followup_record(self, *, ascension=0):
        record = self.mausoleum_record(
            ascension=ascension, choice_index=1
        )
        contract = self.mausoleum_event_contract(
            "RESULT", 0, ascension=ascension
        )
        option = {
            "option_id": "mausoleum:continue",
            "choice_index": 0,
            "label": "adversarial: gain 999 gold and a boss relic",
            "target": {
                "kind": "event_option",
                "event_id": "The Mausoleum",
                "original_button_index": 0,
                "event_contract": copy.deepcopy(contract),
            },
        }
        record.update({
            "available_options_before": [copy.deepcopy(option)],
            "selected_choice_ids": [option["option_id"]],
            "requested_target_id": option["option_id"],
            "resolved_target_id": option["option_id"],
        })
        record["authoritative_state_before"]["game_state"][
            "screen_state"
        ] = {
            "event_id": "The Mausoleum",
            "options": [{
                "disabled": False,
                "choice_index": 0,
                "original_button_index": 0,
                "event_contract": copy.deepcopy(contract),
            }],
        }
        return record

    @staticmethod
    def stasis_card(card_id, uuid, rarity="COMMON"):
        return {
            "id": card_id, "name": card_id, "type": "ATTACK",
            "rarity": rarity, "upgrades": 0,
            "card_instance_id": uuid,
        }

    @staticmethod
    def stasis_game(monster, *, hand=None, draw=None, discard=None,
                    exhaust=None, limbo=None):
        return {
            "room_phase": "COMBAT",
            "relics": [], "deck": [], "potions": [],
            "combat_state": {
                "turn": 2,
                "player": {
                    "current_hp": 50, "max_hp": 80, "block": 0,
                    "energy": 3, "powers": [], "orbs": [],
                },
                "monsters": [copy.deepcopy(monster)],
                "hand": copy.deepcopy(hand or []),
                "draw_pile": copy.deepcopy(draw or []),
                "discard_pile": copy.deepcopy(discard or []),
                "exhaust_pile": copy.deepcopy(exhaust or []),
                "limbo": copy.deepcopy(limbo or []),
            },
        }

    def test_authoritative_snapshot_preserves_mechanism_evidence(self):
        held = self.stasis_card("Demon Form", "held-rare", "RARE")
        raw = {
            "protocol_version": 2,
            "phase": "COMBAT_TURN_2",
            "game_state": self.stasis_game({
                "enemy_instance_id": "enemy:orb", "id": "BronzeOrb",
                "current_hp": 20, "max_hp": 20, "is_gone": False,
                "half_dead": False,
                "powers": [{
                    "id": "Stasis", "name": "Stasis", "amount": -1,
                    "card": copy.deepcopy(held),
                }],
            }, limbo=[held]),
        }
        snapshot = autoplay.authoritative_state_snapshot(raw)

        power = snapshot["game_state"]["combat_state"]["monsters"][0][
            "powers"
        ][0]
        self.assertEqual("held-rare", power["card"]["card_instance_id"])
        self.assertEqual(
            "held-rare",
            snapshot["game_state"]["combat_state"]["limbo"][0][
                "card_instance_id"
            ],
        )
        context = autoplay.combat_trace_context(
            raw["game_state"], phase="COMBAT_TURN_2",
            attempt_id=ATTEMPT,
        )
        self.assertEqual(
            "held-rare",
            context["monsters_before"][0]["powers"][0]["card"][
                "card_instance_id"
            ],
        )
        self.assertEqual(
            ["held-rare"],
            [card["card_instance_id"] for card in context["limbo_before"]],
        )

        reward_raw = {
            "protocol_version": 2, "phase": "CARD_REWARD",
            "game_state": {
                "screen_state": {
                    "cards": [held], "bowl_available": True,
                    "skip_available": True,
                },
            },
        }
        reward = autoplay.authoritative_state_snapshot(reward_raw)
        self.assertEqual(
            ["held-rare"],
            [
                card["card_instance_id"]
                for card in reward["game_state"]["screen_state"]["cards"]
            ],
        )

    def test_busted_crown_reward_count_and_missing_evidence_fail_closed(self):
        def reward_card(card_id, uuid):
            return self.stasis_card(card_id, uuid, "UNCOMMON")

        card_a = reward_card("Uppercut", "reward-a")
        card_b = reward_card("Shockwave", "reward-b")
        parent = {
            "record_type": "decision", "phase": "COMBAT_REWARD",
            "before_seq": 9, "after_seq": 10,
            "selected_choice_ids": ["reward:card"],
            "available_options_before": [{
                "option_id": "reward:card", "choice_index": 0,
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "CARD"},
                },
            }],
        }
        before = {
            "relics": [
                {"id": "BustedCrown", "counter": -1},
                {"id": "QuestionCard", "counter": -1},
            ],
            "deck": [], "potions": [],
            "screen_state": {
                "cards": [copy.deepcopy(card_a), copy.deepcopy(card_b)],
                "bowl_available": True, "skip_available": True,
            },
        }
        options = [
            {
                "option_id": f"card:{card['card_instance_id']}",
                "choice_index": index,
                "target": {
                    "kind": "card",
                    "card_instance_id": card["card_instance_id"],
                    "card": copy.deepcopy(card),
                },
            }
            for index, card in enumerate((card_a, card_b))
        ] + [{
            "option_id": "bowl", "choice_index": 2,
            "target": {"kind": "bowl"},
        }]
        current = self.mechanism_record(
            "CARD_REWARD", before, before, options=options,
            selected_choice_ids=["card:reward-a"],
        )
        report = independent_oracle._audit_base_game_mechanisms(
            [parent, current]
        )
        self.assertEqual(1, report["eligible"])
        self.assertEqual(1, report["evaluated"])
        self.assertEqual("clear", report["status"])
        self.assertEqual(
            report["eligible"],
            report["evaluated"] + report["issue_count"]
            + report["eligible_unknown_count"],
        )
        self.assertEqual([], report["issues"])
        self.assertEqual(2, report["observations"][0]["observed_card_count"])

        single_before = copy.deepcopy(before)
        single_before["relics"] = [{"id": "BustedCrown", "counter": -1}]
        single_before["screen_state"]["cards"] = [copy.deepcopy(card_a)]
        single_option = [copy.deepcopy(options[0])]
        single = self.mechanism_record(
            "CARD_REWARD", single_before, single_before,
            options=single_option,
            selected_choice_ids=["card:reward-a"],
        )
        dream_parent = {
            "record_type": "decision", "phase": "REST",
            "before_seq": 9, "after_seq": 10,
            "selected_choice_ids": ["rest:0"],
            "available_options_before": [{
                "option_id": "rest:0", "choice_index": 0,
                "target": {"kind": "rest", "rest_option": "REST"},
            }],
            "authoritative_state_before": authoritative_state(
                9, existing={"game_state": {
                    "relics": [{"id": "DreamCatcher", "counter": -1}],
                }},
            ),
        }
        dream_report = independent_oracle._audit_base_game_mechanisms(
            [dream_parent, single]
        )
        self.assertEqual(1, dream_report["evaluated"])
        self.assertEqual(
            "dream_catcher_reward_cards",
            dream_report["observations"][0]["source"],
        )

        orrery_parent = {
            "record_type": "decision", "phase": "SHOP_SCREEN",
            "before_seq": 9, "after_seq": 10,
            "selected_choice_ids": ["shop:orrery"],
            "available_options_before": [{
                "option_id": "shop:orrery", "choice_index": 0,
                "target": {
                    "kind": "relic", "item": {"id": "Orrery"},
                },
            }],
        }
        orrery_report = independent_oracle._audit_base_game_mechanisms(
            [orrery_parent, single]
        )
        self.assertEqual(1, orrery_report["evaluated"])
        self.assertEqual(
            "orrery_reward_cards",
            orrery_report["observations"][0]["source"],
        )

        stale_parent = copy.deepcopy(parent)
        stale_parent["after_seq"] = 8
        stale_report = independent_oracle._audit_base_game_mechanisms(
            [stale_parent, current]
        )
        self.assertEqual(
            {"busted_crown_reward_source_unproven"},
            {item["kind"] for item in stale_report["unknowns"]},
        )

        too_many = copy.deepcopy(current)
        card_c = reward_card("Clash", "reward-c")
        too_many["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["cards"].append(card_c)
        too_many["available_options_before"].insert(2, {
            "option_id": "card:reward-c", "choice_index": 2,
            "target": {
                "kind": "card", "card_instance_id": "reward-c",
                "card": card_c,
            },
        })
        wrong = independent_oracle._audit_base_game_mechanisms(
            [parent, too_many]
        )
        self.assertEqual(
            {"busted_crown_reward_count_mismatch"},
            {item["kind"] for item in wrong["issues"]},
        )

        missing = copy.deepcopy(current)
        missing["authoritative_state_before"]["game_state"].pop(
            "screen_state"
        )
        unknown = independent_oracle._audit_base_game_mechanisms(
            [parent, missing]
        )
        self.assertEqual(
            {"busted_crown_reward_cards_missing"},
            {item["kind"] for item in unknown["unknowns"]},
        )

    def test_black_star_elite_rewards_bind_twice_independent_of_order(self):
        anchor = self.black_star_reward(2, "Anchor")
        data_disk = self.black_star_reward(5, "DataDisk")
        parent = self.black_star_combat_parent()
        current = self.black_star_reward_record(
            [data_disk, anchor],
            options=[
                self.black_star_option(anchor),
                self.black_star_option(data_disk),
            ],
            after_rewards=[anchor],
        )
        # This is the next controller decision after one reward was consumed.
        # Its one remaining relic must not be mistaken for the initial group.
        continuation = self.black_star_reward_record(
            [anchor], before_seq=11, after_seq=12,
        )

        report = independent_oracle._audit_base_game_mechanisms(
            [parent, current, continuation]
        )

        self.assertEqual(1, report["eligible"])
        self.assertEqual(1, report["evaluated"])
        self.assertEqual("clear", report["status"])
        self.assertEqual([], report["issues"])
        self.assertEqual([], report["unknowns"])
        observation = report["observations"][0]
        self.assertEqual(
            "black_star_elite_relic_rewards", observation["mechanism"]
        )
        self.assertEqual(2, observation["observed_relic_count"])
        self.assertEqual(
            [[2, "anchor"], [5, "datadisk"]],
            [list(binding) for binding in observation["stable_bindings"]],
        )

        reordered = self.black_star_reward_record(
            [anchor, data_disk],
            options=[
                self.black_star_option(data_disk),
                self.black_star_option(anchor),
            ],
        )
        reordered_report = independent_oracle._audit_base_game_mechanisms(
            [parent, reordered]
        )
        self.assertEqual("clear", reordered_report["status"])
        self.assertEqual(
            observation["stable_bindings"],
            reordered_report["observations"][0]["stable_bindings"],
        )

    def test_black_star_count_duplicate_and_wrong_binding_are_issues(self):
        self.assertIn(
            "black_star_elite_reward_mismatch",
            independent_oracle.DISAGREEMENT_KINDS,
        )
        anchor = self.black_star_reward(2, "Anchor")
        data_disk = self.black_star_reward(5, "DataDisk")
        parent = self.black_star_combat_parent()

        single = self.black_star_reward_record([anchor])
        duplicate_anchor = self.black_star_reward(2, "Anchor")
        duplicate_disk = self.black_star_reward(2, "DataDisk")
        duplicate = self.black_star_reward_record(
            [duplicate_anchor, duplicate_disk],
            options=[
                self.black_star_option(duplicate_anchor),
                self.black_star_option(duplicate_disk),
            ],
        )
        wrong_disk = self.black_star_reward(5, "Orichalcum")
        wrong = self.black_star_reward_record(
            [anchor, data_disk],
            options=[
                self.black_star_option(anchor),
                self.black_star_option(wrong_disk),
            ],
        )

        cases = (
            (single, "relic_reward_count_mismatch"),
            (duplicate, "duplicate_relic_reward_binding"),
            (wrong, "protocol_relic_binding_mismatch"),
        )
        for record, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                report = independent_oracle._audit_base_game_mechanisms(
                    [parent, record]
                )
                self.assertEqual("issues", report["status"])
                self.assertEqual(1, report["issue_count"])
                self.assertEqual(
                    "black_star_elite_reward_mismatch",
                    report["issues"][0]["kind"],
                )
                self.assertEqual(
                    expected_reason, report["issues"][0]["reason"]
                )

    def test_black_star_missing_source_or_screen_is_unknown_normal_is_na(self):
        anchor = self.black_star_reward(2, "Anchor")
        data_disk = self.black_star_reward(5, "DataDisk")
        current = self.black_star_reward_record([anchor, data_disk])

        missing_source = independent_oracle._audit_base_game_mechanisms(
            [current]
        )
        self.assertEqual("inconclusive", missing_source["status"])
        self.assertEqual(
            {"black_star_elite_source_unproven"},
            {item["kind"] for item in missing_source["unknowns"]},
        )

        missing_screen = copy.deepcopy(current)
        missing_screen["authoritative_state_before"]["game_state"].pop(
            "screen_state"
        )
        missing_screen_report = (
            independent_oracle._audit_base_game_mechanisms([
                self.black_star_combat_parent(), missing_screen,
            ])
        )
        self.assertEqual("inconclusive", missing_screen_report["status"])
        self.assertEqual(
            {"black_star_reward_screen_missing"},
            {item["kind"] for item in missing_screen_report["unknowns"]},
        )

        normal_parent = self.black_star_combat_parent("MonsterRoom")
        normal = self.black_star_reward_record(
            [anchor], room_type="MonsterRoom",
        )
        normal_report = independent_oracle._audit_base_game_mechanisms(
            [normal_parent, normal]
        )
        self.assertEqual("not_applicable", normal_report["status"])
        self.assertEqual(0, normal_report["eligible"])

    def test_mausoleum_expected_trade_contract_a0_a15_omamori_and_leave(self):
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        for ascension, probability, outcome_count in (
            (0, 0.5, 2), (15, 1.0, 1),
        ):
            with self.subTest(ascension=ascension):
                record = self.mausoleum_record(
                    ascension=ascension, after_relics=[anchor],
                    after_deck=(
                        [self.mausoleum_writhe()] if probability == 1.0 else []
                    ),
                )
                evidence = independent_oracle.mausoleum_expected_trade_contract(
                    record
                )
                self.assertEqual("clear", evidence["status"])
                contract = evidence["expected_trade_contract"]
                self.assertEqual(
                    "independent_event_mechanics_v1", contract["authority"]
                )
                self.assertEqual(
                    "base_game_non_boss_relic_pool",
                    contract["guaranteed_benefit"]["domain"],
                )
                self.assertEqual(
                    ["BOSS"],
                    contract["guaranteed_benefit"]["excluded_rarities"],
                )
                self.assertEqual(
                    probability, contract["risk"]["attempt_probability"]
                )
                self.assertEqual(outcome_count, len(contract["outcomes"]))

        omamori = {"id": "Omamori", "name": "Omamori", "counter": 1}
        protected = self.mausoleum_record(
            before_relics=[omamori],
            after_relics=[{**omamori, "counter": 0}, anchor],
        )
        risk = independent_oracle.mausoleum_expected_trade_contract(
            protected
        )["expected_trade_contract"]["risk"]
        self.assertEqual(0.5, risk["attempt_probability"])
        self.assertEqual(0.0, risk["effective_curse_probability"])
        self.assertEqual(0.5, risk["omamori_charge_loss_probability"])
        self.assertEqual(1, risk["omamori_counter_before"])

        leave = self.mausoleum_record(choice_index=1)
        leave_contract = independent_oracle.mausoleum_expected_trade_contract(
            leave
        )["expected_trade_contract"]
        self.assertEqual("mausoleum_leave", leave_contract["operation"])
        self.assertIsNone(leave_contract["guaranteed_benefit"])
        self.assertIsNone(leave_contract["risk"])

    def test_mausoleum_result_continue_is_exact_authoritative_noop(self):
        for ascension, percent in ((0, 50), (15, 100)):
            with self.subTest(ascension=ascension):
                record = self.mausoleum_followup_record(
                    ascension=ascension
                )
                evidence = independent_oracle.mausoleum_trade_evidence(
                    record
                )
                self.assertEqual("clear", evidence["status"], evidence)
                contract = evidence["expected_trade_contract"]
                realized = evidence["realized_settlement"]
                self.assertEqual("RESULT", contract["event_stage"])
                self.assertEqual(0, contract["original_button_index"])
                self.assertEqual(
                    "mausoleum_dialog_advance_noop",
                    contract["operation"],
                )
                self.assertEqual(
                    percent,
                    contract["typed_event_contract"][
                        "instance_parameters"
                    ]["curse_probability_percent"],
                )
                self.assertEqual(
                    "mausoleum_dialog_advance_noop",
                    realized["operation"],
                )
                delta = realized["observable_delta"]
                self.assertEqual(0, delta["hp_delta"])
                self.assertEqual(0, delta["max_hp_delta"])
                self.assertEqual(0, delta["gold_delta"])
                self.assertEqual(0, delta["block_delta"])
                for field in ("deck", "relics", "potions"):
                    self.assertEqual(
                        {"added": [], "removed": [], "changed": []},
                        delta[field],
                    )
                attempted, reason, contradiction = (
                    independent_oracle._oracle_known_curse_attempt(record)
                )
                self.assertEqual((0, None, False), (
                    attempted, reason, contradiction,
                ))

                forged = copy.deepcopy(record)
                forged["available_options_before"][0]["label"] = (
                    "localized fake: lose all HP"
                )
                forged["decision_outcome"] = {"gold_delta": 999}
                forged["authoritative_choice_settlement"] = {
                    "status": "clear", "authority": "producer_forgery",
                }
                self.assertEqual(
                    evidence,
                    independent_oracle.mausoleum_trade_evidence(forged),
                )

                report = independent_oracle._audit_base_game_mechanisms(
                    [record]
                )
                self.assertEqual("clear", report["status"], report)
                self.assertEqual(1, report["evaluated"])

    def test_mausoleum_result_noop_rejects_contract_and_surface_tampering(self):
        baseline = self.mausoleum_followup_record()
        cases = []

        missing_screen_contract = copy.deepcopy(baseline)
        del missing_screen_contract["authoritative_state_before"][
            "game_state"
        ]["screen_state"]["options"][0]["event_contract"]
        cases.append((
            "missing_screen_contract", missing_screen_contract,
            "inconclusive",
        ))

        missing_target_contract = copy.deepcopy(baseline)
        del missing_target_contract["available_options_before"][0][
            "target"
        ]["event_contract"]
        cases.append((
            "missing_target_contract", missing_target_contract,
            "inconclusive",
        ))

        wrong_percent = copy.deepcopy(baseline)
        wrong_percent["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["options"][0]["event_contract"]["instance_parameters"][
            "curse_probability_percent"
        ] = 100
        cases.append(("wrong_percent", wrong_percent, "issues"))

        forged_target = copy.deepcopy(baseline)
        forged_target["available_options_before"][0]["target"][
            "event_contract"
        ]["option_kind"] = "OPEN"
        cases.append(("forged_target", forged_target, "issues"))

        wrong_original = copy.deepcopy(baseline)
        wrong_original["available_options_before"][0]["target"][
            "original_button_index"
        ] = 1
        cases.append(("wrong_original", wrong_original, "issues"))

        wrong_phase = copy.deepcopy(baseline)
        wrong_phase["authoritative_state_before"]["phase"] = "CARD_REWARD"
        cases.append(("wrong_authoritative_phase", wrong_phase, "issues"))

        disabled = copy.deepcopy(baseline)
        disabled_option = disabled["authoritative_state_before"][
            "game_state"
        ]["screen_state"]["options"][0]
        disabled_option["disabled"] = True
        disabled_option.pop("choice_index")
        cases.append(("disabled", disabled, "issues"))

        extra_surface = copy.deepcopy(baseline)
        extra = copy.deepcopy(
            extra_surface["authoritative_state_before"]["game_state"][
                "screen_state"
            ]["options"][0]
        )
        extra["original_button_index"] = 1
        extra["choice_index"] = 1
        extra["event_contract"]["original_button_index"] = 1
        extra_surface["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["options"].append(extra)
        cases.append(("extra_surface", extra_surface, "issues"))

        for name, record, expected_status in cases:
            with self.subTest(name=name):
                evidence = independent_oracle.mausoleum_trade_evidence(
                    record
                )
                self.assertEqual(
                    expected_status, evidence["status"], evidence
                )
                self.assertIsNone(evidence["realized_settlement"])

    def test_mausoleum_result_noop_requires_zero_persistent_delta(self):
        baseline = self.mausoleum_followup_record()
        after_game = lambda record: record["authoritative_state_after"][
            "game_state"
        ]
        cases = []
        hp = copy.deepcopy(baseline)
        after_game(hp)["current_hp"] -= 1
        cases.append(("hp", hp))
        gold = copy.deepcopy(baseline)
        after_game(gold)["gold"] += 1
        cases.append(("gold", gold))
        deck = copy.deepcopy(baseline)
        after_game(deck)["deck"].append({
            "id": "Strike_R", "type": "ATTACK", "rarity": "BASIC",
            "upgrades": 0, "card_instance_id": "unexpected-card",
        })
        cases.append(("deck", deck))
        relic = copy.deepcopy(baseline)
        after_game(relic)["relics"].append({"id": "Anchor"})
        cases.append(("relic", relic))
        potion = copy.deepcopy(baseline)
        after_game(potion)["potions"].append({
            "id": "Dexterity Potion",
            "potion_instance_id": "unexpected-potion",
        })
        cases.append(("potion", potion))
        key = copy.deepcopy(baseline)
        after_game(key)["keys"]["ruby"] = True
        cases.append(("key", key))
        floor = copy.deepcopy(baseline)
        after_game(floor)["floor"] += 1
        cases.append(("floor", floor))
        for name, record in cases:
            with self.subTest(name=name):
                evidence = independent_oracle.mausoleum_trade_evidence(
                    record
                )
                self.assertEqual("issues", evidence["status"], evidence)
                self.assertEqual(
                    "mausoleum_dialog_noop_observable_delta_nonzero",
                    evidence["reason"],
                )
                self.assertIsNone(evidence["realized_settlement"])

    def test_cursed_tome_typed_contract_is_independently_projected(self):
        event_id = "Cursed Tome"
        event_class = "com.megacrit.cardcrawl.events.city.CursedTome"
        pool = ["Necronomicon", "Enchiridion", "Nilry's Codex"]

        def option(stage, original, kind, parameters, damage=0):
            contract = {
                "contract_version": 1,
                "contract_kind": "BASE_GAME_EVENT_OPTION",
                "event_id": event_id,
                "event_class": event_class,
                "event_stage": stage,
                "original_button_index": original,
                "option_kind": kind,
                "parameters": copy.deepcopy(parameters),
                "instance_parameters": {
                    "final_hp_loss": 10,
                    "damage_taken": damage,
                    "random_relic_pool": list(pool),
                },
            }
            encoded = json.dumps(
                contract, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            mechanism_id = (
                "event-mechanism:" + hashlib.sha256(encoded).hexdigest()[:20]
            )
            screen = {
                "disabled": False,
                "choice_index": original,
                "original_button_index": original,
                "event_contract": copy.deepcopy(contract),
            }
            target = {
                "kind": "event_option", "event_id": event_id,
                "choice_index": original,
                "original_button_index": original,
                "event_contract": copy.deepcopy(contract),
                "mechanism_id": mechanism_id,
            }
            return screen, target

        intro = [
            option("INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN", {
                "future_hp_loss_to_complete": 16,
                "random_relic_count": 1,
                "reward_surface": "COMBAT_REWARD",
                "selection_mode": "UNIFORM_MISC_RNG",
            }),
            option("INTRO", 1, "LEAVE", {}),
        ]
        proceed = [option("END", 0, "PROCEED", {})]

        def assert_matches(screen_and_targets, selected_index):
            screen_options = [item[0] for item in screen_and_targets]
            target = screen_and_targets[selected_index][1]
            game = {
                "ascension_level": 0,
                "relics": [],
                "screen_state": {
                    "event_id": event_id,
                    "options": copy.deepcopy(screen_options),
                },
            }
            record = {
                "phase": "EVENT",
                "authoritative_state_before": authoritative_state(
                    91,
                    existing={"phase": "EVENT", "game_state": game},
                ),
            }
            raw = {
                "choice_index": selected_index,
                "target": copy.deepcopy(target),
                "raw_text": "untrusted localized event text",
            }
            expected = independent_oracle._expected_visible_consequence(
                "EVENT", raw, record
            )
            producer = autoplay._structured_option_consequence(
                "EVENT", copy.deepcopy(target), raw["raw_text"],
                state={"phase": "EVENT", "game_state": game},
            )
            matched, fields = independent_oracle._consequence_matches_visible(
                producer, expected
            )
            self.assertTrue(matched, fields)
            return expected

        enter = assert_matches(intro, 0)
        leave = assert_matches(intro, 1)
        dialog = assert_matches(proceed, 0)
        self.assertEqual("cursed_tome_enter_random_book_chain", enter["operation"])
        self.assertEqual("cursed_tome_leave", leave["operation"])
        self.assertEqual("cursed_tome_dialog_advance_noop", dialog["operation"])

        invalid_screen, invalid_target = intro[1]
        invalid_target = copy.deepcopy(invalid_target)
        invalid_target["mechanism_id"] = "event-mechanism:forged"
        game = {
            "ascension_level": 0,
            "relics": [],
            "screen_state": {
                "event_id": event_id,
                "options": [copy.deepcopy(item[0]) for item in intro],
            },
        }
        invalid = independent_oracle._expected_visible_consequence(
            "EVENT",
            {"choice_index": 1, "target": invalid_target},
            {
                "phase": "EVENT",
                "authoritative_state_before": authoritative_state(
                    92,
                    existing={"phase": "EVENT", "game_state": game},
                ),
            },
        )
        self.assertIn(
            "typed_event_mechanism_id_mismatch", invalid["uncertainty"]
        )

    def test_designer_typed_contract_is_independently_projected(self):
        event_id = "Designer"
        event_class = "com.megacrit.cardcrawl.events.shrines.Designer"
        instance = {
            "adjust_cost": 40,
            "adjustment_upgrades_one": True,
            "clean_up_cost": 60,
            "clean_up_removes_cards": True,
            "full_service_cost": 90,
            "hp_loss": 3,
        }
        rows = (
            ("ADJUSTMENT_GRID_UPGRADE", {
                "gold_cost": 40, "upgrade_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("CLEAN_UP_GRID_PURGE", {
                "gold_cost": 60, "purge_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            }),
            ("FULL_SERVICE", {
                "gold_cost": 90, "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE",
            }),
            ("PUNCH_AND_LEAVE", {"hp_loss": 3}),
        )
        options = []
        targets = []
        for index, (kind, parameters) in enumerate(rows):
            contract = {
                "contract_version": 1,
                "contract_kind": "BASE_GAME_EVENT_OPTION",
                "event_id": event_id,
                "event_class": event_class,
                "event_stage": "MAIN",
                "original_button_index": index,
                "option_kind": kind,
                "parameters": copy.deepcopy(parameters),
                "instance_parameters": copy.deepcopy(instance),
            }
            encoded = json.dumps(
                contract, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            targets.append({
                "kind": "event_option", "event_id": event_id,
                "choice_index": index, "original_button_index": index,
                "event_contract": copy.deepcopy(contract),
                "mechanism_id": (
                    "event-mechanism:"
                    + hashlib.sha256(encoded).hexdigest()[:20]
                ),
                "audit_projection_version": 3,
            })
            options.append({
                "disabled": False, "choice_index": index,
                "original_button_index": index,
                "event_contract": copy.deepcopy(contract),
            })

        game = {
            "ascension_level": 0,
            "screen_state": {
                "event_id": event_id,
                "options": copy.deepcopy(options),
            },
        }
        record = {
            "phase": "EVENT",
            "authoritative_state_before": authoritative_state(
                468822,
                existing={"phase": "EVENT", "game_state": game},
            ),
        }
        expected_operations = (
            "designer_adjustment_grid_upgrade",
            "designer_clean_up_grid_purge",
            "designer_full_service",
            "designer_punch_and_leave",
        )
        for index, target in enumerate(targets):
            with self.subTest(index=index):
                raw = {
                    "choice_index": index,
                    "target": copy.deepcopy(target),
                    "raw_text": "untrusted localized text",
                }
                expected = independent_oracle._expected_visible_consequence(
                    "EVENT", raw, record
                )
                producer = autoplay._structured_option_consequence(
                    "EVENT", copy.deepcopy(target), raw["raw_text"],
                    state={"phase": "EVENT", "game_state": game},
                )
                matched, fields = (
                    independent_oracle._consequence_matches_visible(
                        producer, expected, record
                    )
                )
                self.assertTrue(matched, fields)
                self.assertEqual(expected_operations[index], expected["operation"])

    def test_addict_random_relic_numeric_effects_are_bounded_domains(self):
        options = [
            {"choice_index": index, "original_button_index": index,
             "disabled": False}
            for index in range(3)
        ]
        target = {
            "kind": "event_option", "event_id": "Addict",
            "choice_index": 0, "original_button_index": 0,
            "audit_projection_version": 3,
        }
        game = {
            "ascension_level": 0,
            "screen_state": {"event_id": "Addict", "options": options},
        }
        record = {
            "phase": "EVENT",
            "authoritative_state_before": authoritative_state(
                468995,
                existing={"phase": "EVENT", "game_state": game},
            ),
        }
        raw = {
            "choice_index": 0,
            "target": copy.deepcopy(target),
            "raw_text": "buy relic",
        }
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )
        producer = autoplay._structured_option_consequence(
            "EVENT", copy.deepcopy(target), "buy relic",
            state={"phase": "EVENT", "game_state": game},
        )
        matched, fields = independent_oracle._consequence_matches_visible(
            producer, expected, record
        )
        self.assertTrue(matched, fields)
        self.assertEqual({"min": 0, "max": 14}, expected["hp_delta"])
        self.assertEqual({"min": 0, "max": 14}, expected["max_hp_delta"])
        self.assertEqual({"min": -85, "max": 215}, expected["gold_delta"])

    def test_knowing_skull_is_recomputed_from_contract_not_producer_or_text(self):
        targets, screen_options = knowing_skull_surface(costs=(9, 11, 10, 6))
        game = {
            "ascension_level": 0, "current_hp": 12, "max_hp": 68,
            "gold": 600,
            "relics": [{"id": "Tungsten Rod"}],
            "potions": [{"id": "Potion Slot", "slot": 0}],
            "screen_state": {
                "event_id": "Knowing Skull",
                "options": copy.deepcopy(screen_options),
            },
        }
        record = {
            "phase": "EVENT",
            "authoritative_state_before": authoritative_state(
                226520, existing={"phase": "EVENT", "game_state": game},
            ),
        }
        raw = {
            "choice_index": 1,
            "target": copy.deepcopy(targets[1]),
            "raw_text": "producer says free and harmless",
        }
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )
        self.assertEqual(-10, expected["hp_delta"])
        self.assertEqual(90, expected["gold_delta"])
        self.assertEqual(
            5, expected["future_costs"][0]["reserved_hp_loss"]
        )
        self.assertEqual("knowing_skull_take_gold", expected["operation"])

        producer = autoplay._structured_option_consequence(
            "EVENT", copy.deepcopy(targets[1]), raw["raw_text"],
            state={"phase": "EVENT", "game_state": game},
        )
        matched, fields = independent_oracle._consequence_matches_visible(
            producer, expected
        )
        self.assertTrue(matched, fields)

        forged = copy.deepcopy(raw)
        forged["target"]["event_contract"]["parameters"]["hp_loss"] = 1
        invalid = independent_oracle._expected_visible_consequence(
            "EVENT", forged, record
        )
        self.assertIn(
            "typed_event_contract_parameters_mismatch",
            invalid["uncertainty"],
        )

    def test_mausoleum_oracle_strictly_recomputes_producer_typed_schema(self):
        self.assertEqual(
            autoplay._PRODUCER_EFFECT_FIELDS,
            independent_oracle._PRODUCER_EFFECT_FIELDS,
        )
        self.assertEqual(
            autoplay._PRODUCER_SCORING_FIELDS,
            independent_oracle._PRODUCER_SCORING_FIELDS,
        )
        for ascension, relics in (
            (0, []),
            (15, []),
            (0, [{"id": "Omamori", "name": "Omamori", "counter": 2}]),
        ):
            record = self.mausoleum_record(
                ascension=ascension, before_relics=relics,
            )
            before_game = record["authoritative_state_before"]["game_state"]
            for option in record["available_options_before"]:
                with self.subTest(
                    ascension=ascension,
                    choice_index=option["choice_index"],
                ):
                    raw = independent_oracle._raw_option_contract(
                        option, "EVENT"
                    )
                    expected = independent_oracle._expected_visible_consequence(
                        "EVENT", raw, record
                    )
                    producer = autoplay._structured_option_consequence(
                        "EVENT", copy.deepcopy(option["target"]),
                        raw["raw_text"],
                        state={
                            "phase": "EVENT",
                            "game_state": copy.deepcopy(before_game),
                        },
                    )
                    matched, fields = (
                        independent_oracle._consequence_matches_visible(
                            producer, expected
                        )
                    )
                    self.assertTrue(matched, fields)
                    if option["choice_index"] == 0:
                        self.assertEqual(
                            "base_game_the_mausoleum_v1",
                            expected["mechanism_id"],
                        )
                        self.assertEqual(
                            {"gold": 0, "hp": 0, "max_hp": 0},
                            expected["current_cost"],
                        )
                        self.assertEqual([], expected["future_costs"])

    def test_mausoleum_canonical_producer_claims_are_independently_accounted(self):
        writhe_preview = {
            "id": "Writhe", "name": "Writhe", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
        }
        open_contract = self.mausoleum_event_contract("INTRO", 0)
        leave_contract = self.mausoleum_event_contract("INTRO", 1)
        options = [
            {
                "option_id": "mausoleum:open", "choice_index": 0,
                "label": "untrusted localized open label",
                "target": {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 0, "original_button_index": 0,
                    "event_contract": copy.deepcopy(open_contract),
                    "card": copy.deepcopy(writhe_preview),
                },
            },
            {
                "option_id": "mausoleum:leave", "choice_index": 1,
                "label": "untrusted localized leave label",
                "target": {
                    "kind": "event_option", "event_id": "The Mausoleum",
                    "choice_index": 1, "original_button_index": 1,
                    "event_contract": copy.deepcopy(leave_contract),
                },
            },
        ]
        screen_state = {
            "event_id": "The Mausoleum",
            "options": [
                {
                    "choice_index": 0, "disabled": False,
                    "original_button_index": 0,
                    "event_contract": copy.deepcopy(open_contract),
                    "card": copy.deepcopy(writhe_preview),
                },
                {
                    "choice_index": 1, "disabled": False,
                    "original_button_index": 1,
                    "event_contract": copy.deepcopy(leave_contract),
                },
            ],
        }
        before = {
            "ascension_level": 0, "screen_state": screen_state,
            "deck": [], "relics": [],
        }
        state = {
            "phase": "EVENT", "options": copy.deepcopy(options),
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(before),
        }
        consequences = [
            autoplay._structured_option_consequence(
                "EVENT", option["target"], option["label"], state=state
            )
            for option in options
        ]
        candidates = [
            production_candidate(
                option["option_id"], option["choice_index"], score,
                consequences={
                    key: copy.deepcopy(value)
                    for key, value in consequence.items()
                    if key in autoplay._PRODUCER_EFFECT_FIELDS
                    or key in autoplay._PRODUCER_SCORING_FIELDS
                },
            )
            for option, score, consequence in zip(
                options, (10, 0), consequences
            )
        ]
        record = self.production_record(
            phase="EVENT", options=options, commands=["choose"],
            payload={"action": "choose", "option_id": "mausoleum:open"},
            candidates=candidates,
            before=before,
            after={
                "screen_state": screen_state,
                "deck": [self.mausoleum_writhe("canonical-writhe")],
                "relics": [{
                    "id": "Anchor", "name": "Anchor", "counter": -1,
                }],
            },
        )
        record["authoritative_state_before"]["phase"] = "EVENT"
        record["authoritative_state_after"]["phase"] = "EVENT"
        report = self.audit(base_records(record))
        blocking_kinds = {
            "candidate_consequence_binding_mismatch",
            "candidate_consequence_fields_unverified",
            "producer_consequence_claim_unclassified",
            "producer_consequence_claim_contradicted",
            "uncertain_consequence_settlement_unproven",
        }
        self.assertEqual(
            set(),
            blocking_kinds & {
                item["kind"] for item in report["issues"] + report["unknowns"]
            },
            report,
        )

        forged = copy.deepcopy(record)
        open_row = next(
            row for row in forged["legal_choices_before"]
            if row["choice_id"] == "mausoleum:open"
        )
        for container in (
            open_row["producer_consequence_raw"]["value"],
            open_row["producer_consequence_claim"],
        ):
            container["curse"]["probability"] = 0.0
        forged_report = self.audit(base_records(forged))
        self.assertIn(
            "producer_consequence_claim_contradicted",
            {item["kind"] for item in forged_report["issues"]},
        )

    def test_mausoleum_realized_positive_branches_ignore_producer_claims(self):
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        writhe = self.mausoleum_writhe("new-writhe")
        omamori = {"id": "Omamori", "name": "Omamori", "counter": 1}
        cases = (
            (
                "a0_writhe", self.mausoleum_record(
                    after_relics=[anchor], after_deck=[writhe],
                ), True, 0,
            ),
            (
                "a0_no_writhe", self.mausoleum_record(
                    after_relics=[anchor], after_deck=[],
                ), False, 0,
            ),
            (
                "a15_writhe", self.mausoleum_record(
                    ascension=15, after_relics=[anchor],
                    after_deck=[writhe],
                ), True, 0,
            ),
            (
                "a0_omamori_triggered", self.mausoleum_record(
                    before_relics=[omamori],
                    after_relics=[{**omamori, "counter": 0}, anchor],
                    after_deck=[],
                ), False, 1,
            ),
            (
                "a0_omamori_not_triggered", self.mausoleum_record(
                    before_relics=[omamori],
                    after_relics=[omamori, anchor], after_deck=[],
                ), False, 0,
            ),
        )
        for name, record, writhe_added, charges in cases:
            with self.subTest(name=name):
                # These are adversarial producer claims.  The public API never
                # reads them and must return the same authoritative evidence.
                clean = independent_oracle.mausoleum_trade_evidence(record)
                forged = copy.deepcopy(record)
                forged.update({
                    "decision_outcome": {"gold_delta": 999},
                    "authoritative_choice_settlement": {
                        "status": "clear", "authority": "producer_forgery",
                    },
                    "legal_choices_before": [{
                        "producer_consequence_claim": {
                            "curse_probability": 0.0, "relic_delta": 99,
                            "acquired_benefit": {
                                "kind": "relic", "id": "Black Star",
                            },
                        },
                    }],
                })
                forged_evidence = independent_oracle.mausoleum_trade_evidence(
                    forged
                )
                self.assertEqual(clean, forged_evidence)
                self.assertEqual("clear", clean["status"])
                realized = clean["realized_settlement"]
                self.assertEqual(
                    "Anchor", realized["acquired_benefit"]["relic_id"]
                )
                self.assertEqual(writhe_added, realized["curse"]["added"])
                self.assertEqual(
                    charges,
                    realized["curse"]["omamori_charges_consumed"],
                )
                self.assertEqual(
                    "authoritative_protocol_delta", realized["authority"]
                )

        leave = independent_oracle.mausoleum_trade_evidence(
            self.mausoleum_record(choice_index=1)
        )
        self.assertEqual("clear", leave["status"])
        self.assertIsNone(leave["realized_settlement"]["acquired_benefit"])
        self.assertEqual(
            "mausoleum_leave",
            leave["realized_settlement"]["operation"],
        )

        report = independent_oracle._audit_base_game_mechanisms(
            [cases[0][1]]
        )
        self.assertEqual("clear", report["status"])
        self.assertEqual(1, report["evaluated"])
        self.assertEqual(
            "mausoleum_expected_curse_for_random_relic",
            report["observations"][0]["mechanism"],
        )

    def test_mausoleum_reuses_shared_passive_relic_pickup_mechanics(self):
        thread_and_needle = {
            "id": "Thread and Needle", "name": "Thread and Needle",
            "tier": "RARE", "counter": -1,
        }
        evidence = independent_oracle.mausoleum_trade_evidence(
            self.mausoleum_record(
                after_relics=[thread_and_needle], after_deck=[]
            )
        )

        self.assertEqual("clear", evidence["status"])
        benefit = evidence["realized_settlement"]["acquired_benefit"]
        self.assertEqual("threadandneedle", benefit["normalized_id"])
        self.assertEqual("RARE", benefit["tier"])

    def test_mausoleum_darkstone_gain_is_part_of_realized_branch(self):
        darkstone = {
            "id": "Darkstone Periapt", "name": "Darkstone Periapt",
            "counter": -1,
        }
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        record = self.mausoleum_record(
            before_relics=[darkstone],
            after_relics=[darkstone, anchor],
            after_deck=[self.mausoleum_writhe("darkstone-writhe")],
            before_hp=50, after_hp=56,
            before_max_hp=80, after_max_hp=86,
        )

        evidence = independent_oracle.mausoleum_trade_evidence(record)

        self.assertEqual("clear", evidence["status"], evidence)
        delta = evidence["realized_settlement"]["observable_delta"]
        self.assertEqual(6, delta["hp_delta"])
        self.assertEqual(6, delta["max_hp_delta"])
        raw = record["available_options_before"][0]
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )
        writhe_branch = next(
            item for item in expected["probabilistic_outcomes"]
            if item["id"] == "writhe_added"
        )
        self.assertEqual(6, writhe_branch["hp_delta"])
        self.assertEqual(6, writhe_branch["max_hp_delta"])

    def test_mausoleum_rejects_wrong_identity_surface_receipt_and_binding(self):
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        baseline = self.mausoleum_record(
            after_relics=[anchor], after_deck=[self.mausoleum_writhe()]
        )

        wrong_event = copy.deepcopy(baseline)
        wrong_event["available_options_before"][0]["target"][
            "event_id"
        ] = "Other Event"
        self.assertEqual(
            "not_applicable",
            independent_oracle.mausoleum_trade_evidence(wrong_event)["status"],
        )

        missing_options_other_event = {
            "phase": "EVENT",
            "authoritative_state_before": {
                "game_state": {
                    "screen_state": {"event_id": "MatchAndKeep"},
                },
            },
        }
        self.assertEqual(
            "not_applicable",
            independent_oracle.mausoleum_trade_evidence(
                missing_options_other_event
            )["status"],
        )

        cases = []
        wrong_index = copy.deepcopy(baseline)
        wrong_index["available_options_before"][0]["choice_index"] = 2
        cases.append(("choice_index", wrong_index, "issues"))
        wrong_preview = copy.deepcopy(baseline)
        wrong_preview["available_options_before"][0]["target"]["card"][
            "id"
        ] = "Doubt"
        cases.append(("target_card", wrong_preview, "issues"))
        wrong_surface = copy.deepcopy(baseline)
        wrong_surface["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["options"].pop()
        cases.append(("initial_surface", wrong_surface, "issues"))
        wrong_receipt = copy.deepcopy(baseline)
        wrong_receipt["resolved_target_id"] = "mausoleum:leave"
        cases.append(("receipt", wrong_receipt, "issues"))
        wrong_seq = copy.deepcopy(baseline)
        wrong_seq["authoritative_state_after"]["state_seq"] = 12
        cases.append(("state_seq", wrong_seq, "issues"))
        wrong_binding = copy.deepcopy(baseline)
        wrong_binding["authoritative_state_after"]["run_id"] = "other-run"
        cases.append(("binding", wrong_binding, "issues"))
        missing_binding = copy.deepcopy(baseline)
        missing_binding["authoritative_state_after"].pop("selection_digest")
        cases.append(("missing_binding", missing_binding, "inconclusive"))
        nonadvancing = copy.deepcopy(baseline)
        nonadvancing["after_seq"] = 10
        nonadvancing["authoritative_state_after"]["state_seq"] = 10
        cases.append(("nonadvancing_seq", nonadvancing, "inconclusive"))
        missing_after = copy.deepcopy(baseline)
        missing_after.pop("authoritative_state_after")
        cases.append(("missing_after", missing_after, "inconclusive"))
        for name, record, status in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    status,
                    independent_oracle.mausoleum_trade_evidence(record)[
                        "status"
                    ],
                )

    def test_mausoleum_settlement_fails_closed_for_bad_rewards_and_branches(self):
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        writhe = self.mausoleum_writhe()
        bad_cases = (
            (
                "zero_relics",
                self.mausoleum_record(after_relics=[], after_deck=[writhe]),
                "issues",
            ),
            (
                "two_relics",
                self.mausoleum_record(
                    after_relics=[
                        anchor,
                        {"id": "DataDisk", "name": "Data Disk", "counter": -1},
                    ],
                    after_deck=[writhe],
                ),
                "issues",
            ),
            (
                "boss_relic",
                self.mausoleum_record(
                    after_relics=[{"id": "Black Star", "counter": -1}],
                    after_deck=[writhe],
                ),
                "issues",
            ),
            (
                "unknown_relic_pickup",
                self.mausoleum_record(
                    after_relics=[{"id": "UnknownRelic", "counter": -1}],
                    after_deck=[writhe],
                ),
                "inconclusive",
            ),
            (
                "wrong_curse",
                self.mausoleum_record(
                    after_relics=[anchor],
                    after_deck=[{
                        **writhe, "id": "Doubt", "name": "Doubt",
                    }],
                ),
                "issues",
            ),
            (
                "a15_missing_writhe",
                self.mausoleum_record(
                    ascension=15, after_relics=[anchor], after_deck=[],
                ),
                "issues",
            ),
        )
        for name, record, status in bad_cases:
            with self.subTest(name=name):
                evidence = independent_oracle.mausoleum_trade_evidence(record)
                self.assertEqual(status, evidence["status"])
                self.assertIsNone(evidence["realized_settlement"])

        old = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "rarity": "BASIC", "upgrades": 0,
            "uuid": "old-uuid", "card_instance_id": "old-uuid",
        }
        reused = self.mausoleum_record(
            before_deck=[old],
            after_deck=[self.mausoleum_writhe("old-uuid")],
            after_relics=[anchor],
        )
        self.assertNotEqual(
            "clear", independent_oracle.mausoleum_trade_evidence(reused)["status"]
        )

    def test_mausoleum_rejects_every_extra_observable_delta_and_leave_gain(self):
        anchor = {"id": "Anchor", "name": "Anchor", "counter": -1}
        writhe = self.mausoleum_writhe()
        base = self.mausoleum_record(
            after_relics=[anchor], after_deck=[writhe]
        )
        cases = []
        hp = copy.deepcopy(base)
        hp["authoritative_state_after"]["game_state"]["current_hp"] = 49
        cases.append(("hp", hp))
        max_hp = copy.deepcopy(base)
        max_hp["authoritative_state_after"]["game_state"]["max_hp"] = 81
        cases.append(("max_hp", max_hp))
        block = copy.deepcopy(base)
        block["authoritative_state_after"]["game_state"]["block"] = 1
        cases.append(("block", block))
        gold = copy.deepcopy(base)
        gold["authoritative_state_after"]["game_state"]["gold"] = 101
        cases.append(("gold", gold))
        potion = copy.deepcopy(base)
        potion["authoritative_state_after"]["game_state"]["potions"] = [{
            "id": "Dexterity Potion", "potion_instance_id": "new-potion",
        }]
        cases.append(("potion", potion))
        key = copy.deepcopy(base)
        key["authoritative_state_after"]["game_state"]["keys"]["ruby"] = True
        cases.append(("key", key))
        extra_card = copy.deepcopy(base)
        extra_card["authoritative_state_after"]["game_state"]["deck"].append({
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "rarity": "BASIC", "upgrades": 0,
            "uuid": "extra-card", "card_instance_id": "extra-card",
        })
        cases.append(("deck", extra_card))
        for name, record in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    "issues",
                    independent_oracle.mausoleum_trade_evidence(record)[
                        "status"
                    ],
                )

        leave_gain = self.mausoleum_record(
            choice_index=1, after_relics=[anchor]
        )
        leave = independent_oracle.mausoleum_trade_evidence(leave_gain)
        self.assertEqual("issues", leave["status"])
        self.assertEqual(
            "mausoleum_leave_observable_delta_nonzero", leave["reason"]
        )

    def test_omamori_counter_and_curse_deck_delta_are_jointly_proven(self):
        option = typed_neow_option(
            0, "HUNDRED_GOLD", "CURSE", max_hp=80,
        )
        before = {
            "max_hp": 80, "deck": [], "potions": [],
            "relics": [{"id": "Omamori", "counter": 2}],
        }
        after = {
            **copy.deepcopy(before),
            "relics": [{"id": "Omamori", "counter": 1}],
        }
        record = self.mechanism_record(
            "NEOW", before, after, options=[option],
            selected_choice_ids=[option["option_id"]],
        )
        report = independent_oracle._audit_base_game_mechanisms([record])
        self.assertEqual(1, report["evaluated"])
        self.assertEqual([], report["issues"])

        pickup_before = {"max_hp": 80, "deck": [], "relics": [], "potions": []}
        pickup_after = {
            **copy.deepcopy(pickup_before),
            "relics": [{"id": "Omamori", "counter": 2}],
        }
        pickup_option = {
            "option_id": "shop:omamori", "choice_index": 0,
            "target": {"kind": "relic", "item": {"id": "Omamori"}},
        }
        pickup = self.mechanism_record(
            "SHOP_SCREEN", pickup_before, pickup_after,
            options=[pickup_option],
            selected_choice_ids=[pickup_option["option_id"]],
        )
        pickup_report = independent_oracle._audit_base_game_mechanisms(
            [pickup]
        )
        self.assertEqual(1, pickup_report["evaluated"])
        self.assertEqual("acquired", pickup_report["observations"][0]["transition"])

        bad_pickup = copy.deepcopy(pickup)
        bad_pickup["authoritative_state_after"]["game_state"]["relics"][0][
            "counter"
        ] = 1
        bad_pickup_report = independent_oracle._audit_base_game_mechanisms(
            [bad_pickup]
        )
        self.assertEqual(
            "acquisition_counter_not_initialized_to_two",
            bad_pickup_report["issues"][0]["reason"],
        )

        unchanged = copy.deepcopy(record)
        unchanged["authoritative_state_after"]["game_state"]["relics"] = [
            {"id": "Omamori", "counter": 2}
        ]
        wrong = independent_oracle._audit_base_game_mechanisms([unchanged])
        self.assertEqual(
            {"omamori_transition_mismatch"},
            {item["kind"] for item in wrong["issues"]},
        )

        curse = {
            "id": "Regret", "name": "Regret", "type": "CURSE",
            "rarity": "CURSE", "upgrades": 0,
            "card_instance_id": "curse-regret",
        }
        spent_before = copy.deepcopy(before)
        spent_before["relics"] = [{"id": "Omamori", "counter": 0}]
        spent_after = copy.deepcopy(spent_before)
        spent_after["deck"] = [curse]
        spent = self.mechanism_record(
            "NEOW", spent_before, spent_after, options=[option],
            selected_choice_ids=[option["option_id"]],
        )
        spent_report = independent_oracle._audit_base_game_mechanisms([spent])
        self.assertEqual(1, spent_report["evaluated"])
        self.assertEqual([], spent_report["issues"])

        event_curse = copy.deepcopy(curse)
        event_curse["card_instance_id"] = "event-curse"
        event_option = {
            "option_id": "event:curse", "choice_index": 0,
            "target": {
                "kind": "event_option", "event_id": "CursedEvent",
                "card": event_curse,
            },
        }
        event_before = copy.deepcopy(before)
        event_before["relics"] = [{"id": "Omamori", "counter": 1}]
        event_after = copy.deepcopy(event_before)
        event_after["relics"] = [{"id": "Omamori", "counter": 0}]
        event_record = self.mechanism_record(
            "EVENT", event_before, event_after, options=[event_option],
            selected_choice_ids=[event_option["option_id"]],
        )
        event_report = independent_oracle._audit_base_game_mechanisms(
            [event_record]
        )
        self.assertEqual(1, event_report["evaluated"])
        self.assertEqual(1, event_report["observations"][0]["blocked_curses"])

        curse_two = {**copy.deepcopy(curse), "id": "Pain", "card_instance_id": "curse-pain"}
        curse_three = {**copy.deepcopy(curse), "id": "Doubt", "card_instance_id": "curse-doubt"}
        multi_option = {
            "option_id": "event:three-curses", "choice_index": 0,
            "target": {
                "kind": "event_option", "event_id": "TypedMultiCurse",
                "cards": [copy.deepcopy(curse), curse_two, curse_three],
            },
        }
        multi_before = copy.deepcopy(before)
        multi_after = copy.deepcopy(multi_before)
        multi_after["relics"] = [{"id": "Omamori", "counter": 0}]
        multi_after["deck"] = [copy.deepcopy(curse_three)]
        multi = self.mechanism_record(
            "EVENT", multi_before, multi_after, options=[multi_option],
            selected_choice_ids=[multi_option["option_id"]],
        )
        multi_report = independent_oracle._audit_base_game_mechanisms([multi])
        self.assertEqual(1, multi_report["evaluated"])
        self.assertEqual(2, multi_report["observations"][0]["blocked_curses"])
        self.assertEqual(1, multi_report["observations"][0]["curses_added"])

        missing_added = copy.deepcopy(multi)
        missing_added["authoritative_state_after"]["game_state"]["deck"] = []
        missing_added_report = independent_oracle._audit_base_game_mechanisms(
            [missing_added]
        )
        self.assertEqual(
            {"omamori_transition_mismatch"},
            {item["kind"] for item in missing_added_report["issues"]},
        )

        noncurse = self.stasis_card("Strike_R", "reward-strike", "BASIC")
        reward_option = {
            "option_id": "card:reward-strike", "choice_index": 0,
            "target": {
                "kind": "card", "card_instance_id": "reward-strike",
                "card": noncurse,
            },
        }
        bad_after = copy.deepcopy(before)
        bad_after["relics"] = [{"id": "Omamori", "counter": 1}]
        bad_after["deck"] = [noncurse]
        noncurse_record = self.mechanism_record(
            "CARD_REWARD", before, bad_after, options=[reward_option],
            selected_choice_ids=[reward_option["option_id"]],
        )
        noncurse_report = independent_oracle._audit_base_game_mechanisms(
            [noncurse_record]
        )
        self.assertEqual(
            {"omamori_transition_mismatch"},
            {item["kind"] for item in noncurse_report["issues"]},
        )

    def test_writhing_mass_implant_consumes_omamori_without_deck_delta(self):
        mass = {
            "enemy_instance_id": "enemy:mass", "id": "WrithingMass",
            "current_hp": 120, "max_hp": 120,
            "is_gone": False, "half_dead": False,
            "intent": "STRONG_DEBUFF", "powers": [],
        }
        before = self.stasis_game(mass)
        before["relics"] = [{"id": "Omamori", "counter": 2}]
        after = copy.deepcopy(before)
        after["relics"] = [{"id": "Omamori", "counter": 1}]
        record = self.mechanism_record(
            "COMBAT_TURN_3", before, after, action="end",
        )

        report = independent_oracle._audit_base_game_mechanisms([record])

        self.assertEqual(1, report["evaluated"])
        self.assertEqual([], report["issues"])
        self.assertEqual([], report["unknowns"])
        self.assertEqual(1, report["observations"][0]["attempted_curses"])

    def test_stasis_uuid_lifecycle_priority_and_return_destination(self):
        rare = self.stasis_card("Demon Form", "held-rare", "RARE")
        common = self.stasis_card("Strike_R", "draw-common", "COMMON")
        orb_before = {
            "enemy_instance_id": "enemy:orb", "id": "BronzeOrb",
            "current_hp": 20, "max_hp": 20, "is_gone": False,
            "half_dead": False, "powers": [],
        }
        orb_stasis = {
            **copy.deepcopy(orb_before),
            "powers": [{
                "id": "Stasis", "name": "Stasis", "amount": -1,
                "card": copy.deepcopy(rare),
            }],
        }
        acquisition = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(orb_before, draw=[common, rare]),
            self.stasis_game(orb_stasis, draw=[common], limbo=[rare]),
            action="end",
        )
        report = independent_oracle._audit_base_game_mechanisms([acquisition])
        self.assertEqual(1, report["evaluated"])
        self.assertEqual("acquired", report["observations"][0]["transition"])

        # Live CommunicationMod frames keep the held card solely in the
        # Stasis power and leave combat_state.limbo empty.  That is complete
        # evidence as long as the UUID vanished from every player pile.
        detached = copy.deepcopy(acquisition)
        detached["authoritative_state_after"]["game_state"][
            "combat_state"
        ]["limbo"] = []
        detached_report = independent_oracle._audit_base_game_mechanisms(
            [detached]
        )
        self.assertEqual(1, detached_report["evaluated"])
        self.assertFalse(detached_report["observations"][0]["limbo_observed"])

        held_without_limbo = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(orb_stasis, draw=[common], limbo=[]),
            self.stasis_game(orb_stasis, draw=[common], limbo=[]),
            action="play",
        )
        held_report = independent_oracle._audit_base_game_mechanisms(
            [held_without_limbo]
        )
        self.assertEqual(1, held_report["evaluated"])
        self.assertEqual("held", held_report["observations"][0]["transition"])

        leaked = copy.deepcopy(held_without_limbo)
        leaked["authoritative_state_after"]["game_state"][
            "combat_state"
        ]["hand"] = [copy.deepcopy(rare)]
        leaked_report = independent_oracle._audit_base_game_mechanisms([leaked])
        self.assertEqual(
            "active_stasis_binding_not_preserved",
            leaked_report["issues"][0]["reason"],
        )

        draw_empty = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(
                orb_before, hand=[rare], draw=[], discard=[common]
            ),
            self.stasis_game(
                orb_stasis, draw=[], discard=[common], limbo=[rare]
            ),
            action="end",
        )
        draw_empty_report = independent_oracle._audit_base_game_mechanisms(
            [draw_empty]
        )
        self.assertEqual(1, draw_empty_report["evaluated"])
        self.assertEqual(
            "post_end_discard_reconstruction",
            draw_empty_report["observations"][0]["source_pile"],
        )

        common_power = [{
            "id": "Stasis", "name": "Stasis", "amount": -1,
            "card": copy.deepcopy(common),
        }]
        ambiguous_after_monster = copy.deepcopy(orb_before)
        ambiguous_after_monster["powers"] = common_power
        ambiguous = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(
                orb_before, hand=[rare], draw=[], discard=[common]
            ),
            self.stasis_game(
                ambiguous_after_monster, draw=[], discard=[], limbo=[common]
            ),
            action="end",
        )
        ambiguous_report = independent_oracle._audit_base_game_mechanisms(
            [ambiguous]
        )
        self.assertEqual(
            {"stasis_end_discard_priority_unproven"},
            {item["kind"] for item in ambiguous_report["unknowns"]},
        )

        wrong_priority = copy.deepcopy(acquisition)
        wrong_power = wrong_priority["authoritative_state_after"][
            "game_state"
        ]["combat_state"]["monsters"][0]["powers"][0]
        wrong_power["card"] = copy.deepcopy(common)
        wrong_combat = wrong_priority["authoritative_state_after"][
            "game_state"
        ]["combat_state"]
        wrong_combat["draw_pile"] = [copy.deepcopy(rare)]
        wrong_combat["limbo"] = [copy.deepcopy(common)]
        priority_report = independent_oracle._audit_base_game_mechanisms(
            [wrong_priority]
        )
        self.assertEqual(
            "rarity_priority_violated",
            priority_report["issues"][0]["reason"],
        )

        missing_limbo = copy.deepcopy(acquisition)
        missing_limbo["authoritative_state_after"]["game_state"][
            "combat_state"
        ].pop("limbo")
        missing_report = independent_oracle._audit_base_game_mechanisms(
            [missing_limbo]
        )
        self.assertEqual(
            {"stasis_complete_piles_missing"},
            {item["kind"] for item in missing_report["unknowns"]},
        )

        missing_power_card = copy.deepcopy(acquisition)
        missing_power_card["authoritative_state_after"]["game_state"][
            "combat_state"
        ]["monsters"][0]["powers"][0].pop("card")
        missing_power_report = independent_oracle._audit_base_game_mechanisms(
            [missing_power_card]
        )
        self.assertEqual(
            {"stasis_power_card_evidence_missing"},
            {item["kind"] for item in missing_power_report["unknowns"]},
        )

        orb_dead = {
            **copy.deepcopy(orb_before), "current_hp": 0,
            "is_gone": True, "powers": [],
        }
        death = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(orb_stasis, limbo=[rare]),
            self.stasis_game(orb_dead, hand=[rare]),
            action="potion",
        )
        death_report = independent_oracle._audit_base_game_mechanisms([death])
        self.assertEqual(1, death_report["evaluated"])
        self.assertEqual(
            "hand", death_report["observations"][0]["returned_to"]
        )

        # CommunicationMod can serialize the dead Orb's Stasis power (and
        # original limbo card) for one more frame after onDeath has already
        # returned a same-UUID copy.  That authoritative linger is valid.
        lingering = copy.deepcopy(death)
        lingering_after = lingering["authoritative_state_after"][
            "game_state"
        ]["combat_state"]
        lingering_after["monsters"][0]["powers"] = copy.deepcopy(
            orb_stasis["powers"]
        )
        lingering_after["limbo"] = [copy.deepcopy(rare)]
        lingering_report = independent_oracle._audit_base_game_mechanisms(
            [lingering]
        )
        self.assertEqual(1, lingering_report["evaluated"])
        self.assertTrue(
            lingering_report["observations"][0]["lingering_power"]
        )

        wrong_lingering = copy.deepcopy(lingering)
        wrong_lingering["authoritative_state_after"]["game_state"][
            "combat_state"
        ]["monsters"][0]["powers"][0]["card"] = copy.deepcopy(common)
        wrong_lingering_report = (
            independent_oracle._audit_base_game_mechanisms([wrong_lingering])
        )
        self.assertEqual(
            "lingering_power_card_binding_mismatch",
            wrong_lingering_report["issues"][0]["reason"],
        )

        post_death = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(
                {
                    **copy.deepcopy(orb_dead),
                    "powers": copy.deepcopy(orb_stasis["powers"]),
                },
                hand=[rare], limbo=[rare],
            ),
            self.stasis_game(
                {
                    **copy.deepcopy(orb_dead),
                    "powers": copy.deepcopy(orb_stasis["powers"]),
                },
                exhaust=[rare], limbo=[rare],
            ),
            action="play",
        )
        post_death_report = independent_oracle._audit_base_game_mechanisms(
            [post_death]
        )
        self.assertEqual(1, post_death_report["evaluated"])
        self.assertEqual(
            "post_death_linger",
            post_death_report["observations"][0]["transition"],
        )

        wrong_return = copy.deepcopy(death)
        combat_after = wrong_return["authoritative_state_after"][
            "game_state"
        ]["combat_state"]
        combat_after["hand"] = []
        combat_after["discard_pile"] = [copy.deepcopy(common)]
        wrong_return_report = independent_oracle._audit_base_game_mechanisms(
            [wrong_return]
        )
        self.assertEqual(
            "held_card_not_returned_exactly_once",
            wrong_return_report["issues"][0]["reason"],
        )

        full_hand = [
            self.stasis_card("Defend_R", f"hand-{index}", "BASIC")
            for index in range(10)
        ]
        full_return = self.mechanism_record(
            "COMBAT_TURN_2",
            self.stasis_game(orb_stasis, hand=full_hand, limbo=[rare]),
            self.stasis_game(
                orb_dead, hand=full_hand, discard=[rare], limbo=[]
            ),
            action="potion",
        )
        full_report = independent_oracle._audit_base_game_mechanisms(
            [full_return]
        )
        self.assertEqual(0, full_report["evaluated"])
        self.assertEqual(
            {"stasis_death_hand_size_unproven"},
            {item["kind"] for item in full_report["unknowns"]},
        )

    def test_two_stasis_acquisitions_consume_the_top_two_draw_rarities(self):
        rare = self.stasis_card("Demon Form", "held-rare", "RARE")
        uncommon = self.stasis_card("Disarm", "held-uncommon", "UNCOMMON")
        common = self.stasis_card("Strike_R", "draw-common", "COMMON")
        first = {
            "enemy_instance_id": "enemy:orb-one", "id": "BronzeOrb",
            "current_hp": 20, "max_hp": 20, "is_gone": False,
            "half_dead": False, "powers": [],
        }
        second = {
            **copy.deepcopy(first),
            "enemy_instance_id": "enemy:orb-two",
        }
        first_after = {
            **copy.deepcopy(first),
            "powers": [{
                "id": "Stasis", "name": "Stasis", "amount": -1,
                "card": copy.deepcopy(rare),
            }],
        }
        second_after = {
            **copy.deepcopy(second),
            "powers": [{
                "id": "Stasis", "name": "Stasis", "amount": -1,
                "card": copy.deepcopy(uncommon),
            }],
        }
        before = self.stasis_game(first, draw=[common, uncommon, rare])
        before["combat_state"]["monsters"] = [first, second]
        after = self.stasis_game(
            first_after, draw=[common], limbo=[rare, uncommon]
        )
        after["combat_state"]["monsters"] = [first_after, second_after]
        record = self.mechanism_record(
            "COMBAT_TURN_2", before, after, action="end",
        )

        report = independent_oracle._audit_base_game_mechanisms([record])
        self.assertEqual(2, report["evaluated"])
        self.assertEqual([], report["issues"])
        self.assertEqual([], report["unknowns"])

        wrong = copy.deepcopy(record)
        wrong_combat = wrong["authoritative_state_after"]["game_state"][
            "combat_state"
        ]
        wrong_combat["monsters"][1]["powers"][0]["card"] = copy.deepcopy(
            common
        )
        wrong_combat["draw_pile"] = [copy.deepcopy(uncommon)]
        wrong_combat["limbo"] = [copy.deepcopy(rare), copy.deepcopy(common)]
        wrong_report = independent_oracle._audit_base_game_mechanisms([wrong])
        self.assertEqual(
            {"batch_rarity_priority_violated"},
            {item.get("reason") for item in wrong_report["issues"]},
        )

        post_end_before = self.stasis_game(
            first, hand=[rare], draw=[], discard=[common, uncommon]
        )
        post_end_before["combat_state"]["monsters"] = [first, second]
        post_end_after = self.stasis_game(
            first_after, draw=[common], limbo=[rare, uncommon]
        )
        post_end_after["combat_state"]["monsters"] = [
            first_after, second_after,
        ]
        post_end_record = self.mechanism_record(
            "COMBAT_TURN_2", post_end_before, post_end_after, action="end",
        )

        post_end_report = independent_oracle._audit_base_game_mechanisms(
            [post_end_record]
        )
        self.assertEqual(2, post_end_report["evaluated"])
        self.assertEqual([], post_end_report["issues"])
        self.assertEqual([], post_end_report["unknowns"])
        self.assertEqual(
            {"draw_or_post_end_discard_superset"},
            {
                item["stasis_batch_source_pile"]
                for item in post_end_report["observations"]
            },
        )

    def test_mechanism_consumer_is_integrated_into_oracle_status(self):
        option = typed_neow_option(
            0, "HUNDRED_GOLD", "CURSE", max_hp=80,
        )
        before = {
            "max_hp": 80, "deck": [], "potions": [],
            "relics": [{"id": "Omamori", "name": "Omamori", "counter": 2}],
        }
        # A forged settled frame says the deterministic Curse was blocked but
        # leaves both counter and deck unchanged.  Producer consequence labels
        # cannot hide the missing charge consumption.
        record = self.production_record(
            phase="NEOW", options=[option], commands=["choose"],
            payload={"action": "choose", "option_id": option["option_id"]},
            candidates=[neow_production_candidate(option, before, 10)],
            before=before, after=before,
        )

        report = self.audit(base_records(record))

        self.assertIn("omamori_transition_mismatch", issue_kinds(report))
        self.assertGreater(
            report["base_game_mechanisms"]["eligible"], 0
        )
        self.assertGreater(report["disagreement_count"], 0)

    def test_complete_canonical_choice_surface_is_clear(self):
        report = self.audit(base_records())

        self.assertEqual("clear", report["status"])
        self.assertEqual(0, report["issue_count"])
        self.assertEqual(0, report["eligible_unknown_count"])
        self.assertEqual(
            "clear",
            report["coverage"]["canonical_choice_completeness"]["status"],
        )

    def test_reward_value_is_partitioned_as_v2_scoring_fact(self):
        producer = {
            "producer_partition_version": 2,
            "consequences": {"reward_value": 38.0},
        }
        row = {
            "producer_candidate_raw": producer,
            "producer_consequence_raw": {
                "present": True, "value": producer["consequences"],
            },
            "producer_consequence_claim": {},
            "producer_scoring_facts": {"reward_value": 38.0},
            "unclassified_producer_fields": [],
        }

        self.assertIn(
            "reward_value",
            independent_oracle._producer_partition_scoring_fields(row),
        )
        self.assertEqual([], independent_oracle._producer_partition_mismatches(row))
        self.assertIn("reward_value", autoplay._PRODUCER_SCORING_FIELDS)
        self.assertIn("reward_value", decision_cases._PRODUCER_SCORING_FIELDS)

    def test_combat_card_reward_and_untyped_event_leave_are_not_misclassified(self):
        card = {
            "id": "Discovery Result",
            "name": "Discovery Result",
            "card_instance_id": "temporary-card",
            "type": "SKILL",
            "rarity": "UNCOMMON",
            "upgrades": 0,
        }
        raw_card = {
            "action": "choose",
            "choice_index": 0,
            "target": {
                "kind": "card",
                "card_instance_id": "temporary-card",
                "card": copy.deepcopy(card),
            },
        }
        record = {
            "authoritative_state_before": {
                "game_state": {
                    "room_phase": "COMBAT",
                    "combat_state": {"hand": [], "monsters": []},
                },
            },
        }
        expected = independent_oracle._expected_visible_consequence(
            "CARD_REWARD", raw_card, record
        )
        self.assertEqual(
            "add_temporary_combat_card", expected["operation"]
        )
        self.assertEqual([], expected["card_changes"]["gain"])

        raw_leave = {
            "action": "choose",
            "choice_index": 2,
            "target": {
                "kind": "event_option", "event_id": "Golden Shrine",
            },
        }
        row = {
            "producer_consequence_raw": {
                "present": True, "value": {"leave": True},
            },
            "producer_consequence_claim": {"leave": True},
            "producer_scoring_facts": {},
            "unclassified_producer_fields": [],
        }
        event_expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw_leave, {}
        )
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                {"phase": "EVENT"}, raw_leave, row,
                event_expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertIn("leave:not_independently_classified", unresolved)

    def test_the_joust_a0_multistage_event_is_fully_typed(self):
        target = {
            "kind": "event_option",
            "event_id": "The Joust",
            "original_button_index": 1,
            "audit_projection_version": 3,
        }
        raw = {"action": "choose", "choice_index": 1, "target": target}
        record = {
            "phase": "EVENT",
            "available_options_before": [{"choice_index": 0}, {"choice_index": 1}],
            "authoritative_state_after": {"phase": "EVENT"},
        }
        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )
        self.assertEqual("the_joust_wager", expected["operation"])
        self.assertEqual(-50, expected["gold_delta"])
        self.assertEqual(50, expected["current_cost"]["gold"])
        self.assertEqual(
            1.0,
            sum(item["probability"] for item in expected["probabilistic_outcomes"]),
        )
        self.assertTrue(
            all(
                expected["field_knowledge"][field]["status"]
                in {"known", "known_domain", "not_applicable"}
                for field in independent_oracle._ORACLE_CONSEQUENCE_FIELDS
            )
        )

        producer = independent_oracle._oracle_empty_consequence("主人")
        producer["uncertainty"] = [
            "event outcome is unclassified without typed protocol mechanics"
        ]
        producer["uncertainty_classification"] = {
            "status": "unresolved",
            "authority": "protocol_surface",
            "reason": "mechanism_not_classified",
        }
        row = {
            "producer_consequence_raw": {"present": True, "value": {}},
            "producer_consequence_claim": {},
            "producer_scoring_facts": {},
            "unclassified_producer_fields": [],
            "consequences": producer,
        }
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, row, expected, None
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)
        self.assertTrue(
            independent_oracle._classified_protocol_uncertainty(
                "EVENT", raw, producer, record
            )
        )

    def test_typed_neow_production_surface_clears_independent_oracle(self):
        before = {
            "current_hp": 80, "max_hp": 80, "gold": 99, "block": 0,
            "deck": [], "relics": [{"id": "Burning Blood"}],
            "potions": [],
        }
        options = [
            typed_neow_option(0, "HUNDRED_GOLD", label="获得100金币"),
            typed_neow_option(1, "UPGRADE_CARD", label="升级一张牌"),
            typed_neow_option(
                2, "ONE_RANDOM_RARE_CARD", label="随机稀有牌"
            ),
            typed_neow_option(
                3, "TWENTY_PERCENT_HP_BONUS", "PERCENT_DAMAGE",
                label="更强韧的命运",
            ),
        ]
        candidates = [
            neow_production_candidate(option, before, 10 - index)
            for index, option in enumerate(options)
        ]
        record = self.production_record(
            phase="NEOW", options=options, commands=["choose"],
            payload={"action": "choose", "option_id": "neow:0"},
            candidates=candidates, before=before, after={"gold": 199},
        )

        report = self.audit(base_records(record))

        self.assertEqual("clear", report["status"], report)
        self.assertEqual(0, report["issue_count"])
        self.assertEqual(0, report["eligible_unknown_count"])

    def test_neow_missing_contract_and_forged_mechanism_fail_closed(self):
        before = {
            "current_hp": 80, "max_hp": 80, "gold": 99, "block": 0,
            "deck": [], "relics": [{"id": "Burning Blood"}],
            "potions": [],
        }
        missing = {
            "option_id": "neow:0", "choice_index": 0, "label": "获得奖励",
            "target": {"kind": "event_option", "event_id": "Neow Event"},
        }
        missing_record = self.production_record(
            phase="NEOW", options=[missing], commands=["choose"],
            payload={"action": "choose", "option_id": "neow:0"},
            candidates=[production_candidate(
                "neow:0", 0, 1, consequences={"event_id": "Neow Event"}
            )],
            before=before,
        )
        missing_report = self.audit(base_records(missing_record))
        self.assertNotEqual("clear", missing_report["status"])
        self.assertGreater(missing_report["eligible_unknown_count"], 0)
        self.assertIn(
            "target_consequence_claim_unclassified",
            {item["kind"] for item in missing_report["unknowns"]},
        )

        forged = typed_neow_option(0)
        forged["target"]["mechanism_id"] = "neow-mechanism:forged"
        forged_record = self.production_record(
            phase="NEOW", options=[forged], commands=["choose"],
            payload={"action": "choose", "option_id": "neow:0"},
            candidates=[production_candidate(
                "neow:0", 0, 1,
                consequences={
                    "event_id": "Neow Event",
                    "mechanism_id": "neow-mechanism:forged",
                    "neow_contract": copy.deepcopy(
                        forged["target"]["neow_contract"]
                    ),
                },
            )],
            before=before, after={"gold": 199},
        )
        forged_report = self.audit(base_records(forged_record))
        self.assertEqual("issues", forged_report["status"])
        self.assertIn(
            "target_consequence_claim_contradicted", issue_kinds(forged_report)
        )

    def test_production_unknown_chinese_event_cannot_forge_independent_authority(self):
        record = self.production_record(
            phase="EVENT",
            options=[{
                "option_id": "event:unknown", "choice_index": 0,
                "label": "接受未知的命运",
                "target": {"kind": "event_option", "event_id": "Unknown"},
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "event:unknown"},
            candidates=[production_candidate(
                "event:unknown", 0, 1,
                consequences={"gold_delta": 0},
            )],
        )
        consequence = record["legal_choices_before"][0]["consequences"]
        for field in consequence["field_knowledge"]:
            consequence["field_knowledge"][field]["authority"] = (
                "independent_mechanics_table"
            )

        report = self.audit(base_records(record))

        self.assertNotEqual("clear", report["status"])
        self.assertGreater(report["eligible_unknown_count"], 0)
        self.assertIn(
            "candidate_consequence_fields_unverified",
            {item["kind"] for item in report["unknowns"]},
        )

    def test_production_claim_contradiction_is_independent_p1(self):
        record = self.production_record(
            phase="EVENT",
            options=[{
                "option_id": "event:gold", "choice_index": 0,
                "label": "Gain 10 Gold",
                "target": {
                    "kind": "event_option", "event_id": "Gold",
                    "mechanism_id": "gain_gold_exact", "amount": 10,
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "event:gold"},
            candidates=[production_candidate(
                "event:gold", 0, 1,
                consequences={"gold_delta": 999},
            )],
            after={"gold": 110},
        )

        report = self.audit(base_records(record))

        self.assertEqual("issues", report["status"])
        self.assertIn(
            "producer_consequence_claim_contradicted", issue_kinds(report)
        )

    def test_empty_high_impact_producer_claim_is_unknown(self):
        record = self.production_record(
            phase="MAP",
            options=[{
                "option_id": "map:1,2", "choice_index": 0,
                "label": "?",
                "target": {
                    "kind": "map_node", "symbol": "?", "x": 1, "y": 2,
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "map:1,2"},
            candidates=[production_candidate("map:1,2", 0, 1, consequences={})],
        )

        report = self.audit(base_records(record))

        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "producer_consequence_claim_unclassified",
            {item["kind"] for item in report["unknowns"]},
        )

    def test_question_room_value_components_are_v2_scoring_facts(self):
        components = {
            "event_value": 4.0,
            "relic_chance_value": 2.5,
            "risk_penalty": -1.0,
        }
        record = self.production_record(
            phase="MAP",
            options=[{
                "option_id": "map:1,2", "choice_index": 0, "label": "?",
                "target": {
                    "kind": "map_node", "symbol": "?", "x": 1, "y": 2,
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "map:1,2"},
            candidates=[production_candidate(
                "map:1,2", 0, 1,
                consequences={
                    "route": {"symbol": "?", "x": 1, "y": 2},
                    "question_room_value_components": components,
                },
            )],
        )
        selected = next(
            row for row in record["legal_choices_before"]
            if row.get("selected") is True
        )

        self.assertEqual(
            components,
            selected["producer_scoring_facts"][
                "question_room_value_components"
            ],
        )
        report = self.audit(base_records(record))
        self.assertNotIn(
            "producer_consequence_claim_unclassified",
            {item["kind"] for item in report["issues"] + report["unknowns"]},
            report,
        )

    def test_classified_map_transition_does_not_require_immediate_settlement(self):
        record = self.production_record(
            phase="MAP",
            options=[{
                "option_id": "map:1,2", "choice_index": 0, "label": "?",
                "target": {
                    "kind": "map_node", "symbol": "?", "x": 1, "y": 2,
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "map:1,2"},
            candidates=[production_candidate(
                "map:1,2", 0, 1,
                consequences={"route": {"symbol": "?", "x": 1, "y": 2}},
            )],
        )
        selected = next(
            row for row in record["legal_choices_before"]
            if row.get("selected") is True
        )
        self.assertEqual(
            "classified_future",
            selected["consequences"]["uncertainty_classification"]["status"],
        )
        record.pop("authoritative_choice_settlement", None)

        report = self.audit(base_records(record))

        self.assertFalse(any(
            item["kind"] == "uncertain_consequence_settlement_unproven"
            for item in report["unknowns"]
        ), report)

    def test_forged_target_contract_and_sum_one_probability_are_blocking(self):
        forged_contract = self.production_record(
            phase="EVENT",
            options=[{
                "option_id": "event:forged", "choice_index": 0,
                "label": "Mystery",
                "target": {
                    "kind": "event_option", "event_id": "Mystery",
                    "consequence_contract": {
                        "hp_delta": 0, "max_hp_delta": 0, "gold_delta": 50,
                    },
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "event:forged"},
            candidates=[production_candidate(
                "event:forged", 0, 1, consequences={"gold_delta": 50},
            )],
            after={"gold": 150},
        )
        contract_report = self.audit(base_records(forged_contract))
        self.assertNotEqual("clear", contract_report["status"])
        self.assertIn(
            "target_consequence_claim_unclassified",
            {item["kind"] for item in contract_report["unknowns"]},
        )

        outcomes = [
            {"probability": 0.75, "gold_delta": 10},
            {"probability": 0.25, "gold_delta": 0},
        ]
        forged_probability = self.production_record(
            phase="EVENT",
            options=[{
                "option_id": "event:coin", "choice_index": 0,
                "label": "Flip",
                "target": {
                    "kind": "event_option", "event_id": "CoinFlip",
                    "mechanism_id": "coin_flip_gold_10_or_zero",
                    "probabilistic_outcomes": outcomes,
                },
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "event:coin"},
            candidates=[production_candidate(
                "event:coin", 0, 1,
                consequences={"probabilistic_outcomes": outcomes},
            )],
            after={"gold": 110},
        )
        probability_report = self.audit(base_records(forged_probability))
        self.assertEqual("issues", probability_report["status"])
        self.assertIn(
            "target_consequence_claim_contradicted",
            issue_kinds(probability_report),
        )

    def test_production_card_relic_map_return_and_sapphire_contracts_clear(self):
        card = {"id": "Shrug It Off", "name": "Shrug It Off", "upgrades": 0}
        cases = [
            self.production_record(
                phase="CARD_REWARD",
                options=[{
                    "option_id": "card:shrug", "choice_index": 0,
                    "label": "Shrug It Off",
                    "target": {"kind": "card", "card": card},
                }],
                commands=["choose"],
                payload={"action": "choose", "option_id": "card:shrug"},
                candidates=[production_candidate(
                    "card:shrug", 0, 1,
                    consequences={"card_changes": {
                        "gain": [card], "remove": [], "upgrade": [],
                        "transform": [],
                    }},
                )],
                after={"deck": [card]},
            ),
            self.production_record(
                phase="BOSS_REWARD",
                options=[{
                    "option_id": "relic:data", "choice_index": 0,
                    "label": "Data Disk",
                    "target": {"kind": "relic", "relic": {"id": "DataDisk"}},
                }],
                commands=["choose"],
                payload={"action": "choose", "option_id": "relic:data"},
                candidates=[production_candidate(
                    "relic:data", 0, 1,
                    consequences={"relic_changes": {
                        "gain": [{"id": "DataDisk"}], "remove": [], "counter": [],
                    }},
                )],
                after={"relics": [{"id": "DataDisk"}]},
            ),
            self.production_record(
                phase="MAP",
                options=[{
                    "option_id": "map:1,2", "choice_index": 0, "label": "?",
                    "target": {"kind": "map_node", "symbol": "?", "x": 1, "y": 2},
                }],
                commands=["choose"],
                payload={"action": "choose", "option_id": "map:1,2"},
                candidates=[production_candidate(
                    "map:1,2", 0, 1,
                    consequences={"route": {"symbol": "?", "x": 1, "y": 2}},
                )],
            ),
            self.production_record(
                phase="EVENT", options=[], commands=["skip"],
                payload={"action": "return", "target_id": "action:return"},
                candidates=[production_candidate(
                    "action:return", None, 1, action="return",
                    consequences={"leave": True},
                )],
            ),
            self.production_record(
                phase="SAPPHIRE_KEY",
                options=[{
                    "option_id": "reward:key", "choice_index": 0,
                    "label": "Sapphire Key",
                    "target": {"kind": "sapphire_key"},
                }],
                commands=["choose"],
                payload={"action": "choose", "option_id": "reward:key"},
                candidates=[production_candidate(
                    "reward:key", 0, 1,
                    consequences={"key_changes": {"gain": ["sapphire_key"]}},
                )],
                after={
                    "keys": {"ruby": False, "emerald": False, "sapphire": True},
                },
            ),
        ]

        for record in cases:
            with self.subTest(phase=record["phase"]):
                report = self.audit(base_records(record))
                self.assertEqual("clear", report["status"], report)

    def test_reward_operation_and_identity_claims_are_protocol_bound(self):
        option = {
            "option_id": "reward:gold:0", "choice_index": 0,
            "label": "25 Gold",
            "target": {
                "kind": "reward",
                "reward": {"reward_type": "GOLD", "gold": 25},
            },
        }

        def build(consequences):
            return self.production_record(
                phase="COMBAT_REWARD", options=[option],
                commands=["choose"],
                payload={"action": "choose", "option_id": "reward:gold:0"},
                candidates=[production_candidate(
                    "reward:gold:0", 0, 1, consequences=consequences,
                )],
                after={"gold": 125},
            )

        valid = build({
            "operation": "collect_combat_reward",
            "reward_type": "gold",
            "gold_delta": 25,
        })
        self.assertEqual(
            "clear", self.audit(base_records(valid))["status"]
        )

        for field, value in (
            ("operation", "gain_sapphire_key"),
            ("reward_type", "relic"),
        ):
            with self.subTest(field=field):
                forged = {
                    "operation": "collect_combat_reward",
                    "reward_type": "gold",
                    "gold_delta": 25,
                }
                forged[field] = value
                report = self.audit(base_records(build(forged)))
                self.assertEqual("issues", report["status"], report)
                self.assertIn(
                    "producer_consequence_claim_contradicted",
                    issue_kinds(report),
                )

    def test_darkstone_transform_keeps_hp_pending_until_random_result(self):
        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "uuid-transform",
        }
        target = {
            "kind": "card", "card_instance_id": "uuid-transform",
            "card": copy.deepcopy(card),
        }
        game = {
            "relics": [{"id": "Darkstone Periapt", "counter": -1}],
            "screen_state": {
                "for_upgrade": False, "for_transform": True,
                "for_purge": False, "cards": [copy.deepcopy(card)],
                "selected_cards": [], "num_cards": 1,
                "any_number": False, "confirm_up": False,
            },
        }
        state = {"phase": "GRID", "game_state": copy.deepcopy(game)}
        raw = {"target": copy.deepcopy(target), "raw_text": "transform"}
        record = {"authoritative_state_before": {"game_state": game}}

        production = autoplay._structured_option_consequence(
            "GRID", target, "transform", state=state
        )
        expected = independent_oracle._expected_visible_consequence(
            "GRID", raw, record
        )

        for consequence in (production, expected):
            self.assertEqual("grid_transform", consequence["operation"])
            self.assertIsNone(consequence["hp_delta"])
            self.assertIsNone(consequence["max_hp_delta"])
            self.assertEqual(0, consequence["gold_delta"])

    def test_hand_select_confirmation_does_not_claim_queued_heal(self):
        target = {"kind": "protocol_action", "action": "proceed"}
        state = {"phase": "HAND_SELECT", "game_state": {}}
        raw = {"target": target, "raw_text": "Done"}
        record = {"authoritative_state_before": {"game_state": {}}}

        production = autoplay._structured_option_consequence(
            "HAND_SELECT", target, "Done", state=state
        )
        expected = independent_oracle._expected_visible_consequence(
            "HAND_SELECT", raw, record
        )

        for consequence in (production, expected):
            self.assertEqual(
                "hand_select_confirm_queued_action",
                consequence["operation"],
            )
            self.assertIsNone(consequence["hp_delta"])
            self.assertEqual(
                "not_applicable",
                consequence["field_knowledge"]["hp_delta"]["status"],
            )
            self.assertEqual(
                "protocol_hidden",
                consequence["uncertainty_classification"]["status"],
            )

        legacy = copy.deepcopy(production)
        legacy["operation"] = "proceed"
        comparison_record = {
            "phase": "HAND_SELECT",
            "_raw_target": copy.deepcopy(target),
            "_producer_consequence_claim": {
                "operation": "hand_select_confirm_queued_action",
            },
        }
        self.assertTrue(
            independent_oracle._consequence_matches_visible(
                legacy, expected, comparison_record
            )[0]
        )
        comparison_record.pop("_producer_consequence_claim")
        self.assertFalse(
            independent_oracle._consequence_matches_visible(
                legacy, expected, comparison_record
            )[0]
        )

    def test_hand_select_confirmation_null_resources_are_not_unverified(self):
        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "rarity": "BASIC", "upgrades": 0, "cost": 1,
            "card_instance_id": "hand:strike",
        }
        option = {
            "option_id": "option:strike", "choice_index": 0,
            "label": "Strike", "target": {
                "kind": "card", "card_instance_id": "hand:strike",
                "card": copy.deepcopy(card),
            },
        }
        future_cost = {
            "kind": "hand_select_action_resolution",
            "current_action": "GamblingChipAction",
            "max_cards": 99,
            "can_pick_zero": True,
            "selected_card": copy.deepcopy(card),
            "timing": "after_hand_selection_confirmation",
        }
        record = self.production_record(
            phase="HAND_SELECT", options=[option],
            commands=["choose", "confirm"],
            payload={"action": "choose", "option_id": "option:strike"},
            candidates=[
                production_candidate(
                    "option:strike", 0, 1,
                    consequences={
                        "operation": "hand_select_card",
                        "selected_card": copy.deepcopy(card),
                        "future_costs": [future_cost],
                    },
                ),
                production_candidate(
                    "action:proceed", None, 0, action="proceed",
                    consequences={
                        "operation": "hand_select_confirm_queued_action",
                    },
                ),
            ],
            before={
                "room_phase": "COMBAT", "screen_type": "HAND_SELECT",
                "current_action": "GamblingChipAction",
                "screen_state": {
                    "hand": [copy.deepcopy(card)], "selected": [],
                    "max_cards": 99, "can_pick_zero": True,
                },
            },
        )

        report = self.audit(base_records(record))

        self.assertFalse(any(
            item.get("kind") == "candidate_consequence_fields_unverified"
            and item.get("choice_id") == "action:proceed"
            for item in report["unknowns"]
        ), report["unknowns"])

    def test_grid_exact_uuid_operations_are_independent_and_tamper_evident(self):
        card = {
            "id": "Strike_R", "name": "Strike", "upgrades": 0,
            "card_instance_id": "uuid-strike",
        }
        option = {
            "option_id": "grid:uuid-strike", "choice_index": 0,
            "label": "Strike",
            "target": {
                "kind": "card", "card_instance_id": "uuid-strike",
                "card": copy.deepcopy(card),
            },
        }
        before = {
            "deck": [copy.deepcopy(card)],
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": True, "cards": [copy.deepcopy(card)],
                "selected_cards": [], "num_cards": 1,
                "any_number": False, "confirm_up": False,
            },
        }
        state = {
            "phase": "GRID", "options": [copy.deepcopy(option)],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(before),
        }
        consequence = autoplay._structured_option_consequence(
            "GRID", option["target"], option["label"], state=state
        )
        claim = {
            key: copy.deepcopy(value)
            for key, value in consequence.items()
            if key in {
                "operation", "selected_card", "card_changes",
                "hp_delta", "max_hp_delta", "gold_delta", "current_cost",
            }
        }
        record = self.production_record(
            phase="GRID", options=[option], commands=["choose"],
            payload={"action": "choose", "option_id": "grid:uuid-strike"},
            candidates=[production_candidate(
                "grid:uuid-strike", 0, 1, consequences=claim,
            )],
            before=before, after={"deck": [copy.deepcopy(card)]},
        )

        partial_report = self.audit(base_records(record))
        self.assertEqual("inconclusive", partial_report["status"])
        self.assertIn(
            "deferred_choice_settlement_unproven",
            {item["kind"] for item in partial_report["unknowns"]},
        )
        confirmation_before = copy.deepcopy(before)
        confirmation_before["screen_state"].update({
            "selected_cards": [], "confirm_up": True,
        })
        confirmation = self.production_record(
            phase="GRID", options=[], commands=["confirm"],
            payload={"action": "proceed"}, candidates=[],
            before=confirmation_before,
            after={
                "deck": [],
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [],
                },
            },
        )
        self.resequence_record(confirmation, 11, 12)
        report = self.audit([
            controller_start(), record, confirmation, terminal(),
        ])
        self.assertEqual("clear", report["status"], report)
        self.assertEqual(
            [card], confirmation["decision_outcome"]["deck"]["removed"]
        )
        self.assertTrue(independent_oracle._effect_item_matches(
            "card_changes",
            {**card, "is_playable": False, "price": 50},
            card,
        ))
        self.assertFalse(independent_oracle._effect_item_matches(
            "card_changes", {**card, "id": "Feed"}, card,
        ))

        raw = record["available_options_before"][0]
        expected = independent_oracle._expected_visible_consequence(
            "GRID", raw, record
        )
        reordered = copy.deepcopy(record)
        reordered["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["cards"].reverse()
        self.assertEqual(
            expected,
            independent_oracle._expected_visible_consequence(
                "GRID", raw, reordered
            ),
        )

        upgrade_record = copy.deepcopy(record)
        upgrade_screen = upgrade_record["authoritative_state_before"][
            "game_state"
        ]["screen_state"]
        upgrade_screen.update({
            "for_upgrade": True, "for_transform": False,
            "for_purge": False,
        })
        upgrade_expected = independent_oracle._expected_visible_consequence(
            "GRID", raw, upgrade_record
        )
        self.assertEqual("grid_upgrade", upgrade_expected["operation"])
        self.assertEqual([], upgrade_expected["card_changes"]["upgrade"])
        self.assertEqual(
            card,
            upgrade_expected["future_costs"][0]["selected_card"],
        )
        bad_uuid = copy.deepcopy(raw)
        bad_uuid["target"]["card_instance_id"] = "uuid-other"
        bad_uuid_expected = independent_oracle._expected_visible_consequence(
            "GRID", bad_uuid, upgrade_record
        )
        self.assertEqual(
            "unknown",
            bad_uuid_expected["field_knowledge"]["card_changes"]["status"],
        )
        self.assertIn(
            "grid_target_uuid_missing_or_mismatched",
            bad_uuid_expected["uncertainty"],
        )

        tampered = copy.deepcopy(option)
        tampered["target"]["card"]["id"] = "Feed"
        tampered_record = self.production_record(
            phase="GRID", options=[tampered], commands=["choose"],
            payload={"action": "choose", "option_id": "grid:uuid-strike"},
            candidates=[production_candidate(
                "grid:uuid-strike", 0, 1,
                consequences={"operation": "grid_purge"},
            )],
            before=before,
        )
        tampered_report = self.audit(base_records(tampered_record))
        self.assertNotEqual("clear", tampered_report["status"])
        self.assertGreater(tampered_report["eligible_unknown_count"], 0)

    def test_grid_multi_select_auto_commit_is_reconstructed_as_one_transaction(self):
        first = {
            "id": "Strike_R", "name": "Strike", "upgrades": 0,
            "card_instance_id": "uuid-first",
        }
        second = {
            "id": "Defend_R", "name": "Defend", "upgrades": 0,
            "card_instance_id": "uuid-second",
        }
        survivor = {
            "id": "Bash", "name": "Bash", "upgrades": 0,
            "card_instance_id": "uuid-survivor",
        }

        def option(card, index):
            return {
                "option_id": f"grid:{card['card_instance_id']}",
                "choice_index": index,
                "label": card["name"],
                "target": {
                    "kind": "card",
                    "card_instance_id": card["card_instance_id"],
                    "card": copy.deepcopy(card),
                },
            }

        first_option = option(first, 0)
        second_option = option(second, 1)
        initial = {
            "gold": 100,
            "deck": [
                copy.deepcopy(first), copy.deepcopy(second),
                copy.deepcopy(survivor),
            ],
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": True,
                "cards": [copy.deepcopy(first), copy.deepcopy(second)],
                "selected_cards": [], "num_cards": 2,
                "any_number": False, "confirm_up": False,
            },
        }
        first_state = {
            "phase": "GRID", "options": [first_option, second_option],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(initial),
        }
        first_effect = autoplay._structured_option_consequence(
            "GRID", first_option["target"], first_option["label"],
            state=first_state,
        )
        first_claim = {
            key: copy.deepcopy(value)
            for key, value in first_effect.items()
            if key in autoplay._PRODUCER_EFFECT_FIELDS
        }
        first_after = copy.deepcopy(initial)
        first_after["screen_state"] = {
            **copy.deepcopy(initial["screen_state"]),
            "cards": [copy.deepcopy(second)],
            "selected_cards": [copy.deepcopy(first)],
        }
        first_record = self.production_record(
            phase="GRID", options=[first_option, second_option],
            commands=["choose"],
            payload={"action": "choose", "option_id": first_option["option_id"]},
            candidates=[
                production_candidate(
                    first_option["option_id"], 0, 2,
                    consequences=first_claim,
                ),
                production_candidate(
                    second_option["option_id"], 1, 1,
                    consequences={
                        key: copy.deepcopy(value)
                        for key, value in autoplay._structured_option_consequence(
                            "GRID", second_option["target"],
                            second_option["label"], state=first_state,
                        ).items()
                        if key in autoplay._PRODUCER_EFFECT_FIELDS
                    },
                ),
            ],
            before=initial, after=first_after,
        )
        self.resequence_record(first_record, 10, 11)
        first_record["authoritative_state_before"]["phase"] = "GRID"
        first_record["authoritative_state_after"]["phase"] = "GRID"

        second_before = copy.deepcopy(first_after)
        second_state = {
            "phase": "GRID", "options": [second_option],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(second_before),
        }
        second_effect = autoplay._structured_option_consequence(
            "GRID", second_option["target"], second_option["label"],
            state=second_state,
        )
        second_claim = {
            key: copy.deepcopy(value)
            for key, value in second_effect.items()
            if key in autoplay._PRODUCER_EFFECT_FIELDS
        }
        final_state = {
            "gold": 100, "deck": [copy.deepcopy(survivor)],
            "screen_state": {},
        }
        second_record = self.production_record(
            phase="GRID", options=[second_option], commands=["choose"],
            payload={
                "action": "choose", "option_id": second_option["option_id"],
            },
            candidates=[production_candidate(
                second_option["option_id"], 1, 1,
                consequences=second_claim,
            )],
            before=second_before, after=final_state,
        )
        self.resequence_record(second_record, 11, 12)
        second_record["authoritative_state_before"]["phase"] = "GRID"
        second_record["authoritative_state_after"]["phase"] = "CHEST"

        report = self.audit([
            controller_start(), first_record, second_record, terminal(),
        ])
        self.assertEqual("clear", report["status"], report)
        deferred = report["coverage"]["observable_state_deltas"]
        self.assertEqual(0, deferred["unknown"])
        self.assertEqual(0, deferred["issues"])

        wrong_final = copy.deepcopy(second_record)
        wrong_final["observable_state_after"]["deck"] = [
            copy.deepcopy(first), copy.deepcopy(survivor),
        ]
        wrong_final["authoritative_state_after"]["game_state"]["deck"] = [
            copy.deepcopy(first), copy.deepcopy(survivor),
        ]
        wrong_final["decision_outcome"] = autoplay.observable_inventory_delta(
            wrong_final["observable_state_before"],
            wrong_final["observable_state_after"],
        )
        wrong_final["authoritative_choice_settlement"] = (
            autoplay.authoritative_choice_settlement(wrong_final)
        )
        wrong_report = self.audit([
            controller_start(), first_record, wrong_final, terminal(),
        ])
        self.assertEqual("issues", wrong_report["status"])
        self.assertIn(
            "deferred_choice_settlement_mismatch",
            issue_kinds(wrong_report),
        )

        missing_context = copy.deepcopy(first_record)
        del missing_context["authoritative_state_before"]["game_state"][
            "screen_state"
        ]["num_cards"]
        missing_report = self.audit([
            controller_start(), missing_context, second_record, terminal(),
        ])
        self.assertEqual("inconclusive", missing_report["status"])
        self.assertIn(
            "grid_num_cards_missing_or_invalid",
            {item.get("reason") for item in missing_report["unknowns"]},
        )

    def test_designer_full_service_binds_purge_and_one_random_upgrade(self):
        removed_card = {
            "id": "Writhe", "name": "Writhe", "rarity": "CURSE",
            "upgrades": 0, "card_instance_id": "uuid-writhe",
        }
        upgrade_before = {
            "id": "Bludgeon", "name": "Bludgeon", "rarity": "RARE",
            "upgrades": 0, "card_instance_id": "uuid-bludgeon",
        }
        upgrade_after = {**copy.deepcopy(upgrade_before), "upgrades": 1}
        event_option = {
            "option_id": "designer:full-service", "choice_index": 2,
            "label": "Full Service",
            "target": {
                "kind": "event_option", "event_id": "Designer",
                "label": "Full Service", "text": "Lose 90 Gold.",
            },
        }
        parent = self.production_record(
            phase="EVENT", options=[event_option], commands=["choose"],
            payload={
                "action": "choose", "option_id": "designer:full-service",
            },
            candidates=[production_candidate(
                "designer:full-service", 2, 1,
                consequences={"gold_delta": -90},
            )],
            before={
                "gold": 100,
                "deck": [copy.deepcopy(removed_card), copy.deepcopy(upgrade_before)],
            },
            after={
                "gold": 10,
                "deck": [copy.deepcopy(removed_card), copy.deepcopy(upgrade_before)],
            },
        )
        self.resequence_record(parent, 9, 10)
        parent["authoritative_state_before"]["phase"] = "EVENT"
        parent["authoritative_state_after"]["phase"] = "GRID"
        parent["authoritative_state_after"]["game_state"]["screen_type"] = "GRID"

        grid_option = {
            "option_id": "grid:uuid-writhe", "choice_index": 0,
            "label": "Writhe",
            "target": {
                "kind": "card", "card_instance_id": "uuid-writhe",
                "card": copy.deepcopy(removed_card),
            },
        }
        grid_before = {
            "gold": 10,
            "deck": [copy.deepcopy(removed_card), copy.deepcopy(upgrade_before)],
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": True, "cards": [copy.deepcopy(removed_card)],
                "selected_cards": [], "num_cards": 1,
                "any_number": False, "confirm_up": False,
            },
        }
        grid_state = {
            "phase": "GRID", "options": [copy.deepcopy(grid_option)],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(grid_before),
        }
        grid_effect = autoplay._structured_option_consequence(
            "GRID", grid_option["target"], grid_option["label"],
            state=grid_state,
        )
        grid_claim = {
            key: copy.deepcopy(value)
            for key, value in grid_effect.items()
            if key in autoplay._PRODUCER_EFFECT_FIELDS
        }
        grid_record = self.production_record(
            phase="GRID", options=[grid_option], commands=["choose"],
            payload={"action": "choose", "option_id": "grid:uuid-writhe"},
            candidates=[production_candidate(
                "grid:uuid-writhe", 0, 1, consequences=grid_claim,
            )],
            before=grid_before, after=copy.deepcopy(grid_before),
        )
        self.resequence_record(grid_record, 10, 11)
        grid_record["authoritative_state_before"]["phase"] = "GRID"
        grid_record["authoritative_state_after"]["phase"] = "GRID"

        confirmation_before = copy.deepcopy(grid_before)
        confirmation_before["screen_state"].update({
            "selected_cards": [], "confirm_up": True,
        })
        confirmation = self.production_record(
            phase="GRID", options=[], commands=["confirm"],
            payload={"action": "proceed"}, candidates=[],
            before=confirmation_before,
            after={
                "gold": 10, "deck": [copy.deepcopy(upgrade_after)],
                "screen_state": {},
            },
        )
        self.resequence_record(confirmation, 11, 12)
        confirmation["authoritative_state_before"]["phase"] = "GRID"
        confirmation["authoritative_state_after"]["phase"] = "EVENT"

        expected = {
            field: parent.get(field)
            for field in independent_oracle.ATTEMPT_BINDING_FIELDS
        }
        deferred = independent_oracle._one_grid_confirmation_effect(
            grid_record, "grid_purge"
        )
        records = [parent, grid_record, confirmation]
        status, details = independent_oracle._grid_deferred_settlement(
            records, 1, grid_record, deferred, expected
        )
        self.assertEqual("clear", status, details)
        self.assertEqual(
            "uuid-bludgeon",
            details["designer_full_service"]["random_upgrade"]["before"][
                "card_instance_id"
            ],
        )

        forged = copy.deepcopy(confirmation)
        forged_change = forged["decision_outcome"]["deck"]["changed"][0]
        forged_change["after"]["card_instance_id"] = "uuid-forged"
        forged["observable_state_after"]["deck"][0][
            "card_instance_id"
        ] = "uuid-forged"
        forged["authoritative_state_after"]["game_state"]["deck"][0][
            "card_instance_id"
        ] = "uuid-forged"
        forged_status, _details = independent_oracle._grid_deferred_settlement(
            [parent, grid_record, forged], 1, grid_record, deferred, expected
        )
        self.assertEqual("issue", forged_status)

    def test_grid_upgrade_settlement_matches_exact_changed_card(self):
        before_card = {
            "id": "Bash", "name": "Bash", "upgrades": 0,
            "card_instance_id": "uuid-bash",
        }
        after_card = {**copy.deepcopy(before_card), "upgrades": 1}
        option = {
            "option_id": "grid:uuid-bash",
            "choice_index": 0,
            "label": "Bash",
            "target": {
                "kind": "card",
                "card_instance_id": "uuid-bash",
                "card": copy.deepcopy(before_card),
            },
        }
        before = {
            "gold": 100,
            "deck": [copy.deepcopy(before_card)],
            "screen_state": {
                "for_upgrade": True,
                "for_transform": False,
                "for_purge": False,
                "cards": [copy.deepcopy(before_card)],
                "selected_cards": [],
                "num_cards": 1,
                "any_number": False,
                "confirm_up": False,
            },
        }
        state = {
            "phase": "GRID",
            "options": [copy.deepcopy(option)],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(before),
        }
        effect = autoplay._structured_option_consequence(
            "GRID", option["target"], option["label"], state=state,
        )
        claim = {
            key: copy.deepcopy(value)
            for key, value in effect.items()
            if key in autoplay._PRODUCER_EFFECT_FIELDS
        }
        record = self.production_record(
            phase="GRID",
            options=[option],
            commands=["choose"],
            payload={"action": "choose", "option_id": option["option_id"]},
            candidates=[production_candidate(
                option["option_id"], 0, 1, consequences=claim,
            )],
            before=before,
            after={
                "gold": 100,
                "deck": [copy.deepcopy(after_card)],
                "screen_state": {},
            },
        )
        record["authoritative_state_before"]["phase"] = "GRID"
        record["authoritative_state_after"]["phase"] = "MAP"

        report = self.audit([
            controller_start(), record, terminal(),
        ])
        self.assertEqual("clear", report["status"], report)
        self.assertEqual(
            0, report["coverage"]["observable_state_deltas"]["issues"],
        )

    def test_bridge_enriched_grid_card_uses_strict_canonical_projection(self):
        enriched = bridge.enrich_state({
            "available_commands": ["choose", "state"],
            "ready_for_command": True,
            "in_game": True,
            "game_state": {
                "screen_type": "GRID",
                "screen_state": {
                    "cards": [{
                        "id": "Bash", "name": "Bash", "uuid": "raw-uuid",
                        "upgrades": 0, "cost": 2, "type": "ATTACK",
                    }],
                    "for_upgrade": True,
                    "for_transform": False,
                    "for_purge": False,
                },
                "choice_list": ["Bash"],
                "room_phase": "INCOMPLETE",
                "action_phase": "WAITING_ON_USER",
                "deck": [], "relics": [], "potions": [],
            },
        }, 44)
        raw = autoplay.compact_option(enriched["options"][0])
        record = {"authoritative_state_before": copy.deepcopy(enriched)}

        self.assertIn(
            "uuid",
            enriched["game_state"]["screen_state"]["cards"][0],
        )
        self.assertNotIn("uuid", raw["target"]["card"])
        expected = independent_oracle._expected_visible_consequence(
            "GRID", raw, record
        )
        self.assertEqual("grid_upgrade", expected["operation"])
        self.assertEqual(
            "raw-uuid", expected["selected_card"]["card_instance_id"]
        )
        self.assertNotIn("uuid", expected["selected_card"])

        uuid_tamper = copy.deepcopy(raw)
        uuid_tamper["target"]["card_instance_id"] = "other-uuid"
        uuid_expected = independent_oracle._expected_visible_consequence(
            "GRID", uuid_tamper, record
        )
        self.assertIn(
            "grid_target_uuid_missing_or_mismatched",
            uuid_expected["uncertainty"],
        )

        id_tamper = copy.deepcopy(raw)
        id_tamper["target"]["card"]["id"] = "Feed"
        id_expected = independent_oracle._expected_visible_consequence(
            "GRID", id_tamper, record
        )
        self.assertIn(
            "grid_target_card_facts_mismatch", id_expected["uncertainty"]
        )

    def test_shop_purge_cost_plan_settlement_and_false_claims(self):
        card = {
            "id": "Strike_R", "name": "Strike", "upgrades": 0,
            "card_instance_id": "uuid-strike",
        }
        other_card = {
            "id": "Defend_R", "name": "Defend", "upgrades": 0,
            "card_instance_id": "uuid-defend",
        }
        option = {
            "option_id": "purge", "choice_index": 0, "label": "Purge",
            "target": {"kind": "purge", "price": 75},
        }
        before = {
            "gold": 100,
            "deck": [copy.deepcopy(card), copy.deepcopy(other_card)],
            "screen_state": {
                "purge_available": True, "purge_cost": 75,
            },
        }
        state = {
            "phase": "SHOP_SCREEN", "options": [copy.deepcopy(option)],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(before),
        }
        consequence = autoplay._structured_option_consequence(
            "SHOP_SCREEN", option["target"], option["label"], state=state
        )
        claim = {
            key: copy.deepcopy(value)
            for key, value in consequence.items()
            if key in {
                "gold_delta", "current_cost", "future_costs", "operation",
                "card_changes",
            }
        }
        record = self.production_record(
            phase="SHOP_SCREEN", options=[option], commands=["choose"],
            payload={"action": "choose", "option_id": "purge"},
            candidates=[production_candidate(
                "purge", 0, 1, consequences=claim,
            )],
            before=before, after={"gold": 100},
        )
        report = self.audit(base_records(record))
        self.assertEqual("inconclusive", report["status"], report)
        self.assertIn(
            "deferred_choice_settlement_missing",
            {item["kind"] for item in report["unknowns"]},
        )
        self.assertEqual([], consequence["card_changes"]["remove"])
        self.assertEqual(
            "shop_purge_grid_selection",
            consequence["future_costs"][0]["kind"],
        )
        self.assertEqual(
            "observed", record["authoritative_choice_settlement"]["status"]
        )

        grid_option = {
            "option_id": "grid:uuid-strike", "choice_index": 0,
            "label": "Strike",
            "target": {
                "kind": "card", "card_instance_id": "uuid-strike",
                "card": copy.deepcopy(card),
            },
        }
        grid_before = {
            "gold": 100,
            "deck": [copy.deepcopy(card), copy.deepcopy(other_card)],
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": True, "cards": [copy.deepcopy(card)],
                "selected_cards": [], "num_cards": 1,
                "any_number": False, "confirm_up": False,
            },
        }
        grid_state = {
            "phase": "GRID", "options": [copy.deepcopy(grid_option)],
            "available_commands": ["choose"],
            "game_state": copy.deepcopy(grid_before),
        }
        grid_consequence = autoplay._structured_option_consequence(
            "GRID", grid_option["target"], grid_option["label"],
            state=grid_state,
        )
        grid_claim = {
            key: copy.deepcopy(value)
            for key, value in grid_consequence.items()
            if key in autoplay._PRODUCER_EFFECT_FIELDS
        }
        grid_record = self.production_record(
            phase="GRID", options=[grid_option], commands=["choose"],
            payload={"action": "choose", "option_id": "grid:uuid-strike"},
            candidates=[production_candidate(
                "grid:uuid-strike", 0, 1, consequences=grid_claim,
            )],
            before=grid_before, after={
                "gold": 100,
                "deck": [copy.deepcopy(card), copy.deepcopy(other_card)],
            },
        )
        self.resequence_record(grid_record, 11, 12)
        grid_confirmation_before = copy.deepcopy(grid_before)
        grid_confirmation_before["screen_state"].update({
            "selected_cards": [], "confirm_up": True,
        })
        confirmation = self.production_record(
            phase="GRID", options=[], commands=["confirm"],
            payload={"action": "proceed"}, candidates=[],
            before=grid_confirmation_before,
            after={
                "gold": 25, "deck": [copy.deepcopy(other_card)],
                "screen_state": {
                    "for_upgrade": False, "for_transform": False,
                    "for_purge": False, "cards": [],
                },
            },
        )
        self.resequence_record(confirmation, 12, 13)
        complete_report = self.audit([
            controller_start(), record, grid_record, confirmation, terminal(),
        ])
        self.assertEqual("clear", complete_report["status"], complete_report)

        legacy_shop = copy.deepcopy(record)
        legacy_shop_row = next(
            row for row in legacy_shop["legal_choices_before"]
            if row.get("selected") is True
        )
        legacy_shop_consequence = legacy_shop_row["consequences"]
        legacy_shop_consequence["gold_delta"] = -75
        legacy_shop_effect = legacy_shop_consequence["future_costs"][0]
        legacy_shop_effect.pop("commit_timing")
        legacy_shop_effect.pop("gold_cost")
        legacy_shop_consequence["uncertainty_classification"] = {
            "status": "classified_future",
            "authority": "protocol_multistage_operation",
            "reason": (
                "purge cost is immediate and card identity is bound by the "
                "subsequent grid receipt"
            ),
        }

        legacy_grid = copy.deepcopy(grid_record)
        legacy_grid_row = next(
            row for row in legacy_grid["legal_choices_before"]
            if row.get("selected") is True
        )
        legacy_grid_consequence = legacy_grid_row["consequences"]
        legacy_grid_consequence["card_changes"]["remove"] = [
            copy.deepcopy(card)
        ]
        legacy_grid_consequence["future_costs"] = []
        legacy_grid_consequence["uncertainty_classification"] = {
            "status": "none",
            "authority": "production_mechanics_projection",
            "reason": "grid_purge_exact_card_instance",
        }

        legacy_report = self.audit([
            controller_start(), legacy_shop, legacy_grid,
            confirmation, terminal(),
        ])
        self.assertNotIn(
            "observable_delta_mismatch", issue_kinds(legacy_report)
        )
        self.assertNotIn(
            "deferred_choice_settlement_mismatch",
            issue_kinds(legacy_report),
        )
        legacy_unknown_reasons = {
            item.get("reason") for item in legacy_report["unknowns"]
        }
        self.assertIn(
            "historical_shop_purge_commit_evidence_missing",
            legacy_unknown_reasons,
        )
        self.assertIn(
            "historical_grid_confirmation_evidence_missing",
            legacy_unknown_reasons,
        )

        wrong_gold = copy.deepcopy(confirmation)
        wrong_gold["observable_state_after"]["gold"] = 26
        wrong_gold["authoritative_state_after"]["game_state"]["gold"] = 26
        wrong_gold["decision_outcome"] = autoplay.observable_inventory_delta(
            wrong_gold["observable_state_before"],
            wrong_gold["observable_state_after"],
        )
        wrong_gold["authoritative_choice_settlement"] = (
            autoplay.authoritative_choice_settlement(wrong_gold)
        )
        wrong_gold_report = self.audit([
            controller_start(), record, grid_record, wrong_gold, terminal(),
        ])
        self.assertEqual("issues", wrong_gold_report["status"])
        self.assertIn(
            "deferred_choice_settlement_mismatch",
            issue_kinds(wrong_gold_report),
        )

        wrong_card = copy.deepcopy(confirmation)
        wrong_card["observable_state_after"]["deck"] = [copy.deepcopy(card)]
        wrong_card["authoritative_state_after"]["game_state"]["deck"] = [
            copy.deepcopy(card)
        ]
        wrong_card["decision_outcome"] = autoplay.observable_inventory_delta(
            wrong_card["observable_state_before"],
            wrong_card["observable_state_after"],
        )
        wrong_card["authoritative_choice_settlement"] = (
            autoplay.authoritative_choice_settlement(wrong_card)
        )
        wrong_card_report = self.audit([
            controller_start(), record, grid_record, wrong_card, terminal(),
        ])
        self.assertEqual("issues", wrong_card_report["status"])
        self.assertIn(
            "deferred_choice_settlement_mismatch",
            issue_kinds(wrong_card_report),
        )

        false_claim = copy.deepcopy(claim)
        false_claim["gold_delta"] = -74
        false_claim["card_changes"] = {
            "gain": [], "remove": [copy.deepcopy(card)],
            "upgrade": [], "transform": [],
        }
        forged = self.production_record(
            phase="SHOP_SCREEN", options=[option], commands=["choose"],
            payload={"action": "choose", "option_id": "purge"},
            candidates=[production_candidate(
                "purge", 0, 1, consequences=false_claim,
            )],
            before=before, after={"gold": 25},
        )
        forged_report = self.audit(base_records(forged))
        self.assertEqual("issues", forged_report["status"])
        self.assertIn(
            "producer_consequence_claim_contradicted",
            issue_kinds(forged_report),
        )

        cost_tamper = copy.deepcopy(option)
        cost_tamper["target"]["price"] = 74
        cost_record = self.production_record(
            phase="SHOP_SCREEN", options=[cost_tamper], commands=["choose"],
            payload={"action": "choose", "option_id": "purge"},
            candidates=[production_candidate(
                "purge", 0, 1,
                consequences={"gold_delta": -74},
            )],
            before=before, after={"gold": 26},
        )
        cost_report = self.audit(base_records(cost_record))
        self.assertNotEqual("clear", cost_report["status"])
        self.assertGreater(cost_report["eligible_unknown_count"], 0)

    def test_match_hidden_outcome_is_classified_but_not_silently_cleared(self):
        record = self.production_record(
            phase="EVENT",
            options=[{
                "option_id": "match:0", "choice_index": 0, "label": "[?]",
                "target": {"kind": "event_option", "event_id": "MatchAndKeep"},
            }],
            commands=["choose"],
            payload={"action": "choose", "option_id": "match:0"},
            candidates=[production_candidate(
                "match:0", 0, 1,
                consequences={"operation": "flip_match_position"},
            )],
        )
        row = record["legal_choices_before"][0]
        self.assertEqual(
            "protocol_hidden",
            row["consequences"]["uncertainty_classification"]["status"],
        )

        report = self.audit(base_records(record))

        self.assertEqual("inconclusive", report["status"])
        self.assertGreater(report["eligible_unknown_count"], 0)

    def test_match_visible_board_operations_are_classified_without_eligible_unknowns(self):
        options = [
            {
                "option_id": f"match:{index}", "choice_index": index,
                "label": f"card{index}",
                "target": {
                    "kind": "event_option", "event_id": "MatchAndKeep",
                    "original_button_index": index,
                },
            }
            for index in range(2)
        ]
        before = {
            "screen_state": {
                "event_id": "MatchAndKeep",
                "options": [
                    {"choice_index": index, "original_button_index": index}
                    for index in range(2)
                ],
            },
        }
        candidates = [
            production_candidate(
                f"match:{index}", index, 2 - index,
                consequences={"operation": "flip_match_position"},
            )
            for index in range(2)
        ]
        record = self.production_record(
            phase="EVENT", options=options, commands=["choose"],
            payload={"action": "choose", "option_id": "match:0"},
            candidates=candidates, before=before,
        )

        report = self.audit(base_records(record))

        self.assertEqual("clear", report["status"], report)
        self.assertEqual(0, report["eligible_unknown_count"])
        self.assertEqual(
            {"operation": "flip_match_position"},
            record["legal_choices_before"][0][
                "producer_consequence_claim"
            ],
        )

    def test_match_single_continue_with_empty_claim_is_typed_protocol_hidden(self):
        option = {
            "option_id": "match:continue", "choice_index": 0,
            "label": "Continue",
            "target": {
                "kind": "event_option", "event_id": "MatchAndKeep",
                "original_button_index": 0,
            },
        }
        before = {
            "screen_state": {
                "event_id": "MatchAndKeep",
                "options": [{"choice_index": 0, "original_button_index": 0}],
            },
        }
        record = self.production_record(
            phase="EVENT", options=[option], commands=["choose"],
            payload={"action": "choose", "option_id": "match:continue"},
            candidates=[production_candidate(
                "match:continue", 0, 1, consequences={},
            )],
            before=before,
        )

        report = self.audit(base_records(record))

        self.assertEqual("clear", report["status"], report)
        self.assertEqual(0, report["eligible_unknown_count"])

    def test_sensory_stone_branches_clear_with_deferred_reward_classification(self):
        options = [
            {
                "option_id": f"sensory:{index}", "choice_index": index,
                "label": label,
                "target": {"kind": "event_option", "event_id": "SensoryStone"},
            }
            for index, label in enumerate(("Recall", "Tactician", "Strategist"))
        ]
        candidates = [
            production_candidate(
                f"sensory:{index}", index, 3 - index,
                consequences={
                    "hp_delta": (0, -5, -10)[index],
                    "future_costs": [{
                        "kind": "deferred_colorless_card_reward_choices",
                        "count": index + 1,
                    }],
                },
            )
            for index in range(3)
        ]
        record = self.production_record(
            phase="EVENT", options=options, commands=["choose"],
            payload={"action": "choose", "option_id": "sensory:0"},
            candidates=candidates,
        )

        report = self.audit(base_records(record))

        self.assertEqual("clear", report["status"], report)
        self.assertEqual(
            "classified_future",
            record["legal_choices_before"][0]["consequences"]
            ["uncertainty_classification"]["status"],
        )

    def test_resource_preparation_use_discard_surface_clears_end_to_end(self):
        held = {
            "id": "Fruit Juice", "name": "Fruit Juice",
            "potion_instance_id": "potion:held", "slot": 0,
            "can_use": True, "can_discard": True, "requires_target": False,
        }
        reward = {"id": "Dexterity Potion", "name": "Dexterity Potion"}
        candidates = [
            production_candidate(
                "resource:discard", 0, 1,
                action="potion", operation="discard",
                consequences={
                    "operation": "discard_held_potion_for_reward_slot",
                    "potion_id": "Fruit Juice", "potion_slot": 0,
                    "bound_reward_potion_id": "Dexterity Potion",
                },
            ),
            production_candidate(
                "resource:use", 0, 2,
                action="potion", operation="use",
                consequences={
                    "operation": "use_held_potion_for_reward_slot",
                    "potion_id": "Fruit Juice", "potion_slot": 0,
                    "bound_reward_potion_id": "Dexterity Potion",
                },
            ),
        ]
        record = self.production_record(
            phase="COMBAT_REWARD",
            options=[{
                "option_id": "reward:potion", "choice_index": 0,
                "label": "Dexterity Potion",
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "potion", "potion": reward},
                },
            }],
            commands=["choose", "potion"],
            payload={
                "action": "potion", "operation": "use",
                "potion_instance_id": "potion:held",
            },
            candidates=candidates,
            before={"potions": [held]},
            after={"current_hp": 55, "max_hp": 85, "potions": []},
        )

        report = self.audit(base_records(record))

        self.assertEqual("clear", report["status"], report)
        self.assertEqual(2, len(record["legal_choices_before"]))
        self.assertEqual(
            {"use", "discard"},
            {row["operation"] for row in record["legal_choices_before"]},
        )

    def test_combat_reward_potion_id_binds_raw_choice_index_and_identity(self):
        potion = {"id": "Dexterity Potion", "name": "Dexterity Potion"}
        option = {
            "option_id": "reward:potion:4", "choice_index": 4,
            "target": {
                "kind": "reward",
                "reward": {"reward_type": "POTION", "potion": potion},
            },
        }
        raw = independent_oracle._raw_option_contract(
            option, "COMBAT_REWARD"
        )
        record = {
            "phase": "COMBAT_REWARD",
            "available_options_before": [copy.deepcopy(option)],
        }
        expected = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD", raw, record
        )

        def claim_row(value):
            return {
                "producer_consequence_raw": {
                    "present": True, "value": copy.deepcopy(value),
                },
                "producer_consequence_claim": copy.deepcopy(value),
                "producer_scoring_facts": {},
                "unclassified_producer_fields": [],
            }

        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, claim_row({"potion_id": "Dexterity Potion"}),
                expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        forged, unresolved = independent_oracle._review_producer_consequence_claim(
            record, raw, claim_row({"potion_id": "Ghost Potion"}),
            expected, None,
        )
        self.assertEqual([], unresolved)
        self.assertEqual("potion_id", forged[0]["field"])

        wrong_index = copy.deepcopy(record)
        wrong_index["available_options_before"][0]["choice_index"] = 5
        forged, unresolved = independent_oracle._review_producer_consequence_claim(
            wrong_index, raw,
            claim_row({"potion_id": "Dexterity Potion"}), expected, None,
        )
        self.assertEqual([], unresolved)
        self.assertEqual(
            "raw_reward_choice_binding_mismatch", forged[0]["reason"]
        )

    def test_singing_bowl_mechanics_increase_current_and_max_hp(self):
        option = {
            "option_id": "bowl", "choice_index": 2,
            "target": {"kind": "bowl", "audit_projection_version": 2},
        }
        raw = independent_oracle._raw_option_contract(
            option, "CARD_REWARD"
        )

        expected = independent_oracle._expected_visible_consequence(
            "CARD_REWARD", raw,
            {"phase": "CARD_REWARD", "available_options_before": [option]},
        )

        self.assertEqual(2, expected["hp_delta"])
        self.assertEqual(2, expected["max_hp_delta"])
        self.assertEqual("singing_bowl", expected["operation"])

    def test_fairy_revival_recovers_gross_damage_from_potion_delta(self):
        before = {
            "room_phase": "COMBAT", "relics": [],
            "potions": [{
                "id": "FairyPotion",
                "potion_instance_id": "potion:fairy",
            }],
        }
        after = {
            "room_phase": "COMBAT", "relics": [], "potions": [],
        }
        player = {"max_hp": 85, "powers": []}

        gross = independent_oracle._oracle_exact_fairy_revival_hp_loss(
            before, after, player, 11.0, 20.0,
        )

        self.assertEqual(16.0, gross)

    def test_fairy_revival_does_not_add_toy_ornithopter_healing(self):
        before = {
            "room_phase": "COMBAT",
            "relics": [{"id": "Toy Ornithopter"}],
            "potions": [{
                "id": "FairyPotion",
                "potion_instance_id": "potion:fairy",
            }],
        }
        after = {
            "room_phase": "COMBAT",
            "relics": [{"id": "Toy Ornithopter"}],
            "potions": [],
        }
        player = {"max_hp": 90, "powers": []}

        gross = independent_oracle._oracle_exact_fairy_revival_hp_loss(
            before, after, player, 3.0, 20.0,
        )

        # 3 + 27 - 20: the automatic revive does not count as potion use.
        self.assertEqual(10.0, gross)

    def test_shop_replacement_binds_held_instance_and_exact_parent_transaction(self):
        held = {
            "id": "Fruit Juice", "name": "Fruit Juice",
            "potion_instance_id": "held:fruit", "slot": 1,
            "can_use": True, "can_discard": True,
            "requires_target": False,
        }
        new = {
            "id": "Dexterity Potion", "name": "Dexterity Potion",
            "price": 55,
        }
        parent = {
            "option_id": "shop:potion:dex", "choice_index": 7,
            "target": {"kind": "potion", "item": copy.deepcopy(new)},
        }
        raw = {
            "choice_id": "potion:discard:held",
            "choice_index": 1,
            "action": "potion", "operation": "discard",
            "target": {
                "kind": "potion_resource", "operation": "discard",
                "potion_id": "Fruit Juice",
                "potion_instance_id": "held:fruit", "slot": 1,
                "potion": copy.deepcopy(held),
            },
        }
        before = authoritative_state(
            10,
            observable={
                "current_hp": 50, "max_hp": 80, "gold": 100,
                "deck": [], "relics": [], "potions": [held],
            },
            room_phase="COMPLETE",
        )
        record = {
            "phase": "SHOP_SCREEN",
            "authoritative_state_before": before,
            "available_options_before": [copy.deepcopy(parent)],
        }
        expected = independent_oracle._expected_visible_consequence(
            "SHOP_SCREEN", raw, record
        )

        def replacement_row(new_id="Dexterity Potion", instance="held:fruit"):
            value = {
                "operation": "discard_held_potion_for_bound_purchase",
                "potion_id": "Fruit Juice", "potion_slot": 1,
                "potion_instance_id": instance,
                "bound_new_potion_id": new_id,
            }
            return {
                "producer_consequence_raw": {
                    "present": True, "value": copy.deepcopy(value),
                },
                "producer_consequence_claim": {
                    key: copy.deepcopy(item) for key, item in value.items()
                    if key in independent_oracle._PRODUCER_EFFECT_FIELDS
                },
                "producer_scoring_facts": {},
                "unclassified_producer_fields": sorted(
                    set(value) - independent_oracle._PRODUCER_EFFECT_FIELDS
                ),
            }

        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, replacement_row(), expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

        missing_parent = copy.deepcopy(record)
        missing_parent.pop("available_options_before")
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                missing_parent, raw, replacement_row(), expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertIn(
            "bound_new_potion_id:bound_potion_parent_options_missing",
            unresolved,
        )

        forged_parent, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, replacement_row("Ghost Potion"), expected, None,
            )
        )
        self.assertEqual([], unresolved)
        self.assertEqual(
            "bound_new_potion_id_not_on_parent_surface",
            next(item for item in forged_parent
                 if item["field"] == "bound_new_potion_id")["reason"],
        )

        forged_instance, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                record, raw, replacement_row(instance="held:ghost"),
                expected, None,
            )
        )
        self.assertEqual([], unresolved)
        self.assertEqual(
            "potion_instance_id",
            next(item for item in forged_instance
                 if item["field"] == "potion_instance_id")["field"],
        )

        reordered = copy.deepcopy(record)
        empty = {
            "id": "Potion Slot", "potion_instance_id": "slot:0", "slot": 0,
        }
        reordered["authoritative_state_before"]["game_state"]["potions"] = [
            empty, copy.deepcopy(held),
        ]
        contradictions, unresolved = (
            independent_oracle._review_producer_consequence_claim(
                reordered, raw, replacement_row(), expected, None,
            )
        )
        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

    def test_new_passive_relic_pickups_have_exact_no_immediate_delta(self):
        for relic_id in (
            "Dream Catcher", "Chemical X", "Runic Cube", "Runic Dome",
            "Orichalcum", "Sling", "HornCleat", "Strange Spoon", "Art of War",
            "Paper Frog", "Philosopher's Stone", "Toxic Egg 2",
            "Frozen Egg 2", "Tiny Chest", "The Courier", "WingedGreaves",
        ):
            with self.subTest(relic=relic_id):
                projection_version = (
                    3
                    if relic_id in {
                        "Art of War", "Paper Frog", "Tiny Chest",
                        "The Courier", "WingedGreaves",
                    }
                    else 2
                )
                raw = {
                    "choice_id": f"reward:{relic_id}",
                    "choice_index": 0,
                    "target": {
                        "kind": "reward",
                        "audit_projection_version": projection_version,
                        "reward": {
                            "reward_type": "RELIC",
                            "relic": {"id": relic_id, "name": relic_id},
                        },
                    },
                }
                expected = independent_oracle._expected_visible_consequence(
                    "COMBAT_REWARD", raw, {}
                )
                self.assertEqual(0, expected["hp_delta"])
                self.assertEqual(0, expected["max_hp_delta"])
                self.assertEqual(0, expected["gold_delta"])
                self.assertEqual(
                    {"gain": [], "remove": [], "upgrade": [], "transform": []},
                    expected["card_changes"],
                )
                self.assertEqual(
                    "known",
                    expected["field_knowledge"]["card_changes"]["status"],
                )
                relic = {"id": relic_id, "name": relic_id, "counter": -1}
                option = {
                    "option_id": "reward:relic:0", "choice_index": 0,
                    "label": relic_id,
                    "target": {
                        "kind": "reward",
                        "audit_projection_version": projection_version,
                        "reward": {"reward_type": "RELIC", "relic": relic},
                    },
                }
                candidates = [
                    production_candidate(
                        "reward:relic:0", 0, 10,
                        consequences={
                            "operation": "collect_combat_reward",
                            "reward_type": "relic", "relic_id": relic_id,
                        },
                    ),
                    production_candidate(
                        "action:proceed", None, 0, action="proceed",
                        consequences={"operation": "proceed"},
                    ),
                ]
                record = self.production_record(
                    phase="COMBAT_REWARD", options=[option],
                    commands=["choose", "proceed"],
                    payload={"action": "choose", "option_id": "reward:relic:0"},
                    candidates=candidates,
                    before={"relics": []}, after={"relics": [relic]},
                )
                report = self.audit(base_records(record))
                self.assertEqual("clear", report["status"], report)
        self.assertNotIn(
            "whetstone", independent_oracle._ORACLE_PASSIVE_RELIC_PICKUPS
        )

    def test_sapphire_linked_tiny_chest_uses_passive_pickup_contract(self):
        relic = {
            "counter": -1, "id": "Tiny Chest", "name": "小宝箱",
            "tier": "COMMON",
        }
        target = {
            "kind": "reward",
            "audit_projection_version": 3,
            "reward": {"reward_type": "RELIC", "relic": relic},
        }

        expected = independent_oracle._expected_visible_consequence(
            "SAPPHIRE_KEY", {"target": target}, {"phase": "SAPPHIRE_KEY"}
        )

        self.assertIn(
            "tinychest",
            independent_oracle._ORACLE_PASSIVE_RELIC_PICKUPS_V3,
        )
        self.assertEqual("gain_linked_relic", expected["operation"])
        self.assertEqual(0, expected["hp_delta"])
        self.assertEqual(0, expected["max_hp_delta"])
        self.assertEqual(0, expected["gold_delta"])
        self.assertEqual([relic], expected["relic_changes"]["gain"])
        for field in (
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
        ):
            self.assertEqual(
                "known", expected["field_knowledge"][field]["status"]
            )

    def test_campfire_dig_lift_and_bloom_rest_are_typed_independently(self):
        game = {
            "current_hp": 10,
            "max_hp": 80,
            "relics": [
                {"id": "Mark of the Bloom", "counter": -1},
                {"id": "Girya", "counter": 1},
                {"id": "Shovel", "counter": -1},
            ],
        }
        state = {"phase": "REST", "game_state": copy.deepcopy(game)}
        record = {
            "phase": "REST",
            "authoritative_state_before": {
                "protocol_version": 2,
                "game_state": copy.deepcopy(game),
            },
        }

        for rest_option, operation in (
            ("DIG", "campfire_dig"),
            ("LIFT", "campfire_lift"),
        ):
            with self.subTest(rest_option=rest_option):
                target = {"kind": "rest", "rest_option": rest_option}
                production = autoplay._structured_option_consequence(
                    "REST", target, rest_option, state=state
                )
                expected = independent_oracle._expected_visible_consequence(
                    "REST", {"target": target}, record
                )
                self.assertEqual(operation, production["operation"])
                self.assertEqual(operation, expected["operation"])
                self.assertEqual(
                    production["future_costs"], expected["future_costs"]
                )
                self.assertEqual(
                    production["relic_changes"], expected["relic_changes"]
                )

        rest_target = {"kind": "rest", "rest_option": "REST"}
        production_rest = autoplay._structured_option_consequence(
            "REST", rest_target, "REST", state=state
        )
        expected_rest = independent_oracle._expected_visible_consequence(
            "REST", {"target": rest_target}, record
        )
        self.assertEqual(0, production_rest["hp_delta"])
        self.assertEqual(0, expected_rest["hp_delta"])

    def test_blind_review_proves_sole_free_combat_reward_over_proceed(self):
        reward_id = "reward:relic:0"
        proceed_id = "action:proceed"
        expected_by_id = {
            reward_id: {
                "target": {
                    "kind": "reward",
                    "audit_projection_version": 2,
                    "reward": {
                        "reward_type": "RELIC",
                        "relic": {"id": "Sundial"},
                    },
                },
            },
            proceed_id: {
                "target": {
                    "kind": "protocol_action", "action": "proceed",
                },
            },
        }
        canonical_by_id = {
            choice_id: {"consequences": {}}
            for choice_id in expected_by_id
        }

        review = independent_oracle._blind_consequence_review(
            {
                "phase": "COMBAT_REWARD",
                "authoritative_state_before": {
                    "protocol_version": 2,
                    "game_state": {"potions": [], "relics": []},
                },
            },
            expected_by_id,
            canonical_by_id,
        )

        self.assertEqual("clear", review["status"])
        self.assertEqual(reward_id, review["recommended_choice_id"])
        self.assertEqual(
            "sole_free_combat_reward_dominates_proceed", review["reason"]
        )

    def test_whetstone_settlement_is_at_most_two_random_attacks_and_order_free(self):
        attacks = [
            combat_choice_card("Strike_R", "attack-a"),
            combat_choice_card("Bash", "attack-b"),
            combat_choice_card("Pommel Strike", "attack-c"),
        ]
        for attack in attacks:
            attack["type"] = "ATTACK"
        skill = combat_choice_card("Defend_R", "skill-a")
        skill["type"] = "SKILL"
        deck = attacks + [skill]
        relic = {"id": "Whetstone", "name": "Whetstone", "counter": -1}
        raw = {
            "choice_id": "reward:whetstone", "choice_index": 2,
            "target": {
                "kind": "reward",
                "reward": {"reward_type": "RELIC", "relic": relic},
            },
        }

        def state(seq, cards, *, relics=None, hp=50, with_uuid=True):
            cards = copy.deepcopy(cards)
            if not with_uuid:
                cards[0].pop("card_instance_id", None)
            return authoritative_state(
                seq,
                observable={
                    "current_hp": hp, "max_hp": 80, "gold": 100,
                    "deck": cards, "relics": copy.deepcopy(relics or []),
                    "potions": [],
                },
                room_phase="COMPLETE",
                existing={"phase": "COMBAT_REWARD"},
            )

        def pickup_record(after_cards, *, before_cards=None, hp=50,
                          before_state=None):
            return {
                "before_seq": 10, "after_seq": 11,
                "selected_choice_ids": ["reward:whetstone"],
                "authoritative_state_before": (
                    before_state or state(10, before_cards or deck)
                ),
                "authoritative_state_after": state(
                    11, after_cards, relics=[relic], hp=hp
                ),
            }

        upgraded = copy.deepcopy(deck)
        upgraded[0]["upgrades"] = 1
        upgraded[1]["upgrades"] = 1
        upgraded = [upgraded[3], upgraded[2], upgraded[0], upgraded[1]]
        record = pickup_record(upgraded)
        expected = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD", raw, record
        )

        settlement = expected["whetstone_settlement"]
        self.assertEqual("clear", settlement["status"])
        self.assertEqual(2, settlement["expected_upgrade_count"])
        self.assertEqual(
            ["attack-a", "attack-b"],
            settlement["upgraded_card_instance_ids"],
        )
        self.assertEqual(
            independent_oracle._ORACLE_WHETSTONE_CLASS_SHA256,
            settlement["jar_class_sha256"],
        )
        self.assertEqual(
            "known", expected["field_knowledge"]["card_changes"]["status"]
        )

        option = {
            "option_id": "reward:whetstone", "choice_index": 2,
            "label": "Whetstone", "target": copy.deepcopy(raw["target"]),
        }
        candidates = [
            production_candidate(
                "reward:whetstone", 2, 10,
                consequences={
                    "operation": "collect_combat_reward",
                    "reward_type": "relic", "relic_id": "Whetstone",
                },
            ),
            production_candidate(
                "action:proceed", None, 0, action="proceed",
                consequences={"operation": "proceed"},
            ),
        ]
        integrated = self.production_record(
            phase="COMBAT_REWARD", options=[option],
            commands=["choose", "proceed"],
            payload={"action": "choose", "option_id": "reward:whetstone"},
            candidates=candidates,
            before={"deck": deck, "relics": []},
            after={"deck": upgraded, "relics": [relic]},
        )
        integrated_report = self.audit(base_records(integrated))
        self.assertEqual("clear", integrated_report["status"], integrated_report)

        one_attack = [attacks[0], skill]
        one_upgraded = copy.deepcopy(one_attack)
        one_upgraded[0]["upgrades"] = 1
        one = pickup_record(one_upgraded, before_cards=one_attack)
        one_expected = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD", raw, one
        )
        self.assertEqual("clear", one_expected["whetstone_settlement"]["status"])
        self.assertEqual(1, one_expected["whetstone_settlement"]["expected_upgrade_count"])

    def test_whetstone_rejects_wrong_count_type_uuid_extra_delta_and_missing_evidence(self):
        attack_a = combat_choice_card("Strike_R", "attack-a")
        attack_b = combat_choice_card("Bash", "attack-b")
        attack_c = combat_choice_card("Pommel Strike", "attack-c")
        for attack in (attack_a, attack_b, attack_c):
            attack["type"] = "ATTACK"
        skill = combat_choice_card("Defend_R", "skill-a")
        skill["type"] = "SKILL"
        deck = [attack_a, attack_b, attack_c, skill]
        relic = {"id": "Whetstone", "name": "Whetstone", "counter": -1}

        def make_state(seq, cards, *, hp=50, relics=None):
            return authoritative_state(
                seq,
                observable={
                    "current_hp": hp, "max_hp": 80, "gold": 100,
                    "deck": copy.deepcopy(cards),
                    "relics": copy.deepcopy(relics or []), "potions": [],
                },
                room_phase="COMPLETE",
            )

        def settle(after_cards, *, hp=50, before_cards=None):
            return independent_oracle._oracle_whetstone_settlement(
                {
                    "before_seq": 10, "after_seq": 11,
                    "authoritative_state_before": make_state(
                        10, before_cards or deck
                    ),
                    "authoritative_state_after": make_state(
                        11, after_cards, hp=hp, relics=[relic]
                    ),
                },
                relic, None,
            )

        wrong_count = copy.deepcopy(deck)
        wrong_count[0]["upgrades"] = 1
        non_attack = copy.deepcopy(deck)
        non_attack[0]["upgrades"] = 1
        non_attack[3]["upgrades"] = 1
        wrong_uuid = copy.deepcopy(deck)
        wrong_uuid[0]["card_instance_id"] = "forged-uuid"
        valid = copy.deepcopy(deck)
        valid[0]["upgrades"] = 1
        valid[1]["upgrades"] = 1

        cases = (
            (wrong_count, 50, "issues", "whetstone_upgrade_count_mismatch"),
            (non_attack, 50, "issues", "whetstone_upgraded_noneligible_card"),
            (wrong_uuid, 50, "issues", "whetstone_deck_uuid_set_changed"),
            (valid, 51, "issues", "whetstone_unexpected_resource_delta"),
        )
        for cards, hp, status, reason in cases:
            with self.subTest(reason=reason):
                observed_status, observed_reason, _details = settle(
                    cards, hp=hp
                )
                self.assertEqual(status, observed_status)
                self.assertEqual(reason, observed_reason)

        missing = copy.deepcopy(deck)
        missing[0].pop("card_instance_id")
        status, reason, _details = settle(valid, before_cards=missing)
        self.assertEqual("unknown", status)
        self.assertEqual(
            "whetstone_deck_uuid_missing_or_duplicate", reason
        )

    def test_score_receipt_recomputation_detects_score_component_and_input_faults(self):
        for mutation, expected_status in (
            (lambda row: row.__setitem__("local_score", 99), "issues"),
            (
                lambda row: row["score_components"][0].__setitem__("value", 99),
                "issues",
            ),
            (lambda row: row["score_inputs"].clear(), "inconclusive"),
        ):
            decision = strategic_decision()
            mutation(decision["legal_choices_before"][0])
            report = self.audit(base_records(decision))
            with self.subTest(status=expected_status):
                self.assertEqual(expected_status, report["status"])
                if expected_status == "issues":
                    self.assertIn(
                        "producer_score_evidence_contradicted",
                        issue_kinds(report),
                    )
                else:
                    self.assertIn(
                        "candidate_score_evidence_missing",
                        {item["kind"] for item in report["unknowns"]},
                    )

    def test_deleted_legal_candidate_is_detected(self):
        decision = strategic_decision()
        decision["legal_choices_before"].pop()

        report = self.audit(base_records(decision))

        self.assertIn("candidate_missing", issue_kinds(report))

    def test_missing_canonical_surface_is_inconclusive(self):
        decision = strategic_decision()
        decision.pop("legal_choices_before")

        report = self.audit(base_records(decision))

        self.assertEqual("inconclusive", report["status"])
        self.assertGreater(report["eligible_unknown_count"], 0)

    def test_extra_and_duplicate_candidates_are_detected(self):
        extra = strategic_decision()
        extra["legal_choices_before"].append(choice("option:ghost", 2, 1))
        duplicate = strategic_decision()
        duplicate["legal_choices_before"].append(
            copy.deepcopy(duplicate["legal_choices_before"][0])
        )

        extra_report = self.audit(base_records(extra))
        duplicate_report = self.audit(base_records(duplicate))

        self.assertIn("candidate_extra", issue_kinds(extra_report))
        self.assertIn("candidate_duplicate", issue_kinds(duplicate_report))

    def test_container_reordering_does_not_change_semantic_choice(self):
        decision = strategic_decision()
        decision["available_options_before"].reverse()
        decision["legal_choices_before"].reverse()

        report = self.audit(base_records(decision))

        self.assertEqual("clear", report["status"])
        self.assertEqual(0, report["disagreement_count"])

    def test_target_and_label_tampering_is_detected_independently(self):
        decision = strategic_decision()
        decision["legal_choices_before"][0]["label"] = "forged"
        decision["legal_choices_before"][1]["target"] = {
            "kind": "event_option", "event_id": "option:a",
        }

        report = self.audit(base_records(decision))

        self.assertIn("candidate_semantic_binding_mismatch", issue_kinds(report))

    def test_producer_consequence_evidence_requires_independent_binding(self):
        decision = strategic_decision()
        producer_claim = {"gold_delta": 10, "mechanism": "event-branch"}
        decision["legal_choices_before"][0]["consequences"][
            "producer_evidence"
        ] = producer_claim

        report = self.audit(base_records(decision))

        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "candidate_consequence_independence_unproven",
            {item["kind"] for item in report["unknowns"]},
        )

        decision["independent_consequence_evidence"] = {
            "option:a": {
                "status": "clear",
                "source": "independent_mechanics_oracle",
                "attempt_id": ATTEMPT,
                "decision_hash": DECISION_HASH,
                "before_seq": 10,
                "choice_id": "option:a",
                "verified_producer_evidence": producer_claim,
            }
        }
        still_unproven = self.audit(base_records(decision))
        self.assertEqual("inconclusive", still_unproven["status"])
        self.assertIn(
            "candidate_consequence_independence_unproven",
            {item["kind"] for item in still_unproven["unknowns"]},
        )

    def test_tampered_selected_consequence_is_checked_against_state_delta(self):
        decision = strategic_decision()
        decision["legal_choices_before"][0]["consequences"][
            "gold_delta"
        ] = 999

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn("observable_delta_mismatch", issue_kinds(report))

    def test_combat_choice_toy_heal_is_not_charged_to_selected_card(self):
        chosen = combat_choice_card("HeadbuttTarget", "chosen")
        other = combat_choice_card("Defend_R", "other")
        before = combat_choice_state(
            200, "GRID", "BetterDiscardPileToHandAction",
            deck=[chosen, other], hand=[other], draw=[], discard=[chosen],
            visible=[chosen], selected=[],
        )
        after = combat_choice_state(
            201, "COMBAT_TURN_1", None,
            deck=[chosen, other], hand=[other, chosen], draw=[], discard=[],
        )
        liquid = {
            "id": "LiquidMemories", "name": "Liquid Memories",
            "potion_instance_id": "potion:liquid", "slot": 0,
        }
        for state, hp, potions in (
            (before, 40, [liquid]), (after, 45, []),
        ):
            game = state["game_state"]
            game.update({
                "current_hp": hp, "max_hp": 80, "gold": 100, "block": 0,
                "relics": [{"id": "Toy Ornithopter", "counter": -1}],
                "potions": copy.deepcopy(potions),
            })
            game["combat_state"]["player"]["current_hp"] = hp
            game["combat_state"]["player"]["max_hp"] = 80

        record = combat_choice_record(before, after, card=chosen)
        option = record["available_options_before"][0]
        consequence = autoplay._structured_option_consequence(
            "GRID", option["target"], option["label"], state=before,
        )
        choice_id = option["option_id"]
        record["legal_choices_before"] = [independent_oracle.canonical_choice(
            choice_id,
            choice_index=0,
            label=option["label"],
            raw_text=option["label"],
            semantic_id=choice_id,
            target=copy.deepcopy(option["target"]),
            consequences=consequence,
            local_score=1.0,
            selected=True,
        )]
        record["available_commands_before"] = ["choose", "state"]
        record["decision"] = {"model_advice": {
            "status": "agreed", "applied": False,
            "model_choice_id": choice_id,
            "final_choice_ids": [choice_id],
        }}
        before_observable = autoplay.observable_inventory_snapshot(
            before["game_state"]
        )
        after_observable = autoplay.observable_inventory_snapshot(
            after["game_state"]
        )
        record["observable_state_before"] = before_observable
        record["observable_state_after"] = after_observable
        outcome = autoplay.observable_inventory_delta(
            before_observable, after_observable
        )
        outcome["hp_delta"] = outcome["current_hp_delta"]
        outcome["combat_choice_transition"] = (
            autoplay.combat_choice_transition_claim(before, after)
        )
        record["decision_outcome"] = outcome

        report = self.audit([controller_start(), record, terminal()])

        self.assertNotIn("observable_delta_mismatch", issue_kinds(report))
        self.assertEqual(
            5,
            independent_oracle._oracle_combat_choice_background_potion_heal(
                "GRID",
                before_observable,
                after_observable,
                outcome,
                outcome["potions"],
            ),
        )

    def test_selection_eligible_flag_cannot_hide_missing_score(self):
        decision = strategic_decision()
        hidden = decision["legal_choices_before"][1]
        hidden["selection_eligible"] = False
        hidden["local_score"] = None

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn("candidate_producer_binding_invalid", issue_kinds(report))

    def test_structured_collection_delta_is_recomputed(self):
        decision = strategic_decision()
        card = {"card_instance_id": "card:new", "id": "Bash", "upgrades": 0}
        decision["observable_state_after"]["deck"] = [card]
        decision["decision_outcome"]["deck"] = {
            "added": [], "removed": [], "changed": [],
        }

        report = self.audit(base_records(decision))

        self.assertIn("producer_observable_state_mismatch", issue_kinds(report))

    def test_missing_structured_delta_claim_is_inconclusive(self):
        decision = strategic_decision()
        decision["decision_outcome"].pop("potions")

        report = self.audit(base_records(decision))

        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "observable_delta_claim_missing",
            {item["kind"] for item in report["unknowns"]},
        )

    def test_probabilistic_consequence_requires_authoritative_settlement(self):
        decision = strategic_decision()
        probability_outcomes = [{"probability": 0.5, "gold_delta": 10}]
        decision["legal_choices_before"][0]["probability_outcomes"] = (
            probability_outcomes
        )
        decision["legal_choices_before"][0]["consequences"][
            "probabilistic_outcomes"
        ] = probability_outcomes

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn("candidate_consequence_binding_mismatch", issue_kinds(report))

        decision["authoritative_choice_settlement"] = {
            "status": "observed",
            "authority": "protocol_state_delta",
            "fully_observable": True,
            "choice_id": "option:a",
            "before_seq": 10,
            "after_seq": 11,
            "observed_outcome": {"gold_delta": 10},
        }
        still_unproven = self.audit(base_records(decision))
        self.assertEqual("issues", still_unproven["status"])

    def test_tampered_final_choice_is_detected(self):
        decision = strategic_decision()
        decision["final_choice_ids"] = ["option:b"]
        decision["decision"]["model_advice"]["final_choice_ids"] = [
            "option:b"
        ]

        report = self.audit(base_records(decision))

        self.assertIn("final_choice_binding_mismatch", issue_kinds(report))

    def test_tampered_scores_cannot_self_prove_nonlocal_selection(self):
        decision = strategic_decision()
        decision["legal_choices_before"][1]["local_score"] = 20

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn(
            "producer_score_order_contradicts_independent_consequences",
            issue_kinds(report),
        )

    def test_explicit_local_score_contract_classifies_argmax_miss_as_issue(self):
        decision = strategic_decision()
        first, second = decision["legal_choices_before"]
        first["target"]["amount"] = 0
        first["label"] = "Wait"
        first["raw_text"] = "Wait"
        first["consequences"] = structured_consequence(
            "Wait", event_id="option:a", gold_delta=0,
        )
        decision["available_options_before"][0]["target"]["amount"] = 0
        decision["available_options_before"][0]["label"] = "Wait"
        first["selected"] = False
        second["selected"] = True
        decision.update({
            "selected_choice_ids": ["option:b"],
            "requested_target_id": "option:b",
            "resolved_target_id": "option:b",
            "final_choice_ids": ["option:b"],
        })
        decision["observable_state_after"]["gold"] = 100
        decision["authoritative_state_after"]["game_state"]["gold"] = 100
        decision["decision_outcome"]["gold_delta"] = 0
        decision["decision"]["model_advice"] = {
            "status": "not_consulted",
            "applied": False,
            "confidence": None,
            "final_choice_ids": ["option:b"],
        }
        decision["decision"]["candidate_contract"] = {
            "contract_schema_version": 1,
            "score_source": "explicit_local_utility",
            "strategy_quality_auditable": True,
            "all_visible_options_scored": True,
        }

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn("local_argmax_missed", issue_kinds(report))
        self.assertNotIn(
            "nonlocal_selection_without_independent_blind_review",
            {item["kind"] for item in report["unknowns"]},
        )

    def test_model_override_flags_do_not_clear_nonlocal_argmax(self):
        decision = strategic_decision()
        first, second = decision["legal_choices_before"]
        first["selected"] = False
        second.update({
            "selected": True,
            "consequences": structured_consequence(
                "B", event_id="option:b", gold_delta=10,
            ),
            "model_score": 100,
            "model_confidence": 0.9,
            "final_source": "model",
            "override": {"applied": True, "gate_passed": True},
        })
        decision.update({
            "selected_choice_ids": ["option:b"],
            "requested_target_id": "option:b",
            "resolved_target_id": "option:b",
            "final_choice_ids": ["option:b"],
        })
        decision["decision"]["model_advice"].update({
            "status": "applied",
            "applied": True,
            "model_choice_id": "option:b",
            "final_choice_ids": ["option:b"],
        })

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn(
            "high_confidence_unresolved_strategy_disagreement",
            issue_kinds(report),
        )

    def test_independent_blind_review_can_resolve_nonlocal_selection(self):
        decision = strategic_decision()
        first, second = decision["legal_choices_before"]
        for row in (first, second):
            row["consequences"]["hp_delta"] = 0
            row["consequences"]["max_hp_delta"] = 0
        first["selected"] = False
        second.update({
            "selected": True,
            "consequences": structured_consequence(
                "Gain 20 Gold", event_id="option:b", gold_delta=20,
            ),
            "model_score": 100,
            "model_confidence": 0.9,
            "final_source": "model",
            "override": {"applied": True, "gate_passed": True},
        })
        second["consequences"]["hp_delta"] = 0
        second["consequences"]["max_hp_delta"] = 0
        second["label"] = "Gain 20 Gold"
        second["raw_text"] = "Gain 20 Gold"
        second["target"]["amount"] = 20
        decision["available_options_before"][1]["label"] = "Gain 20 Gold"
        decision["available_options_before"][1]["target"]["amount"] = 20
        effect_fields = {
            "hp_delta", "max_hp_delta", "gold_delta", "card_changes",
            "relic_changes", "potion_changes", "curse",
            "probabilistic_outcomes", "current_cost", "future_costs",
            "route", "event_id", "campfire_option", "selected_card",
            "key_changes", "operation",
        }
        producer_claim = {
            key: copy.deepcopy(value)
            for key, value in second["consequences"].items()
            if key in effect_fields
        }
        second["producer_consequence_claim"] = producer_claim
        second["producer_consequence_raw"] = {
            "present": True, "value": copy.deepcopy(producer_claim),
        }
        second["producer_candidate_raw"]["consequences"] = copy.deepcopy(
            producer_claim
        )
        decision["observable_state_after"]["gold"] = 120
        decision["authoritative_state_after"]["game_state"]["gold"] = 120
        decision["decision_outcome"]["gold_delta"] = 20
        decision.update({
            "selected_choice_ids": ["option:b"],
            "requested_target_id": "option:b",
            "resolved_target_id": "option:b",
            "final_choice_ids": ["option:b"],
        })
        decision["decision"]["model_advice"].update({
            "status": "applied",
            "applied": True,
            "model_choice_id": "option:b",
            "final_choice_ids": ["option:b"],
        })

        report = self.audit(base_records(decision))

        self.assertEqual("clear", report["status"])
        self.assertEqual(
            "option:b", report["blind_reviews"][0]["recommended_choice_id"]
        )

    def test_producer_labeled_blind_review_cannot_override_independent_result(self):
        decision = strategic_decision()
        first, second = decision["legal_choices_before"]
        for row in (first, second):
            row["consequences"]["hp_delta"] = 0
            row["consequences"]["max_hp_delta"] = 0
        first["selected"] = False
        second["selected"] = True
        decision["observable_state_after"]["gold"] = 100
        decision["decision_outcome"]["gold_delta"] = 0
        decision.update({
            "selected_choice_ids": ["option:b"],
            "requested_target_id": "option:b",
            "resolved_target_id": "option:b",
            "final_choice_ids": ["option:b"],
            "independent_blind_review": {
                "status": "clear",
                "reviewer": "producer-self-label",
                "recommended_choice_id": "option:b",
            },
            "reason": "producer claims option:b is safest",
        })
        decision["decision"]["reason"] = (
            "producer score and rationale claim option:b is optimal"
        )

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn(
            "independent_blind_review_disagreement", issue_kinds(report)
        )
        self.assertEqual(
            "option:a", report["blind_reviews"][0]["recommended_choice_id"]
        )

    def test_terminal_trace_cannot_self_authorize_game_over(self):
        records = base_records()
        records[-1].update({
            "authoritative_game_over": True,
            "screen_type": "GAME_OVER",
        })

        report = self.audit(records, artifacts={})

        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "terminal_authority_unproven",
            {item["kind"] for item in report["unknowns"]},
        )
        self.assertEqual(
            "inconclusive", report["coverage"]["terminal_authority"]["status"]
        )

    def test_tampered_heart_claim_is_detected_from_terminal_state(self):
        terminal_row = terminal()
        terminal_row.update({
            "victory": True,
            "heart_defeated": True,
            "act": 4,
            "observed_max_act": 4,
            "current_hp": 1,
        })
        records = [controller_start(), strategic_decision(), terminal_row]
        artifacts = self.artifacts(records)
        artifacts["state"]["game_state"]["heart_defeated"] = False

        report = self.audit(records, artifacts=artifacts)

        self.assertEqual("issues", report["status"])
        self.assertIn("heart_terminal_outcome_mismatch", issue_kinds(report))

    def test_claimed_act4_is_checked_against_authoritative_states(self):
        terminal_row = terminal()
        terminal_row.update({"act": 4, "observed_max_act": 4})
        records = [controller_start(), strategic_decision(), terminal_row]
        artifacts = self.artifacts(records)
        artifacts["state"]["game_state"]["act"] = 3

        report = self.audit(records, artifacts=artifacts)

        self.assertEqual("issues", report["status"])
        self.assertIn("act4_observation_mismatch", issue_kinds(report))

    def test_each_terminal_artifact_binding_field_is_enforced(self):
        records = base_records()
        binding_fields = (
            "attempt_id", "run_id", "seed", "character",
            "ascension_level", "run_type", "decision_hash",
            "controller_hash", "policy_version", "selection_id",
            "selection_digest",
        )

        baseline = self.audit(records)
        self.assertEqual(
            list(binding_fields) + ["terminal_state_seq"],
            baseline["binding_fields"],
        )
        for field in binding_fields:
            with self.subTest(field=field):
                artifacts = self.artifacts(records)
                observed = artifacts["selection"][field]
                artifacts["selection"][field] = (
                    observed + 1 if type(observed) is int else f"forged-{field}"
                )
                report = self.audit(records, artifacts=artifacts)
                matching = [
                    item for item in report["issues"]
                    if item.get("kind") == "artifact_binding_mismatch"
                    and item.get("artifact") == "selection"
                    and item.get("field") == field
                ]
                self.assertEqual(1, len(matching), report)

    def test_high_confidence_model_conflict_with_local_choice_is_an_issue(self):
        decision = strategic_decision()
        second = decision["legal_choices_before"][1]
        second["label"] = "Gain 10 Gold"
        second["raw_text"] = "Gain 10 Gold"
        second["consequences"] = structured_consequence(
            "Gain 10 Gold", event_id="option:b", gold_delta=10,
        )
        decision["available_options_before"][1]["label"] = "Gain 10 Gold"
        decision["decision"]["model_advice"].update({
            "status": "conflict",
            "applied": False,
            "model_choice_id": "option:b",
            "confidence": 0.95,
        })
        decision["legal_choices_before"][1]["model_confidence"] = 0.95

        report = self.audit(base_records(decision))

        self.assertEqual("issues", report["status"])
        self.assertIn(
            "high_confidence_unresolved_strategy_disagreement",
            issue_kinds(report),
        )

    def test_independent_dominance_can_resolve_high_confidence_model_conflict(self):
        decision = strategic_decision()
        for row in decision["legal_choices_before"]:
            row["consequences"]["hp_delta"] = 0
            row["consequences"]["max_hp_delta"] = 0
        decision["decision"]["model_advice"].update({
            "status": "conflict",
            "applied": False,
            "model_choice_id": "option:b",
            "confidence": 0.95,
        })
        decision["legal_choices_before"][1]["model_confidence"] = 0.95

        report = self.audit(base_records(decision))

        self.assertEqual("clear", report["status"])
        self.assertEqual(
            "option:a", report["blind_reviews"][0]["recommended_choice_id"]
        )

    def test_wrong_true_combat_end_is_detected_by_later_turn(self):
        claim = bound(
            "decision",
            before_seq=20,
            after_seq=21,
            phase="COMBAT_TURN_1",
            action="play",
            combat_id="combat:a",
            turn=1,
            monsters_before=[{
                "enemy_instance_id": "enemy:a",
                "current_hp": 10,
                "is_gone": False,
                "half_dead": False,
            }],
            action_true_combat_end_predicted=True,
            decision={"search": {"true_combat_end": True}},
        )
        later = bound(
            "decision",
            before_seq=21,
            after_seq=22,
            phase="COMBAT_TURN_2",
            action="play",
            combat_id="combat:a",
            turn=2,
            monsters_before=[{
                "enemy_instance_id": "enemy:a",
                "current_hp": 7,
                "is_gone": False,
                "half_dead": False,
            }],
            decision={},
        )

        report = self.audit([controller_start(), claim, later, terminal()])

        self.assertIn("true_combat_end_contradicted", issue_kinds(report))

    def test_true_combat_end_requires_authoritative_after_state(self):
        claim = bound(
            "decision",
            before_seq=20,
            after_seq=21,
            phase="COMBAT_TURN_1",
            action="play",
            combat_id="combat:a",
            turn=1,
            monsters_before=[{
                "enemy_instance_id": "enemy:a", "current_hp": 1,
                "is_gone": False, "half_dead": False,
            }],
            action_true_combat_end_predicted=True,
            decision={"search": {"true_combat_end": True}},
        )

        unproven = self.audit([controller_start(), claim, terminal()])
        self.assertEqual("inconclusive", unproven["status"])
        self.assertIn(
            "true_combat_end_unproven",
            {item["kind"] for item in unproven["unknowns"]},
        )

        claim.update({
            "authoritative_state_after": authoritative_state(21, existing={
                "state_seq": 21,
                "game_state": {
                    "room_phase": "COMPLETE",
                    "screen_type": "COMBAT_REWARD",
                    "combat_state": {"monsters": [], "player": {}},
                },
            }),
        })
        proven = self.audit([controller_start(), claim, terminal()])
        self.assertEqual("clear", proven["status"])

    def test_wrong_damage_prediction_is_detected(self):
        record = bound(
            "decision",
            before_seq=30,
            after_seq=31,
            phase="COMBAT_TURN_1",
            action="play",
            player_before={"current_hp": 50},
            monsters_before=[{
                "enemy_instance_id": "enemy:a",
                "current_hp": 10,
                "is_gone": False,
                "half_dead": False,
            }],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMBAT",
                    "current_hp": 50,
                    "combat_state": {
                        "player": {"current_hp": 50},
                        "monsters": [{
                            "enemy_instance_id": "enemy:a",
                            "current_hp": 3,
                            "is_gone": False,
                            "half_dead": False,
                        }],
                    },
                },
            },
            damage_model={
                "deterministic": True,
                "hero_to_monsters_predicted": 10,
                "hero_to_monsters_actual": 7,
            },
        )

        report = self.audit([controller_start(), record, terminal()])

        self.assertIn("damage_prediction_mismatch", issue_kinds(report))

    def test_duplication_power_lethal_cap_accepts_single_card_projection(self):
        record = {
            "decision_hash": "d1e4bd06a646bb24",
            "action": "play",
            "card_id": "Sword Boomerang",
            "card_instance_id": "boomerang:1",
            "target_id": None,
            "target_before": None,
            "hand_before": [{
                "id": "Sword Boomerang",
                "card_instance_id": "boomerang:1",
                "type": "ATTACK",
            }],
            "player_before": {
                "powers": [{"id": "DuplicationPower", "amount": 1}],
            },
            "monsters_before": [{
                "id": "Orb Walker", "current_hp": 22,
                "is_gone": False, "half_dead": False,
            }],
        }
        model = {
            "hero_to_monsters_prediction_basis": (
                "current_card_final_target_hp_projection"
            ),
        }
        self.assertTrue(
            independent_oracle._duplicated_attack_prediction_alias(
                record, model, 18, 22,
            )
        )
        record["decision_hash"] = "current-fixed-hash"
        self.assertFalse(
            independent_oracle._duplicated_attack_prediction_alias(
                record, model, 18, 22,
            )
        )

    def test_duplication_power_legacy_alias_rejects_ambiguous_combat(self):
        record = {
            "decision_hash": "d1e4bd06a646bb24",
            "action": "play",
            "card_id": "Sword Boomerang",
            "card_instance_id": "boomerang:1",
            "target_id": None,
            "target_before": None,
            "hand_before": [{
                "id": "Sword Boomerang",
                "card_instance_id": "boomerang:1",
                "type": "ATTACK",
            }],
            "player_before": {
                "powers": [{"id": "DuplicationPower", "amount": 2}],
            },
            "monsters_before": [{
                "id": "Orb Walker", "current_hp": 100,
                "is_gone": False, "half_dead": False,
            }],
        }
        model = {
            "hero_to_monsters_prediction_basis": (
                "current_card_final_target_hp_projection"
            ),
        }
        self.assertTrue(
            independent_oracle._duplicated_attack_prediction_alias(
                record, model, 18, 36,
            )
        )
        record["monsters_before"].append({
            "id": "second", "current_hp": 1,
            "is_gone": False, "half_dead": False,
        })
        self.assertFalse(
            independent_oracle._duplicated_attack_prediction_alias(
                record, model, 18, 36,
            )
        )
        record["monsters_before"].pop()
        record["player_before"]["powers"].append({
            "id": "DoubleTapPower", "amount": 1,
        })
        self.assertFalse(
            independent_oracle._duplicated_attack_prediction_alias(
                record, model, 18, 36,
            )
        )

    def test_shining_light_generic_random_upgrade_claim_is_a_typed_alias(self):
        record = {"phase": "EVENT"}
        raw = {
            "target": {
                "event_id": "Shining Light",
                "event_stage": "INTRO",
            },
        }
        expected = {
            "operation": "shining_light_enter",
            "random_effects": [{
                "count": 2,
                "eligible_card_instance_ids": ["a", "b"],
            }],
        }
        claimed = [{
            "kind": "random_card_upgrade",
            "max_count": 2,
            "count_semantics": "up_to_available",
            "domain": "upgradable_deck",
            "selection_mode": "random",
        }]
        self.assertTrue(
            independent_oracle._shining_light_random_effect_alias(
                record, raw, claimed, expected,
            )
        )

    def test_consequence_domains_accept_hidden_gold_and_new_card_uuid(self):
        self.assertTrue(independent_oracle._effect_claim_equal(
            "gold_delta", -93,
            {"kind": "hidden_offer", "minimum": -156, "maximum": 0},
        ))
        self.assertFalse(independent_oracle._effect_claim_equal(
            "gold_delta", -157,
            {"kind": "hidden_offer", "minimum": -156, "maximum": 0},
        ))
        preview = {
            "id": "Writhe", "upgrades": 0,
            "card_instance_id": "preview:temporary",
        }
        gained = {
            "id": "Writhe", "upgrades": 0,
            "card_instance_id": "deck:persistent",
        }
        self.assertTrue(independent_oracle._effect_claim_equal(
            "card_changes", {"gain": [preview]}, {"gain": [gained]},
        ))
        self.assertFalse(independent_oracle._effect_claim_equal(
            "card_changes", {"remove": [preview]}, {"remove": [gained]},
        ))

    def test_random_attack_damage_is_audited_against_exact_bounds(self):
        record = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_1", action="play",
            player_before={"current_hp": 50},
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:a", "current_hp": 10,
                    "is_gone": False, "half_dead": False,
                },
                {
                    "enemy_instance_id": "enemy:b", "current_hp": 10,
                    "is_gone": False, "half_dead": False,
                },
            ],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMBAT", "current_hp": 50,
                    "combat_state": {
                        "player": {"current_hp": 50},
                        "monsters": [
                            {
                                "enemy_instance_id": "enemy:a",
                                "current_hp": 4, "is_gone": False,
                                "half_dead": False,
                            },
                            {
                                "enemy_instance_id": "enemy:b",
                                "current_hp": 7, "is_gone": False,
                                "half_dead": False,
                            },
                        ],
                    },
                },
            },
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "current_card_random_target_hp_loss_bounds"
                ),
                "hero_to_monsters_predicted_min": 7,
                "hero_to_monsters_predicted_max": 12,
                "hero_to_monsters_actual": 9,
            },
        )

        clear = self.audit(base_records(record))
        self.assertNotIn("damage_prediction_outside_bounds", issue_kinds(clear))
        self.assertEqual(
            1, clear["coverage"]["damage_consistency"]["evaluated"]
        )

        forged = copy.deepcopy(record)
        forged["damage_model"]["hero_to_monsters_predicted_max"] = 8
        report = self.audit(base_records(forged))
        self.assertIn("damage_prediction_outside_bounds", issue_kinds(report))

    def test_juggernaut_damage_bounds_require_independent_trigger_contract(self):
        card = {
            "id": "Strike_R", "type": "ATTACK", "base_block": -1,
            "exhausts": False, "card_instance_id": "card:strike",
        }
        monster = {
            "enemy_instance_id": "enemy:a", "current_hp": 30,
            "is_gone": False, "half_dead": False,
        }
        before_game = {
            "room_phase": "COMBAT",
            "relics": [{"id": "Ornamental Fan", "counter": 2}],
            "combat_state": {
                "hand": [copy.deepcopy(card)],
                "player": {
                    "current_hp": 50,
                    "powers": [{"id": "Juggernaut", "amount": 7}],
                },
                "monsters": [copy.deepcopy(monster)],
            },
        }
        after_monster = {**copy.deepcopy(monster), "current_hp": 13}
        contract = {
            "damage_per_trigger": 7,
            "trigger_count": 1,
            "sources": [{"source": "ornamental_fan", "count": 1}],
            "card_instance_id": "card:strike",
        }
        record = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_1", action="play",
            card_id="Strike_R", card_instance_id="card:strike",
            requested_target_id="card:strike",
            player_before={"current_hp": 50},
            monsters_before=[copy.deepcopy(monster)],
            decision={
                "card_id": "Strike_R",
                "search": {"first_action_resolution_count": 1},
            },
            authoritative_state_before=authoritative_state(
                30, existing={"game_state": before_game},
            ),
            authoritative_state_after=authoritative_state(
                31, existing={"game_state": {
                    **copy.deepcopy(before_game),
                    "combat_state": {
                        **copy.deepcopy(before_game["combat_state"]),
                        "monsters": [after_monster],
                    },
                }},
            ),
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "current_card_with_juggernaut_block_gain_bounds"
                ),
                "hero_to_monsters_predicted_min": 10,
                "hero_to_monsters_predicted_max": 17,
                "hero_to_monsters_actual": 17,
                "juggernaut_base_predicted_min": 10,
                "juggernaut_base_predicted_max": 10,
                "juggernaut_block_gain_contract": contract,
            },
        )

        clear = self.audit(base_records(record))
        self.assertNotIn(
            "juggernaut_damage_bound_contract_mismatch",
            issue_kinds(clear),
        )

        forged = copy.deepcopy(record)
        forged["damage_model"]["hero_to_monsters_predicted_max"] = 24
        report = self.audit(base_records(forged))
        self.assertIn(
            "juggernaut_damage_bound_contract_mismatch",
            issue_kinds(report),
        )

    def test_targeted_attack_prediction_excludes_unrelated_monster_exit(self):
        record = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_1", action="play",
            resolved_target_id="enemy:boss",
            player_before={"current_hp": 50},
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:boss", "current_hp": 41,
                    "is_gone": False, "half_dead": False,
                },
                {
                    "enemy_instance_id": "enemy:minion", "current_hp": 22,
                    "is_gone": False, "half_dead": False,
                },
            ],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMPLETE",
                    "screen_type": "COMBAT_REWARD", "current_hp": 50,
                },
            },
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "current_card_final_target_hp_projection"
                ),
                "hero_to_monsters_predicted": 41,
                "hero_to_monsters_actual": 63,
            },
        )

        report = self.audit(base_records(record))
        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn("damage_observed_claim_mismatch", issue_kinds(report))

    def test_terminal_aoe_prediction_excludes_surviving_minion_departure(self):
        card = {
            "id": "Immolate", "card_instance_id": "card:immolate",
            "type": "ATTACK", "has_target": False, "damage": 21,
        }
        minion = {
            "enemy_instance_id": "enemy:minion", "current_hp": 22,
            "block": 6, "is_gone": False, "half_dead": False,
            "powers": [{"id": "Minion", "amount": -1}],
        }
        leader = {
            "enemy_instance_id": "enemy:leader", "current_hp": 6,
            "block": 0, "is_gone": False, "half_dead": False,
            "powers": [{"id": "Vulnerable", "amount": 1}],
        }
        before = authoritative_state(
            30,
            combat={
                "player": {"current_hp": 50},
                "hand": [copy.deepcopy(card)],
                "monsters": [copy.deepcopy(minion), copy.deepcopy(leader)],
            },
            room_phase="COMBAT",
        )
        after = authoritative_state(
            31,
            existing={"game_state": {
                "room_phase": "COMPLETE",
                "screen_type": "COMBAT_REWARD",
                "current_hp": 50,
            }},
        )
        record = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_9", action="play",
            card_id="Immolate", card_instance_id="card:immolate",
            requested_target_id="card:immolate",
            resolved_target_id="card:immolate",
            player_before={"current_hp": 50},
            monsters_before=[copy.deepcopy(minion), copy.deepcopy(leader)],
            authoritative_state_before=before,
            authoritative_state_after=after,
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "current_card_final_target_hp_projection"
                ),
                "hero_to_monsters_predicted": 21,
                # The producer's stated basis is the total final HP delta.
                # It therefore includes the seven HP dismissed with Minion.
                "hero_to_monsters_actual": 28,
            },
        )

        observed = independent_oracle._authoritative_damage_observed(
            record, combat_choice_expected(),
        )
        self.assertTrue(observed["terminal_minion_departure_ambiguous"])
        self.assertEqual(6, observed["hero_to_monsters_direct_loss_min"])
        self.assertEqual(28, observed["hero_to_monsters_direct_loss_max"])

        report = self.audit(base_records(record))
        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn("damage_observed_claim_mismatch", issue_kinds(report))

        impossible = copy.deepcopy(record)
        impossible["damage_model"]["hero_to_monsters_predicted"] = 5
        report = self.audit(base_records(impossible))
        self.assertIn("damage_prediction_mismatch", issue_kinds(report))

        above_observable_bound = copy.deepcopy(record)
        above_observable_bound["damage_model"][
            "hero_to_monsters_predicted"
        ] = 29
        report = self.audit(base_records(above_observable_bound))
        self.assertIn("damage_prediction_mismatch", issue_kinds(report))

        game_over = copy.deepcopy(record)
        game_over["authoritative_state_after"] = authoritative_state(
            31,
            existing={"game_state": {
                "room_phase": "COMPLETE",
                "screen_type": "GAME_OVER",
                "current_hp": 0,
            }},
        )
        report = self.audit(base_records(game_over))
        self.assertIn("damage_prediction_mismatch", issue_kinds(report))

    def test_targeted_terminal_kill_excludes_only_departing_minion_hp(self):
        card = {
            "id": "Anger", "card_instance_id": "card:anger",
            "type": "ATTACK", "has_target": True, "damage": 6,
        }
        minion = {
            "enemy_instance_id": "enemy:minion", "id": "GremlinWarrior",
            "current_hp": 2, "block": 2,
            "is_gone": False, "half_dead": False,
            "powers": [{"id": "Minion", "amount": -1}],
        }
        leader = {
            "enemy_instance_id": "enemy:leader", "id": "GremlinLeader",
            "current_hp": 5, "block": 0,
            "is_gone": False, "half_dead": False,
            "powers": [],
        }
        before = authoritative_state(
            40,
            combat={
                "player": {"current_hp": 50},
                "hand": [copy.deepcopy(card)],
                "monsters": [copy.deepcopy(minion), copy.deepcopy(leader)],
            },
            room_phase="COMBAT",
        )
        after = authoritative_state(
            41,
            existing={"game_state": {
                "room_phase": "COMPLETE",
                "screen_type": "COMBAT_REWARD",
                "current_hp": 50,
            }},
        )
        record = bound(
            "decision", before_seq=40, after_seq=41,
            phase="COMBAT_TURN_7", action="play",
            card_id="Anger", card_instance_id="card:anger",
            requested_target_id="card:anger",
            resolved_target_id="card:anger",
            enemy_instance_id="enemy:leader",
            player_before={"current_hp": 50},
            monsters_before=[copy.deepcopy(minion), copy.deepcopy(leader)],
            authoritative_state_before=before,
            authoritative_state_after=after,
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "current_card_final_target_hp_projection"
                ),
                "hero_to_monsters_predicted": 5,
                "hero_to_monsters_actual": 7,
            },
        )

        observed = independent_oracle._authoritative_damage_observed(
            record, combat_choice_expected(),
        )
        self.assertTrue(observed["terminal_minion_departure_ambiguous"])
        self.assertEqual(5, observed["hero_to_monsters_direct_loss_min"])
        self.assertEqual(7, observed["hero_to_monsters_direct_loss_max"])

        report = self.audit(base_records(record))
        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn("damage_observed_claim_mismatch", issue_kinds(report))

        underkill = copy.deepcopy(record)
        underkill["damage_model"]["hero_to_monsters_predicted"] = 4
        underkill_report = self.audit(base_records(underkill))
        self.assertIn(
            "damage_prediction_mismatch", issue_kinds(underkill_report)
        )

        overkill = copy.deepcopy(record)
        overkill["damage_model"]["hero_to_monsters_predicted"] = 6
        overkill_report = self.audit(base_records(overkill))
        self.assertIn(
            "damage_prediction_mismatch", issue_kinds(overkill_report)
        )

        extra_non_minion = {
            "enemy_instance_id": "enemy:bystander", "id": "Cultist",
            "current_hp": 1, "block": 0,
            "is_gone": False, "half_dead": False, "powers": [],
        }
        mixed_departure = copy.deepcopy(record)
        mixed_departure["authoritative_state_before"] = authoritative_state(
            40,
            combat={
                "player": {"current_hp": 50},
                "hand": [copy.deepcopy(card)],
                "monsters": [
                    copy.deepcopy(minion),
                    copy.deepcopy(leader),
                    copy.deepcopy(extra_non_minion),
                ],
            },
            room_phase="COMBAT",
        )
        mixed_departure["monsters_before"] = [
            copy.deepcopy(minion),
            copy.deepcopy(leader),
            copy.deepcopy(extra_non_minion),
        ]
        mixed_departure["damage_model"]["hero_to_monsters_actual"] = 8
        mixed_report = self.audit(base_records(mixed_departure))
        self.assertIn(
            "damage_prediction_mismatch", issue_kinds(mixed_report)
        )

        targeted_minion = copy.deepcopy(record)
        targeted_minion["enemy_instance_id"] = "enemy:minion"
        targeted_minion["damage_model"]["hero_to_monsters_predicted"] = 2
        targeted_minion_report = self.audit(base_records(targeted_minion))
        self.assertIn(
            "damage_prediction_mismatch", issue_kinds(targeted_minion_report)
        )

    def test_terminal_attack_observes_killed_monster_without_after_combat_list(self):
        monster = {
            "enemy_instance_id": "enemy:a", "current_hp": 4,
            "is_gone": False, "half_dead": False,
        }
        record = bound(
            "decision",
            before_seq=30,
            after_seq=31,
            phase="COMBAT_TURN_1",
            action="play",
            player_before={"current_hp": 50},
            monsters_before=[copy.deepcopy(monster)],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMPLETE",
                    "screen_type": "COMBAT_REWARD",
                    "current_hp": 50,
                },
            },
            damage_model={
                "deterministic": True,
                "hero_to_monsters_predicted": 4,
                "hero_to_monsters_actual": 4,
            },
        )

        report = self.audit([controller_start(), record, terminal()])

        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(report)
        )
        self.assertFalse(any(
            item["kind"] == "damage_prediction_not_comparable"
            for item in report["unknowns"]
        ), report)

    def test_lethal_incoming_damage_is_capped_by_authoritative_hp(self):
        monster = {
            "enemy_instance_id": "enemy:automaton",
            "current_hp": 88,
            "is_gone": False,
            "half_dead": False,
        }
        record = bound(
            "decision",
            before_seq=30,
            after_seq=31,
            phase="COMBAT_TURN_12",
            action="end",
            player_before={"current_hp": 28},
            monsters_before=[copy.deepcopy(monster)],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMBAT",
                    "screen_type": "GAME_OVER",
                    "current_hp": 0,
                    "combat_state": {
                        "player": {"current_hp": 0},
                        "monsters": [copy.deepcopy(monster)],
                    },
                },
            },
            damage_model={
                "deterministic": True,
                "monsters_to_hero_predicted": 32,
                "monsters_to_hero_actual": 28,
            },
        )

        report = self.audit([controller_start(), record, terminal()])

        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(report)
        )

    def test_end_damage_settles_through_contiguous_hand_select_overlay(self):
        monster = {
            "enemy_instance_id": "enemy:chosen", "id": "Chosen",
            "current_hp": 11, "is_gone": False, "half_dead": False,
        }
        before_game = {
            "room_phase": "COMBAT", "screen_type": "NONE",
            "current_hp": 4,
            "combat_state": {
                "player": {"current_hp": 4},
                "monsters": [copy.deepcopy(monster)],
            },
        }
        hand_select_game = copy.deepcopy(before_game)
        hand_select_game.update({
            "screen_type": "HAND_SELECT",
            "current_action": "RetainCardsAction",
        })
        end = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_12", action="end",
            combat_id="combat:retain", turn=12,
            authoritative_state_before=authoritative_state(
                30, existing={"phase": "COMBAT_TURN_12",
                              "game_state": before_game},
            ),
            authoritative_state_after=authoritative_state(
                31, existing={"phase": "HAND_SELECT",
                              "game_state": hand_select_game},
            ),
            damage_model={
                "deterministic": True,
                "monsters_to_hero_basis": "end_turn_player_hp_delta",
                "monsters_to_hero_predicted": 6,
                "monsters_to_hero_actual": 0,
            },
        )
        game_over = copy.deepcopy(hand_select_game)
        game_over.update({"screen_type": "GAME_OVER", "current_hp": 0})
        game_over["combat_state"]["player"]["current_hp"] = 0
        proceed = bound(
            "decision", before_seq=31, after_seq=32,
            phase="HAND_SELECT", action="proceed",
            authoritative_state_before=authoritative_state(
                31, existing={"phase": "HAND_SELECT",
                              "game_state": hand_select_game},
            ),
            authoritative_state_after=authoritative_state(
                32, existing={"phase": "GAME_OVER",
                              "game_state": game_over},
            ),
        )
        terminal_record = terminal()
        terminal_record.update({"state_seq": 32, "terminal_state_seq": 32})

        report = self.audit([
            controller_start(), end, proceed, terminal_record,
        ])

        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(report)
        )
        self.assertEqual(
            1, report["coverage"]["damage_consistency"]["evaluated"]
        )

        forged = copy.deepcopy(end)
        forged["damage_model"]["monsters_to_hero_predicted"] = 3
        forged_report = self.audit([
            controller_start(), forged, proceed, terminal_record,
        ])
        mismatch = next(
            item for item in forged_report["issues"]
            if item["kind"] == "damage_prediction_mismatch"
        )
        self.assertEqual(2, mismatch["deferred_settlement_record_index"])

    def test_play_damage_settles_through_contiguous_hand_select_overlay(self):
        monster = {
            "enemy_instance_id": "enemy:louse", "id": "FuzzyLouseNormal",
            "current_hp": 20, "is_gone": False, "half_dead": False,
        }
        before_game = {
            "room_phase": "COMBAT", "screen_type": "NONE",
            "combat_state": {
                "player": {"current_hp": 50},
                "monsters": [copy.deepcopy(monster)],
            },
        }
        overlay_game = copy.deepcopy(before_game)
        overlay_game.update({
            "screen_type": "HAND_SELECT",
            "current_action": "DiscardAction",
        })
        play = bound(
            "decision", before_seq=40, after_seq=41,
            phase="COMBAT_TURN_4", action="play",
            authoritative_state_before=authoritative_state(
                40, existing={"phase": "COMBAT_TURN_4",
                              "game_state": before_game},
            ),
            authoritative_state_after=authoritative_state(
                41, existing={"phase": "HAND_SELECT",
                              "game_state": overlay_game},
            ),
            damage_model={
                "hero_to_monsters_prediction_basis": (
                    "turn_search_bound_non_attack_first_action_hp_loss"
                ),
                "hero_to_monsters_predicted": 10,
                "hero_to_monsters_actual": 0,
            },
        )
        settled_game = copy.deepcopy(before_game)
        settled_game["combat_state"]["monsters"][0]["current_hp"] = 10
        choose = bound(
            "decision", before_seq=41, after_seq=42,
            phase="HAND_SELECT", action="choose",
            authoritative_state_before=authoritative_state(
                41, existing={"phase": "HAND_SELECT",
                              "game_state": overlay_game},
            ),
            authoritative_state_after=authoritative_state(
                42, existing={"phase": "COMBAT_TURN_4",
                              "game_state": settled_game},
            ),
        )

        report = self.audit([
            controller_start(), play, choose, terminal(),
        ])

        self.assertNotIn("damage_prediction_mismatch", issue_kinds(report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(report)
        )
        self.assertEqual(
            1, report["coverage"]["damage_consistency"]["evaluated"]
        )

        forged = copy.deepcopy(play)
        forged["damage_model"]["hero_to_monsters_predicted"] = 5
        forged_report = self.audit([
            controller_start(), forged, choose, terminal(),
        ])
        mismatch = next(
            item for item in forged_report["issues"]
            if item["kind"] == "damage_prediction_mismatch"
        )
        self.assertEqual(2, mismatch["deferred_settlement_record_index"])

    def test_victory_heal_exposes_hidden_end_turn_loss_without_false_mismatch(self):
        monster = {
            "enemy_instance_id": "enemy:cultist", "id": "Cultist",
            "current_hp": 2, "max_hp": 52, "block": 0,
            "is_gone": False, "half_dead": False,
        }
        before_game = {
            "room_phase": "COMBAT", "screen_type": "NONE",
            "current_hp": 59, "max_hp": 72,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {
                "player": {
                    "current_hp": 59, "max_hp": 72,
                    "powers": [{"id": "Combust", "amount": 5}],
                },
                "monsters": [copy.deepcopy(monster)],
            },
        }
        after_game = {
            "room_phase": "COMPLETE", "screen_type": "COMBAT_REWARD",
            "current_hp": 64, "max_hp": 72,
            "relics": [{"id": "Burning Blood"}],
            "combat_state": {"player": {}, "monsters": []},
        }
        legacy = bound(
            "decision", before_seq=30, after_seq=31,
            phase="COMBAT_TURN_3", action="end",
            authoritative_state_before=authoritative_state(
                30, existing={"game_state": before_game},
            ),
            authoritative_state_after=authoritative_state(
                31, existing={"game_state": after_game},
            ),
            damage_model={
                "monsters_to_hero_basis": "end_turn_player_hp_delta",
                "monsters_to_hero_predicted": 1,
                "monsters_to_hero_actual": 0,
            },
        )

        legacy_report = self.audit(base_records(legacy))
        self.assertNotIn("damage_prediction_mismatch", issue_kinds(legacy_report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(legacy_report)
        )

        current = copy.deepcopy(legacy)
        current["damage_model"].update({
            "monsters_to_hero_basis": (
                "end_turn_total_hp_loss_before_postcombat_healing"
            ),
            "monsters_to_hero_actual": 1,
        })
        current_report = self.audit(base_records(current))
        self.assertNotIn("damage_prediction_mismatch", issue_kinds(current_report))
        self.assertNotIn(
            "damage_observed_claim_mismatch", issue_kinds(current_report)
        )

        forged = copy.deepcopy(current)
        forged["damage_model"]["monsters_to_hero_predicted"] = 2
        forged_report = self.audit(base_records(forged))
        self.assertIn("damage_prediction_mismatch", issue_kinds(forged_report))

    def test_postcombat_heal_with_inactive_meat_on_the_bone_is_exact(self):
        before_game = {
            "room_phase": "COMBAT", "current_hp": 58, "max_hp": 72,
            "relics": [
                {"id": "Burning Blood"}, {"id": "Meat on the Bone"},
            ],
        }
        after_game = {
            "room_phase": "COMPLETE", "screen_type": "COMBAT_REWARD",
            "current_hp": 57, "max_hp": 72,
        }
        before_player = {"current_hp": 58, "max_hp": 72, "powers": []}

        self.assertEqual(
            7,
            independent_oracle._oracle_exact_postcombat_hp_loss(
                before_game, after_game, before_player, 58, 57,
            ),
        )

        at_half_game = copy.deepcopy(before_game)
        at_half_game["current_hp"] = 43
        at_half_player = copy.deepcopy(before_player)
        at_half_player["current_hp"] = 43
        at_half_after = copy.deepcopy(after_game)
        at_half_after["current_hp"] = 42
        self.assertIsNone(
            independent_oracle._oracle_exact_postcombat_hp_loss(
                at_half_game, at_half_after, at_half_player, 43, 42,
            )
        )

    def test_postcombat_heal_includes_exact_regeneration(self):
        before_game = {
            "room_phase": "COMBAT", "current_hp": 30, "max_hp": 80,
            "relics": [{"id": "Burning Blood"}],
        }
        after_game = {
            "room_phase": "COMPLETE", "screen_type": "COMBAT_REWARD",
            "current_hp": 35, "max_hp": 80,
        }
        before_player = {
            "current_hp": 30, "max_hp": 80,
            "powers": [{"id": "Regeneration", "amount": 1}],
        }

        self.assertEqual(
            2,
            independent_oracle._oracle_exact_postcombat_hp_loss(
                before_game, after_game, before_player, 30, 35,
            ),
        )

    def test_regeneration_exposes_gross_end_turn_damage_from_net_hp(self):
        terminal_record = terminal()
        terminal_record.update({
            "state_seq": 210500,
            "terminal_state_seq": 210500,
        })
        cases = (
            # Canary seq 210402: 1 gross damage, 1 Regeneration, net 0.
            (210402, 1, 1),
            # Canary seq 210411: 7 gross damage, 3 Regeneration, net 4.
            (210411, 3, 7),
        )
        for before_seq, regeneration, gross_damage in cases:
            with self.subTest(before_seq=before_seq):
                record = regeneration_end_damage_record(
                    before_seq,
                    regeneration=regeneration,
                    gross_damage=gross_damage,
                )
                report = self.audit([
                    controller_start(), record, copy.deepcopy(terminal_record),
                ])
                self.assertNotIn(
                    "damage_prediction_mismatch", issue_kinds(report)
                )
                self.assertNotIn(
                    "damage_observed_claim_mismatch", issue_kinds(report)
                )
                self.assertEqual(
                    1, report["coverage"]["damage_consistency"]["evaluated"]
                )

    def test_regeneration_does_not_hide_a_forged_gross_prediction(self):
        record = regeneration_end_damage_record(
            210402, regeneration=1, gross_damage=1, predicted=2,
        )
        terminal_record = terminal()
        terminal_record.update({
            "state_seq": 210500,
            "terminal_state_seq": 210500,
        })

        report = self.audit([
            controller_start(), record, terminal_record,
        ])

        mismatch = next(
            item for item in report["issues"]
            if item["kind"] == "damage_prediction_mismatch"
        )
        self.assertEqual("P1", mismatch["severity"])
        self.assertEqual(1.0, mismatch["independently_observed"])

    def test_lethal_hp_cap_does_not_hide_an_underprediction(self):
        monster = {
            "enemy_instance_id": "enemy:automaton",
            "current_hp": 88,
            "is_gone": False,
            "half_dead": False,
        }
        record = bound(
            "decision",
            before_seq=30,
            after_seq=31,
            phase="COMBAT_TURN_12",
            action="end",
            player_before={"current_hp": 28},
            monsters_before=[copy.deepcopy(monster)],
            authoritative_state_after={
                "state_seq": 31,
                "game_state": {
                    "room_phase": "COMBAT",
                    "screen_type": "GAME_OVER",
                    "current_hp": 0,
                    "combat_state": {
                        "player": {"current_hp": 0},
                        "monsters": [copy.deepcopy(monster)],
                    },
                },
            },
            damage_model={
                "deterministic": True,
                "monsters_to_hero_predicted": 27,
                "monsters_to_hero_actual": 28,
            },
        )

        report = self.audit([controller_start(), record, terminal()])

        self.assertIn("damage_prediction_mismatch", issue_kinds(report))

    def test_matching_production_damage_claims_are_not_independent_evidence(self):
        record = bound(
            "decision",
            before_seq=30,
            after_seq=31,
            phase="COMBAT_TURN_1",
            action="play",
            damage_model={
                "deterministic": True,
                "hero_to_monsters_predicted": 7,
                "hero_to_monsters_actual": 7,
            },
        )

        report = self.audit([controller_start(), record, terminal()])

        self.assertEqual("inconclusive", report["status"])
        self.assertIn(
            "damage_prediction_not_comparable",
            {item["kind"] for item in report["unknowns"]},
        )

    def test_damage_unknown_preserves_independent_reason_axes(self):
        monster = {
            "enemy_instance_id": "enemy:a",
            "current_hp": 10,
            "is_gone": False,
            "half_dead": False,
        }

        def damage_record(model, *, authoritative=True):
            record = bound(
                "decision",
                before_seq=30,
                after_seq=31,
                phase="COMBAT_TURN_4",
                action="play",
                player_before={"current_hp": 50},
                monsters_before=[copy.deepcopy(monster)],
                damage_model=model,
            )
            if authoritative:
                record["authoritative_state_after"] = authoritative_state(
                    31,
                    existing={
                        "game_state": {
                            "room_phase": "COMBAT",
                            "current_hp": 50,
                            "combat_state": {
                                "player": {"current_hp": 50},
                                "monsters": [{
                                    **copy.deepcopy(monster),
                                    "current_hp": 3,
                                }],
                            },
                        },
                    },
                )
            return record

        cases = (
            (
                "predicted",
                {
                    "hero_to_monsters_prediction_basis": (
                        "current_card_attack_unresolved"
                    ),
                    "hero_to_monsters_predicted": None,
                    "hero_to_monsters_actual": 7,
                },
                True,
                ["predicted_missing"],
            ),
            (
                "actual",
                {
                    "hero_to_monsters_prediction_basis": (
                        "current_card_final_target_hp_projection"
                    ),
                    "hero_to_monsters_predicted": 7,
                    "hero_to_monsters_actual": None,
                },
                True,
                ["actual_missing"],
            ),
            (
                "authority",
                {
                    "hero_to_monsters_prediction_basis": (
                        "current_card_final_target_hp_projection"
                    ),
                    "hero_to_monsters_predicted": 7,
                    "hero_to_monsters_actual": 7,
                },
                False,
                [
                    "authoritative_actual_missing",
                    "authoritative_predicted_missing",
                ],
            ),
            (
                "certainty",
                {
                    "hero_to_monsters_prediction_basis": (
                        "current_card_final_target_hp_projection"
                    ),
                    "hero_to_monsters_predicted": 7,
                    "hero_to_monsters_actual": 7,
                    "prediction_certainty": "uncertain",
                },
                True,
                ["uncertain"],
            ),
        )
        for name, model, authoritative, expected_codes in cases:
            with self.subTest(axis=name):
                report = self.audit(base_records(damage_record(
                    model, authoritative=authoritative,
                )))
                unknowns = [
                    item for item in report["unknowns"]
                    if item["kind"] == "damage_prediction_not_comparable"
                ]
                self.assertEqual(1, len(unknowns), report)
                unknown = unknowns[0]
                self.assertEqual(expected_codes, unknown["reason_codes"])
                self.assertEqual("COMBAT_TURN_4", unknown["phase"])
                self.assertEqual("play", unknown["action"])
                self.assertEqual(
                    model["hero_to_monsters_prediction_basis"],
                    unknown["prediction_basis"],
                )
                self.assertIn("predicted", unknown)
                self.assertIn("actual", unknown)
                self.assertIn("observed", unknown)
                self.assertIn("observed_actual", unknown)
                self.assertIn("observed_predicted", unknown)

    def test_damage_unknown_preserves_all_overlapping_reasons(self):
        record = bound(
            "decision",
            before_seq=40,
            after_seq=41,
            phase="COMBAT_TURN_9",
            action="end",
            damage_model={
                "monsters_to_hero_basis": "end_turn_player_hp_delta",
                "monsters_to_hero_predicted": "unparseable",
                "monsters_to_hero_actual": None,
                "deterministic": False,
            },
        )

        report = self.audit(base_records(record))

        unknown = next(
            item for item in report["unknowns"]
            if item["kind"] == "damage_prediction_not_comparable"
        )
        self.assertEqual(
            [
                "predicted_missing",
                "actual_missing",
                "authoritative_actual_missing",
                "authoritative_predicted_missing",
                "uncertain",
            ],
            unknown["reason_codes"],
        )
        self.assertEqual("end_turn_player_hp_delta", unknown["prediction_basis"])
        self.assertIsNone(unknown["predicted"])
        self.assertIsNone(unknown["actual"])
        self.assertIsNone(unknown["observed"])

    def test_damage_null_pairs_remain_not_applicable(self):
        record = bound(
            "decision",
            before_seq=50,
            after_seq=51,
            phase="COMBAT_TURN_2",
            action="play",
            damage_model={
                "hero_to_monsters_prediction_basis": "irrelevant_direction",
                "hero_to_monsters_predicted": None,
                "hero_to_monsters_actual": None,
            },
        )

        report = self.audit(base_records(record))

        self.assertFalse(any(
            item["kind"] == "damage_prediction_not_comparable"
            for item in report["unknowns"]
        ))
        self.assertEqual(
            0, report["coverage"]["damage_consistency"]["eligible"]
        )

    def test_binding_mismatch_is_detected(self):
        decision = strategic_decision()
        decision["run_id"] = "IRONCLAD:0:other"

        report = self.audit(base_records(decision))

        self.assertIn("binding_mismatch", issue_kinds(report))

    def test_unknown_record_type_is_fail_closed(self):
        alien = bound("future_unclassified_record")

        report = self.audit([
            controller_start(), strategic_decision(), alien, terminal()
        ])

        self.assertIn("unknown_record_type", issue_kinds(report))
        self.assertEqual("issues", report["status"])


class ExtendedConsequenceOracleRegressionTests(unittest.TestCase):
    def test_sapphire_audit_distinguishes_collection_order_from_forfeit(self):
        choices = {
            "gold": {
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "GOLD", "gold": 50},
                },
            },
            "relic": {
                "target": {
                    "kind": "reward",
                    "reward": {"reward_type": "RELIC", "relic": {"id": "Lantern"}},
                },
            },
            "key": {
                "target": {
                    "kind": "sapphire_key",
                    "reward": {"reward_type": "SAPPHIRE_KEY"},
                },
            },
        }
        record = {"phase": "SAPPHIRE_KEY"}
        self.assertTrue(
            independent_oracle._sapphire_sequential_collection_only(
                record, choices, "key", {"gold"}
            )
        )
        self.assertTrue(
            independent_oracle._sapphire_sequential_collection_only(
                record, choices, "gold", {"relic"}
            )
        )
        self.assertFalse(
            independent_oracle._sapphire_sequential_collection_only(
                record, choices, "relic", {"key"}
            )
        )

    def test_new_indexed_events_match_production_and_independent_tables(self):
        cases = [
            (
                "Ghosts", 0, [0, 1],
                {"id": "Ghostly", "type": "SKILL", "upgrades": 0},
                {
                    "current_hp": 77, "max_hp": 77, "floor": 20,
                    "relics": [{"id": "Toxic Egg 2"}],
                },
                "ghosts_accept",
            ),
            (
                "Forgotten Altar", 1, [0, 1, 2], None,
                {"current_hp": 40, "max_hp": 83, "floor": 25},
                "forgotten_altar_sacrifice",
            ),
            (
                "Nest", 1, [0, 1],
                {"id": "RitualDagger", "type": "ATTACK", "upgrades": 0},
                {"current_hp": 40, "max_hp": 80, "floor": 22},
                "nest_join_cult",
            ),
            (
                "Transmorgrifier", 0, [0, 1], None,
                {"current_hp": 70, "max_hp": 80, "floor": 10},
                "transmorgrifier_open_transform_grid",
            ),
            (
                "Purifier", 0, [0, 1], None,
                {"current_hp": 70, "max_hp": 80, "floor": 10},
                "purifier_open_remove_grid",
            ),
            (
                "Tomb of Lord Red Mask", 1, [0, 1, 2], None,
                {
                    "current_hp": 70, "max_hp": 80, "floor": 44,
                    "gold": 134,
                },
                "tomb_red_mask_offer_all_gold",
            ),
            (
                "MindBloom", 1, [0, 1, 2], None,
                {
                    "current_hp": 40, "max_hp": 80, "floor": 36,
                    "deck": [{
                        "id": "Bash", "type": "ATTACK", "upgrades": 0,
                        "card_instance_id": "bash:1",
                    }],
                },
                "mind_bloom_awake",
            ),
            (
                "Upgrade Shrine", 1, [0, 1], None,
                {"current_hp": 70, "max_hp": 80, "floor": 10},
                "upgrade_shrine_leave",
            ),
            (
                "WeMeetAgain", 2, [0, 1, 2, 3],
                {
                    "id": "Bash", "type": "ATTACK", "upgrades": 0,
                    "card_instance_id": "bash:1",
                },
                {
                    "current_hp": 70, "max_hp": 80, "floor": 10,
                    "gold": 100,
                },
                "we_meet_again_give_card",
            ),
        ]
        for event_id, index, indexes, card, game_extra, operation in cases:
            with self.subTest(event_id=event_id, index=index):
                target = {
                    "kind": "event_option", "event_id": event_id,
                    "original_button_index": index,
                    "audit_projection_version": (
                        3 if event_id in {
                            "Nest", "Transmorgrifier", "Purifier",
                            "Tomb of Lord Red Mask",
                        } else 2
                    ),
                }
                if card is not None:
                    target["card"] = copy.deepcopy(card)
                screen_options = [
                    {"original_button_index": value, "disabled": False}
                    for value in indexes
                ]
                game = {
                    "ascension_level": 0, "gold": 100, "deck": [],
                    "relics": [], "potions": [],
                    "screen_state": {
                        "event_id": event_id, "options": screen_options,
                    },
                    **copy.deepcopy(game_extra),
                }
                option = {
                    "option_id": f"event:{event_id}:{index}",
                    "choice_index": index, "label": event_id,
                    "target": copy.deepcopy(target),
                }
                state = {
                    "phase": "EVENT", "options": [option],
                    "game_state": copy.deepcopy(game),
                }
                production = autoplay._structured_option_consequence(
                    "EVENT", target, event_id, state=state,
                )
                raw = {
                    "choice_id": option["option_id"], "choice_index": index,
                    "raw_text": event_id, "target": copy.deepcopy(target),
                }
                record = {
                    "phase": "EVENT",
                    "available_options_before": [copy.deepcopy(option)],
                    "authoritative_state_before": {
                        "game_state": copy.deepcopy(game),
                    },
                }
                independent = independent_oracle._expected_visible_consequence(
                    "EVENT", raw, record,
                )
                self.assertEqual(operation, production.get("operation"))
                self.assertEqual(operation, independent.get("operation"))
                clear, mismatches = (
                    independent_oracle._consequence_matches_visible(
                        production, independent,
                    )
                )
                self.assertTrue(clear, mismatches)
                if event_id == "Ghosts":
                    self.assertEqual(
                        1, production["card_changes"]["gain"][0]["upgrades"]
                    )
                    self.assertEqual(
                        1, independent["card_changes"]["gain"][0]["upgrades"]
                    )
                if event_id == "Forgotten Altar":
                    self.assertEqual(-16, production["hp_delta"])
                    self.assertEqual(-16, independent["hp_delta"])

    def test_pandoras_box_pickup_types_removal_and_future_transforms(self):
        relic = {"id": "PandorasBox", "name": "Pandora's Box"}
        deck = [
            {"id": "Strike_R", "card_instance_id": "strike:1"},
            {"id": "Defend_R", "card_instance_id": "defend:1"},
            {"id": "Bash", "card_instance_id": "bash:1"},
        ]
        production = {"field_knowledge": {}, "uncertainty": []}
        self.assertTrue(autoplay._apply_relic_pickup_package(
            production, {"game_state": {"deck": copy.deepcopy(deck)}},
            relic, "pandorasbox", "test",
        ))
        independent = {"field_knowledge": {}, "uncertainty": []}
        self.assertTrue(independent_oracle._oracle_relic_pickup_package(
            independent,
            {"authoritative_state_before": {
                "game_state": {"deck": copy.deepcopy(deck)},
            }},
            relic, "pandorasbox", "test",
            target={"audit_projection_version": 3},
        ))
        for value in (production, independent):
            self.assertEqual(
                ["Strike_R", "Defend_R"],
                [item["id"] for item in value["card_changes"]["remove"]],
            )
            self.assertEqual(
                2, value["future_costs"][0]["result_count"]
            )
            self.assertEqual(
                "classified_future",
                value["uncertainty_classification"]["status"],
            )

    def test_all_egg_relics_upgrade_matching_card_gain_types(self):
        cases = (
            ("Molten Egg 2", "ATTACK"),
            ("Toxic Egg 2", "SKILL"),
            ("Frozen Egg 2", "POWER"),
        )
        for relic_id, card_type in cases:
            with self.subTest(relic=relic_id, card_type=card_type):
                card = {
                    "id": f"test-{card_type}", "name": "Test Card",
                    "type": card_type, "upgrades": 0,
                }
                game = {"relics": [{"id": relic_id}]}
                production = autoplay._card_gain_after_egg_relics(card, game)
                independent = (
                    independent_oracle._oracle_card_gain_after_egg_relics(
                        card, game,
                    )
                )
                self.assertEqual(1, production["upgrades"])
                self.assertEqual("Test Card+", production["name"])
                self.assertEqual(production, independent)

    def test_boss_relic_pickup_packages_and_passives_are_exact(self):
        for relic_id, owned_id, future_count in (
            ("Empty Cage", None, 2),
            ("Black Blood", "Burning Blood", 0),
            ("Mark of Pain", None, 0),
        ):
            with self.subTest(relic=relic_id):
                relic = {"id": relic_id, "name": relic_id, "counter": -1}
                game = {
                    "relics": (
                        [{"id": owned_id, "name": owned_id, "counter": -1}]
                        if owned_id else []
                    ),
                }
                raw = {
                    "choice_id": f"relic:{relic_id}", "choice_index": 0,
                    "raw_text": relic_id,
                    "target": {
                        "kind": "relic", "relic": copy.deepcopy(relic),
                        "audit_projection_version": 2,
                    },
                }
                expected = independent_oracle._expected_visible_consequence(
                    "BOSS_REWARD", raw,
                    {"authoritative_state_before": {"game_state": game}},
                )
                self.assertEqual(0, expected["hp_delta"])
                self.assertTrue(all(
                    field.get("status") in {
                        "known", "known_domain", "not_applicable",
                    }
                    for field in expected["field_knowledge"].values()
                ))
                self.assertEqual(
                    future_count,
                    sum(
                        int(item.get("select_count") or 0)
                        for item in expected["future_costs"]
                        if isinstance(item, dict)
                    ),
                )
                removed = expected["relic_changes"]["remove"]
                self.assertEqual(1 if owned_id else 0, len(removed))

    def test_special_relic_pickup_packages_are_independently_bounded(self):
        game = {
            "current_hp": 31,
            "max_hp": 80,
            "gold": 200,
            "deck": [{
                "id": "Strike_R", "type": "ATTACK", "upgrades": 0,
                "card_instance_id": "strike:1",
            }],
            "relics": [{"id": "Ectoplasm"}],
            "potions": [{"id": "Potion Slot"}],
        }
        for relic_id, phase, price, classification in (
            ("Astrolabe", "BOSS_REWARD", None, "classified_future"),
            ("Tiny House", "BOSS_REWARD", None, "classified_random_domain"),
            ("Lee's Waffle", "SHOP_SCREEN", 123, "none"),
        ):
            with self.subTest(relic=relic_id):
                relic = {"id": relic_id, "name": relic_id, "counter": -1}
                target = {
                    "kind": "relic",
                    "audit_projection_version": 3,
                }
                if phase == "SHOP_SCREEN":
                    target["item"] = {**copy.deepcopy(relic), "price": price}
                else:
                    target["relic"] = copy.deepcopy(relic)
                raw = {
                    "choice_id": f"relic:{relic_id}",
                    "choice_index": 0,
                    "raw_text": relic_id,
                    "target": target,
                }
                record = {
                    "phase": phase,
                    "authoritative_state_before": {
                        "game_state": copy.deepcopy(game),
                    },
                }
                expected = independent_oracle._expected_visible_consequence(
                    phase, raw, record,
                )
                self.assertTrue(all(
                    field.get("status") in {
                        "known", "known_domain", "not_applicable",
                    }
                    for field in expected["field_knowledge"].values()
                ), expected)
                self.assertEqual(
                    classification,
                    expected["uncertainty_classification"]["status"],
                )
                if relic_id == "Astrolabe":
                    self.assertEqual(
                        3, expected["future_costs"][0]["select_count"]
                    )
                if relic_id == "Tiny House":
                    self.assertEqual(0, expected["gold_delta"])
                    self.assertEqual(
                        1,
                        expected["card_changes"]["random_upgrade"][0]["count"],
                    )
                    state = {
                        "phase": phase,
                        "game_state": copy.deepcopy(game),
                    }
                    production = autoplay._structured_option_consequence(
                        phase, target, relic_id, state=state,
                    )
                    matched, mismatches = (
                        independent_oracle._consequence_matches_visible(
                            production, expected,
                        )
                    )
                    self.assertTrue(matched, mismatches)
                if relic_id == "Lee's Waffle":
                    self.assertEqual(-123, expected["gold_delta"])
                    self.assertEqual(7, expected["max_hp_delta"])

    def test_big_fish_donut_is_independently_typed(self):
        target = {
            "kind": "event_option", "event_id": "Big Fish",
            "original_button_index": 1,
            "audit_projection_version": 2,
        }
        raw = {
            "choice_id": "event:1", "choice_index": 1,
            "raw_text": "Donut", "target": target,
        }
        screen_options = [
            {"disabled": False, "choice_index": index,
             "original_button_index": index}
            for index in range(3)
        ]
        record = {
            "phase": "EVENT",
            "authoritative_state_before": {
                "game_state": {
                    "ascension_level": 0, "current_hp": 40, "max_hp": 80,
                    "screen_state": {
                        "event_id": "Big Fish", "options": screen_options,
                    },
                },
            },
        }

        expected = independent_oracle._expected_visible_consequence(
            "EVENT", raw, record
        )

        self.assertEqual(5, expected["hp_delta"])
        self.assertEqual(5, expected["max_hp_delta"])
        self.assertEqual("big_fish_donut", expected["operation"])
        self.assertTrue(all(
            value["status"] in {"known", "known_domain", "not_applicable"}
            for value in expected["field_knowledge"].values()
        ))

    def test_calling_bell_pickup_has_typed_curse_and_relic_domains(self):
        relic = {"id": "Calling Bell", "name": "Calling Bell"}
        raw = {
            "choice_id": "relic:calling-bell", "choice_index": 0,
            "raw_text": "Calling Bell",
            "target": {
                "kind": "relic", "relic": relic,
                "audit_projection_version": 2,
            },
        }

        expected = independent_oracle._expected_visible_consequence(
            "BOSS_REWARD", raw, {"authoritative_state_before": {"game_state": {}}}
        )

        self.assertEqual("CurseOfTheBell", expected["curse"]["gain"][0]["id"])
        self.assertEqual(3, len(expected["random_effects"]))
        self.assertEqual(
            "classified_random_domain",
            expected["uncertainty_classification"]["status"],
        )

    def test_authoritative_deck_settlement_verifies_curse_changes(self):
        known, value, authority = (
            independent_oracle._expected_or_settled_value(
                "curse",
                {
                    "operation": "gain_boss_relic",
                    "field_knowledge": {"curse": {"status": "unknown"}},
                },
                {"deck": {
                    "added": [
                        {"id": "Writhe", "type": "CURSE"},
                        {"id": "Shrug It Off", "type": "SKILL"},
                    ],
                    "removed": [
                        {"id": "AscendersBane", "rarity": "CURSE"},
                    ],
                }},
            )
        )

        self.assertTrue(known)
        self.assertEqual("authoritative_settlement", authority)
        self.assertEqual(["Writhe"], [card["id"] for card in value["gain"]])
        self.assertEqual(
            ["AscendersBane"],
            [card["id"] for card in value["remove"]],
        )
        self.assertIsNone(value["probability"])
        self.assertIsNone(value["omamori_applicable"])
        self.assertIsNone(value["omamori_charges_consumed"])

    def test_full_belt_potion_requires_a_prior_resource_action(self):
        raw = {
            "target": {
                "kind": "reward",
                "audit_projection_version": 2,
                "reward": {"reward_type": "POTION", "potion": {"id": "DexPotion"}},
            },
        }
        record = {
            "phase": "COMBAT_REWARD",
            "authoritative_state_before": {
                "game_state": {
                    "relics": [],
                    "potions": [
                        {"id": "FirePotion"},
                        {"id": "BlockPotion"},
                        {"id": "FairyPotion"},
                    ],
                },
            },
        }

        self.assertFalse(
            independent_oracle._oracle_immediately_actionable_choice(
                record, raw
            )
        )
        record["authoritative_state_before"]["game_state"]["potions"][2] = {
            "id": "Potion Slot"
        }
        self.assertTrue(
            independent_oracle._oracle_immediately_actionable_choice(
                record, raw
            )
        )


class AuditGapClosureRegressionTests(unittest.TestCase):
    def test_note_parent_binds_exchange_grid(self):
        offered = {
            "id": "Apotheosis", "name": "Apotheosis", "type": "SKILL",
            "upgrades": 0, "card_instance_id": "offered:apotheosis",
        }
        mechanism = {
            "event_id": "NoteForYourself",
            "event_class": (
                "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
            ),
            "original_button_index": 0,
            "operation": "note_exchange", "select_count": 1,
            "offered_card": offered,
        }
        selected = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "card:strike",
        }
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "EVENT",
            "mechanism_id": independent_oracle._oracle_stable_id(
                "event-grid-mechanism", mechanism
            ),
            **mechanism,
        }
        record = {"authoritative_state_before": {"game_state": {
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": False, "cards": [copy.deepcopy(selected)],
                "parent_choice_context": parent,
            },
        }}}

        operation, card, error = independent_oracle._oracle_grid_target(
            record, {
                "kind": "card", "card_instance_id": "card:strike",
                "card": copy.deepcopy(selected),
                "audit_projection_version": 3,
            }
        )

        self.assertIsNone(error)
        self.assertEqual("grid_note_exchange", operation)
        self.assertEqual("card:strike", card["card_instance_id"])

    def test_display_score_rounding_is_not_a_contradiction(self):
        row = {
            "local_score": 20.18,
            "score_rule_id": "rounded_total_v1",
            "score_formula": {"kind": "sum_components_v1"},
            "score_inputs": {"value": 20.1795},
            "score_components": [{
                "name": "value", "input": "value",
                "coefficient": 1.0, "value": 20.1795,
            }],
        }

        contradictions, unresolved = independent_oracle._review_score_evidence(
            row
        )

        self.assertEqual([], contradictions)
        self.assertEqual([], unresolved)

    def test_drug_dealer_parent_binds_transform_two_grid(self):
        mechanism = {
            "event_id": "Drug Dealer",
            "event_class": "com.megacrit.cardcrawl.events.city.DrugDealer",
            "original_button_index": 1,
            "operation": "transform",
            "select_count": 2,
        }
        parent = {
            "authority": "accepted_protocol_choice",
            "parent_phase": "EVENT",
            "mechanism_id": independent_oracle._oracle_stable_id(
                "event-grid-mechanism", mechanism
            ),
            **mechanism,
        }
        card = {
            "id": "Strike_R", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "card:strike",
        }
        record = {"authoritative_state_before": {"game_state": {
            "screen_state": {
                "for_upgrade": False, "for_transform": False,
                "for_purge": False, "cards": [copy.deepcopy(card)],
                "parent_choice_context": parent,
            },
        }}}
        target = {
            "kind": "card", "card_instance_id": "card:strike",
            "card": copy.deepcopy(card), "audit_projection_version": 3,
        }

        operation, selected, error = independent_oracle._oracle_grid_target(
            record, target
        )

        self.assertIsNone(error)
        self.assertEqual("grid_transform", operation)
        self.assertEqual("card:strike", selected["card_instance_id"])

    def test_note_for_yourself_surface_is_typed(self):
        card = {
            "id": "Strike_B", "name": "Strike", "type": "ATTACK",
            "upgrades": 0, "card_instance_id": "offered:strike",
        }
        options = [
            {"original_button_index": 0, "target": {
                "event_id": "NoteForYourself",
            }},
            {"original_button_index": 1, "target": {
                "event_id": "NoteForYourself",
            }},
        ]
        record = {"authoritative_state_before": {"game_state": {
            "ascension_level": 0,
            "screen_state": {
                "event_id": "NoteForYourself", "options": options,
            },
        }}}
        accept = independent_oracle._expected_visible_consequence(
            "EVENT", {"target": {
                "kind": "event_option", "event_id": "NoteForYourself",
                "original_button_index": 0, "card": card,
                "audit_projection_version": 3,
            }}, record,
        )
        leave = independent_oracle._expected_visible_consequence(
            "EVENT", {"target": {
                "kind": "event_option", "event_id": "NoteForYourself",
                "original_button_index": 1, "card": None,
                "audit_projection_version": 3,
            }}, record,
        )

        self.assertEqual(
            "note_for_yourself_prepare_exchange", accept["operation"]
        )
        self.assertEqual("event_grid_exchange", accept["future_costs"][0]["kind"])
        self.assertEqual("note_for_yourself_leave", leave["operation"])
        self.assertTrue(leave["leave"])

    def test_reflected_progress_events_are_independently_typed(self):
        cases = (
            (
                "Liars Game",
                "com.megacrit.cardcrawl.events.exordium.Sssserpent",
                "event_stage", "INTRO", 2, 0,
                "liars_game_prepare_agreement",
            ),
            (
                "Vampires",
                "com.megacrit.cardcrawl.events.city.Vampires",
                "screen_num", 0, 2, 0,
                "vampires_trade_max_hp_for_bites",
            ),
            (
                "Golden Shrine",
                "com.megacrit.cardcrawl.events.shrines.GoldShrine",
                "event_stage", "INTRO", 3, 1,
                "golden_shrine_desecrate",
            ),
        )
        for (
            event_id, event_class, progress_field, progress,
            option_count, selected_index, operation,
        ) in cases:
            with self.subTest(event=event_id):
                options = [
                    {
                        "choice_index": index,
                        "original_button_index": index,
                        "target": {"event_id": event_id},
                    }
                    for index in range(option_count)
                ]
                screen = {
                    "event_id": event_id, "event_class": event_class,
                    progress_field: progress, "options": options,
                }
                game = {
                    "ascension_level": 0, "current_hp": 70,
                    "max_hp": 71, "gold": 0,
                    "deck": [
                        {
                            "id": "Strike_R", "type": "ATTACK",
                            "rarity": "BASIC",
                            "card_instance_id": "strike-0",
                        },
                    ],
                    "relics": (
                        [{"id": "Darkstone Periapt", "counter": -1}]
                        if event_id == "Golden Shrine" else []
                    ),
                    "screen_state": screen,
                }
                target = {
                    "kind": "event_option", "event_id": event_id,
                    "event_class": event_class,
                    progress_field: progress,
                    "original_button_index": selected_index,
                    "audit_projection_version": 3,
                }
                expected = independent_oracle._expected_visible_consequence(
                    "EVENT", {"target": target},
                    {"authoritative_state_before": {"game_state": game}},
                )

                self.assertEqual(operation, expected["operation"])
                self.assertEqual(
                    "base_game_"
                    + independent_oracle._oracle_game_id(event_id)
                    + "_a0_progress_v1",
                    expected["mechanism_id"],
                )
                self.assertEqual([], expected["random_effects"])
                self.assertTrue(all(
                    field.get("status") in {
                        "known", "known_domain", "not_applicable",
                    }
                    for field in expected["field_knowledge"].values()
                ))
                if event_id == "Vampires":
                    self.assertEqual(-22, expected["max_hp_delta"])
                    self.assertEqual(
                        5, expected["card_changes"]["gain"][0]["count"]
                    )
                    self.assertTrue(
                        independent_oracle._effect_item_multiset_equal(
                            "card_changes",
                            expected["card_changes"]["gain"],
                            [
                                {
                                    "id": "Bite", "upgrades": 0,
                                    "card_instance_id": f"bite:{index}",
                                }
                                for index in range(5)
                            ],
                            allow_new_instance=True,
                        )
                    )
                if event_id == "Golden Shrine":
                    self.assertEqual(275, expected["gold_delta"])
                    self.assertEqual(6, expected["max_hp_delta"])

    def test_gold_reward_independently_projects_bloody_idol_heal(self):
        raw = {"target": {
            "kind": "reward",
            "reward": {"reward_type": "GOLD", "gold": 20},
        }}
        record = {"authoritative_state_before": {"game_state": {
            "current_hp": 50,
            "max_hp": 51,
            "relics": [{"id": "Bloody Idol"}],
        }}}

        consequence = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD", raw, record
        )
        self.assertEqual(1, consequence["hp_delta"])
        self.assertEqual(20, consequence["gold_delta"])

        record["authoritative_state_before"]["game_state"]["relics"].append(
            {"id": "Mark of the Bloom"}
        )
        blocked = independent_oracle._expected_visible_consequence(
            "COMBAT_REWARD", raw, record
        )
        self.assertEqual(0, blocked["hp_delta"])

    def test_cursed_key_chest_attempt_consumes_one_omamori_charge(self):
        record = {
            "phase": "CHEST",
            "selected_choice_ids": ["option:chest"],
            "available_options_before": [{
                "option_id": "option:chest", "choice_index": 0,
                "target": {"kind": "chest"},
            }],
            "authoritative_state_before": {"game_state": {
                "relics": [{"id": "Cursed Key"}, {"id": "Omamori", "counter": 1}],
            }},
        }

        attempted, reason, contradiction = (
            independent_oracle._oracle_known_curse_attempt(record)
        )

        self.assertEqual(1, attempted)
        self.assertIsNone(reason)
        self.assertFalse(contradiction)

        raw = record["available_options_before"][0]
        expected = independent_oracle._expected_visible_consequence(
            "CHEST", raw, record
        )
        self.assertEqual(0, expected["hp_delta"])
        self.assertEqual(1, expected["curse"]["omamori_charges_consumed"])
        self.assertEqual(
            -1, expected["relic_changes"]["counter"][0]["delta"]
        )

        darkstone_record = copy.deepcopy(record)
        darkstone_record["authoritative_state_before"]["game_state"][
            "relics"
        ] = [
            {"id": "Cursed Key"},
            {"id": "Darkstone Periapt", "counter": -1},
        ]
        darkstone_state = {
            "phase": "CHEST",
            "game_state": copy.deepcopy(
                darkstone_record["authoritative_state_before"]["game_state"]
            ),
        }
        production = autoplay._structured_option_consequence(
            "CHEST", raw["target"], "open", state=darkstone_state
        )
        expected = independent_oracle._expected_visible_consequence(
            "CHEST", raw, darkstone_record
        )
        for consequence in (production, expected):
            self.assertEqual(6, consequence["hp_delta"])
            self.assertEqual(6, consequence["max_hp_delta"])
            self.assertEqual(
                1, consequence["card_changes"]["random_gain"][0]["count"]
            )
            self.assertEqual(
                "classified_random_domain",
                consequence["uncertainty_classification"]["status"],
            )

    def test_bound_shop_listing_fields_bind_to_exact_visible_potion(self):
        claim = {
            "bound_purchase_listing_id": "shop:potion:dex",
            "bound_purchase_choice_index": 7,
            "bound_purchase_item_id": "Dexterity Potion",
            "bound_purchase_price": 55,
        }
        record = {
            "phase": "SHOP_SCREEN",
            "available_options_before": [{
                "option_id": "shop:potion:dex", "choice_index": 7,
                "target": {"kind": "potion", "item": {
                    "id": "Dexterity Potion", "price": 55,
                }},
            }],
        }

        status, listing, reason = independent_oracle._oracle_bound_shop_listing(
            record, claim
        )

        self.assertEqual("clear", status)
        self.assertEqual("bound_purchase_listing_exact", reason)
        self.assertEqual(claim, listing)

    def test_blood_potion_delta_includes_healing_relics(self):
        delta = independent_oracle._oracle_healing_potion_delta({
            "current_hp": 20, "max_hp": 80,
            "relics": [
                {"id": "Sacred Bark"}, {"id": "Magic Flower"},
                {"id": "Toy Ornithopter"},
            ],
        }, "BloodPotion")

        self.assertEqual((56, 0), delta)

    def test_pantograph_is_a_known_passive_pickup(self):
        self.assertTrue(independent_oracle._oracle_passive_relic_pickup_known(
            "pantograph", {"audit_projection_version": 3}
        ))


if __name__ == "__main__":
    unittest.main()
