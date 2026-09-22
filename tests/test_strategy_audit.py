import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autoplay
import independent_oracle
import strategy_audit
from spirecomm.spire.card import CardType


def decision(**overrides):
    value = {
        "record_type": "decision",
        "decision_schema_version": 2,
        "decision_hash": "new",
        "policy_version": "fast-policy-v5",
        "trace_schema_version": 3,
        "attempt_id": "attempt-a",
        "run_id": "DEFECT:0:123",
        "seed": 123,
        "before_seq": 10,
        "phase": "COMBAT_TURN_1",
        "action": "end",
        "turn": 1,
        "combat_id": "combat:test",
        "audit_oracle_version": "independent-oracle-v2",
        "trace_capabilities": [
            "ordered_hand", "player_state", "monster_state", "potions",
            "next_turn_start_hp_loss",
        ],
        "hp_before": 20,
        "energy_before": 2,
        "projected_hp_loss_before": 3,
        "projected_attack_hp_loss_before": 3,
        "projected_end_turn_hp_loss_before": 0,
        "projected_next_turn_start_hp_loss_before": 0,
        "projected_block_before": 0,
        "player_block_before": 0,
        "orichalcum_active_before": False,
        "decision_outcome": {"hp_delta": -8},
        "hand_before": [],
        "player_before": {
            "current_hp": 20, "max_hp": 70, "block": 0, "energy": 2,
            "powers": [], "orbs": [],
        },
        "monsters_before": [{
            "enemy_instance_id": "enemy:test", "id": "Cultist",
            "monster_index": 0, "current_hp": 48, "max_hp": 48,
            "block": 0, "intent": "ATTACK", "move_adjusted_damage": 3,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [],
        }],
        "potions_before": [],
        "relic_ids_before": [],
    }
    value.update(overrides)
    return value


def lagavulin_attrition_incident_records(*, take_tempo=False):
    """Compact trace of attempt 59e6bac7, seed 6887903629271680007."""

    monster = {
        "enemy_instance_id": "enemy:a424075128ef8e3664a8",
        "id": "Lagavulin",
        "monster_index": 0,
        "current_hp": 100,
        "max_hp": 109,
        "block": 4,
        "intent": "ATTACK",
        "move_adjusted_damage": 18,
        "move_hits": 1,
        "is_gone": False,
        "half_dead": False,
        "powers": [{"id": "Metallicize", "amount": 4}],
    }
    hand = [
        {
            "id": "Defend_R", "card_instance_id": "defend-1",
            "type": "SKILL", "cost": 1, "damage": 0,
            "block": 7, "base_block": 5, "is_playable": True,
        },
        {
            "id": "Strike_R", "card_instance_id": "strike-1",
            "type": "ATTACK", "cost": 1, "damage": 6,
            "base_damage": 6, "block": 1, "base_block": -1,
            "is_playable": True,
        },
        {
            "id": "Defend_R", "card_instance_id": "defend-2",
            "type": "SKILL", "cost": 1, "damage": 0,
            "block": 7, "base_block": 5, "is_playable": True,
        },
        {
            "id": "Carnage", "card_instance_id": "carnage-1",
            "type": "ATTACK", "cost": 2, "damage": 20,
            "base_damage": 20, "block": 1, "base_block": -1,
            "is_playable": True,
        },
        {
            "id": "Defend_R", "card_instance_id": "defend-3",
            "type": "SKILL", "cost": 1, "damage": 0,
            "block": 7, "base_block": 5, "is_playable": True,
        },
    ]
    player = {
        "current_hp": 78, "max_hp": 85, "block": 0, "energy": 3,
        "powers": [{"id": "Dexterity", "amount": 2}], "orbs": [],
    }
    played = (
        [("Defend_R", "defend-1"), ("Carnage", "carnage-1")]
        if take_tempo else
        [
            ("Defend_R", "defend-1"),
            ("Defend_R", "defend-2"),
            ("Strike_R", "strike-1"),
        ]
    )
    records = []
    for offset, (card_id, instance_id) in enumerate(played):
        records.append(decision(
            attempt_id="59e6bac7-a2d7-411c-9b13-180e5e3eb302",
            run_id="IRONCLAD:0:6887903629271680007",
            seed=6887903629271680007,
            before_seq=291512 + offset,
            phase="COMBAT_TURN_2",
            turn=2,
            combat_id="combat:lagavulin-floor-6",
            action="play",
            card_id=card_id,
            card_instance_id=instance_id,
            hp_before=78,
            energy_before=3,
            projected_incoming_before=18,
            hand_before=copy.deepcopy(hand),
            player_before=copy.deepcopy(player),
            monsters_before=[copy.deepcopy(monster)],
            decision={
                "reason": "ordered_turn_search",
                "search": {"scaling_pressure": 0.0},
            },
        ))
    end_enemy_hp = 74 if take_tempo else 95
    end_player_hp = 67 if take_tempo else 74
    records.append(decision(
        attempt_id="59e6bac7-a2d7-411c-9b13-180e5e3eb302",
        run_id="IRONCLAD:0:6887903629271680007",
        seed=6887903629271680007,
        before_seq=291515,
        phase="COMBAT_TURN_2",
        turn=2,
        combat_id="combat:lagavulin-floor-6",
        action="end",
        hp_before=78,
        energy_before=0,
        projected_incoming_before=18,
        hand_before=[],
        player_before=copy.deepcopy(player),
        monsters_before=[copy.deepcopy(monster)],
        decision={"reason": "play_phase_exhausted"},
        authoritative_state_after={
            "game_state": {"combat_state": {
                "player": {"current_hp": end_player_hp},
                "monsters": [{"id": "Lagavulin", "current_hp": end_enemy_hp}],
            }},
        },
    ))
    debuff_monster = copy.deepcopy(monster)
    debuff_monster.update({"current_hp": end_enemy_hp, "intent": "STRONG_DEBUFF"})
    records.append(decision(
        attempt_id="59e6bac7-a2d7-411c-9b13-180e5e3eb302",
        run_id="IRONCLAD:0:6887903629271680007",
        seed=6887903629271680007,
        before_seq=291522,
        phase="COMBAT_TURN_4",
        turn=4,
        combat_id="combat:lagavulin-floor-6",
        action="end",
        hp_before=end_player_hp,
        energy_before=0,
        projected_incoming_before=0,
        hand_before=[],
        player_before={**player, "current_hp": end_player_hp},
        monsters_before=[debuff_monster],
        authoritative_state_after={
            "game_state": {"combat_state": {
                "player": {"current_hp": end_player_hp},
                "monsters": [{"id": "Lagavulin", "current_hp": end_enemy_hp}],
            }},
        },
    ))
    death_monster = copy.deepcopy(monster)
    death_monster.update({"current_hp": 17, "block": 2})
    records.append(decision(
        attempt_id="59e6bac7-a2d7-411c-9b13-180e5e3eb302",
        run_id="IRONCLAD:0:6887903629271680007",
        seed=6887903629271680007,
        before_seq=291561,
        phase="COMBAT_TURN_15",
        turn=15,
        combat_id="combat:lagavulin-floor-6",
        action="end",
        hp_before=12,
        energy_before=0,
        projected_incoming_before=18,
        hand_before=[],
        player_before={
            **player,
            "current_hp": 12,
            "powers": [
                {"id": "Strength", "amount": -4},
                {"id": "Dexterity", "amount": -2},
            ],
        },
        monsters_before=[death_monster],
        authoritative_state_after={
            "game_state": {"combat_state": {
                "player": {"current_hp": 0},
                "monsters": [{"id": "Lagavulin", "current_hp": 17}],
            }},
        },
    ))
    return records


def clear_core_audit_report():
    return {
        "schema_version": 2,
        "decision_hash": "hash",
        "attempt_id": "attempt",
        "audit_status": "clear",
        "issue_count": 0,
        "review_finding_count": 0,
        "eligible_unknown_count": 0,
        "terminal_observed": True,
        "protocol_correctness": {"status": "clear"},
        "mechanics_coverage": {"status": "clear"},
        "strategy_quality": {"status": "clear"},
        "combat_strategy_review": {
            "status": "not_applicable",
            "eligible_records": 0,
            "evaluated_records": 0,
            "unknown_records": 0,
            "issue_count": 0,
            "issue_kinds": {},
            "issues": [],
            "decision_case_corpus_includes_combat": False,
        },
        "issues": [],
        "issue_counts": {},
    }


def clear_oracle_report(**overrides):
    coverage = {
        name: {
            "eligible": 0,
            "evaluated": 0,
            "unknown": 0,
            "issues": 0,
            "status": "not_applicable",
        }
        for name in independent_oracle.REQUIRED_COVERAGE_KEYS
    }
    value = {
        "oracle_version": independent_oracle.ORACLE_VERSION,
        "coverage_contract_version": (
            independent_oracle.ORACLE_COVERAGE_CONTRACT_VERSION
        ),
        "binding_fields": list(
            independent_oracle.ATTEMPT_BINDING_FIELDS
        ) + ["terminal_state_seq"],
        "required_coverage_keys": sorted(
            independent_oracle.REQUIRED_COVERAGE_KEYS
        ),
        "disagreement_kinds": sorted(
            independent_oracle.DISAGREEMENT_KINDS
        ),
        "status": "clear",
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "disagreement_count": 0,
        "issues": [],
        "unknowns": [],
        "coverage": coverage,
        "blind_reviews": [],
        "base_game_mechanisms": {
            "contract_version": (
                independent_oracle.BASE_GAME_MECHANISM_CONTRACT_VERSION
            ),
            "eligible": 0,
            "evaluated": 0,
            "issues": [],
            "unknowns": [],
            "observations": [],
        },
    }
    value.update(overrides)
    return value


def clear_audit_receipt(attempt_id="attempt", decision_hash="hash"):
    return {
        "receipt_schema_version": 1,
        "attempt_id": attempt_id,
        "decision_hash": decision_hash,
        "exact_attempt_suffix": True,
        "trace_path": "C:/audit/attempt/autoplay.log",
        "trace_sha256": "0" * 64,
        "record_count": 3,
        "artifact_sha256": {
            name: "1" * 64
            for name in ("run_context", "run_result", "state", "selection")
        },
        "artifacts_complete": True,
    }


def not_applicable_replay_report(**overrides):
    value = {
        "status": "not_applicable",
        "issue_count": 0,
        "eligible_unknown_count": 0,
        "issues": [],
        "unknowns": [],
    }
    value.update(overrides)
    return value


class StrategyAuditTests(unittest.TestCase):
    def test_gold_death_diagnostic_distinguishes_unreachable_shop(self):
        records = [
            {
                "record_type": "decision",
                "phase": "MAP",
                "decision": {"chosen": "M@1,1"},
            },
        ]
        artifacts = {
            "state": {
                "game_state": {
                    "class": "THE_SILENT",
                    "act": 2,
                    "floor": 33,
                    "current_hp": 0,
                    "gold": 623,
                },
            },
        }

        diagnostic = strategy_audit._gold_death_diagnostic(
            records, artifacts
        )

        self.assertEqual("high_gold_death", diagnostic["status"])
        self.assertEqual("death_without_reachable_shop", diagnostic["reason"])
        self.assertEqual(623, diagnostic["gold"])
        self.assertEqual(0, diagnostic["shop_decision_count"])

    def test_scored_match_game_uncertainty_is_not_a_default_choice(self):
        candidate = {
            "id": "match:0",
            "score": 40.0,
            "score_rule_id": "match_board_semantic_priority_v1",
            "consequences": {
                "operation": "flip_match_position",
                "unknown_card": True,
            },
        }
        record = {
            "decision": {
                "reason": "match_game_explore_unknown",
                "candidate_contract": {
                    "strategy_quality_auditable": True,
                    "all_visible_options_scored": True,
                },
                "candidates": [candidate],
            },
        }

        self.assertIsNone(strategy_audit._default_or_unknown_reason(record))

        incomplete = json.loads(json.dumps(record))
        incomplete["decision"]["candidate_contract"][
            "all_visible_options_scored"
        ] = False
        self.assertEqual(
            "match_game_explore_unknown",
            strategy_audit._default_or_unknown_reason(incomplete),
        )

    def test_damage_model_prefers_current_card_over_stale_terminal_search(self):
        with patch.object(
            autoplay,
            "_current_card_attack_prediction",
            return_value=10,
        ) as predictor:
            model = autoplay.damage_model_context(
                "play",
                {
                    "reason": "terminal_plan_continuation",
                    "card_damage": 0,
                    "search": {"first_action_enemy_hp_loss": 0},
                },
                {"enemy_hp_loss": 10},
                before_game={"room_phase": "COMBAT"},
                available_commands=[],
                card_id="Doom and Gloom",
                card_instance_id="card:doom",
                target_id=None,
            )

        self.assertEqual(10, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_final_target_hp_projection",
            model["hero_to_monsters_prediction_basis"],
        )
        predictor.assert_called_once()

    def test_damage_model_does_not_bind_combo_attack_to_current_setup_skill(self):
        with patch.object(
            autoplay,
            "_current_card_attack_prediction",
            return_value=autoplay._CURRENT_CARD_NON_ATTACK,
        ):
            model = autoplay.damage_model_context(
                "play",
                {
                    "reason": "guaranteed_combo_setup_before_attacks",
                    # This projection belongs to the attack after Rage, not to
                    # the currently confirmed Rage play.
                    "first_action_enemy_hp_loss": 11,
                    "search": {},
                },
                {"enemy_hp_loss": 0, "phase_after": "COMBAT_TURN_8"},
                before_game={"room_phase": "COMBAT"},
                available_commands=[],
                card_id="Rage",
                card_instance_id="card:rage",
            )

        self.assertEqual(
            "current_card_non_attack",
            model["hero_to_monsters_prediction_basis"],
        )
        self.assertNotIn("hero_to_monsters_predicted", model)

    def test_damage_model_rejects_stale_card_damage_when_current_card_is_missing(self):
        model = autoplay.damage_model_context(
            "play",
            {
                "reason": "terminal_plan_continuation",
                "card_damage": 7,
                "search": {"first_action_enemy_hp_loss": 0},
            },
            {"enemy_hp_loss": 7},
            before_game={"room_phase": "COMBAT"},
            available_commands=[],
            card_id="missing-card",
            card_instance_id="missing-instance",
        )

        self.assertIsNone(model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_attack_unresolved",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_damage_model_uses_card_damage_when_authoritative_frame_is_unavailable(self):
        model = autoplay.damage_model_context(
            "play",
            {
                "reason": "terminal_plan_continuation",
                "card_damage": 7,
                "search": {"first_action_enemy_hp_loss": 0},
            },
            {"enemy_hp_loss": 7},
        )

        self.assertEqual(7, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "decision_card_damage",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_current_card_prediction_binds_target_by_raw_enemy_slot(self):
        card = SimpleNamespace(
            card_id="Strike_R",
            uuid="card:b",
            type=CardType.ATTACK,
            damage=6,
            upgrades=0,
        )
        first = SimpleNamespace(
            monster_id="Cultist", monster_index=0, current_hp=20,
            half_dead=False, is_gone=False, block=0, powers=[],
        )
        second = SimpleNamespace(
            monster_id="JawWorm", monster_index=1, current_hp=20,
            half_dead=False, is_gone=False, block=4, powers=[],
        )
        parsed = SimpleNamespace(
            hand=[card], monsters=[first, second], relics=[],
            player=SimpleNamespace(powers=[], orbs=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "hand": [{
                    "id": "Strike_R", "uuid": "card:b",
                }],
                # The bridge omits monster_index in some live frames; the
                # bound enemy_instance_id still identifies list slot 1.
                "monsters": [
                    {"enemy_instance_id": "enemy:a", "id": "Cultist"},
                    {"enemy_instance_id": "enemy:b", "id": "JawWorm"},
                ],
            },
        }
        with patch.object(autoplay.Game, "from_json", return_value=parsed):
            model = autoplay.damage_model_context(
                "play",
                {
                    "reason": "terminal_plan_continuation",
                    "search": {"first_action_enemy_hp_loss": 0},
                },
                {"enemy_hp_loss": 2},
                before_game=before,
                available_commands=[],
                card_id="Strike_R",
                card_instance_id="card:b",
                target_id="enemy:b",
            )

        self.assertEqual(2, model["hero_to_monsters_predicted"])
        self.assertEqual(
            "current_card_final_target_hp_projection",
            model["hero_to_monsters_prediction_basis"],
        )

    def test_current_card_prediction_caps_overkill_at_bound_target_hp(self):
        card = SimpleNamespace(
            card_id="Strike_R", uuid="card:overkill", type=CardType.ATTACK,
            damage=9, upgrades=0,
        )
        target = SimpleNamespace(
            monster_id="Cultist", monster_index=0, current_hp=2,
            half_dead=False, is_gone=False, block=0, powers=[],
        )
        parsed = SimpleNamespace(
            hand=[card], monsters=[target], relics=[],
            player=SimpleNamespace(powers=[], orbs=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "hand": [{"id": "Strike_R", "uuid": "card:overkill"}],
                "monsters": [{
                    "enemy_instance_id": "enemy:overkill", "id": "Cultist",
                    "current_hp": 2,
                }],
            },
        }
        with patch.object(autoplay.Game, "from_json", return_value=parsed):
            predicted = autoplay._current_card_attack_prediction(
                before, [], card_id="Strike_R",
                card_instance_id="card:overkill",
                target_id="enemy:overkill",
            )

        self.assertEqual(2, predicted)

    def test_damage_model_omits_irrelevant_null_direction(self):
        play = autoplay.damage_model_context(
            "play", {"card_damage": 6}, {"enemy_hp_loss": 6},
        )
        end = autoplay.damage_model_context(
            "end", {"projected_attack_hp_loss": 3},
            {"player_hp_loss": 3},
        )

        self.assertNotIn("monsters_to_hero_predicted", play)
        self.assertNotIn("monsters_to_hero_actual", play)
        self.assertNotIn("hero_to_monsters_predicted", end)
        self.assertNotIn("hero_to_monsters_actual", end)

    def test_action_true_combat_end_requires_single_remaining_action(self):
        self.assertFalse(autoplay.action_true_combat_end_prediction({
            "planned_sequence": [{"card_uuid": "a"}, {"card_uuid": "b"}],
            "search": {"true_combat_end": True},
        }))
        self.assertTrue(autoplay.action_true_combat_end_prediction({
            "planned_sequence": [{"card_uuid": "b"}],
            "search": {"true_combat_end": True},
        }, current_action_enemy_hp_loss=12, living_enemy_hp_total=12))
        self.assertFalse(autoplay.action_true_combat_end_prediction({
            "planned_sequence": [{"card_uuid": "b"}],
            "search": {"true_combat_end": True},
        }, current_action_enemy_hp_loss=8, living_enemy_hp_total=12))

    def test_current_bane_prediction_uses_bound_targets_poison(self):
        card = SimpleNamespace(
            card_id="Bane",
            uuid="card:bane",
            type=CardType.ATTACK,
            damage=10,
            upgrades=0,
        )
        poison = SimpleNamespace(power_id="Poison", power_name="Poison", amount=3)
        target = SimpleNamespace(
            monster_id="Cultist",
            monster_index=0,
            current_hp=40,
            half_dead=False,
            is_gone=False,
            block=0,
            powers=[poison],
        )
        parsed = SimpleNamespace(
            hand=[card],
            monsters=[target],
            relics=[],
            player=SimpleNamespace(powers=[], orbs=[]),
        )
        before = {
            "room_phase": "COMBAT",
            "combat_state": {
                "hand": [{"id": "Bane", "uuid": "card:bane"}],
                "monsters": [{
                    "enemy_instance_id": "enemy:poisoned",
                    "id": "Cultist",
                }],
            },
        }
        with patch.object(autoplay.Game, "from_json", return_value=parsed):
            predicted = autoplay._current_card_attack_prediction(
                before,
                [],
                card_id="Bane",
                card_instance_id="card:bane",
                target_id="enemy:poisoned",
            )

        self.assertEqual(20, predicted)

    def test_post_action_context_records_stable_enemy_hp_delta(self):
        before = {
            "current_hp": 50,
            "combat_state": {"monsters": [{
                "enemy_instance_id": "enemy:a",
                "id": "Cultist",
                "current_hp": 40,
                "block": 0,
            }]},
        }
        after = copy.deepcopy(before)
        after["current_hp"] = 47
        after["combat_state"]["monsters"][0]["current_hp"] = 34

        outcome = autoplay.combat_post_action_context(before, after)

        self.assertEqual(3, outcome["player_hp_loss"])
        self.assertEqual(-3, outcome["player_hp_delta"])
        self.assertEqual(0, outcome["player_hp_gain"])
        self.assertEqual(6, outcome["enemy_hp_loss"])
        self.assertEqual(40, outcome["enemy_hp_before_total"])
        self.assertEqual(34, outcome["enemy_hp_after_total"])
        self.assertEqual(1, outcome["matched_enemy_count"])
        self.assertEqual(
            6, outcome["enemy_hp_changes"][0]["hp_loss"]
        )

    def test_post_action_context_infers_final_hp_on_lethal_combat_transition(self):
        before = {
            "room_phase": "COMBAT",
            "current_hp": 30,
            "combat_state": {"monsters": [{
                "enemy_instance_id": "enemy:a",
                "id": "JawWorm",
                "current_hp": 4,
                "block": 0,
                "is_gone": False,
            }]},
        }
        after = {
            "room_phase": "REWARD",
            "current_hp": 36,
            "combat_state": {"monsters": []},
        }

        outcome = autoplay.combat_post_action_context(
            before, after, action="play"
        )

        self.assertEqual(4, outcome["enemy_hp_loss"])
        self.assertEqual(1, outcome["matched_enemy_count"])
        self.assertTrue(outcome["enemy_hp_changes"][0]["final_hp_inferred"])

        # END damage (poison/Combust/orbs) is not hero card damage.
        end_outcome = autoplay.combat_post_action_context(
            before, after, action="end"
        )
        self.assertEqual(0, end_outcome["enemy_hp_loss"])
        self.assertEqual(0, end_outcome["matched_enemy_count"])

    def test_post_action_context_exposes_positive_hp_delta(self):
        before = {
            "current_hp": 3,
            "combat_state": {"monsters": []},
        }
        after = {
            "current_hp": 24,
            "combat_state": {"monsters": []},
        }

        outcome = autoplay.combat_post_action_context(before, after, "end")

        self.assertEqual(21, outcome["player_hp_delta"])
        self.assertEqual(0, outcome["player_hp_loss"])
        self.assertEqual(21, outcome["player_hp_gain"])

    def test_end_turn_overprediction_records_positive_hp_delta_reason(self):
        record = decision(
            action="end",
            hp_before=3,
            hp_after=24,
            decision_outcome={"hp_delta": 21, "player_hp_gain": 21},
            projected_attack_hp_loss_before=9,
            projected_end_turn_hp_loss_before=0,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 0,
                "playable_card_ids": [],
                "reason": "play_phase_exhausted",
                "safety_class": "no_legal_resources",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        issue = next(
            issue for issue in report["issues"]
            if issue["kind"] == "end_turn_damage_overprediction"
        )
        self.assertEqual(
            "observed_positive_hp_delta_or_unmodeled_healing",
            issue["reason"],
        )

    def test_end_turn_overprediction_identifies_lizard_tail_trigger(self):
        record = decision(
            action="end",
            hp_before=2,
            hp_after=35,
            relic_ids_before=["Lizard Tail"],
            decision_outcome={"hp_delta": 33, "player_hp_gain": 33},
            projected_attack_hp_loss_before=8,
            projected_end_turn_hp_loss_before=0,
            end_turn_resources={
                "energy_before": 0,
                "playable_card_count": 0,
                "playable_card_ids": [],
                "reason": "no_playable_card_or_target",
                "safety_class": "no_legal_resources",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        issue = next(
            issue for issue in report["issues"]
            if issue["kind"] == "end_turn_damage_overprediction"
        )
        self.assertEqual(
            "lizard_tail_survival_trigger_or_healing_relic",
            issue["reason"],
        )

    def test_relic_audit_reclassifies_known_aliases_from_raw_trace_ids(self):
        record = decision(
            relic_ids_before=[
                "PreservedInsect", "Whetstone", "FossilizedHelix",
                "Smiling Mask", "Strawberry",
            ],
            relic_model_coverage={
                "modeled_relic_ids": [],
                "state_reflected_relic_ids": [],
                "noncombat_relic_ids": [],
                "unclassified_relic_ids": [
                    "preservedinsect", "whetstone", "fossilizedhelix",
                    "smiling mask", "strawberry",
                ],
            },
        )

        coverage = strategy_audit.audit_records(
            [record], "new"
        )["relic_model_coverage"]

        self.assertEqual({}, coverage["unclassified_relics"])
        self.assertEqual(5, sum(coverage["state_reflected_relics"].values()))
        self.assertEqual(0, coverage["records_with_unclassified"])

    def test_black_star_reclassifies_as_observable_noncombat_not_unknown(self):
        report = strategy_audit.audit_records([
            decision(relic_ids_before=["Black Star"]),
            decision(before_seq=11, relic_ids_before=["BlackStar"]),
        ], "new")

        coverage = report["relic_model_coverage"]
        self.assertEqual({"black star": 1, "blackstar": 1},
                         coverage["noncombat_relics"])
        self.assertEqual({}, coverage["unclassified_relics"])
        self.assertEqual(0, coverage["records_with_unclassified"])
        self.assertNotIn(
            "black star", report["mechanics_coverage"]["unclassified_ids"]
        )
        self.assertNotIn(
            "blackstar", report["mechanics_coverage"]["unclassified_ids"]
        )
        self.assertEqual("clear", report["mechanics_coverage"]["status"])

    def test_relic_audit_reports_four_coverage_levels_without_false_exactness(self):
        record = decision(
            relic_ids_before=[
                "Orichalcum", "Nuclear Battery", "Sundial",
                "Mummified Hand",
            ],
        )

        coverage = strategy_audit.audit_records(
            [record], "new"
        )["relic_model_coverage"]

        self.assertEqual(2, coverage["coverage_contract_version"])
        self.assertEqual(
            {"orichalcum": 1, "sundial": 1},
            coverage["exact_branch_relics"],
        )
        self.assertEqual(
            coverage["exact_branch_relics"], coverage["modeled_relics"]
        )
        self.assertEqual(
            {"nuclear battery": 1}, coverage["state_reflected_relics"]
        )
        self.assertEqual(
            {"mummified hand": 1}, coverage["heuristic_relics"]
        )
        self.assertEqual({}, coverage["unsupported_relics"])
        self.assertEqual(0, coverage["records_with_unsupported"])

    def test_relic_audit_reclassifies_stale_modeled_ids_without_raw_trace(self):
        record = decision(
            relic_model_coverage={
                "modeled_relic_ids": [
                    "mummified hand", "sundial", "ice cream",
                    "self forming clay",
                ],
                "state_reflected_relic_ids": [],
                "noncombat_relic_ids": [],
                "unclassified_relic_ids": [],
            },
        )
        record.pop("relic_ids_before")

        coverage = strategy_audit.audit_records(
            [record], "new"
        )["relic_model_coverage"]

        self.assertEqual({"sundial": 1}, coverage["exact_branch_relics"])
        self.assertEqual({"sundial": 1}, coverage["modeled_relics"])
        self.assertEqual(
            {
                "ice cream": 1, "mummified hand": 1,
                "self forming clay": 1,
            },
            coverage["heuristic_relics"],
        )
        self.assertEqual({}, coverage["unsupported_relics"])

    def test_combat_trace_records_raw_power_coverage_for_both_sides(self):
        context = autoplay.combat_trace_context({
            "class": "IRONCLAD",
            "ascension_level": 20,
            "seed": 123,
            "act": 2,
            "floor": 30,
            "room_type": "MONSTER",
            "relics": [],
            "potions": [],
            "combat_state": {
                "turn": 2,
                "hand": [],
                "player": {
                    "current_hp": 30, "max_hp": 70, "block": 0,
                    "energy": 3, "orbs": [],
                    "powers": [{
                        "id": "Hex", "name": "Hex", "amount": 1,
                    }],
                },
                "monsters": [{
                    "enemy_instance_id": "enemy:snake",
                    "id": "SnakePlant",
                    "monster_index": 0,
                    "current_hp": 70,
                    "max_hp": 70,
                    "block": 0,
                    "intent": "ATTACK",
                    "move_adjusted_damage": 8,
                    "move_hits": 3,
                    "is_gone": False,
                    "half_dead": False,
                    "powers": [{
                        "id": "Painful Stabs", "name": "Painful Stabs",
                        "amount": 1,
                    }],
                }],
            },
        }, phase="COMBAT_TURN_2", attempt_id="attempt-a")

        self.assertIn("power_model_coverage", context["trace_capabilities"])
        entries = context["power_model_coverage"]["active_powers"]
        self.assertEqual(
            [("player", "Hex"), ("monster", "Painful Stabs")],
            [(entry["owner_role"], entry["raw_id"]) for entry in entries],
        )
        self.assertTrue(all(
            entry["category"] == "exact_branch" for entry in entries
        ))
        self.assertTrue(all(entry["handler"] for entry in entries))

    def test_power_audit_reclassifies_old_raw_state_and_reports_frequency(self):
        record = decision(
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0, "energy": 2,
                "orbs": [],
                "powers": [
                    {"id": "Hex", "name": "Hex", "amount": 1},
                    {
                        "id": "StaticDischarge",
                        "name": "Static Discharge",
                        "amount": 1,
                    },
                ],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:test", "id": "SnakePlant",
                "monster_index": 0, "current_hp": 48, "max_hp": 48,
                "block": 0, "intent": "ATTACK",
                "move_adjusted_damage": 3, "move_hits": 1,
                "is_gone": False, "half_dead": False,
                "powers": [
                    {
                        "id": "Painful Stabs", "name": "Painful Stabs",
                        "amount": 1,
                    },
                    {
                        "id": "FutureModPower", "name": "Future Mod Power",
                        "amount": 2,
                    },
                ],
            }],
            # Deliberately stale/wrong persisted labels. Raw state must win.
            power_model_coverage={
                "exact_branch_power_ids": [
                    "hex", "painful stabs", "futuremodpower",
                ],
                "unsupported_power_ids": [],
                "unclassified_power_ids": [],
            },
        )

        coverage = strategy_audit.audit_records(
            [record], "new"
        )["power_model_coverage"]

        self.assertEqual(1, coverage["coverage_contract_version"])
        self.assertEqual(4, coverage["active_power_occurrences"])
        self.assertEqual(
            {
                "hex": 1,
                "painful stabs": 1,
                "staticdischarge": 1,
            },
            coverage["exact_branch_powers"],
        )
        self.assertEqual({}, coverage["unsupported_powers"])
        self.assertEqual(
            {"futuremodpower": 1}, coverage["unclassified_powers"]
        )
        self.assertEqual(0, coverage["records_with_unsupported"])
        self.assertEqual(1, coverage["records_with_unclassified"])
        self.assertEqual(
            {"hex": 1, "staticdischarge": 1},
            coverage["by_owner"]["player"]["exact_branch_powers"],
        )
        self.assertEqual(
            {"painful stabs": 1},
            coverage["by_owner"]["monster"]["exact_branch_powers"],
        )

    def test_infinite_blades_is_authoritative_next_frame_heuristic(self):
        record = decision(
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0, "energy": 2,
                "orbs": [],
                "powers": [{
                    "id": "InfiniteBladesPower",
                    "name": "localized display",
                    "amount": 1,
                }],
            },
            monsters_before=[],
        )

        report = strategy_audit.audit_records([record], "new")
        coverage = report["power_model_coverage"]

        self.assertEqual(
            {"infinitebladespower": 1}, coverage["heuristic_powers"]
        )
        self.assertEqual({}, coverage["unsupported_powers"])
        self.assertEqual(0, coverage["records_with_unsupported"])
        self.assertNotIn(
            "active_unsupported_mechanics",
            {item["kind"] for item in report["anomalies"]},
        )

    def test_next_turn_block_is_authoritative_next_frame_heuristic(self):
        for power_id in ("Next Turn Block", "NextTurnBlock"):
            with self.subTest(power_id=power_id):
                record = decision(
                    player_before={
                        "current_hp": 20, "max_hp": 70, "block": 0,
                        "energy": 2, "orbs": [],
                        "powers": [{
                            "id": power_id,
                            "name": "localized display",
                            "amount": 3,
                        }],
                    },
                    monsters_before=[],
                )

                report = strategy_audit.audit_records([record], "new")
                coverage = report["power_model_coverage"]

                self.assertEqual(
                    {power_id.lower(): 1}, coverage["heuristic_powers"]
                )
                self.assertEqual({}, coverage["unsupported_powers"])
                self.assertEqual(0, coverage["records_with_unsupported"])
                self.assertNotIn(
                    "active_unsupported_mechanics",
                    {item["kind"] for item in report["anomalies"]},
                )

    def test_juggernaut_is_heuristic_with_independent_damage_bounds(self):
        record = decision(
            player_before={
                "current_hp": 40, "max_hp": 80, "block": 0,
                "powers": [{
                    "id": "Juggernaut", "name": "Juggernaut", "amount": 5,
                }],
            },
            monsters_before=[],
        )

        report = strategy_audit.audit_records([record], "new")
        coverage = report["power_model_coverage"]

        self.assertEqual(
            {"juggernaut": 1}, coverage["heuristic_powers"]
        )
        self.assertEqual({}, coverage["unsupported_powers"])
        self.assertEqual(0, coverage["records_with_unsupported"])
        self.assertNotIn(
            "active_unsupported_mechanics",
            {item["kind"] for item in report["anomalies"]},
        )

    def test_turn_damage_summary_compares_final_monster_and_end_turn_hp(self):
        play = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            damage_model={
                "hero_to_monsters_predicted": 6,
                "hero_to_monsters_actual": 5,
            },
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 5},
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
        )
        end = decision(
            action="end",
            hp_before=20,
            hp_after=17,
            decision_outcome={"hp_delta": -3, "player_hp_loss": 3},
            projected_hp_loss_before=3,
            projected_attack_hp_loss_before=3,
            projected_end_turn_hp_loss_before=0,
            end_turn_resources={
                "energy_before": 0,
                "playable_card_count": 0,
                "playable_card_ids": [],
                "reason": "no_playable_card_or_target",
                "safety_class": "no_legal_resources",
            },
        )

        report = strategy_audit.audit_records([play, end], "new")

        metrics = report["turn_damage_metrics"]
        self.assertEqual(1, metrics["hero_to_monsters"]["evaluated"])
        self.assertEqual(1, metrics["monsters_to_hero"]["evaluated"])
        self.assertEqual(-1, report["turn_damage_summaries"][0][
            "hero_to_monsters"
        ]["delta"])
        self.assertEqual(0, report["turn_damage_summaries"][0][
            "monsters_to_hero"
        ]["delta"])

    def test_turn_damage_prediction_uses_first_search_final_hp_once(self):
        first = decision(
            action="play",
            card_id="Streamline",
            card_instance_id="streamline-card",
            monsters_before=[
                {"id": "JawWorm", "current_hp": 20},
                {"id": "Cultist", "current_hp": 10},
            ],
            decision={"search": {"final_enemy_hp": [8, 0]}},
            damage_model={
                "hero_to_monsters_predicted": 5,
                "hero_to_monsters_actual": 5,
            },
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 5},
        )
        second = decision(
            action="play",
            card_id="Strike_R",
            card_instance_id="strike-card",
            damage_model={
                "hero_to_monsters_predicted": 10,
                "hero_to_monsters_actual": 10,
            },
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 10},
        )
        end = decision(
            action="end",
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 7},
        )

        report = strategy_audit.audit_records([first, second, end], "new")
        summary = report["turn_damage_summaries"][0]["hero_to_monsters"]
        self.assertEqual(22, summary["predicted"])
        self.assertEqual(22, summary["actual"])
        self.assertEqual(0, summary["delta"])
        self.assertEqual(
            "planned_search_final_monster_hp_delta", summary["predicted_basis"]
        )
        adherence = report["turn_damage_metrics"]["hero_to_monsters"][
            "by_plan_adherence"
        ]
        self.assertEqual(1, adherence["no_initial_card_sequence"]["evaluated"])

    def test_end_turn_with_positive_resources_is_audited(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=8,
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                # A genuinely lethal attack is an avoidable survival action;
                # ordinary non-lethal chip damage is intentionally ignored by
                # the END-resource audit because it may expose Thorns.
                "type": "ATTACK", "cost": 1, "damage": 60,
                "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Strike_B"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"]["end_turn_with_resources"])

    def test_fairy_revival_equivalent_block_is_not_reported_as_wasted(self):
        record = decision(
            action="end",
            hp_before=10,
            player_before={
                "current_hp": 10, "max_hp": 80, "block": 0, "energy": 1,
                "powers": [{"id": "Metallicize", "amount": 3}], "orbs": [],
            },
            energy_before=1,
            projected_hp_loss_before=10,
            projected_attack_hp_loss_before=10,
            projected_block_before=3,
            player_block_before=0,
            monsters_before=[{
                "enemy_instance_id": "enemy:reptomancer",
                "id": "Reptomancer", "current_hp": 171, "max_hp": 189,
                "block": 8, "intent": "ATTACK",
                "move_adjusted_damage": 30, "move_hits": 1,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 6, "damage": 0, "is_playable": True,
            }],
            potions_before=[{"id": "FairyPotion"}],
            relic_ids_before=["Torii"],
            end_turn_resources={
                "energy_before": 1, "playable_card_count": 1,
                "playable_card_ids": ["Defend_R"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
            decision={"reason": "no_positive_marginal_action"},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn("end_turn_with_resources", report["issue_counts"])
        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_fairy_revival_exemption_rejects_changed_revive_timing(self):
        record = {
            "player_before": {
                "current_hp": 10, "max_hp": 80, "powers": [],
            },
            "projected_block_before": 3,
            "projected_end_turn_hp_loss_before": 0,
            "projected_next_turn_start_hp_loss_before": 0,
            "potions_before": [{"id": "FairyPotion"}],
            "relic_ids_before": [],
            "end_turn_resources": {
                "playable_card_ids": ["Defend_R"],
            },
            "monsters_before": [{
                "id": "BookOfStabbing", "current_hp": 100,
                "intent": "ATTACK", "move_adjusted_damage": 15,
                "move_hits": 2, "is_gone": False, "half_dead": False,
            }],
        }

        self.assertFalse(
            strategy_audit._fairy_revival_proves_block_neutral(record, 6)
        )

    def test_inconsistent_reason_with_only_redundant_block_is_not_a_resource_issue(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            player_block_before=12,
            monsters_before=[{
                "id": "SnakePlant", "current_hp": 4, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 1,
                "move_hits": 1, "is_gone": False, "half_dead": False,
            }],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                # Historical compact traces could carry a stale positive
                # damage field on non-Attacks.  It is not a lethal action.
                "block": 5, "damage": 5, "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Defend_R"],
                "reason": "no_positive_marginal_action",
                "safety_class": "inconsistent_no_playable_reason",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )

    def test_redundant_covered_defend_against_chosen_hex_is_audited(self):
        record = decision(
            action="play",
            card_id="Defend_G",
            card_instance_id="defend-card",
            hp_before=57,
            energy_before=1,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=9,
            player_block_before=9,
            player_before={
                "current_hp": 57, "max_hp": 56, "block": 9, "energy": 1,
                "powers": [{"id": "Hex", "name": "Hex", "amount": 1}],
                "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:chosen",
                "id": "Chosen", "monster_index": 0,
                "current_hp": 43, "max_hp": 100, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 7,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Defend_G", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["redundant_covered_block_play"]
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"][
                "redundant_covered_block_play"
            ]["status"],
        )
        self.assertEqual(
            1,
            report["audit_coverage"][
                "redundant_covered_block_play"
            ]["eligible"],
        )

    def test_redundant_covered_defend_against_any_attacker_is_audited(self):
        """The check must not be gated on Chosen/Hex specifically."""
        record = decision(
            action="play",
            card_id="Defend_G",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=19,
            player_block_before=19,
            player_before={
                "current_hp": 40, "max_hp": 70, "block": 19, "energy": 1,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:collector",
                "id": "TheCollector", "monster_index": 0,
                "current_hp": 190, "max_hp": 282, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 19,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Defend_G", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["redundant_covered_block_play"]
        )

    def test_zero_cost_corruption_defend_is_not_a_redundant_block_violation(self):
        record = decision(
            action="play",
            card_id="Defend_R",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=12,
            player_block_before=12,
            player_before={
                "current_hp": 40, "max_hp": 70, "block": 12,
                "energy": 0,
                "powers": [{
                    "id": "Corruption", "name": "Corruption", "amount": -1,
                }],
                "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:guardian",
                "id": "TheGuardian", "monster_index": 0,
                "current_hp": 80, "max_hp": 240, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 12,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [{
                    "id": "Sharp Hide", "name": "Sharp Hide", "amount": 4,
                }],
            }],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 0, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "redundant_covered_block_play", report["issue_counts"]
        )

    def test_letter_opener_trigger_is_not_a_redundant_block_violation(self):
        record = decision(
            action="play",
            card_id="Defend_R",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=8,
            player_block_before=8,
            player_before={
                "current_hp": 47, "max_hp": 80, "block": 8,
                "energy": 1, "powers": [], "orbs": [],
            },
            relic_ids_before=["Letter Opener"],
            relics_before=[{
                "id": "Letter Opener", "name": "Letter Opener",
                "counter": 2,
            }],
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:red-louse",
                    "id": "RedLouse", "monster_index": 0,
                    "current_hp": 15, "max_hp": 15, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 5,
                    "move_hits": 1, "is_gone": False,
                    "half_dead": False, "powers": [],
                },
                {
                    "enemy_instance_id": "enemy:green-louse",
                    "id": "GreenLouse", "monster_index": 1,
                    "current_hp": 14, "max_hp": 14, "block": 0,
                    "intent": "BUFF", "move_adjusted_damage": 0,
                    "move_hits": 0, "is_gone": False,
                    "half_dead": False, "powers": [],
                },
            ],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 10},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "redundant_covered_block_play", report["issue_counts"]
        )
        self.assertEqual(
            0,
            report["audit_coverage"][
                "redundant_covered_block_play"
            ]["eligible"],
        )

    def test_block_card_before_attack_has_giant_head_slow_setup_value(self):
        defend = decision(
            before_seq=10,
            after_seq=11,
            action="play",
            card_id="Defend_R",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=16,
            player_block_before=16,
            player_before={
                "current_hp": 40, "max_hp": 70, "block": 16,
                "energy": 3, "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:head",
                "id": "GiantHead", "monster_index": 0,
                "current_hp": 373, "max_hp": 500, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 13,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [{"id": "Slow", "name": "Slow", "amount": 3}],
            }],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )
        attack = decision(
            before_seq=11,
            after_seq=12,
            action="play",
            card_id="Headbutt",
            card_instance_id="attack-card",
            hand_before=[{
                "id": "Headbutt", "card_instance_id": "attack-card",
                "type": "ATTACK", "cost": 1, "damage": 20,
                "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 20},
        )

        report = strategy_audit.audit_records([defend, attack], "new")

        self.assertNotIn(
            "redundant_covered_block_play", report["issue_counts"]
        )

    def test_calipers_block_is_a_resource_when_it_increases_retained_block(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            player_block_before=38,
            player_before={
                "current_hp": 1, "max_hp": 70, "block": 38, "energy": 1,
                "powers": [], "orbs": [],
            },
            relic_ids_before=["Calipers"],
            monsters_before=[{
                "enemy_instance_id": "enemy:mass",
                "id": "WrithingMass", "monster_index": 0,
                "current_hp": 144, "max_hp": 160, "block": 4,
                "intent": "ATTACK_DEBUFF", "move_adjusted_damage": 7,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Defend_G", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 8,
                "block": 8, "damage": 0, "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Defend_G"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"]["end_turn_with_resources"])

    def test_redundant_covered_defend_against_chosen_without_hex_field_is_audited(self):
        """Chosen's intrinsic Hex must not disappear when the power field is stale."""

        record = decision(
            action="play",
            card_id="Defend_G",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=9,
            player_block_before=9,
            player_before={
                "current_hp": 57, "max_hp": 70, "block": 9, "energy": 1,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:chosen",
                "id": "Chosen", "monster_index": 0,
                "current_hp": 43, "max_hp": 100, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 7,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Defend_G", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "damage": 0, "is_playable": True,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["redundant_covered_block_play"]
        )

    def test_redundant_covered_survivor_against_chosen_hex_is_audited(self):
        """Survivor must not evade the covered-attack/Hex audit."""

        record = decision(
            action="play",
            card_id="Survivor",
            card_instance_id="survivor-card",
            hp_before=33,
            energy_before=1,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_block_before=12,
            player_block_before=12,
            player_before={
                "current_hp": 33, "max_hp": 70, "block": 12, "energy": 1,
                "powers": [{"id": "Hex", "name": "Hex", "amount": 1}],
                "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:chosen",
                "id": "Chosen", "monster_index": 0,
                "current_hp": 29, "max_hp": 100, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 5,
                "move_hits": 2, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[
                {"id": "Dazed", "card_instance_id": "dazed-1",
                 "type": "STATUS", "cost": -2, "is_playable": False},
                {"id": "Dazed", "card_instance_id": "dazed-2",
                 "type": "STATUS", "cost": -2, "is_playable": False},
                {"id": "Survivor", "card_instance_id": "survivor-card",
                 "type": "SKILL", "cost": 1, "base_block": 9,
                 "block": 9, "damage": 0, "is_playable": True},
            ],
            decision={
                "reason": "prevent_avoidable_hp_loss",
                "card_id": "Survivor",
                "search": {"generated_dazed": 1},
            },
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["redundant_covered_block_play"]
        )
        issue = next(
            item for item in report["issues"]
            if item["kind"] == "redundant_covered_block_play"
        )
        self.assertEqual("Survivor", issue["card_id"])
        self.assertTrue(issue["chosen_hex"])

    def test_passive_doom_end_turn_requires_recomputable_lightning_proof(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_5",
            turn=5,
            hp_before=52,
            energy_before=1,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_next_turn_start_hp_loss_before=0,
            player_before={
                "current_hp": 52, "max_hp": 82, "block": 0, "energy": 1,
                "powers": [],
                "orbs": [
                    {"id": "Lightning", "passive_amount": 6},
                    {"id": "Lightning", "passive_amount": 6},
                    {"id": "Lightning", "passive_amount": 6},
                ],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:darkling",
                "id": "Darkling", "monster_index": 0,
                "current_hp": 17, "max_hp": 48, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 8,
                "move_hits": 2, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "damage": 9,
                "is_playable": True,
            }],
            decision={"reason": "all_enemies_passively_doomed"},
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "passive_doom_claim_unproven", report["issue_counts"]
        )
        proof = report["passive_doom_evidence"]
        self.assertEqual(1, len(proof))
        self.assertEqual("proven", proof[0]["status"])
        self.assertEqual(
            "single_target_lightning_orbs", proof[0]["source"]
        )
        coverage = report["audit_coverage"]["passive_doom_claim_proof"]
        self.assertEqual("clear", coverage["status"])
        self.assertEqual(1, coverage["eligible"])
        self.assertEqual(1, coverage["evaluated"])

    def test_passive_doom_end_turn_without_damage_proof_is_blocking(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_5",
            decision={"reason": "all_enemies_passively_doomed"},
            hp_before=52,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            player_before={
                "current_hp": 52, "max_hp": 82, "block": 0, "energy": 1,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:darkling",
                "id": "Darkling", "monster_index": 0,
                "current_hp": 17, "max_hp": 48, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 8,
                "move_hits": 2, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            hand_before=[],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["passive_doom_claim_unproven"]
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"][
                "passive_doom_claim_proof"
            ]["status"],
        )

    def test_passive_doom_combust_proof_handles_block_and_multiple_targets(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_5",
            hp_before=20,
            energy_before=3,
            projected_hp_loss_before=1,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=1,
            player_before={
                "current_hp": 20, "max_hp": 80, "block": 0,
                "energy": 3,
                "powers": [{"id": "Combust", "amount": 5}],
                "orbs": [],
            },
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:a", "id": "Louse",
                    "current_hp": 2, "max_hp": 12, "block": 0,
                    "is_gone": False, "half_dead": False, "powers": [],
                },
                {
                    "enemy_instance_id": "enemy:b", "id": "Louse",
                    "current_hp": 4, "max_hp": 12, "block": 1,
                    "is_gone": False, "half_dead": False, "powers": [],
                },
            ],
            hand_before=[{
                "id": "Strike_R", "card_instance_id": "strike",
                "type": "ATTACK", "cost": 1, "damage": 6,
                "is_playable": True,
            }],
            decision={"reason": "all_enemies_passively_doomed"},
            decision_outcome={"hp_delta": -1, "enemy_hp_loss": 6},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "passive_doom_claim_unproven", report["issue_counts"]
        )
        self.assertNotIn("end_turn_with_resources", report["issue_counts"])
        self.assertEqual(
            "combust", report["passive_doom_evidence"][0]["source"]
        )

    def test_passive_doom_combust_proof_rejects_unabsorbed_block(self):
        record = decision(
            hp_before=20,
            projected_end_turn_hp_loss_before=1,
            player_before={
                "current_hp": 20, "max_hp": 80, "block": 0,
                "energy": 1,
                "powers": [{"id": "Combust", "amount": 5}],
                "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:a", "id": "Cultist",
                "current_hp": 4, "max_hp": 48, "block": 2,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
        )

        proof = strategy_audit._record_passive_doom_proof(record)

        self.assertEqual("unproven", proof["status"])

    def test_passive_doom_stone_calendar_proves_turn_seven_kill(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_7",
            turn=7,
            hp_before=30,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            relic_ids_before=["StoneCalendar"],
            player_before={
                "current_hp": 30, "max_hp": 51, "block": 0,
                "energy": 2, "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:head", "id": "GiantHead",
                "current_hp": 48, "max_hp": 500, "block": 0,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
            hand_before=[],
            decision={"reason": "all_enemies_passively_doomed"},
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 48},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "passive_doom_claim_unproven", report["issue_counts"]
        )
        self.assertEqual(
            "stone_calendar", report["passive_doom_evidence"][0]["source"]
        )

    def test_passive_doom_stone_calendar_rejects_wrong_turn(self):
        record = decision(
            turn=6,
            relic_ids_before=["Stone Calendar"],
            player_before={
                "current_hp": 30, "max_hp": 51, "block": 0,
                "energy": 2, "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:head", "id": "GiantHead",
                "current_hp": 48, "max_hp": 500, "block": 0,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
        )

        proof = strategy_audit._record_passive_doom_proof(record)

        self.assertEqual("unproven", proof["status"])

    def test_audit_flags_forced_exhaust_of_damage_reserve(self):
        true_grit = decision(
            before_seq=10,
            after_seq=11,
            action="play",
            phase="COMBAT_TURN_16",
            hp_before=27,
            projected_hp_loss_before=5,
            card_id="True Grit",
            card_instance_id="true-grit",
            hand_before=[
                {
                    "id": "True Grit", "card_instance_id": "true-grit",
                    "type": "SKILL", "cost": 1, "block": 9,
                    "is_playable": True,
                },
                {
                    "id": "Perfected Strike",
                    "card_instance_id": "perfected", "type": "ATTACK",
                    "cost": 2, "damage": 8, "is_playable": True,
                },
                {
                    "id": "Thunderclap", "card_instance_id": "clap",
                    "type": "ATTACK", "cost": 1, "damage": 4,
                    "is_playable": True,
                },
            ],
            decision={
                "reason": "ordered_turn_search",
                "planned_sequence": [
                    {"card_id": "True Grit", "card_uuid": "true-grit"},
                    {"card_id": "Thunderclap", "card_uuid": "clap"},
                ],
            },
        )
        cards = [
            {
                "id": "Perfected Strike", "card_instance_id": "perfected",
                "type": "ATTACK", "cost": 2, "damage": 8,
                "is_playable": False,
            },
            {
                "id": "Thunderclap", "card_instance_id": "clap",
                "type": "ATTACK", "cost": 1, "damage": 4,
                "is_playable": True,
            },
        ]
        options = [
            {
                "option_id": "option:" + card["card_instance_id"],
                "target": {"kind": "card", "card": card},
            }
            for card in cards
        ]
        selection = decision(
            before_seq=11,
            after_seq=12,
            action="choose",
            phase="HAND_SELECT",
            hp_before=27,
            projected_hp_loss_before=None,
            available_options_before=options,
            chosen_option_before=options[0],
            decision={
                "reason": "hand_select_context_score",
                "selection_action": "ExhaustAction",
                "selection_semantics": "remove",
            },
            authoritative_state_before={
                "game_state": {
                    "current_action": "ExhaustAction",
                    "combat_state": {
                        "hand": cards,
                        "draw_pile": [],
                        "discard_pile": [],
                        "limbo": [],
                        "monsters": [{
                            "enemy_instance_id": "enemy:parasite",
                            "id": "Shelled Parasite", "current_hp": 52,
                            "block": 12, "is_gone": False,
                            "half_dead": False,
                            "powers": [{
                                "id": "Plated Armor", "amount": 12,
                            }],
                        }],
                    },
                },
            },
        )

        report = strategy_audit.audit_records(
            [true_grit, selection], "new"
        )

        self.assertEqual(
            1, report["issue_counts"]["forced_damage_source_exhaustion"]
        )
        coverage = report["audit_coverage"][
            "forced_damage_source_exhaustion"
        ]
        self.assertEqual(1, coverage["eligible"])
        self.assertEqual(1, coverage["evaluated"])
        self.assertEqual(1, coverage["violations"])

    def test_audit_flags_plan_using_only_card_true_grit_must_exhaust(self):
        record = decision(
            action="play",
            phase="COMBAT_TURN_18",
            card_id="True Grit",
            card_instance_id="true-grit",
            hand_before=[
                {
                    "id": "True Grit", "card_instance_id": "true-grit",
                    "type": "SKILL", "cost": 1, "block": 9,
                    "is_playable": True,
                },
                {
                    "id": "Thunderclap", "card_instance_id": "clap",
                    "type": "ATTACK", "cost": 1, "damage": 4,
                    "is_playable": True,
                },
            ],
            decision={
                "reason": "ordered_turn_search",
                "planned_sequence": [
                    {"card_id": "True Grit", "card_uuid": "true-grit"},
                    {"card_id": "Thunderclap", "card_uuid": "clap"},
                ],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "impossible_post_forced_exhaust_plan"
            ],
        )

    def test_coffee_dripper_vampires_bite_purge_is_audited_as_package_break(self):
        def option(option_id, card_id, instance_id):
            return {
                "option_id": option_id,
                "choice_index": 0 if card_id == "Bite" else 1,
                "label": card_id,
                "target": {
                    "kind": "card", "card_instance_id": instance_id,
                    "card": {"id": card_id, "name": card_id,
                              "card_instance_id": instance_id},
                },
            }

        bite = option("grid:bite", "Bite", "bite-1")
        defend = option("grid:defend", "Defend_R", "defend-1")
        record = decision(
            action="choose",
            phase="GRID",
            relic_ids_before=["Coffee Dripper"],
            decision_context={
                "relic_ids": ["Coffee Dripper"],
                "deck_counts": {"Bite+0": 5, "Defend_R+0": 4},
            },
            selected_choice_ids=["grid:bite"],
            final_choice_ids=["grid:bite"],
            requested_target_id="grid:bite",
            resolved_target_id="grid:bite",
            chosen_option_before=bite,
            available_options_before=[bite, defend],
            decision={
                "reason": "grid_remove_or_transform_weakest",
                "chosen": "Bite",
                "candidates": [
                    {
                        "id": "Bite", "choice_id": "grid:bite",
                        "score": 100.0,
                        "selection_eligible": True,
                        "consequences": {
                            "operation": "grid_purge",
                            "selected_card": {
                                "id": "Bite",
                                "card_instance_id": "bite-1",
                            },
                        },
                    },
                    {
                        "id": "Defend_R", "choice_id": "grid:defend",
                        "score": 24.0,
                        "selection_eligible": True,
                        "consequences": {
                            "operation": "grid_purge",
                            "selected_card": {
                                "id": "Defend_R",
                                "card_instance_id": "defend-1",
                            },
                        },
                    },
                ],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["vampires_bite_package_purged"]
        )

    def test_vampires_package_fallback_uses_authoritative_grid_without_candidates(self):
        event = decision(
            phase="EVENT",
            action="choose",
            chosen_option_before={
                "option_id": "event:vampires",
                "target": {
                    "event_id": "Vampires",
                    "card": {"id": "Bite", "card_instance_id": "bite-reward"},
                },
            },
            selected_choice_ids=["event:vampires"],
            decision_outcome={"max_hp_delta": -24, "hp_delta": -24},
        )
        grid = decision(
            phase="GRID",
            action="choose",
            relic_ids_before=["Coffee Dripper"],
            decision_context={
                "relic_ids": ["Coffee Dripper"],
                "deck_counts": {"Bite+0": 5, "Defend_R+0": 4},
            },
            chosen_option_before={
                "option_id": "grid:bite",
                "target": {
                    "kind": "card",
                    "card": {"id": "Bite", "card_instance_id": "bite-1"},
                },
            },
            selected_choice_ids=["grid:bite"],
            available_options_before=[
                {"option_id": "grid:bite", "target": {
                    "card": {"id": "Bite", "card_instance_id": "bite-1"},
                }},
                {"option_id": "grid:defend", "target": {
                    "card": {"id": "Defend_R", "card_instance_id": "defend-1"},
                }},
            ],
            decision={"reason": "grid_remove_or_transform_weakest"},
            authoritative_choice_settlement={
                "status": "observed",
                "classified_future_costs": [{"operation": "grid_purge"}],
                "observed_outcome": {"deck": {"removed": []}},
            },
        )

        report = strategy_audit.audit_records([event, grid], "new")

        self.assertEqual(
            1, report["issue_counts"]["vampires_bite_package_purged"]
        )

    def test_vampires_live_schema_binds_top_level_event_and_canonical_choice(self):
        # Live v2 frames may omit chosen_option_before/decision.candidates.
        # The event selection is still authoritative in event_id plus the
        # canonical choice surface, and must seed the later package ledger.
        bite_card = {"id": "Bite", "card_instance_id": "bite-reward"}
        event = decision(
            phase="EVENT",
            action="choose",
            event_id="Vampires",
            chosen={
                "choice_index": 0,
                "requested_target_id": "option:event-bite",
                "resolved_target_id": "option:event-bite",
            },
            selected_choice_ids=["option:event-bite"],
            final_choice_ids=["option:event-bite"],
            canonical_choices=[
                {
                    "choice_id": "option:event-bite",
                    "choice_index": 0,
                    "selected": True,
                    "target": {"event_id": "Vampires", "card": bite_card},
                },
                {
                    "choice_id": "option:event-leave",
                    "choice_index": 1,
                    "selected": False,
                    "target": {"event_id": "Vampires", "card": None},
                },
            ],
            available_options=[
                {"choice_index": 0, "target": {"event_id": "Vampires", "card": bite_card}},
                {"choice_index": 1, "target": {"event_id": "Vampires", "card": None}},
            ],
            decision_outcome={
                "hp_delta": -24,
                "max_hp_delta": -24,
                "deck": {"added": [bite_card] * 5, "removed": [{"id": "Strike_R"}] * 5},
            },
        )
        selected_bite = {
            "id": "Bite",
            "card_instance_id": "bite-1",
        }
        grid = decision(
            phase="GRID",
            action="choose",
            relic_ids_before=["Coffee Dripper"],
            decision_context={
                "relic_ids": ["Coffee Dripper"],
                "deck_counts": {"Bite+0": 5, "Defend_R+0": 4},
            },
            chosen={"choice_index": 0},
            selected_choice_ids=["grid:bite"],
            final_choice_ids=["grid:bite"],
            canonical_choices=[
                {
                    "choice_id": "grid:bite",
                    "choice_index": 0,
                    "selected": True,
                    "target": {"card": selected_bite},
                    "consequences": {
                        "operation": "grid_purge",
                        "selected_card": selected_bite,
                    },
                },
                {
                    "choice_id": "grid:defend",
                    "choice_index": 1,
                    "selected": False,
                    "target": {"card": {"id": "Defend_R", "card_instance_id": "defend-1"}},
                    "consequences": {
                        "operation": "grid_purge",
                        "selected_card": {"id": "Defend_R", "card_instance_id": "defend-1"},
                    },
                },
            ],
            available_options=[
                {"choice_index": 0, "target": {"card": selected_bite}},
                {"choice_index": 1, "target": {"card": {"id": "Defend_R", "card_instance_id": "defend-1"}}},
            ],
            decision={"reason": "grid_remove_or_transform_weakest"},
            authoritative_choice_settlement={
                "status": "observed",
                "classified_future_costs": [{"operation": "grid_purge"}],
                "observed_outcome": {
                    "deck": {"removed": [selected_bite]},
                },
            },
        )

        report = strategy_audit.audit_records([event, grid], "new")

        self.assertEqual(
            1, report["issue_counts"]["vampires_bite_package_purged"]
        )

    def test_vampires_package_audits_each_later_bite_purge(self):
        """The package ledger must survive multiple GRID purge frames."""
        event = decision(
            phase="EVENT",
            action="choose",
            event_id="Vampires",
            chosen_option_before={
                "option_id": "event:vampires",
                "target": {
                    "event_id": "Vampires",
                    "card": {"id": "Bite", "card_instance_id": "bite-reward"},
                },
            },
            selected_choice_ids=["event:vampires"],
            decision_outcome={"max_hp_delta": -24, "hp_delta": -24},
        )

        def grid(seq, bite_count, instance):
            selected = {"id": "Bite", "card_instance_id": instance}
            return decision(
                before_seq=seq,
                phase="GRID",
                action="choose",
                relic_ids_before=["Coffee Dripper"],
                decision_context={
                    "relic_ids": ["Coffee Dripper"],
                    "deck_counts": {
                        "Bite+0": bite_count,
                        "Defend_R+0": 4,
                    },
                },
                chosen_option_before={
                    "option_id": f"grid:bite:{seq}",
                    "target": {"card": selected},
                },
                available_options_before=[
                    {
                        "option_id": f"grid:bite:{seq}",
                        "target": {"card": selected},
                    },
                    {
                        "option_id": f"grid:defend:{seq}",
                        "target": {
                            "card": {
                                "id": "Defend_R",
                                "card_instance_id": f"defend-{seq}",
                            },
                        },
                    },
                ],
                selected_choice_ids=[f"grid:bite:{seq}"],
                decision={"reason": "grid_unknown_conservative_weak_card"},
                authoritative_choice_settlement={
                    "status": "observed",
                    "classified_future_costs": [{"operation": "grid_purge"}],
                    "observed_outcome": {"deck": {"removed": [selected]}},
                },
            )

        report = strategy_audit.audit_records(
            [event, grid(20, 5, "bite-1"), grid(21, 4, "bite-2")],
            "new",
        )

        self.assertEqual(
            2, report["issue_counts"]["vampires_bite_package_purged"]
        )

    def test_addict_authoritative_relic_resolves_gold_loss(self):
        record = decision(
            phase="EVENT",
            action="choose",
            chosen_option_before={
                "option_id": "event:addict",
                "target": {"event_id": "Addict"},
            },
            decision_outcome={
                "gold_delta": -85,
                "relics": {"added": [{"id": "Anchor"}]},
            },
            authoritative_choice_settlement={
                "status": "unresolved",
                "before_seq": 10,
                "after_seq": 12,
                "observed_outcome": {
                    "gold_delta": -85,
                    "relics": {"added": [{"id": "Anchor"}]},
                },
            },
        )

        resolution = strategy_audit._addict_trade_resolution(record)

        self.assertEqual("clear", resolution["status"])
        self.assertEqual("Anchor", resolution["acquired_benefit"]["relic_id"])

    def test_vampires_authoritative_bite_package_resolves_max_hp_loss(self):
        bindings = {
            "attempt_id": "attempt-a", "run_id": "IRONCLAD:0:123",
            "seed": 123, "character": "IRONCLAD", "ascension_level": 0,
            "run_type": "standard", "decision_hash": "new",
            "controller_hash": "controller", "policy_version": "fast-policy-v5",
            "selection_id": "selection", "selection_digest": "digest",
        }
        strikes = [
            {"id": "Strike_R", "card_instance_id": f"strike-{index}"}
            for index in range(4)
        ]
        defend_card = {"id": "Defend_R", "card_instance_id": "defend-0"}
        bites = [
            {"id": "Bite", "card_instance_id": f"bite-{index}"}
            for index in range(5)
        ]
        before_game = {
            "current_hp": 80, "max_hp": 80, "gold": 100,
            "deck": strikes + [defend_card],
            "relics": [{"id": "Burning Blood", "counter": -1}],
        }
        after_game = {
            "current_hp": 56, "max_hp": 56, "gold": 100,
            "deck": [defend_card] + bites,
            "relics": [{"id": "Burning Blood", "counter": -1}],
        }

        def envelope(seq, game):
            return {
                "protocol_version": 2, "state_seq": seq,
                "game_state": copy.deepcopy(game), **bindings,
            }

        record = decision(
            **bindings, phase="EVENT", action="choose",
            before_seq=10, after_seq=11,
            chosen_option_before={
                "option_id": "vampires:accept",
                "target": {
                    "kind": "event_option", "event_id": "Vampires",
                    "original_button_index": 0,
                },
            },
            authoritative_state_before=envelope(10, before_game),
            authoritative_state_after=envelope(11, after_game),
        )

        resolution = strategy_audit._vampires_bite_package_resolution(record)

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(5, resolution["acquired_benefit"]["count"])
        self.assertEqual(
            4,
            resolution["acquired_benefit"][
                "removed_starter_strike_count"
            ],
        )
        tampered = copy.deepcopy(record)
        tampered["authoritative_state_after"]["game_state"]["max_hp"] = 57
        self.assertIsNone(
            strategy_audit._vampires_bite_package_resolution(tampered)
        )

    def test_shelled_parasite_end_with_affordable_attack_is_audited(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_1",
            decision={"reason": "no_positive_marginal_action"},
            energy_before=2,
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
            projected_end_turn_hp_loss_before=0,
            player_before={
                "current_hp": 30, "max_hp": 70, "block": 0, "energy": 2,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:shelled",
                "id": "ShelledParasite", "monster_index": 0,
                "current_hp": 60, "max_hp": 60, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 8,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [{
                    "id": "PlatedArmor", "name": "Plated Armor",
                    "amount": 14,
                }],
            }],
            hand_before=[{
                "id": "Strike_R", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "damage": 6,
                "base_damage": 6, "is_playable": True,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["missed_shelled_parasite_progress"],
        )
        self.assertEqual(
            1,
            report["audit_coverage"][
                "missed_shelled_parasite_progress"
            ]["violations"],
        )

    def test_shelled_parasite_zero_energy_x_attack_is_not_a_false_positive(self):
        record = decision(
            action="end",
            phase="COMBAT_TURN_1",
            decision={"reason": "no_positive_marginal_action"},
            energy_before=0,
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
            projected_end_turn_hp_loss_before=0,
            player_before={
                "current_hp": 30, "max_hp": 70, "block": 0, "energy": 0,
                "powers": [], "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:shelled",
                "id": "ShelledParasite", "monster_index": 0,
                "current_hp": 60, "max_hp": 60, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 8,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [{"id": "PlatedArmor", "amount": 14}],
            }],
            hand_before=[{
                "id": "Whirlwind", "card_instance_id": "x-card",
                "type": "ATTACK", "cost": -1, "damage": 5,
                "base_damage": 5, "is_playable": True,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "missed_shelled_parasite_progress", report["issue_counts"]
        )

    def test_barricade_liveness_stall_is_audited_after_twelve_turns(self):
        records = []
        for turn in range(1, 13):
            records.append(decision(
                before_seq=100 + turn,
                phase=f"COMBAT_TURN_{turn}",
                turn=turn,
                action="end",
                decision={"reason": "no_positive_marginal_action"},
                energy_before=0,
                hp_before=40,
                projected_hp_loss_before=0,
                projected_attack_hp_loss_before=0,
                projected_end_turn_hp_loss_before=0,
                player_before={
                    "current_hp": 40, "max_hp": 70, "block": 0,
                    "energy": 0, "powers": [], "orbs": [],
                },
                monsters_before=[{
                    "enemy_instance_id": "enemy:spheric",
                    "id": "SphericGuardian", "monster_index": 0,
                    "current_hp": 20, "max_hp": 20,
                    "block": 40 + turn,
                    "intent": "ATTACK", "move_adjusted_damage": 7,
                    "move_hits": 2, "is_gone": False,
                    "half_dead": False,
                    "powers": [{
                        "id": "Barricade", "name": "Barricade",
                        "amount": -1,
                    }],
                }],
                hand_before=[],
                decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
            ))

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(
            1, report["issue_counts"]["barricade_liveness_stall"]
        )
        issue = next(
            item for item in report["combat_strategy_review"]["issues"]
            if item["kind"] == "barricade_liveness_stall"
        )
        self.assertEqual(12, issue["turns_without_progress"])
        self.assertEqual(41, issue["best_block"])

    def test_barricade_liveness_requires_durable_block_progress_evidence(self):
        records = []
        for turn in range(1, 13):
            records.append(decision(
                before_seq=200 + turn,
                phase=f"COMBAT_TURN_{turn}",
                turn=turn,
                action="end",
                decision={"reason": "no_positive_marginal_action"},
                energy_before=0,
                hp_before=40,
                projected_hp_loss_before=0,
                projected_attack_hp_loss_before=0,
                projected_end_turn_hp_loss_before=0,
                player_before={
                    "current_hp": 40, "max_hp": 70, "block": 0,
                    "energy": 0, "powers": [], "orbs": [],
                },
                monsters_before=[{
                    "enemy_instance_id": "enemy:ordinary",
                    "id": "JawWorm", "monster_index": 0,
                    "current_hp": 20, "max_hp": 20,
                    "block": 40 + turn,
                    "intent": "ATTACK", "move_adjusted_damage": 7,
                    "move_hits": 2, "is_gone": False,
                    "half_dead": False, "powers": [],
                }],
                hand_before=[],
                decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
            ))

        report = strategy_audit.audit_records(records, "new")

        self.assertNotIn(
            "barricade_liveness_stall", report["issue_counts"]
        )

    def test_end_turn_with_nonlethal_attack_is_not_false_positive(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=8,
            monsters_before=[{
                "id": "SphericGuardian", "current_hp": 48,
                "block": 20, "intent": "ATTACK", "move_adjusted_damage": 7,
                "move_hits": 1,
            }],
            hand_before=[{
                "id": "Sweep", "card_instance_id": "sweep-card",
                "type": "ATTACK", "cost": 1, "damage": 7,
                "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Sweep"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )

    def test_combust_self_damage_does_not_make_defend_a_survival_gain(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=1,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=1,
            monsters_before=[{
                "id": "Cultist", "current_hp": 48, "block": 0,
                "intent": "BUFF", "move_adjusted_damage": 0,
                "move_hits": 0, "is_gone": False, "half_dead": False,
            }],
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "is_playable": True,
            }, {
                "id": "Limit Break", "card_instance_id": "limit-card",
                "type": "SKILL", "cost": 1, "damage": 0,
                "block": 0, "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 2,
                "playable_card_ids": ["Defend_R", "Limit Break"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )

    def test_attack_damage_with_playable_block_remains_audited(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            projected_hp_loss_before=3,
            projected_attack_hp_loss_before=3,
            projected_end_turn_hp_loss_before=0,
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "is_playable": True,
            }],
            energy_before=1,
            end_turn_resources={
                "energy_before": 1,
                "playable_card_count": 1,
                "playable_card_ids": ["Defend_R"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"]["end_turn_with_resources"])

    def test_card_damage_post_action_mismatch_is_reported(self):
        record = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            enemy_instance_id="enemy:test",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
            decision={
                "card_damage": 9,
                "search": {"first_action_enemy_hp_loss": 6},
            },
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_loss": 4,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:test",
                    "hp_before": 48,
                    "hp_after": 44,
                    "hp_loss": 4,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"][
            "card_damage_overprediction"
        ])
        self.assertEqual(
            {"actual_below_prediction": 1},
            report["damage_consistency"]["player_actions"],
        )

    def test_card_damage_waits_for_repeated_attack_grid_settlement(self):
        attack = decision(
            before_seq=20,
            after_seq=21,
            action="play",
            card_id="Headbutt",
            card_instance_id="headbutt-card",
            enemy_instance_id="enemy:test",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss", "post_action_damage",
            ],
            hand_before=[{
                "id": "Headbutt", "card_instance_id": "headbutt-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
            decision={
                "reason": "ordered_turn_search",
                "search": {
                    "first_action_enemy_hp_loss": 58,
                    "first_action_resolution_count": 2,
                },
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:test", "id": "GiantHead",
                "monster_index": 0, "current_hp": 100, "max_hp": 500,
                "block": 0, "intent": "ATTACK",
                "move_adjusted_damage": 13, "move_hits": 1,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
            decision_outcome={
                "hp_delta": 0, "phase_after": "GRID", "enemy_hp_loss": 28,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:test",
                    "hp_before": 100, "hp_after": 72, "hp_loss": 28,
                }],
            },
        )
        settled = decision(
            before_seq=24,
            after_seq=25,
            action="end",
            turn=1,
            combat_id="combat:test",
            monsters_before=[{
                "enemy_instance_id": "enemy:test", "id": "GiantHead",
                "monster_index": 0, "current_hp": 42, "max_hp": 500,
                "block": 0, "intent": "ATTACK",
                "move_adjusted_damage": 13, "move_hits": 1,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
            decision_outcome={"hp_delta": -13},
        )

        report = strategy_audit.audit_records([attack, settled], "new")

        self.assertNotIn("card_damage_overprediction", report["issue_counts"])
        self.assertEqual(
            1,
            report["damage_consistency"]["player_actions"]["exact"],
        )

    def test_card_damage_ignores_null_rows_proven_dead_before_action(self):
        record = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            enemy_instance_id="enemy:live",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:dead", "id": "Slime",
                    "current_hp": 0, "max_hp": 12, "block": 0,
                    "intent": "NONE", "move_adjusted_damage": 0,
                    "move_hits": 1, "is_gone": True,
                    "half_dead": False, "powers": [],
                },
                {
                    "enemy_instance_id": "enemy:live", "id": "Louse",
                    "current_hp": 5, "max_hp": 12, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 5,
                    "move_hits": 1, "is_gone": False,
                    "half_dead": False, "powers": [],
                },
            ],
            decision={"search": {"first_action_enemy_hp_loss": 5}},
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_loss": 5,
                "enemy_hp_changes": [
                    {
                        "enemy_instance_id": "enemy:dead",
                        "hp_before": 0, "hp_after": None,
                        "hp_loss": None,
                    },
                    {
                        "enemy_instance_id": "enemy:live",
                        "hp_before": 5, "hp_after": 0, "hp_loss": 5,
                    },
                ],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            "clear",
            report["audit_coverage"]["card_damage_consistency"]["status"],
        )
        self.assertNotIn(
            "card_damage_observation_incomplete",
            report["review_finding_counts"],
        )

    def test_card_damage_live_null_row_is_a_blocking_finding(self):
        record = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            enemy_instance_id="enemy:test",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
            decision={"search": {"first_action_enemy_hp_loss": 6}},
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:test",
                    "hp_before": 48, "hp_after": None, "hp_loss": None,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            "inconclusive",
            report["audit_coverage"]["card_damage_consistency"]["status"],
        )
        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "card_damage_observation_incomplete"
        )
        self.assertEqual(
            "live_enemy_hp_loss_missing",
            finding["unresolved_changes"][0]["reason"],
        )

    def test_coarse_card_damage_is_not_used_for_consistency(self):
        record = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            enemy_instance_id="enemy:test",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "is_playable": True,
            }],
            decision={"card_damage": 99},
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_loss": 4,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:test",
                    "hp_before": 4,
                    "hp_after": 0,
                    "hp_loss": 4,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "card_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            {}, report["damage_consistency"]["player_actions"]
        )

    def test_terminal_continuation_ignores_stale_plan_damage(self):
        record = decision(
            action="play",
            card_id="Strike_B",
            card_instance_id="strike-card",
            enemy_instance_id="enemy:test",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Strike_B", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 0, "is_playable": True,
            }],
            decision={
                "reason": "terminal_plan_continuation",
                # This is the original whole-plan value: Strike 6 plus a
                # later passive Lightning packet.  It is not this card's
                # immediate HP loss.
                "search": {
                    "first_action_enemy_hp_loss": 10,
                    "true_combat_end": True,
                },
            },
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_loss": 6,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:test",
                    "hp_before": 13,
                    "hp_after": 7,
                    "hp_loss": 6,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "card_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual({}, report["damage_consistency"]["player_actions"])

    def test_choker_fallback_uses_target_bound_damage_not_stale_search(self):
        record = decision(
            action="play",
            card_id="Neutralize",
            card_instance_id="neutralize-card",
            enemy_instance_id="enemy:guardian",
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "post_action_damage",
            ],
            hand_before=[{
                "id": "Neutralize",
                "card_instance_id": "neutralize-card",
                "type": "ATTACK", "cost": 0,
                "damage": 4, "is_playable": True,
            }],
            monsters_before=[{
                "enemy_instance_id": "enemy:guardian",
                "id": "SphericGuardian",
                "current_hp": 20, "max_hp": 20, "block": 40,
                "intent": "DEFEND", "move_adjusted_damage": 0,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            decision={
                "reason": "velvet_choker_progress_fallback",
                "card_damage": 0,
                "search": {"first_action_enemy_hp_loss": 4},
            },
            decision_outcome={
                "hp_delta": 0,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:guardian",
                    "hp_before": 20, "hp_after": 20, "hp_loss": 0,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "card_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            1, report["damage_consistency"]["player_actions"]["exact"]
        )

    def test_zero_energy_x_attack_without_effect_is_reported(self):
        record = decision(
            action="play",
            card_id="Whirlwind",
            card_instance_id="whirlwind-card",
            energy_before=0,
            hand_before=[{
                "id": "Whirlwind",
                "card_instance_id": "whirlwind-card",
                "type": "ATTACK",
                "cost": -1,
                "damage": 5,
                "is_playable": True,
            }],
            decision={
                "search": {"first_action_enemy_hp_loss": 0}
            },
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "zero_energy_x_attack_no_effect"
            ],
        )

    def test_small_defend_into_nob_enrage_with_attack_is_reported(self):
        record = decision(
            action="play",
            turn=3,
            hp_before=52,
            # This Defend consumes the only Energy and displaces the attack.
            # A low-net-block Defend that still leaves the attack affordable
            # is not automatically a Nob mistake.
            energy_before=1,
            projected_hp_loss_before=15,
            card_id="Defend_R",
            card_instance_id="defend-card",
            hand_before=[
                {
                    "id": "Defend_R",
                    "card_instance_id": "defend-card",
                    "type": "SKILL",
                    "cost": 1,
                    "base_block": 5,
                    "block": 5,
                    "is_playable": True,
                },
                {
                    "id": "Hemokinesis",
                    "card_instance_id": "hemo-card",
                    "type": "ATTACK",
                    "cost": 1,
                    "damage": 15,
                    "is_playable": True,
                },
            ],
            monsters_before=[{
                "enemy_instance_id": "enemy:nob",
                "id": "GremlinNob",
                "current_hp": 38,
                "max_hp": 83,
                "block": 4,
                "intent": "ATTACK",
                "move_adjusted_damage": 18,
                "move_hits": 1,
                "is_gone": False,
                "half_dead": False,
                "powers": [{"id": "Anger", "amount": 2}],
            }],
            decision_outcome={"hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["low_value_nob_enrage_skill"],
        )

    def test_nob_audit_ignores_fake_dexterity_block_on_non_block_skill(self):
        record = decision(
            action="play",
            turn=3,
            hp_before=56,
            energy_before=1,
            projected_hp_loss_before=2,
            card_id="Battle Trance",
            card_instance_id="trance-card",
            hand_before=[
                {
                    "id": "Battle Trance",
                    "card_instance_id": "trance-card",
                    "type": "SKILL",
                    "cost": 0,
                    "base_block": -1,
                    "block": 4,
                    "is_playable": True,
                },
                {
                    "id": "Strike_R",
                    "card_instance_id": "strike-card",
                    "type": "ATTACK",
                    "cost": 1,
                    "damage": 6,
                    "is_playable": True,
                },
            ],
            monsters_before=[{
                "enemy_instance_id": "enemy:nob",
                "id": "GremlinNob",
                "current_hp": 57,
                "max_hp": 85,
                "block": 0,
                "intent": "ATTACK",
                "move_adjusted_damage": 24,
                "move_hits": 1,
                "is_gone": False,
                "half_dead": False,
                "powers": [{"id": "Anger", "amount": 2}],
            }],
            decision_outcome={"hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        issue = next(
            item for item in report["issues"]
            if item["kind"] == "low_value_nob_enrage_skill"
        )
        self.assertEqual(0, issue["block"])
        self.assertEqual(-2, issue["net_immediate_block"])

    def test_third_card_draw_with_normality_is_reported(self):
        shared = {
            "turn": 2,
            "combat_id": "combat:normality",
            "hp_before": 58,
            "energy_before": 3,
        }
        first = decision(
            **shared,
            action="play",
            card_id="Strike_R",
            card_instance_id="strike-card",
            hand_before=[{
                "id": "Strike_R", "card_instance_id": "strike-card",
                "type": "ATTACK", "cost": 1, "damage": 6,
                "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0},
        )
        second = decision(
            **shared,
            before_seq=11,
            action="play",
            card_id="Flame Barrier",
            card_instance_id="barrier-card",
            hand_before=[{
                "id": "Flame Barrier",
                "card_instance_id": "barrier-card",
                "type": "SKILL", "cost": 2, "block": 16,
                "is_playable": True,
            }],
            decision_outcome={"hp_delta": 0},
        )
        third = decision(
            **shared,
            before_seq=12,
            action="play",
            card_id="Offering",
            card_instance_id="offering-card",
            hand_before=[
                {
                    "id": "Normality",
                    "card_instance_id": "normality-card",
                    "type": "CURSE", "cost": -2,
                    "is_playable": False,
                },
                {
                    "id": "Offering",
                    "card_instance_id": "offering-card",
                    "type": "SKILL", "cost": 0,
                    "is_playable": True,
                },
            ],
            decision_outcome={"hp_delta": -6},
        )

        report = strategy_audit.audit_records(
            [first, second, third], "new"
        )

        self.assertEqual(
            1,
            report["issue_counts"]["normality_draw_at_card_limit"],
        )

    def test_end_turn_overprediction_classifies_passive_slime_split(self):
        record = decision(
            hp_before=75,
            projected_hp_loss_before=28,
            projected_attack_hp_loss_before=28,
            decision_outcome={"hp_delta": 0},
            player_before={
                "current_hp": 75, "max_hp": 75, "block": 5,
                "energy": 0, "powers": [],
                "orbs": [{
                    "id": "Lightning", "passive_amount": 3,
                    "evoke_amount": 8,
                }],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:slime", "id": "SlimeBoss",
                "current_hp": 72, "max_hp": 140, "block": 0,
                "intent": "ATTACK", "move_adjusted_damage": 35,
                "move_hits": 1, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        issue = next(
            item for item in report["issues"]
            if item["kind"] == "end_turn_damage_overprediction"
        )
        self.assertEqual("passive_split_transition", issue["reason"])

    def test_random_lightning_kill_is_not_a_prediction_defect(self):
        record = decision(
            hp_before=49,
            projected_hp_loss_before=4,
            projected_attack_hp_loss_before=4,
            decision_outcome={"hp_delta": 0},
            player_before={
                "current_hp": 49, "max_hp": 51, "block": 0,
                "energy": 0, "powers": [],
                "orbs": [
                    {
                        "id": "Lightning",
                        "passive_amount": 13,
                        "evoke_amount": 21,
                    },
                    {
                        "id": "Frost",
                        "passive_amount": 7,
                        "evoke_amount": 10,
                    },
                    {
                        "id": "Frost",
                        "passive_amount": 7,
                        "evoke_amount": 10,
                    },
                ],
            },
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:low", "id": "Cultist",
                    "current_hp": 3, "max_hp": 50, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 9,
                    "move_hits": 1, "is_gone": False,
                    "half_dead": False, "powers": [],
                },
                {
                    "enemy_instance_id": "enemy:high", "id": "Cultist",
                    "current_hp": 25, "max_hp": 50, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 9,
                    "move_hits": 1, "is_gone": False,
                    "half_dead": False, "powers": [],
                },
            ],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            1,
            report["damage_consistency"]["enemy_turns"][
                "actual_below_prediction"
            ],
        )

    def test_poison_corpse_explosion_overprediction_is_migrated(self):
        record = decision(
            hp_before=12,
            projected_hp_loss_before=21,
            projected_attack_hp_loss_before=21,
            decision_outcome={"hp_delta": 0},
            player_before={
                "current_hp": 12, "max_hp": 44, "block": 5,
                "energy": 0, "powers": [], "orbs": [],
            },
            monsters_before=[
                {
                    "enemy_instance_id": "enemy:source",
                    "id": "Orb Walker",
                    "current_hp": 17, "max_hp": 93, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 15,
                    "move_hits": 1, "is_gone": False,
                    "half_dead": False,
                    "powers": [
                        {"id": "Poison", "amount": 33},
                        {"id": "CorpseExplosionPower", "amount": 1},
                    ],
                },
                {
                    "enemy_instance_id": "enemy:later",
                    "id": "Orb Walker",
                    "current_hp": 68, "max_hp": 91, "block": 0,
                    "intent": "ATTACK_DEBUFF",
                    "move_adjusted_damage": 22, "move_hits": 1,
                    "is_gone": False, "half_dead": False,
                    "powers": [{"id": "Poison", "amount": 5}],
                },
            ],
        )

        self.assertEqual(
            "deterministic_poison_corpse_explosion",
            strategy_audit._end_turn_overprediction_reason(record, 21, 0),
        )
        report = strategy_audit.audit_records([record], "new")
        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )

    def test_end_turn_damage_uses_following_proceed_frame(self):
        """END is acknowledged before the deferred enemy attack resolves."""

        end = decision(
            hp_before=47,
            hp_after=47,
            projected_hp_loss_before=12,
            projected_attack_hp_loss_before=12,
            decision_outcome={"hp_delta": 0},
        )
        choose = decision(
            phase="HAND_SELECT",
            action="choose",
            hp_before=47,
            hp_after=47,
            decision_outcome={"hp_delta": 0},
        )
        proceed = decision(
            phase="HAND_SELECT",
            action="proceed",
            hp_before=47,
            hp_after=35,
            decision_outcome={"hp_delta": -12, "player_hp_loss": 12},
        )

        report = strategy_audit.audit_records([end, choose, proceed], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            {"exact": 1}, report["damage_consistency"]["enemy_turns"]
        )

    def test_lethal_hp_cap_is_exact_for_damage_consistency(self):
        record = decision(
            hp_before=3,
            projected_hp_loss_before=17,
            projected_attack_hp_loss_before=17,
            decision_outcome={"hp_delta": -3},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            {"exact": 1}, report["damage_consistency"]["enemy_turns"]
        )

    def test_fairy_revival_compares_gross_damage_and_stable_hp_delta(self):
        record = decision(
            hp_before=11,
            projected_hp_loss_before=16,
            projected_attack_hp_loss_before=16,
            projected_fairy_revive_consumed_before=True,
            projected_fairy_revive_healing_before=25,
            projected_player_hp_after_turn_before=20,
            projected_player_hp_delta_before=9,
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss",
                "fairy_revival",
            ],
            decision_outcome={"hp_delta": 9, "player_hp_gain": 9},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertNotIn(
            "fairy_revival_prediction_mismatch", report["issue_counts"]
        )
        self.assertEqual(
            {"exact": 1}, report["damage_consistency"]["enemy_turns"]
        )

    def test_fairy_revival_recomputes_heal_from_authoritative_state(self):
        binding = {
            "attempt_id": "attempt-a",
            "run_id": "DEFECT:0:123",
            "seed": 123,
            "character": "DEFECT",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "new",
            "controller_hash": "controller",
            "policy_version": "fast-policy-v5",
            "selection_id": "selection",
            "selection_digest": "a" * 64,
        }
        before_game = {
            "current_hp": 7, "max_hp": 90, "gold": 0,
            "potions": [{"id": "FairyPotion"}],
            "relics": [{"id": "Toy Ornithopter"}],
        }
        after_game = {
            "current_hp": 27, "max_hp": 90, "gold": 0,
            "potions": [{"id": "Potion Slot"}],
            "relics": [{"id": "Toy Ornithopter"}],
        }
        record = decision(
            **binding,
            before_seq=10,
            after_seq=11,
            hp_before=7,
            projected_hp_loss_before=7,
            projected_attack_hp_loss_before=7,
            projected_fairy_revive_consumed_before=True,
            projected_fairy_revive_healing_before=32,
            projected_player_hp_after_turn_before=32,
            projected_player_hp_delta_before=25,
            trace_capabilities=[
                "ordered_hand", "player_state", "monster_state",
                "potions", "next_turn_start_hp_loss", "fairy_revival",
            ],
            decision_outcome={"hp_delta": 20, "player_hp_gain": 20},
            authoritative_state_before={
                "protocol_version": 2, "state_seq": 10, **binding,
                "game_state": before_game,
            },
            authoritative_state_after={
                "protocol_version": 2, "state_seq": 11, **binding,
                "game_state": after_game,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        issue = next(
            item for item in report["issues"]
            if item["kind"] == "fairy_revival_prediction_mismatch"
        )
        self.assertEqual(7, issue["actual_gross_damage"])
        self.assertEqual(27, issue["revive_healing"])
        self.assertEqual(32, issue["predicted_revive_healing"])
        self.assertEqual(
            "authoritative_state_transition",
            issue["revive_healing_authority"],
        )

    def test_regeneration_is_removed_from_stable_end_turn_hp_delta(self):
        """Replay seq 117781: 13 damage minus 8 healing is net 5."""

        record = decision(
            hp_before=44,
            projected_hp_loss_before=13,
            projected_attack_hp_loss_before=13,
            projected_end_turn_hp_loss_before=0,
            decision_outcome={"hp_delta": -5},
            player_before={
                "current_hp": 44, "max_hp": 75, "block": 8,
                "energy": 5,
                "powers": [{"id": "Regeneration", "amount": 5}],
                "orbs": [],
            },
            relic_ids_before=["Magic Flower"],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            {"exact": 1}, report["damage_consistency"]["enemy_turns"]
        )

    def test_combat_end_healing_marks_enemy_turn_damage_unobservable(self):
        """Replay seq 121339: intermediate attack loss is not in the frame."""

        record = decision(
            hp_before=45,
            hp_after=50,
            projected_hp_loss_before=4,
            projected_attack_hp_loss_before=4,
            projected_end_turn_hp_loss_before=0,
            projected_end_turn_healing_before=3,
            relic_ids_before=["Burning Blood", "Mercury Hourglass"],
            player_before={
                "current_hp": 45, "max_hp": 80, "block": 5,
                "energy": 0,
                "powers": [{"id": "Regeneration", "amount": 3}],
                "orbs": [],
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:slaver",
                "id": "SlaverBlue",
                "current_hp": 2,
                "max_hp": 50,
                "block": 0,
                "intent": "ATTACK",
                "move_adjusted_damage": 9,
                "move_hits": 1,
                "is_gone": False,
                "half_dead": False,
                "powers": [],
            }],
            decision_outcome={
                "hp_delta": 5,
                "enemy_hp_loss": 0,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:slaver",
                    "id": "SlaverBlue",
                    "hp_before": 2,
                    "hp_after": None,
                    "hp_loss": None,
                }],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_overprediction", report["issue_counts"]
        )
        self.assertEqual(
            {"unknown": 1},
            report["damage_consistency"]["enemy_turns"],
        )
        self.assertEqual(
            1,
            report["audit_coverage"]["end_turn_damage_underprediction"][
                "unknown"
            ],
        )

    def test_exact_postcombat_damage_model_defeats_healing_obscurity(self):
        record = decision(
            hp_before=34,
            hp_after=40,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            relic_ids_before=["Burning Blood"],
            decision_outcome={
                "hp_delta": 6,
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:hexaghost",
                    "hp_before": 1, "hp_after": None, "hp_loss": None,
                }],
            },
            damage_model={
                "monsters_to_hero_basis": (
                    "end_turn_total_hp_loss_before_postcombat_healing"
                ),
                "monsters_to_hero_predicted": 0,
                "monsters_to_hero_actual": 0,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            {"exact": 1}, report["damage_consistency"]["enemy_turns"]
        )
        self.assertEqual(
            0,
            report["audit_coverage"]["end_turn_damage_underprediction"][
                "unknown"
            ],
        )

    def test_mayhem_start_of_turn_play_marks_end_damage_unobservable(self):
        record = decision(
            action="end",
            hp_before=75,
            hp_after=72,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            decision_outcome={
                "hp_delta": -3,
                "phase_after": "COMBAT_TURN_9",
                "enemy_hp_changes": [{
                    "enemy_instance_id": "enemy:guardian",
                    "hp_before": 76,
                    "hp_after": 66,
                    "hp_loss": 10,
                }],
            },
            player_before={
                "current_hp": 75, "max_hp": 90, "block": 9,
                "energy": 0,
                "powers": [{"id": "Mayhem", "amount": 1}],
                "orbs": [],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "end_turn_damage_underprediction", report["issue_counts"]
        )
        self.assertEqual(
            {"unknown": 1}, report["damage_consistency"]["enemy_turns"]
        )

    def test_invalid_non_attack_after_unload_is_reported(self):
        record = decision(
            action="play",
            card_id="Unload",
            card_instance_id="unload-card",
            hand_before=[
                {
                    "id": "Unload", "card_instance_id": "unload-card",
                    "type": "ATTACK", "cost": 1, "is_playable": True,
                },
                {
                    "id": "Accuracy", "card_instance_id": "accuracy-card",
                    "type": "POWER", "cost": 1, "is_playable": True,
                },
            ],
            decision={
                "planned_sequence": [
                    {"card_id": "Unload"},
                    {"card_id": "Accuracy"},
                ]
            },
            decision_outcome={"hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"]["invalid_post_unload_plan"])

    def test_affordable_late_thousand_cuts_is_reported(self):
        initial_hand = [
            {
                "id": "Shiv", "card_instance_id": "shiv-card",
                "type": "ATTACK", "cost": 0, "is_playable": True,
            },
            {
                "id": "A Thousand Cuts", "card_instance_id": "cuts-card",
                "type": "POWER", "cost": 2, "is_playable": True,
            },
        ]
        records = [
            decision(
                action="play",
                before_seq=10,
                energy_before=2,
                card_id="Shiv",
                card_instance_id="shiv-card",
                hand_before=initial_hand,
                decision_outcome={"hp_delta": 0},
            ),
            decision(
                action="play",
                before_seq=11,
                energy_before=2,
                card_id="A Thousand Cuts",
                card_instance_id="cuts-card",
                hand_before=[initial_hand[1]],
                decision_outcome={"hp_delta": 0},
            ),
        ]

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(1, report["issue_counts"]["late_trigger_power_setup"])

    def test_end_with_active_thousand_cuts_and_safe_card_is_reported(self):
        record = decision(
            action="end",
            energy_before=1,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            hand_before=[{
                "id": "Defend_G", "card_instance_id": "defend-card",
                "type": "SKILL", "cost": 1, "base_block": 5,
                "block": 5, "is_playable": True,
            }],
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 5,
                "energy": 1,
                "powers": [{
                    "id": "Thousand Cuts", "name": "Thousand Cuts",
                    "amount": 2,
                }],
                "orbs": [],
            },
            decision_outcome={"hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["missed_thousand_cuts_trigger"]
        )

    def test_panic_button_reserve_is_not_reported_as_avoidable_defend(self):
        report = strategy_audit.audit_records([
            decision(
                hp_before=61,
                energy_before=1,
                projected_hp_loss_before=5,
                projected_attack_hp_loss_before=5,
                projected_block_before=10,
                hand_before=[{
                    "id": "PanicButton", "type": "SKILL",
                    "is_playable": True, "block": 22,
                    "base_block": 30, "cost": 0,
                }],
                end_turn_resources={
                    "energy_before": 1,
                    "playable_card_count": 1,
                    "playable_card_ids": ["PanicButton"],
                    "reason": "no_positive_marginal_action",
                    "safety_class": (
                        "evaluated_no_positive_marginal_action"
                    ),
                },
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )
        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )

    def test_attrition_kill_action_without_end_is_evaluated_not_unknown(self):
        opportunity, reason = (
            strategy_audit._persistent_attrition_turn_opportunity([{
                "action": "play",
                "authoritative_state_after": {
                    "phase": "COMBAT_REWARD",
                    "game_state": {"room_phase": "COMPLETE"},
                },
            }])
        )

        self.assertIsNone(opportunity)
        self.assertEqual("combat_ended_before_turn_end", reason)

    def test_attrition_explicit_end_to_combat_reward_is_evaluated_not_unknown(self):
        opportunity, reason = (
            strategy_audit._persistent_attrition_turn_opportunity([
                {
                    "action": "play",
                    "monsters_before": [{
                        "id": "Orb Walker",
                        "enemy_instance_id": "orb:living",
                        "current_hp": 30,
                        "block": 0,
                        "intent": "ATTACK",
                        "is_gone": False,
                        "half_dead": False,
                        "powers": [{"id": "Ritual", "amount": 3}],
                    }],
                },
                {
                    "action": "end",
                    "authoritative_state_after": {
                        "phase": "COMBAT_REWARD",
                        "game_state": {"room_phase": "COMPLETE"},
                    },
                },
            ])
        )

        self.assertIsNone(opportunity)
        self.assertEqual("combat_ended_before_turn_end", reason)

    def test_attrition_turn_binds_duplicate_monster_ids_by_instance(self):
        living = {
            "id": "Orb Walker",
            "enemy_instance_id": "orb:living",
            "current_hp": 30,
            "block": 0,
            "intent": "ATTACK",
            "is_gone": False,
            "half_dead": False,
            "powers": [{"id": "Ritual", "amount": 3}],
        }
        dead = {
            **copy.deepcopy(living),
            "enemy_instance_id": "orb:dead",
            "current_hp": 0,
            "is_gone": True,
        }
        opportunity, reason = (
            strategy_audit._persistent_attrition_turn_opportunity([
                {
                    "action": "play",
                    "turn": 4,
                    "energy_before": 3,
                    "projected_incoming_before": 10,
                    "player_before": {
                        "current_hp": 50, "max_hp": 80, "block": 0,
                    },
                    "monsters_before": [
                        copy.deepcopy(living), copy.deepcopy(dead),
                    ],
                    "hand_before": [{
                        "id": "Strike_R",
                        "card_instance_id": "strike:1",
                        "type": "ATTACK",
                        "cost": 1,
                        "damage": 12,
                        "base_damage": 12,
                        "base_block": -1,
                        "is_playable": True,
                    }],
                    "decision": {"search": {}},
                },
                {
                    "action": "end",
                    "authoritative_state_after": {
                        "game_state": {"combat_state": {
                            "player": {"current_hp": 40},
                            "monsters": [
                                copy.deepcopy(living), copy.deepcopy(dead),
                            ],
                        }},
                    },
                },
            ])
        )

        self.assertIsNone(reason)
        self.assertIsNotNone(opportunity)
        self.assertEqual("orb:living", living["enemy_instance_id"])
        self.assertEqual(["Strike_R"], opportunity["counterfactual"]["cards"])

    def test_passive_doomed_wait_is_not_called_missed_thousand_cuts(self):
        report = strategy_audit.audit_records([
            decision(
                action="end",
                energy_before=1,
                projected_hp_loss_before=0,
                decision={"reason": "all_enemies_passively_doomed"},
                hand_before=[{
                    "id": "PanicButton", "type": "SKILL",
                    "is_playable": True, "block": 30,
                    "base_block": 30, "cost": 0,
                }],
                player_before={
                    "current_hp": 20, "max_hp": 70, "block": 0,
                    "energy": 1,
                    "powers": [{
                        "id": "Thousand Cuts", "name": "Thousand Cuts",
                        "amount": 1,
                    }],
                    "orbs": [],
                },
            ),
        ], "new")

        self.assertNotIn(
            "missed_thousand_cuts_trigger", report["issue_counts"]
        )

    def test_power_after_power_is_not_late_trigger_setup(self):
        initial_hand = [
            {
                "id": "After Image", "card_instance_id": "image-card",
                "type": "POWER", "cost": 1, "is_playable": True,
            },
            {
                "id": "A Thousand Cuts", "card_instance_id": "cuts-card",
                "type": "POWER", "cost": 2, "is_playable": True,
            },
        ]
        records = [
            decision(
                action="play", before_seq=20, energy_before=3,
                card_id="After Image", card_instance_id="image-card",
                hand_before=initial_hand,
                decision_outcome={"hp_delta": 0},
            ),
            decision(
                action="play", before_seq=21, energy_before=2,
                card_id="A Thousand Cuts", card_instance_id="cuts-card",
                hand_before=[initial_hand[1]],
                decision_outcome={"hp_delta": 0},
            ),
        ]

        report = strategy_audit.audit_records(records, "new")

        self.assertNotIn(
            "late_trigger_power_setup", report["issue_counts"]
        )

    def test_empty_cohort_is_inconclusive_not_clear(self):
        report = strategy_audit.audit_records([], "new")

        self.assertEqual(0, report["records"])
        self.assertEqual("inconclusive", report["audit_status"])

    def test_model_advice_cache_latency_and_cost_are_aggregated(self):
        records = [
            {
                "record_type": "model_advice",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "advisor": {
                    "status": "shadow",
                    "latency_ms": 100,
                    "local_cache_hit": False,
                    "usage": {
                        "prompt_cache_hit_tokens": 700,
                        "prompt_cache_miss_tokens": 300,
                        "estimated_cost_usd": 0.001,
                    },
                },
            },
            decision(
                action="choose",
                decision={
                    "model_advice": {
                        "status": "applied",
                        "applied": True,
                        "latency_ms": 200,
                        "local_cache_hit": True,
                        "usage": {
                            "prompt_cache_hit_tokens": 500,
                            "prompt_cache_miss_tokens": 500,
                            "estimated_cost_usd": 0.002,
                        },
                    }
                },
            ),
        ]

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(2, report["model_advice"]["records"])
        self.assertEqual(1, report["model_advice"]["applied"])
        self.assertEqual(1, report["model_advice"]["local_cache_hits"])
        self.assertEqual(1200, report["model_advice"]["prompt_cache_hit_tokens"])
        self.assertEqual(800, report["model_advice"]["prompt_cache_miss_tokens"])
        self.assertEqual(0.6, report["model_advice"]["prompt_cache_hit_ratio"])
        self.assertEqual(0.003, report["model_advice"]["estimated_cost_usd"])

    def test_model_advice_distinguishes_remote_valid_agreement_and_override(self):
        records = [
            {
                "record_type": "model_advice",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "advisor": {
                    "status": "shadow",
                    "rule_choice_id": "card-a",
                    "model_choice_id": "card-a",
                    "latency_ms": 120,
                    "transport": "chat_json",
                },
            },
            decision(
                action="choose",
                decision={
                    "model_advice": {
                        "status": "applied",
                        "applied": True,
                        "rule_choice_id": "card-a",
                        "model_choice_id": "card-b",
                        "latency_ms": 280,
                        "transport": "chat_json",
                    }
                },
            ),
            decision(
                action="choose",
                before_seq=11,
                decision={
                    "model_advice": {
                        "status": "recommended",
                        "rule_choice_id": "card-b",
                        "model_choice_id": "card-b",
                        "local_cache_hit": True,
                        "latency_ms": 0,
                    }
                },
            ),
            decision(
                action="choose",
                before_seq=12,
                decision={
                    "model_advice": {
                        "status": "skipped",
                        "error_class": "deterministic_margin",
                        "latency_ms": 0,
                    }
                },
            ),
        ]

        model = strategy_audit.audit_records(records, "new")["model_advice"]

        self.assertEqual(2, model["remote_consultations"])
        self.assertEqual(3, model["valid_recommendations"])
        self.assertEqual(2, model["remote_valid_recommendations"])
        self.assertEqual(2, model["model_agreements"])
        self.assertEqual(1, model["effective_overrides"])
        self.assertEqual({"deterministic_margin": 1}, model["error_counts"])
        self.assertEqual(
            {"maximum": 280, "average": 200.0},
            model["remote_latency_ms"],
        )
        self.assertTrue(model["healthy"])

    def test_model_advice_separates_protocol_confirmation_from_choice_change(self):
        records = [
            decision(
                action="return",
                decision={
                    "model_advice": {
                        "status": "agreed",
                        "rule_choice_id": "action:return",
                        "model_choice_id": "action:return",
                        "consultation_outcome": "protocol_confirmation",
                    }
                },
            ),
            decision(
                action="choose",
                decision={
                    "model_advice": {
                        "status": "applied",
                        "applied": True,
                        "rule_choice_id": "card:a",
                        "model_choice_id": "card:b",
                        "consultation_outcome": "choice_change",
                    }
                },
            ),
            decision(
                action="choose",
                decision={
                    "model_advice": {
                        "status": "agreed",
                        "rule_choice_id": "card:b",
                        "model_choice_id": "card:b",
                        "consultation_outcome": "local_confirmation",
                    }
                },
            ),
        ]

        model = strategy_audit.audit_records(records, "new")["model_advice"]

        self.assertEqual(1, model["protocol_confirmations"])
        self.assertEqual(1, model["semantic_choice_changes"])
        self.assertEqual(1, model["local_confirmations"])
        self.assertEqual(2, model["semantic_adoption_denominator"])
        self.assertEqual(0.5, model["semantic_adoption_rate"])

    def test_model_advice_errors_and_suppressions_are_classified(self):
        def advice(error, latency=0):
            return decision(
                action="choose",
                before_seq=20 + len(records),
                decision={
                    "model_advice": {
                        "status": "fallback",
                        "error_class": error,
                        "latency_ms": latency,
                    }
                },
            )

        records = []
        records.extend([
            advice("invalid_ranking_score", latency=90),
            advice("timeout", latency=1000),
            advice("circuit_or_budget"),
            advice("circuit_open"),
            advice("call_budget"),
            advice("advisor_disabled"),
        ])

        model = strategy_audit.audit_records(records, "new")["model_advice"]

        self.assertEqual(2, model["remote_consultations"])
        self.assertEqual(
            {
                "invalid_ranking_score": 1,
                "timeout": 1,
                "circuit_or_budget": 1,
                "circuit_open": 1,
                "call_budget": 1,
                "advisor_disabled": 1,
            },
            model["error_counts"],
        )
        self.assertEqual(2, model["circuit_suppressions"])
        self.assertEqual(2, model["budget_suppressions"])
        self.assertEqual(1, model["advisor_disabled_suppressions"])
        self.assertEqual(4, model["suppressed_consultations"])
        self.assertFalse(model["healthy"])

    def test_single_network_error_marks_model_adviser_unhealthy(self):
        record = decision(
            action="choose",
            decision={
                "model_advice": {
                    "status": "fallback",
                    "error_class": "network_error",
                    "latency_ms": 30,
                    "transport": "chat_json",
                }
            },
        )

        model = strategy_audit.audit_records(
            [record], "new"
        )["model_advice"]

        self.assertFalse(model["healthy"])

    def test_attack_hp_loss_exceeding_reported_plan_is_audit_issue(self):
        attack = {
            "card_instance_id": "boomerang-1",
            "id": "Sword Boomerang",
            "type": "ATTACK",
            "cost": 1,
            "is_playable": True,
            "base_block": -1,
            "block": 0,
            "damage": 9,
            "upgrades": 1,
        }
        record = decision(
            action="play",
            card_instance_id="boomerang-1",
            card_id="Sword Boomerang",
            hp_before=7,
            hp_after=0,
            decision_outcome={"hp_delta": -7},
            hand_before=[attack],
            decision={"search": {"actual_loss": 0}},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual("issues", report["audit_status"])
        self.assertEqual(
            1,
            report["issue_counts"][
                "card_play_damage_underprediction"
            ],
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"][
                "card_play_damage_underprediction"
            ]["status"],
        )

    def test_malformed_model_usage_and_latency_do_not_break_audit(self):
        records = [
            {
                "record_type": "model_advice",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "advisor": {
                    "status": "fallback",
                    "latency_ms": "not-a-number",
                    "usage": {
                        "prompt_cache_hit_tokens": "NaN",
                        "prompt_cache_miss_tokens": {},
                        "estimated_cost_usd": "Infinity",
                    },
                },
            },
            {
                "record_type": "cache_warmup",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "advisor": {
                    "usage": {"estimated_cost_usd": "bad"},
                },
            },
        ]

        model = strategy_audit.audit_records(records, "new")["model_advice"]

        self.assertEqual(0, model["prompt_cache_hit_tokens"])
        self.assertEqual(0, model["prompt_cache_miss_tokens"])
        self.assertEqual(0.0, model["estimated_cost_usd"])
        self.assertEqual({"maximum": 0, "average": 0.0}, model["latency_ms"])
        self.assertEqual(0, model["remote_consultations"])

    def test_protocol_recovery_events_are_counted_by_exact_cohort(self):
        records = [
            {
                "record_type": "protocol_event",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "event": "receipt_timeout",
            },
            {
                "record_type": "protocol_event",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-a",
                "event": "state_resync_succeeded",
            },
            {
                "record_type": "protocol_event",
                "decision_schema_version": 2,
                "decision_hash": "old",
                "attempt_id": "attempt-a",
                "event": "receipt_timeout",
            },
        ]

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(2, report["protocol_events"])
        self.assertEqual(
            {"receipt_timeout": 1, "state_resync_succeeded": 1},
            report["protocol_event_counts"],
        )

    def test_later_game_over_does_not_hide_operational_error_taint(self):
        def termination(attempt_id, kind):
            record = {
                "schema_version": 2,
                "decision_hash": "new",
                "attempt_id": attempt_id,
                "termination_kind": kind,
            }
            if kind == "game_over":
                record["record_type"] = "terminal_result"
            return record

        report = strategy_audit.audit_records([
            termination("attempt-recovered", "operational_error"),
            termination("attempt-failed", "operational_error"),
            termination("attempt-recovered", "game_over"),
        ], "new")

        self.assertEqual(2, report["terminal_attempts"])
        self.assertEqual(
            {"operational_error": 1, "game_over": 1},
            report["termination_counts"],
        )
        self.assertEqual(2, report["operational_error_attempts"])
        self.assertEqual(2, report["operational_error_events"])
        self.assertEqual(
            2,
            report["issue_counts"]["operational_error_tainted_attempt"],
        )

    def test_target_binding_rejection_is_visible_in_combat_review(self):
        report = strategy_audit.audit_records([
            {
                "schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-target-race",
                "run_id": "IRONCLAD:0:39",
                "termination_kind": "operational_error",
                "state_seq": 248424,
                "error": "SafetyError",
                "message": (
                    "action failed: enemy_instance_id does not resolve uniquely"
                ),
            },
        ], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["combat_target_binding_rejection"],
        )
        self.assertEqual(
            "issues", report["combat_strategy_review"]["status"]
        )
        target_issue = next(
            item for item in report["combat_strategy_review"]["issues"]
            if item.get("kind") == "combat_target_binding_rejection"
        )
        self.assertEqual(248424, target_issue["result_state_seq"])

    def test_unbound_preflight_error_is_not_a_terminal_attempt(self):
        report = strategy_audit.audit_records([
            {
                "schema_version": 2,
                "decision_hash": "new",
                "attempt_id": None,
                "termination_kind": "operational_error",
                "error": "SafetyError",
            },
            {
                "schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-bound",
                "termination_kind": "game_over",
                "record_type": "terminal_result",
            },
        ], "new")

        self.assertEqual(1, report["terminal_attempts"])
        self.assertEqual({"game_over": 1}, report["termination_counts"])

    def test_death_with_actionable_potion_is_reported_from_terminal_belt(self):
        attempt_id = "attempt-potion-death"
        critical_turn = decision(
            attempt_id=attempt_id,
            action="end",
            hp_before=15,
            projected_hp_loss_before=10,
            projected_attack_hp_loss_before=10,
            projected_end_turn_hp_loss_before=0,
            projected_next_turn_start_hp_loss_before=0,
            projected_end_turn_healing_before=0,
            decision_outcome={"hp_delta": -10},
            player_before={
                "current_hp": 15, "max_hp": 80, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
            potions_before=[{
                "id": "EssenceOfSteel", "slot": 0, "can_use": True,
            }],
        )
        terminal = {
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": attempt_id,
            "run_id": "DEFECT:0:123",
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": False,
            "current_hp": 0,
            "act": 2,
            "floor": 33,
            "class": "DEFECT",
            "potions": [
                {"id": "EssenceOfSteel", "slot": 0, "can_use": False},
                {"id": "FairyPotion", "slot": 1, "can_use": False},
                {"id": "Potion Slot", "slot": 2, "can_use": False},
            ],
        }

        report = strategy_audit.audit_records(
            [critical_turn, terminal], "new"
        )

        self.assertEqual(
            1,
            report["issue_counts"][
                "death_with_actionable_potion_retained"
            ],
        )
        issue = next(
            item for item in report["issues"]
            if item["kind"] == "death_with_actionable_potion_retained"
        )
        self.assertEqual(
            [{"id": "EssenceOfSteel", "slot": 0}],
            issue["retained_potions"],
        )
        self.assertEqual(
            "exact_serialized_critical_turn_mitigation",
            issue["evidence_basis"],
        )
        self.assertEqual(
            4, issue["missed_opportunities"][0]["hp_loss_reduction"]
        )

    def test_terminal_potion_name_without_proven_mitigation_is_not_an_issue(self):
        terminal = {
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "unquantified-potion-death",
            "run_id": "DEFECT:0:124",
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": False,
            "current_hp": 0,
            "potions": [{"id": "EnergyPotion", "slot": 0}],
        }

        report = strategy_audit.audit_records([terminal], "new")

        self.assertNotIn(
            "death_with_actionable_potion_retained",
            report["issue_counts"],
        )

    def test_victory_and_fairy_only_terminal_belts_are_not_potion_deaths(self):
        base = {
            "schema_version": 2,
            "decision_hash": "new",
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "current_hp": 0,
            "potions": [{"id": "FairyPotion", "slot": 0}],
        }
        fairy_death = dict(base, attempt_id="fairy-only", victory=False)
        victory = dict(
            base,
            attempt_id="victory",
            victory=True,
            potions=[{"id": "WeakPotion", "slot": 0}],
        )

        report = strategy_audit.audit_records(
            [fairy_death, victory], "new"
        )

        self.assertNotIn(
            "death_with_actionable_potion_retained",
            report["issue_counts"],
        )

    def test_debuff_potion_blocked_by_artifact_is_not_a_rescue_finding(self):
        attempt_id = "artifact-blocked-potion"
        final_decision = decision(
            attempt_id=attempt_id,
            action="end",
            hp_before=13,
            projected_hp_loss_before=16,
            projected_attack_hp_loss_before=16,
            projected_end_turn_hp_loss_before=0,
            projected_next_turn_start_hp_loss_before=0,
            player_before={
                "current_hp": 13, "max_hp": 70, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
            potions_before=[{
                "id": "WeakPotion", "slot": 0, "can_use": True,
            }],
            monsters_before=[{
                "enemy_instance_id": "enemy:boss",
                "id": "BronzeAutomaton",
                "current_hp": 88,
                "block": 0,
                "intent": "ATTACK",
                "move_adjusted_damage": 16,
                "move_hits": 1,
                "is_gone": False,
                "half_dead": False,
                "powers": [{"id": "Artifact", "amount": 1}],
            }],
        )
        terminal = {
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": attempt_id,
            "run_id": "IRONCLAD:0:123",
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": False,
            "current_hp": 0,
            "act": 2,
            "floor": 33,
            "class": "IRONCLAD",
            "potions": [{"id": "WeakPotion", "slot": 0}],
        }

        report = strategy_audit.audit_records(
            [final_decision, terminal], "new"
        )

        self.assertNotIn(
            "death_with_actionable_potion_retained",
            report["issue_counts"],
        )

        final_decision["monsters_before"][0]["powers"] = []
        report = strategy_audit.audit_records(
            [final_decision, terminal], "new"
        )
        self.assertEqual(
            1,
            report["issue_counts"][
                "death_with_actionable_potion_retained"
            ],
        )

    def test_only_current_schema_and_hash_are_audited(self):
        report = strategy_audit.audit_records([
            decision(),
            decision(decision_hash="old"),
            decision(decision_schema_version=1),
        ], "new")
        self.assertEqual(1, report["records"])

    def test_end_turn_underprediction_and_playable_block_are_flagged(self):
        report = strategy_audit.audit_records([
            decision(hand_before=[{
                "id": "Defend", "is_playable": True,
                "block": 5, "base_block": 5, "cost": 1,
            }]),
        ], "new")
        self.assertEqual(1, report["issue_counts"]["end_turn_damage_underprediction"])
        self.assertEqual(1, report["issue_counts"]["avoidable_loss_end_turn_candidate"])
        self.assertEqual("attempt-a", report["issues"][0]["attempt_id"])
        self.assertEqual("DEFECT:0:123", report["issues"][0]["run_id"])
        self.assertEqual(123, report["issues"][0]["seed"])

    def test_turn_loss_components_must_equal_projected_total(self):
        report = strategy_audit.audit_records([
            decision(
                projected_hp_loss_before=4,
                projected_attack_hp_loss_before=3,
                projected_end_turn_hp_loss_before=0,
                projected_next_turn_start_hp_loss_before=0,
                decision_outcome={"hp_delta": -3},
            ),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["turn_loss_component_mismatch"]
        )

    def test_stacked_brutality_underprediction_is_not_hidden_by_tolerance(self):
        report = strategy_audit.audit_records([
            decision(
                hp_before=10,
                projected_hp_loss_before=4,
                projected_attack_hp_loss_before=4,
                projected_end_turn_hp_loss_before=0,
                projected_next_turn_start_hp_loss_before=0,
                decision_outcome={"hp_delta": -7},
                player_before={
                    "current_hp": 10, "max_hp": 70, "block": 0,
                    "energy": 0,
                    "powers": [{"id": "Brutality", "amount": 3}],
                    "orbs": [],
                },
            ),
        ], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "next_turn_start_damage_underprediction"
            ],
        )
        self.assertEqual(
            1, report["issue_counts"]["end_turn_damage_underprediction"]
        )
        start_issue = next(
            issue for issue in report["issues"]
            if issue["kind"] == "next_turn_start_damage_underprediction"
        )
        self.assertEqual(3, start_issue["expected"])

    def test_brutality_total_is_clear_and_true_end_may_suppress_it(self):
        brutality_player = {
            "current_hp": 10, "max_hp": 70, "block": 0, "energy": 0,
            "powers": [{"id": "Brutality", "amount": 3}], "orbs": [],
        }
        survived = decision(
            hp_before=10,
            projected_hp_loss_before=7,
            projected_attack_hp_loss_before=4,
            projected_end_turn_hp_loss_before=0,
            projected_next_turn_start_hp_loss_before=3,
            decision_outcome={"hp_delta": -7},
            player_before=brutality_player,
        )
        terminal = decision(
            before_seq=11,
            hp_before=10,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            projected_next_turn_start_hp_loss_before=0,
            decision={"search": {"true_combat_end": True}},
            decision_outcome={"hp_delta": 0},
            player_before=brutality_player,
        )

        report = strategy_audit.audit_records([survived, terminal], "new")

        self.assertNotIn(
            "next_turn_start_damage_underprediction", report["issue_counts"]
        )
        self.assertNotIn(
            "turn_loss_component_mismatch", report["issue_counts"]
        )

    def test_brutality_audit_applies_tungsten_once_to_stacked_event(self):
        report = strategy_audit.audit_records([
            decision(
                hp_before=10,
                projected_hp_loss_before=6,
                projected_attack_hp_loss_before=4,
                projected_end_turn_hp_loss_before=0,
                projected_next_turn_start_hp_loss_before=2,
                decision_outcome={"hp_delta": -6},
                player_before={
                    "current_hp": 10, "max_hp": 70, "block": 0,
                    "energy": 0,
                    "powers": [{"id": "Brutality", "amount": 3}],
                    "orbs": [],
                },
                relic_ids_before=["Tungsten Rod"],
            ),
        ], "new")

        self.assertNotIn(
            "next_turn_start_damage_underprediction", report["issue_counts"]
        )

    def test_end_turn_only_damage_does_not_treat_block_as_mitigation(self):
        report = strategy_audit.audit_records([
            decision(
                projected_attack_hp_loss_before=0,
                hand_before=[
                    {
                        "id": "Flame Barrier", "is_playable": True,
                        "block": 12, "base_block": 12, "cost": 2,
                    }
                ],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_orichalcum_is_not_replaced_by_smaller_defend(self):
        report = strategy_audit.audit_records([
            decision(
                projected_hp_loss_before=7,
                projected_attack_hp_loss_before=7,
                projected_block_before=6,
                player_block_before=0,
                orichalcum_active_before=True,
                energy_before=1,
                hand_before=[
                    {
                        "id": "Defend_G", "is_playable": True,
                        "block": 3, "base_block": 5, "cost": 1,
                    }
                ],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_orichalcum_six_block_card_is_not_false_avoidable_mitigation(self):
        """A six-block card only replaces Orichalcum's automatic six block."""

        report = strategy_audit.audit_records([
            decision(
                projected_hp_loss_before=15,
                projected_attack_hp_loss_before=15,
                projected_block_before=0,
                player_block_before=0,
                orichalcum_active_before=True,
                energy_before=0,
                hand_before=[{
                    "id": "Steam", "is_playable": True,
                    "block": 6, "base_block": 6, "cost": 0,
                }],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_real_block_and_affordable_defend_stack_for_audit(self):
        report = strategy_audit.audit_records([
            decision(
                projected_hp_loss_before=5,
                projected_attack_hp_loss_before=5,
                projected_block_before=4,
                player_block_before=4,
                energy_before=1,
                hand_before=[
                    {
                        "id": "Defend_G", "is_playable": True,
                        "block": 3, "base_block": 5, "cost": 1,
                    }
                ],
            ),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["avoidable_loss_end_turn_candidate"]
        )

    def test_dynamic_block_on_non_block_card_is_ignored(self):
        report = strategy_audit.audit_records([
            decision(
                projected_hp_loss_before=5,
                projected_attack_hp_loss_before=5,
                hand_before=[{
                    "id": "Acrobatics", "is_playable": True,
                    "block": 5, "base_block": -1, "cost": 1,
                }],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_mitigation_that_remains_lethal_is_not_called_avoidable(self):
        report = strategy_audit.audit_records([
            decision(
                hp_before=5,
                projected_hp_loss_before=12,
                projected_attack_hp_loss_before=12,
                hand_before=[{
                    "id": "Defend", "is_playable": True,
                    "block": 5, "base_block": 5, "cost": 1,
                }],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_second_wind_alone_is_not_mistaken_for_fixed_block(self):
        """Replay seq 117556: its displayed seven is per other card."""

        report = strategy_audit.audit_records([
            decision(
                hp_before=74,
                energy_before=1,
                projected_hp_loss_before=3,
                projected_attack_hp_loss_before=3,
                projected_block_before=13,
                hand_before=[{
                    "id": "Second Wind", "type": "SKILL",
                    "is_playable": True, "block": 7, "base_block": 7,
                    "cost": 1,
                }],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )
        self.assertNotIn(
            "end_turn_with_resources", report["issue_counts"]
        )

    def test_second_wind_counts_each_other_nonattack_for_audit(self):
        report = strategy_audit.audit_records([
            decision(
                hp_before=20,
                energy_before=1,
                projected_hp_loss_before=10,
                projected_attack_hp_loss_before=10,
                projected_block_before=0,
                hand_before=[
                    {
                        "id": "Second Wind", "type": "SKILL",
                        "is_playable": True, "block": 7,
                        "base_block": 7, "cost": 1,
                    },
                    {
                        "id": "Wound", "type": "STATUS",
                        "is_playable": False, "block": 0,
                        "base_block": -1, "cost": -2,
                    },
                ],
            ),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["avoidable_loss_end_turn_candidate"]
        )

    def test_time_eater_twelfth_card_block_is_not_false_avoidable_loss(self):
        """The final Time Warp card adds +2 Strength to all three hits."""

        report = strategy_audit.audit_records([
            decision(
                hp_before=33,
                energy_before=3,
                projected_hp_loss_before=30,
                projected_attack_hp_loss_before=30,
                projected_block_before=3,
                hand_before=[
                    {
                        "id": "Survivor", "is_playable": True,
                        "block": 4, "base_block": 8, "cost": 1,
                    },
                    {
                        "id": "Dash", "is_playable": True,
                        "block": 6, "base_block": 10, "cost": 2,
                    },
                ],
                monsters_before=[{
                    "enemy_instance_id": "enemy:time", "id": "TimeEater",
                    "move_hits": 3,
                    "powers": [
                        {"id": "Time Warp", "amount": 11},
                    ],
                }],
            ),
        ], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )

    def test_time_eater_twelfth_card_with_excess_block_is_audited(self):
        """Block above the forced +2-per-hit still prevents real HP loss."""

        report = strategy_audit.audit_records([
            decision(
                hp_before=33,
                energy_before=2,
                projected_hp_loss_before=30,
                projected_attack_hp_loss_before=30,
                projected_block_before=3,
                hand_before=[{
                    "id": "Power Through", "is_playable": True,
                    "block": 15, "base_block": 15, "cost": 2,
                }],
                monsters_before=[{
                    "enemy_instance_id": "enemy:time", "id": "TimeEater",
                    "move_hits": 3,
                    "powers": [
                        {"id": "Time Warp", "amount": 11},
                    ],
                }],
            ),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["avoidable_loss_end_turn_candidate"]
        )

    def test_time_eater_safe_counter_reset_is_audited(self):
        """Replay seq 117777: carrying eleven creates a one-card turn."""

        report = strategy_audit.audit_records([
            decision(
                hp_before=68,
                energy_before=1,
                projected_hp_loss_before=14,
                projected_attack_hp_loss_before=14,
                projected_block_before=10,
                hand_before=[{
                    "id": "Strike_R", "type": "ATTACK",
                    "is_playable": True, "damage": 9, "block": 0,
                    "cost": 1,
                }],
                monsters_before=[{
                    "enemy_instance_id": "enemy:time", "id": "TimeEater",
                    "current_hp": 395, "max_hp": 456, "block": 0,
                    "move_hits": 1,
                    "powers": [{"id": "Time Warp", "amount": 11}],
                }],
            ),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["time_eater_safe_reset_missed"]
        )

    def test_time_eater_lethal_counter_reset_is_not_audited(self):
        """Replay seq 117795: ending survives, forced reset does not."""

        report = strategy_audit.audit_records([
            decision(
                hp_before=31,
                energy_before=4,
                projected_hp_loss_before=31,
                projected_attack_hp_loss_before=31,
                projected_block_before=8,
                hand_before=[{
                    "id": "Pommel Strike", "type": "ATTACK",
                    "is_playable": True, "damage": 9, "block": 0,
                    "cost": 1,
                }],
                monsters_before=[{
                    "enemy_instance_id": "enemy:time", "id": "TimeEater",
                    "current_hp": 302, "max_hp": 456, "block": 20,
                    "move_hits": 1,
                    "powers": [{"id": "Time Warp", "amount": 11}],
                }],
            ),
        ], "new")

        self.assertNotIn(
            "time_eater_safe_reset_missed", report["issue_counts"]
        )

    def test_legacy_player_hp_before_remains_supported(self):
        record = decision(
            hp_before=None,
            player_hp_before=5,
            projected_hp_loss_before=12,
            projected_attack_hp_loss_before=12,
            hand_before=[{
                "id": "Defend", "is_playable": True,
                "block": 5, "base_block": 5, "cost": 1,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )
        self.assertEqual(
            "clear",
            report["audit_coverage"][
                "avoidable_loss_end_turn_candidate"
            ]["status"],
        )

    def test_missing_hp_is_inconclusive_instead_of_accusing_policy(self):
        record = decision(
            hp_before=None,
            projected_hp_loss_before=12,
            projected_attack_hp_loss_before=12,
            hand_before=[{
                "id": "Defend", "is_playable": True,
                "block": 5, "base_block": 5, "cost": 1,
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "avoidable_loss_end_turn_candidate", report["issue_counts"]
        )
        coverage = report["audit_coverage"][
            "avoidable_loss_end_turn_candidate"
        ]
        self.assertEqual(1, coverage["eligible"])
        self.assertEqual(0, coverage["evaluated"])
        self.assertEqual(1, coverage["unknown"])
        self.assertEqual("inconclusive", coverage["status"])
        self.assertEqual("inconclusive", report["audit_status"])

    def test_missing_combat_trace_context_is_reported_unknown(self):
        report = strategy_audit.audit_records([
            decision(
                trace_schema_version=None,
                turn=None,
                combat_id=None,
                hand_before=None,
            ),
        ], "new")

        coverage = report["audit_coverage"]["combat_decision_context"]
        self.assertEqual(1, coverage["eligible"])
        self.assertEqual(0, coverage["evaluated"])
        self.assertEqual(1, coverage["unknown"])
        self.assertEqual("inconclusive", coverage["status"])

    def test_combat_trace_rejects_old_or_forged_oracle_version(self):
        for observed in ("independent-oracle-v1", "producer-forged", None):
            with self.subTest(observed=observed):
                report = strategy_audit.audit_records([
                    decision(audit_oracle_version=observed),
                ], "new")

                self.assertEqual("issues", report["audit_status"])
                self.assertEqual(
                    1,
                    report["issue_counts"][
                        "combat_audit_oracle_version_mismatch"
                    ],
                )
                self.assertEqual(
                    "issues",
                    report["audit_coverage"][
                        "combat_decision_context"
                    ]["status"],
                )

    def test_changed_combat_id_in_same_combat_is_an_issue(self):
        report = strategy_audit.audit_records([
            decision(before_seq=10, turn=1, combat_id="combat:first"),
            decision(before_seq=11, turn=1, combat_id="combat:changed"),
        ], "new")

        self.assertEqual(
            1, report["issue_counts"]["combat_trace_identity_changed"]
        )
        self.assertEqual("issues", report["audit_status"])
        self.assertEqual(
            "issues",
            report["audit_coverage"]["combat_decision_context"]["status"],
        )

    def test_same_player_turn_allows_transient_turn_counter_reset(self):
        """A lower serialized turn is not a new turn without an energy refill."""

        report = strategy_audit.audit_records([
            decision(
                before_seq=10,
                phase="COMBAT_TURN_11",
                action="play",
                turn=11,
                energy_before=2,
                player_before={
                    "current_hp": 41, "max_hp": 70, "block": 0,
                    "energy": 2, "powers": [], "orbs": [],
                },
            ),
            decision(
                before_seq=11,
                phase="COMBAT_TURN_1",
                action="play",
                turn=1,
                energy_before=1,
                player_before={
                    "current_hp": 41, "max_hp": 70, "block": 0,
                    "energy": 1, "powers": [], "orbs": [],
                },
            ),
        ], "new")

        self.assertNotIn(
            "combat_trace_turn_regressed", report["issue_counts"]
        )
        self.assertEqual(
            0,
            report["audit_coverage"]["combat_decision_context"][
                "violations"
            ],
        )

    def test_reward_potion_discard_resets_combat_scope(self):
        reward_discard = decision(
            before_seq=11,
            phase="COMBAT_REWARD",
            action="potion",
            potion_instance_id="potion:discarded",
            potion_operation="discard",
        )
        for field in (
            "turn",
            "combat_id",
            "audit_oracle_version",
            "trace_capabilities",
            "hp_before",
            "energy_before",
            "projected_hp_loss_before",
            "projected_attack_hp_loss_before",
            "projected_end_turn_hp_loss_before",
            "projected_next_turn_start_hp_loss_before",
            "projected_block_before",
            "player_block_before",
            "orichalcum_active_before",
            "hand_before",
            "player_before",
            "monsters_before",
            "potions_before",
            "relic_ids_before",
        ):
            reward_discard.pop(field, None)

        report = strategy_audit.audit_records([
            decision(
                before_seq=10,
                combat_id="combat:first",
                decision_outcome={"hp_delta": -3},
            ),
            reward_discard,
            decision(
                before_seq=12,
                combat_id="combat:second",
                decision_outcome={"hp_delta": -3},
            ),
        ], "new")

        coverage = report["audit_coverage"]["combat_decision_context"]
        self.assertEqual(2, coverage["eligible"])
        self.assertEqual(2, coverage["evaluated"])
        self.assertEqual(0, coverage["unknown"])
        self.assertNotIn(
            "combat_trace_identity_changed", report["issue_counts"]
        )

    def test_play_trace_missing_action_identity_is_inconclusive(self):
        report = strategy_audit.audit_records([
            decision(action="play", card_instance_id=None, card_id=None),
        ], "new")

        coverage = report["audit_coverage"]["combat_decision_context"]
        self.assertEqual(1, coverage["unknown"])
        self.assertEqual("inconclusive", report["audit_status"])

    def test_potion_trace_missing_action_identity_is_inconclusive(self):
        report = strategy_audit.audit_records([
            decision(
                action="potion", potion_instance_id=None,
                potion_operation=None,
            ),
        ], "new")

        coverage = report["audit_coverage"]["combat_decision_context"]
        self.assertEqual(1, coverage["unknown"])
        self.assertEqual("inconclusive", report["audit_status"])

    def test_empty_nested_combat_snapshots_are_inconclusive(self):
        report = strategy_audit.audit_records([
            decision(
                player_before={},
                monsters_before=[{}],
                potions_before=[{}],
                decision_outcome={"hp_delta": -3},
            ),
        ], "new")

        coverage = report["audit_coverage"]["combat_decision_context"]
        self.assertEqual(0, coverage["evaluated"])
        self.assertEqual(1, coverage["unknown"])
        self.assertEqual("inconclusive", report["audit_status"])

    def test_combat_trace_context_is_stable_and_compact(self):
        first = {
            "class": "DEFECT",
            "ascension_level": 0,
            "seed": 123,
            "act": 1,
            "floor": 8,
            "room_type": "MonsterRoomElite",
            "combat_state": {
                "turn": 2,
                "hand": [{
                    "id": "Zap",
                    "name": "Zap",
                    "card_instance_id": "card:zap",
                    "cost": 0,
                    "is_playable": True,
                    "description": "not retained in compact trace",
                }],
                "monsters": [{
                    "enemy_instance_id": "enemy:nob",
                    "id": "GremlinNob",
                    "current_hp": 82,
                    "max_hp": 90,
                }],
            },
        }
        later = {
            **first,
            "current_hp": 40,
            "combat_state": {
                "turn": 3,
                "hand": [],
                "monsters": [{
                    "enemy_instance_id": "enemy:nob",
                    "id": "GremlinNob",
                    "current_hp": 30,
                    "max_hp": 90,
                }],
            },
        }

        first_context = autoplay.combat_trace_context(
            first, "COMBAT_TURN_2", "attempt-a"
        )
        later_context = autoplay.combat_trace_context(
            later, "COMBAT_TURN_3", "attempt-a"
        )

        self.assertEqual(
            first_context["combat_id"], later_context["combat_id"]
        )
        self.assertEqual(2, first_context["turn"])
        self.assertEqual("card:zap", first_context["hand_before"][0][
            "card_instance_id"
        ])
        self.assertNotIn("description", first_context["hand_before"][0])

    def test_combat_trace_id_survives_slime_boss_splits(self):
        before_split = {
            "class": "IRONCLAD",
            "ascension_level": 0,
            "seed": 123,
            "act": 1,
            "floor": 16,
            "room_type": "MonsterRoomBoss",
            "combat_state": {
                "turn": 3,
                "monsters": [{
                    "id": "SlimeBoss",
                    "enemy_instance_id": "enemy:boss",
                    "current_hp": 56,
                    "max_hp": 140,
                }],
            },
        }
        after_split = {
            **before_split,
            "combat_state": {
                "turn": 4,
                "monsters": [
                    {
                        "id": "SpikeSlime_L",
                        "enemy_instance_id": "enemy:spike",
                        "current_hp": 56,
                        "max_hp": 56,
                    },
                    {
                        "id": "SlimeBoss",
                        "enemy_instance_id": "enemy:boss",
                        "current_hp": 0,
                        "max_hp": 140,
                        "is_gone": True,
                    },
                    {
                        "id": "AcidSlime_L",
                        "enemy_instance_id": "enemy:acid",
                        "current_hp": 56,
                        "max_hp": 56,
                    },
                ],
            },
        }

        before_context = autoplay.combat_trace_context(
            before_split, "COMBAT_TURN_3", "attempt-a"
        )
        after_context = autoplay.combat_trace_context(
            after_split, "COMBAT_TURN_4", "attempt-a"
        )

        self.assertEqual(
            before_context["combat_id"], after_context["combat_id"]
        )

    def test_combat_trace_id_changes_for_a_different_room(self):
        game = {
            "class": "IRONCLAD",
            "ascension_level": 0,
            "seed": 123,
            "act": 1,
            "floor": 8,
            "room_type": "MonsterRoom",
            "combat_state": {"turn": 1, "monsters": []},
        }
        next_room = {**game, "floor": 9}

        first_context = autoplay.combat_trace_context(
            game, "COMBAT_TURN_1", "attempt-a"
        )
        next_context = autoplay.combat_trace_context(
            next_room, "COMBAT_TURN_1", "attempt-a"
        )

        self.assertNotEqual(
            first_context["combat_id"], next_context["combat_id"]
        )

    def test_affordable_split_interrupt_miss_is_flagged(self):
        boss = {
            "enemy_instance_id": "enemy:slime", "id": "SlimeBoss",
            "monster_index": 0, "current_hp": 88, "max_hp": 140,
            "block": 0, "intent": "ATTACK", "move_adjusted_damage": 35,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [{"id": "Split", "name": "Split", "amount": -1}],
        }
        hand = [
            {
                "card_instance_id": "choke", "id": "Choke",
                "type": "ATTACK", "cost": 2, "is_playable": True,
                "has_target": True, "damage": 12, "upgrades": 0,
            },
            {
                "card_instance_id": "spray", "id": "Dagger Spray",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "has_target": False, "damage": 4, "upgrades": 0,
            },
        ]
        record = decision(
            action="play",
            card_instance_id="wail",
            card_id="PiercingWail",
            energy_before=3,
            hand_before=hand,
            monsters_before=[boss],
            decision={
                "search": {
                    "final_enemy_hp": [74],
                    "action_suppressed_enemy_indexes": [],
                }
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["dominated_split_interrupt_missed"]
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"]["split_interrupt_opportunity"]["status"],
        )

    def test_affordable_split_interrupt_is_clear_when_action_is_suppressed(self):
        boss = {
            "enemy_instance_id": "enemy:slime", "id": "SlimeBoss",
            "monster_index": 0, "current_hp": 88, "max_hp": 140,
            "block": 0, "intent": "ATTACK", "move_adjusted_damage": 35,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [],
        }
        hand = [
            {
                "card_instance_id": "choke", "id": "Choke",
                "type": "ATTACK", "cost": 2, "is_playable": True,
                "has_target": True, "damage": 12, "upgrades": 0,
            },
            {
                "card_instance_id": "spray", "id": "Dagger Spray",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "has_target": False, "damage": 4, "upgrades": 0,
            },
        ]
        record = decision(
            action="play",
            card_instance_id="choke",
            card_id="Choke",
            energy_before=3,
            hand_before=hand,
            monsters_before=[boss],
            decision={
                "search": {
                    "final_enemy_hp": [65],
                    "action_suppressed_enemy_indexes": [0],
                }
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "dominated_split_interrupt_missed", report["issue_counts"]
        )
        self.assertEqual(
            "clear",
            report["audit_coverage"]["split_interrupt_opportunity"]["status"],
        )

    def test_affordable_split_interrupt_is_clear_when_the_enemy_is_killed(self):
        slime = {
            "enemy_instance_id": "enemy:acid", "id": "AcidSlime_L",
            "monster_index": 0, "current_hp": 11, "max_hp": 21,
            "block": 0, "intent": "ATTACK", "move_adjusted_damage": 16,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [{"id": "Split", "name": "Split", "amount": -1}],
        }
        hand = [
            {
                "card_instance_id": "strike-a", "id": "Strike_R",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "has_target": True, "damage": 4, "upgrades": 0,
            },
            {
                "card_instance_id": "strike-b", "id": "Strike_R",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "has_target": True, "damage": 4, "upgrades": 0,
            },
            {
                "card_instance_id": "hemo", "id": "Hemokinesis",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "has_target": True, "damage": 11, "upgrades": 0,
            },
        ]
        record = decision(
            action="play",
            card_instance_id="strike-a",
            card_id="Strike_R",
            energy_before=3,
            hand_before=hand,
            monsters_before=[slime],
            decision={
                "search": {
                    "final_enemy_hp": [0],
                    "action_suppressed_enemy_indexes": [],
                }
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "dominated_split_interrupt_missed", report["issue_counts"]
        )
        self.assertEqual(
            "clear",
            report["audit_coverage"]["split_interrupt_opportunity"]["status"],
        )

    def test_compact_action_adds_context_to_play_potion_and_end(self):
        card = {
            "id": "Zap",
            "card_instance_id": "card:zap",
            "cost": 0,
            "is_playable": True,
        }
        monster = {
            "enemy_instance_id": "enemy:nob",
            "id": "GremlinNob",
            "current_hp": 82,
            "max_hp": 90,
            "block": 0,
            "powers": [],
        }
        game = {
            "class": "DEFECT",
            "ascension_level": 0,
            "seed": 123,
            "act": 1,
            "floor": 8,
            "room_type": "MonsterRoomElite",
            "room_phase": "COMBAT",
            "current_hp": 50,
            "gold": 100,
            "relics": [],
            "potions": [{
                "id": "Block Potion",
                "potion_instance_id": "potion:block",
            }],
            "combat_state": {
                "turn": 4,
                "player": {"energy": 3, "block": 0},
                "hand": [card],
                "monsters": [monster],
            },
        }
        state = {
            "state_seq": 10,
            "phase": "COMBAT_TURN_4",
            "available_commands": [],
            "game_state": game,
        }
        after = {
            "state_seq": 11,
            "phase": "COMBAT_TURN_4",
            "available_commands": [],
            "game_state": game,
        }
        payloads = [
            {"action": "play", "card_instance_id": "card:zap"},
            {
                "action": "potion",
                "potion_instance_id": "potion:block",
                "operation": "use",
            },
            {"action": "end"},
        ]
        predictors = (
            "incoming_damage",
            "projected_player_block",
            "projected_attack_hp_loss",
            "player_end_turn_hp_loss",
            "projected_turn_hp_loss",
        )

        with patch.object(
            autoplay.Game, "from_json", return_value=object()
        ), patch.object(
            autoplay, "projected_doomed_enemy_ids", return_value=[]
        ):
            patches = [
                patch.object(autoplay.combat_predictor, name, return_value=0)
                for name in predictors
            ]
            for item in patches:
                item.start()
            try:
                records = [
                    autoplay.compact_action(
                        state,
                        payload,
                        {},
                        after,
                        run_context={"attempt_id": "attempt-a"},
                    )
                    for payload in payloads
                ]
            finally:
                for item in reversed(patches):
                    item.stop()

        self.assertEqual(
            {autoplay.TRACE_SCHEMA_VERSION},
            {record["trace_schema_version"] for record in records},
        )
        self.assertEqual({4}, {record["turn"] for record in records})
        self.assertEqual(1, len({record["combat_id"] for record in records}))
        self.assertTrue(all(record["hand_before"] for record in records))
        self.assertTrue(all(record["player_before"] for record in records))
        self.assertTrue(all(record["monsters_before"] for record in records))
        self.assertTrue(all(
            "post_action_damage" in record["trace_capabilities"]
            for record in records
        ))
        self.assertTrue(all(
            record["decision_outcome"]["enemy_hp_loss"] == 0
            for record in records
        ))
        self.assertTrue(all(
            record["audit_oracle_version"] == autoplay.AUDIT_ORACLE_VERSION
            for record in records
        ))
        self.assertEqual(
            "independent-oracle-v2", autoplay.AUDIT_ORACLE_VERSION
        )

    def test_redundant_doomed_target_has_explicit_exception_registry(self):
        ordinary = decision(
            action="play",
            enemy_instance_id="enemy-1",
            doomed_enemy_ids_before=["enemy-1"],
            card_id="Strike_G",
        )
        corpse = dict(ordinary, before_seq=11, card_id="Corpse Explosion")
        report = strategy_audit.audit_records([ordinary, corpse], "new")
        self.assertEqual(1, report["issue_counts"]["redundant_doomed_target"])

    def test_doomed_attacking_target_can_be_killed_to_prevent_damage(self):
        record = decision(
            action="play",
            enemy_instance_id="enemy:test",
            doomed_enemy_ids_before=["enemy:test"],
            card_id="Poisoned Stab",
            decision={"card_damage": 8},
            monsters_before=[{
                "enemy_instance_id": "enemy:test",
                "id": "Darkling",
                "current_hp": 4,
                "block": 0,
                "intent": "ATTACK",
                "move_adjusted_damage": 6,
            }],
        )
        report = strategy_audit.audit_records([record], "new")
        self.assertNotIn("redundant_doomed_target", report["issue_counts"])

    def test_doomed_attacking_target_is_still_flagged_when_hit_is_nonlethal(self):
        record = decision(
            action="play",
            enemy_instance_id="enemy:test",
            doomed_enemy_ids_before=["enemy:test"],
            card_id="Poisoned Stab",
            decision={"card_damage": 3},
            monsters_before=[{
                "enemy_instance_id": "enemy:test",
                "id": "Darkling",
                "current_hp": 4,
                "block": 0,
                "intent": "ATTACK",
                "move_adjusted_damage": 6,
            }],
        )
        report = strategy_audit.audit_records([record], "new")
        self.assertEqual(1, report["issue_counts"]["redundant_doomed_target"])

    def test_attempt_filter_prevents_cross_run_findings(self):
        first = decision(attempt_id="attempt-a", run_id="DEFECT:0:1", seed=1)
        second = decision(attempt_id="attempt-b", run_id="DEFECT:0:2", seed=2)

        report = strategy_audit.audit_records(
            [first, second], "new", attempt_id="attempt-b"
        )

        self.assertEqual("attempt-b", report["attempt_id"])
        self.assertEqual(1, report["records"])
        self.assertEqual({"attempt-b"}, {item["attempt_id"] for item in report["issues"]})

    def test_zero_loss_liquid_memories_without_marginal_gain_is_flagged(self):
        record = decision(
            action="potion",
            potion={"id": "LiquidMemories"},
            potion_instance_id="potion-1",
            decision={
                "potion_id": "LiquidMemories",
                "planned_turn_hp_loss": 0,
                "liquid_memories_marginal_kills": 0,
                "liquid_memories_critical_setup": False,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(1, report["issue_counts"]["liquid_memories_zero_loss_use"])

    def test_zero_loss_liquid_memories_with_confirmed_kill_is_not_flagged(self):
        record = decision(
            action="potion",
            potion={"id": "LiquidMemories"},
            potion_instance_id="potion-1",
            decision={
                "potion_id": "LiquidMemories",
                "planned_turn_hp_loss": 0,
                "liquid_memories_marginal_kills": 1,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn("liquid_memories_zero_loss_use", report["issue_counts"])

    def test_leaving_ghosts_before_five_cards_arrive_is_flagged(self):
        context = {"deck_counts": {"Strike_B+0": 4, "Defend_B+0": 4}}
        accept = decision(
            action="choose",
            phase="EVENT",
            before_seq=20,
            decision_context=context,
            chosen_option_before={
                "target": {
                    "kind": "event_option",
                    "event_id": "Ghosts",
                    "label": "接受",
                    "text": "[接受] 获得5张 灵体 牌。失去最大生命。",
                },
            },
        )
        leave = decision(
            action="choose",
            phase="EVENT",
            before_seq=21,
            decision_context=context,
            chosen_option_before={
                "target": {
                    "kind": "event_option",
                    "event_id": "Ghosts",
                    "label": "离开",
                    "text": "[离开]",
                },
            },
        )

        report = strategy_audit.audit_records([accept, leave], "new")

        self.assertEqual(1, report["issue_counts"]["event_reward_left_unsettled"])

    def test_event_card_grant_clears_after_expected_deck_growth(self):
        accept = decision(
            action="choose",
            phase="EVENT",
            before_seq=20,
            decision_context={"deck_counts": {"Strike_B+0": 4}},
            chosen_option_before={
                "target": {
                    "event_id": "Ghosts",
                    "label": "Accept",
                    "text": "Obtain 5 cards.",
                },
            },
        )
        settled = decision(
            action="state",
            phase="EVENT",
            before_seq=21,
            decision_context={
                "deck_counts": {"Strike_B+0": 4, "Apparition+0": 5},
            },
            chosen_option_before={},
        )
        leave = decision(
            action="choose",
            phase="EVENT",
            before_seq=22,
            decision_context=settled["decision_context"],
            chosen_option_before={
                "target": {
                    "event_id": "Ghosts",
                    "label": "Leave",
                    "text": "Leave",
                },
            },
        )

        report = strategy_audit.audit_records([accept, settled, leave], "new")

        self.assertNotIn("event_reward_left_unsettled", report["issue_counts"])

    def test_vampires_replacement_tracks_bites_not_net_deck_growth(self):
        accept = decision(
            action="choose",
            phase="EVENT",
            before_seq=30,
            decision_context={
                "deck_counts": {"Strike_R+0": 4, "Defend_R+0": 4}
            },
            chosen_option_before={
                "choice_index": 0,
                "target": {
                    "event_id": "Vampires",
                    "label": "Accept",
                    "text": "Remove all Strikes. Obtain 5 Bites.",
                    "card": {"id": "Bite"},
                },
            },
        )
        leave = decision(
            action="choose",
            phase="EVENT",
            before_seq=31,
            decision_context={
                "deck_counts": {"Defend_R+0": 4, "Bite+0": 5}
            },
            chosen_option_before={
                "target": {
                    "event_id": "Vampires",
                    "label": "Leave",
                    "text": "Leave",
                },
            },
        )

        report = strategy_audit.audit_records([accept, leave], "new")

        self.assertNotIn("event_reward_left_unsettled", report["issue_counts"])

    def test_vampires_refusal_at_index_one_does_not_expect_bites(self):
        context = {
            "deck_counts": {"Strike_R+0": 1, "Defend_R+0": 4}
        }
        refuse = decision(
            action="choose",
            phase="EVENT",
            before_seq=30,
            decision_context=context,
            chosen_option_before={
                "choice_index": 1,
                "target": {
                    "event_id": "Vampires",
                    "label": "拒绝",
                    "text": "[拒绝]",
                    "card": None,
                },
            },
        )
        leave = decision(
            action="choose",
            phase="EVENT",
            before_seq=31,
            decision_context=context,
            chosen_option_before={
                "choice_index": 0,
                "target": {
                    "event_id": "Vampires",
                    "label": "离开",
                    "text": "[离开]",
                    "card": None,
                },
            },
        )

        report = strategy_audit.audit_records([refuse, leave], "new")

        self.assertNotIn("event_reward_left_unsettled", report["issue_counts"])

    def test_vampires_accept_still_flags_missing_bites(self):
        context = {
            "deck_counts": {"Strike_R+0": 4, "Defend_R+0": 4}
        }
        accept = decision(
            action="choose",
            phase="EVENT",
            before_seq=40,
            decision_context=context,
            chosen_option_before={
                "choice_index": 0,
                "target": {
                    "event_id": "Vampires",
                    "label": "Accept",
                    "text": "Remove all Strikes. Obtain 5 Bites.",
                    "card": {"id": "Bite"},
                },
            },
        )
        leave = decision(
            action="choose",
            phase="EVENT",
            before_seq=41,
            decision_context=context,
            chosen_option_before={
                "choice_index": 0,
                "target": {
                    "event_id": "Vampires",
                    "label": "Leave",
                    "text": "Leave",
                    "card": None,
                },
            },
        )

        report = strategy_audit.audit_records([accept, leave], "new")

        self.assertEqual(1, report["issue_counts"]["event_reward_left_unsettled"])

    def test_same_turn_end_does_not_contradict_passive_combat_end(self):
        claimed = decision(
            action="play",
            before_seq=50,
            card_instance_id="strike-1",
            card_id="Strike_R",
            hand_before=[{
                "card_instance_id": "strike-1", "id": "Strike_R",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "base_block": -1, "block": 0, "damage": 6,
            }],
            decision={"search": {
                "true_combat_end": True, "final_enemy_hp": [0],
            }},
        )
        contradicted = decision(
            action="end",
            before_seq=51,
            energy_before=0,
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
        )

        report = strategy_audit.audit_records(
            [claimed, contradicted], "new"
        )

        self.assertEqual(
            0,
            report["issue_counts"].get(
                "false_combat_end_prediction", 0
            ),
        )

    def test_later_combat_turn_contradicts_true_combat_end(self):
        claimed = decision(
            action="play",
            before_seq=50,
            turn=2,
            card_instance_id="strike-1",
            card_id="Strike_R",
            hand_before=[{
                "card_instance_id": "strike-1", "id": "Strike_R",
                "type": "ATTACK", "cost": 1, "is_playable": True,
                "base_block": -1, "block": 0, "damage": 6,
            }],
            decision={"search": {
                "true_combat_end": True, "final_enemy_hp": [0],
            }},
        )
        contradicted = decision(
            action="play",
            before_seq=51,
            turn=3,
            energy_before=3,
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0,
                "energy": 3, "powers": [], "orbs": [],
            },
        )

        report = strategy_audit.audit_records(
            [claimed, contradicted], "new"
        )

        self.assertEqual(
            1, report["issue_counts"]["false_combat_end_prediction"]
        )

    def test_terminal_plan_without_orb_is_audited(self):
        claimed = decision(
            action="play",
            before_seq=50,
            card_instance_id="dualcast-1",
            card_id="Dualcast",
            hand_before=[{
                "card_instance_id": "dualcast-1", "id": "Dualcast",
                "type": "SKILL", "cost": 1, "is_playable": True,
            }],
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0,
                "energy": 1, "powers": [], "orbs": [],
            },
            decision={"search": {
                "true_combat_end": True,
                "action_true_combat_end_predicted": False,
                "planned_sequence": [{"card_id": "Dualcast"}],
            }},
        )

        report = strategy_audit.audit_records([claimed], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["terminal_plan_missing_orb_resource"],
        )

    def test_event_boundary_separates_same_room_multistage_combats(self):
        claimed = decision(
            action="play",
            before_seq=50,
            turn=3,
            card_instance_id="anger-1",
            card_id="Anger",
            hand_before=[{
                "card_instance_id": "anger-1", "id": "Anger",
                "type": "ATTACK", "cost": 0, "is_playable": True,
                "base_block": -1, "block": 0, "damage": 9,
            }],
            decision={"search": {
                "true_combat_end": True, "final_enemy_hp": [0],
            }},
        )
        interlude = decision(
            action="choose",
            phase="EVENT",
            before_seq=51,
            after_seq=52,
            combat_id=None,
            turn=None,
            chosen_option_before={
                "option_id": "colosseum:continue",
                "target": {"event_id": "Colosseum"},
            },
        )
        second_stage = decision(
            action="play",
            before_seq=52,
            turn=4,
            # Legacy traces used the same act/floor/room-derived identifier
            # for both Colosseum stages.
            combat_id="combat:test",
            monsters_before=[{
                "enemy_instance_id": "enemy:nob", "id": "GremlinNob",
                "monster_index": 0, "current_hp": 49, "max_hp": 85,
                "block": 0, "intent": "ATTACK",
                "move_adjusted_damage": 30, "move_hits": 1,
                "is_gone": False, "half_dead": False, "powers": [],
            }],
        )

        report = strategy_audit.audit_records(
            [claimed, interlude, second_stage], "new"
        )

        self.assertNotIn(
            "false_combat_end_prediction", report["issue_counts"]
        )

    def test_battle_trance_after_spending_all_energy_is_flagged(self):
        strike_card = {
            "card_instance_id": "strike-1", "id": "Strike_R",
            "type": "ATTACK", "cost": 1, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 6,
        }
        trance_card = {
            "card_instance_id": "trance-1", "id": "Battle Trance",
            "type": "SKILL", "cost": 0, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 0,
        }
        spend = decision(
            action="play",
            before_seq=60,
            energy_before=1,
            card_instance_id="strike-1",
            card_id="Strike_R",
            hand_before=[strike_card, trance_card],
        )
        late_draw = decision(
            action="play",
            before_seq=61,
            energy_before=0,
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
            card_instance_id="trance-1",
            card_id="Battle Trance",
            hand_before=[trance_card],
        )

        report = strategy_audit.audit_records([spend, late_draw], "new")

        self.assertEqual(1, report["issue_counts"]["late_zero_cost_draw"])

    def test_optional_gambling_that_discards_protected_draw_is_flagged(self):
        def option(instance_id, card_id, card_type="SKILL", block=0):
            return {
                "target": {"card": {
                    "card_instance_id": instance_id,
                    "id": card_id,
                    "type": card_type,
                    "block": block,
                }}
            }

        strike_option = option("strike-1", "Strike_G", "ATTACK")
        trance_option = option("trance-1", "Battle Trance")
        choose_strike = decision(
            phase="HAND_SELECT",
            action="choose",
            before_seq=70,
            decision={
                "selection_action": "GamblersBrewAction",
                "selection_semantics": "remove",
            },
            available_options_before=[strike_option, trance_option],
            chosen_option_before=strike_option,
        )
        choose_trance = decision(
            phase="HAND_SELECT",
            action="choose",
            before_seq=71,
            decision={
                "selection_action": "GamblersBrewAction",
                "selection_semantics": "remove",
            },
            available_options_before=[trance_option],
            chosen_option_before=trance_option,
        )
        resume = decision(before_seq=72, action="end")

        report = strategy_audit.audit_records(
            [choose_strike, choose_trance, resume], "new"
        )

        self.assertEqual(
            1, report["issue_counts"]["optional_hand_overselection"]
        )

    def test_hand_select_plan_veto_requires_bound_previous_combat_plan(self):
        def option(option_id, index, card_id, card_uuid):
            return {
                "option_id": option_id,
                "choice_index": index,
                "label": card_id,
                "target": {
                    "kind": "card",
                    "card_instance_id": card_uuid,
                    "card": {
                        "id": card_id,
                        "card_instance_id": card_uuid,
                    },
                },
            }

        true_grit = "true-grit"
        defend = "future-defend"
        strike = "discard-strike"
        source = decision(
            before_seq=100,
            after_seq=101,
            phase="COMBAT_TURN_3",
            action="play",
            turn=3,
            act=2,
            floor=18,
            requested_target_id=true_grit,
            resolved_target_id=true_grit,
            decision={
                "_combat_context": [9, 2, 18, 3],
                "planned_sequence": [
                    {"card_id": "True Grit", "card_uuid": true_grit},
                    {"card_id": "Defend_R", "card_uuid": defend},
                ],
            },
        )
        discard = option("option:discard", 0, "Strike_R", strike)
        preserve = option("option:preserve", 1, "Defend_R", defend)
        protection = {
            "schema_version": 1,
            "kind": "exact_combat_plan_card_preservation",
            "source_combat_context": [9, 2, 18, 3],
            "source_planned_sequence": [
                {"card_instance_id": true_grit, "card_id": "True Grit"},
                {"card_instance_id": defend, "card_id": "Defend_R"},
            ],
            "visible_card_instance_ids": sorted([strike, defend]),
            "protected_card_instance_ids": [defend],
            "selection_action": "ExhaustAction",
            "selection_semantics": "remove",
            "required_selection_count": 1,
        }
        hand = decision(
            before_seq=101,
            after_seq=102,
            phase="HAND_SELECT",
            action="choose",
            act=2,
            floor=18,
            requested_target_id="option:discard",
            resolved_target_id="option:discard",
            chosen_option_before=discard,
            available_options_before=[discard, preserve],
            decision={
                "selection_action": "ExhaustAction",
                "selection_semantics": "remove",
                "combat_plan_protection": protection,
                "candidates": [
                    {
                        "id": "option:discard",
                        "score": 1.0,
                        "selection_eligible": True,
                        "consequences": {},
                    },
                    {
                        "id": "option:preserve",
                        "score": 9.0,
                        "selection_eligible": False,
                        "veto_reason": (
                            "exact_combat_plan_card_preservation"
                        ),
                        "consequences": {},
                    },
                ],
            },
        )

        accepted = strategy_audit.audit_records([source, hand], "new")

        self.assertNotIn(
            "hand_select_plan_protection_unverified",
            accepted["issue_counts"],
        )
        self.assertNotIn(
            "noncombat_candidate_argmax_missed", accepted["issue_counts"],
        )

        tampered = copy.deepcopy(hand)
        tampered["decision"]["combat_plan_protection"][
            "source_planned_sequence"
        ][1]["card_instance_id"] = "forged-future-card"
        rejected = strategy_audit.audit_records([source, tampered], "new")

        self.assertEqual(
            1,
            rejected["issue_counts"][
                "hand_select_plan_protection_unverified"
            ],
        )
        self.assertEqual(
            1,
            rejected["issue_counts"]["noncombat_candidate_argmax_missed"],
        )

    def test_exact_potion_with_no_loss_reduction_is_flagged(self):
        record = decision(
            action="potion",
            potion={"id": "EnergyPotion"},
            potion_instance_id="potion-1",
            potion_operation="use",
            decision={
                "potion_id": "EnergyPotion",
                "planned_turn_hp_loss": 8,
                "potion_candidate_hp_loss": 8,
                "potion_candidate_combat_end": False,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["potion_without_marginal_gain"]
        )

    def test_second_unproven_random_potion_same_turn_is_flagged(self):
        first = decision(
            action="potion",
            before_seq=80,
            potion={"id": "GamblersBrew"},
            potion_instance_id="potion-1",
            potion_operation="use",
            decision={"potion_id": "GamblersBrew"},
        )
        second = decision(
            action="potion",
            before_seq=81,
            potion={"id": "AttackPotion"},
            potion_instance_id="potion-2",
            potion_operation="use",
            decision={"potion_id": "AttackPotion"},
        )

        report = strategy_audit.audit_records([first, second], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["repeated_unproven_random_potion"],
        )

    def test_rage_kept_in_initial_hand_but_played_after_attack_is_flagged(self):
        strike_card = {
            "card_instance_id": "strike-1", "id": "Strike_R",
            "type": "ATTACK", "cost": 1, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 6,
        }
        rage_card = {
            "card_instance_id": "rage-1", "id": "Rage",
            "type": "SKILL", "cost": 0, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 0,
        }
        attack = decision(
            action="play",
            before_seq=90,
            card_instance_id="strike-1",
            card_id="Strike_R",
            hand_before=[strike_card, rage_card],
        )
        late_rage = decision(
            action="play",
            before_seq=91,
            card_instance_id="rage-1",
            card_id="Rage",
            hand_before=[rage_card],
            projected_attack_hp_loss_before=8,
        )

        report = strategy_audit.audit_records([attack, late_rage], "new")

        self.assertEqual(1, report["issue_counts"]["late_attack_setup"])

    def test_offering_as_sixth_velvet_choker_card_is_flagged(self):
        def hand_card(instance_id, card_id, card_type="ATTACK", cost=1):
            return {
                "card_instance_id": instance_id,
                "id": card_id,
                "type": card_type,
                "cost": cost,
                "is_playable": True,
                "base_block": -1,
                "block": 0,
                "damage": 6 if card_type == "ATTACK" else 0,
            }

        records = []
        for index in range(5):
            card = hand_card(f"strike-{index}", "Strike_R")
            records.append(decision(
                action="play",
                before_seq=100 + index,
                card_instance_id=card["card_instance_id"],
                card_id=card["id"],
                hand_before=[card],
                relic_ids_before=["Velvet Choker"],
                projected_hp_loss_before=0,
            ))
        offering = hand_card("offering-1", "Offering", "SKILL", cost=0)
        records.append(decision(
            action="play",
            before_seq=105,
            card_instance_id="offering-1",
            card_id="Offering",
            hand_before=[offering],
            relic_ids_before=["Velvet Choker"],
            decision={
                "card_self_hp_cost": 6,
                "search": {"voluntary_self_hp_cost": 6},
            },
            projected_hp_loss_before=0,
        ))

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(
            1, report["issue_counts"]["offering_at_choker_limit"]
        )

    def test_selected_dominated_self_damage_plan_is_flagged(self):
        record = decision(
            action="play",
            card_id="Offering",
            card_instance_id="offering-1",
            hand_before=[{
                "id": "Offering", "card_instance_id": "offering-1",
                "type": "SKILL", "cost": 0, "is_playable": True,
            }],
            decision={
                "card_self_hp_cost": 6,
                "search": {
                    "voluntary_self_hp_cost": 6,
                    "voluntary_self_damage_comparison": {
                        "evaluated": True,
                        "dominated": True,
                        "status": "selected_dominated_plan",
                        "reason": "same_combat_end_less_hp_loss",
                    },
                },
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["avoidable_voluntary_self_damage"],
        )
        check = report["audit_coverage"][
            "voluntary_self_damage_dominance"
        ]
        self.assertEqual("issues", check["status"])
        self.assertEqual(1, check["evaluated"])

    def test_rejected_dominated_self_damage_plan_is_clear(self):
        record = decision(
            action="play",
            card_id="Defend_R",
            card_instance_id="defend-1",
            hand_before=[{
                "id": "Defend_R", "card_instance_id": "defend-1",
                "type": "SKILL", "cost": 1, "is_playable": True,
            }],
            decision={
                # Pain makes this ordinary Defend cost one HP.  The rejected
                # plan paid more Pain damage for the same outcome; this
                # record is the dominating replacement, not the defect.
                "card_self_hp_cost": 1,
                "search": {
                    "voluntary_self_hp_cost": 1,
                    "voluntary_self_damage_comparison": {
                        "evaluated": True,
                        "dominated": True,
                        "status": "rejected_dominated_plan",
                        "reason": "same_combat_end_less_hp_loss",
                        "chosen_plan": {"voluntary_self_hp_cost": 2},
                        "rejected_plan": {"voluntary_self_hp_cost": 3},
                    },
                },
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "avoidable_voluntary_self_damage", report["issue_counts"]
        )
        check = report["audit_coverage"][
            "voluntary_self_damage_dominance"
        ]
        self.assertEqual("clear", check["status"])
        self.assertEqual(1, check["evaluated"])

    def test_self_damage_without_counterfactual_is_inconclusive(self):
        record = decision(
            action="play",
            card_id="Offering",
            card_instance_id="offering-1",
            hand_before=[{
                "id": "Offering", "card_instance_id": "offering-1",
                "type": "SKILL", "cost": 0, "is_playable": True,
            }],
            decision={
                "card_self_hp_cost": 6,
                "search": {"voluntary_self_hp_cost": 6},
            },
        )

        report = strategy_audit.audit_records([record], "new")

        check = report["audit_coverage"][
            "voluntary_self_damage_dominance"
        ]
        self.assertEqual("inconclusive", check["status"])
        self.assertEqual(1, check["eligible"])
        self.assertEqual(0, check["evaluated"])
        self.assertEqual(1, check["unknown"])

    def test_reactive_damage_is_not_misclassified_as_voluntary_self_damage(self):
        record = decision(
            action="play",
            card_id="Strike_R",
            card_instance_id="strike-1",
            decision={
                # Aggregate first-action cost includes Sharp Hide damage.
                "card_self_hp_cost": 3,
                "search": {
                    "reactive_hp_cost": 3,
                    "voluntary_self_hp_cost": 0,
                },
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:guardian",
                "id": "TheGuardian",
                "monster_index": 0,
                "current_hp": 160,
                "max_hp": 240,
                "block": 0,
                "intent": "ATTACK_BUFF",
                "move_adjusted_damage": 8,
                "move_hits": 2,
                "is_gone": False,
                "half_dead": False,
                "powers": [{
                    "id": "Sharp Hide", "name": "Sharp Hide", "amount": 3,
                }],
            }],
        )

        report = strategy_audit.audit_records([record], "new")

        check = report["audit_coverage"][
            "voluntary_self_damage_dominance"
        ]
        self.assertEqual(0, check["eligible"])
        self.assertEqual(0, check["unknown"])
        self.assertNotIn(
            "avoidable_voluntary_self_damage", report["issue_counts"]
        )

    def test_safe_long_fight_scaling_end_is_flagged(self):
        demon_form = {
            "id": "Demon Form", "card_instance_id": "demon-form-1",
            "type": "POWER", "cost": 3, "is_playable": True,
            "damage": -1, "base_damage": -1,
        }
        heavy_blade = {
            "id": "Heavy Blade", "card_instance_id": "heavy-blade-1",
            "type": "ATTACK", "cost": 2, "is_playable": False,
            "damage": -1, "base_damage": 14,
        }
        record = decision(
            action="end",
            energy_before=5,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            hand_before=[demon_form],
            draw_pile_before=[heavy_blade],
            discard_pile_before=[],
            monsters_before=[{
                "enemy_instance_id": "enemy:giant-head", "id": "GiantHead",
                "monster_index": 0, "current_hp": 625, "max_hp": 625,
                "block": 0, "intent": "BUFF", "move_adjusted_damage": 0,
                "move_hits": 0, "is_gone": False, "half_dead": False,
                "powers": [],
            }],
            decision={
                "reason": "no_positive_marginal_action",
                "rejected_lifecycle": {
                    "kind": "demonform", "expected_benefit": 0,
                    "expected_cost": 6,
                },
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["missed_long_fight_scaling_setup"],
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"]["long_fight_scaling_setup"]["status"],
        )

    def test_whole_turn_audit_retains_spent_demon_form_opportunity(self):
        demon_form = {
            "id": "Demon Form", "card_instance_id": "demon-form-1",
            "type": "POWER", "cost": 3, "is_playable": True,
            "damage": -1, "base_damage": -1,
        }
        shrug = {
            "id": "Shrug It Off", "card_instance_id": "shrug-1",
            "type": "SKILL", "cost": 1, "is_playable": True,
            "block": 8, "damage": -1, "base_damage": -1,
        }
        heavy_blade = {
            "id": "Heavy Blade", "card_instance_id": "heavy-blade-1",
            "type": "ATTACK", "cost": 2, "is_playable": False,
            "damage": -1, "base_damage": 14,
        }
        guardian = {
            "enemy_instance_id": "enemy:spheric", "id": "SphericGuardian",
            "monster_index": 0, "current_hp": 20, "max_hp": 20,
            "block": 40, "intent": "DEFEND", "move_adjusted_damage": 0,
            "move_hits": 0, "is_gone": False, "half_dead": False,
            "powers": [
                {"id": "Barricade", "name": "Barricade", "amount": -1},
                {"id": "Artifact", "name": "Artifact", "amount": 3},
            ],
        }
        first = decision(
            action="play",
            before_seq=282352,
            energy_before=3,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            card_id="Shrug It Off",
            card_instance_id="shrug-1",
            hand_before=[shrug, demon_form],
            draw_pile_before=[heavy_blade],
            discard_pile_before=[],
            monsters_before=[guardian],
            decision_outcome={"hp_delta": 0, "enemy_hp_loss": 0},
            decision={"reason": "ordered_turn_search", "search": {
                "projected_loss": 0, "actual_loss": 0,
                "true_combat_end": False,
            }},
        )
        terminal_demon = dict(demon_form, is_playable=False)
        end = decision(
            action="end",
            before_seq=282356,
            energy_before=0,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            hand_before=[terminal_demon],
            draw_pile_before=[heavy_blade],
            discard_pile_before=[shrug],
            monsters_before=[guardian],
            decision={"reason": "no_positive_marginal_action", "search": {
                "projected_loss": 0, "actual_loss": 0,
                "true_combat_end": False,
            }},
        )

        report = strategy_audit.audit_records([first, end], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["missed_long_fight_scaling_setup"],
        )
        issue = next(
            issue for issue in report["issues"]
            if issue["kind"] == "missed_long_fight_scaling_setup"
        )
        self.assertEqual(282352, issue["opportunity_before_seq"])
        self.assertTrue(issue["persistent_enemy_defense"])
        self.assertEqual(["shrugitoff"], issue["played_cards"])

    def test_elite_lethal_line_with_usable_smoke_bomb_is_flagged(self):
        smoke = {
            "id": "SmokeBomb", "name": "Smoke Bomb",
            "potion_instance_id": "potion:smoke", "can_use": True,
            "requires_target": True,
        }
        strike = {
            "id": "Strike_R", "card_instance_id": "strike-1",
            "type": "ATTACK", "cost": 1, "is_playable": True,
            "damage": 6, "base_damage": 6,
        }
        record = decision(
            action="play",
            before_seq=281091,
            room_type_before="MonsterRoomElite",
            hp_before=9,
            energy_before=3,
            projected_hp_loss_before=61,
            projected_attack_hp_loss_before=61,
            card_id="Strike_R",
            card_instance_id="strike-1",
            hand_before=[strike],
            potions_before=[smoke],
            decision={"reason": "ordered_turn_search", "search": {
                "projected_loss": 51, "actual_loss": 51,
                "true_combat_end": False,
            }},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["avoidable_smoke_bomb_omission"],
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"]["smoke_bomb_escape"]["status"],
        )

        boss = copy.deepcopy(record)
        boss["room_type_before"] = "MonsterRoomBoss"
        accepted = strategy_audit.audit_records([boss], "new")
        self.assertNotIn(
            "avoidable_smoke_bomb_omission", accepted["issue_counts"]
        )

        alternative_rescue = copy.deepcopy(record)
        alternative_rescue.update({
            "action": "potion",
            "potion_operation": "use",
            "potion_instance_id": "potion:block",
            "potions_before": [
                smoke,
                {
                    "id": "BlockPotion", "name": "Block Potion",
                    "potion_instance_id": "potion:block",
                    "can_use": True, "requires_target": False,
                },
            ],
        })
        rescued = strategy_audit.audit_records(
            [alternative_rescue], "new"
        )
        self.assertNotIn(
            "avoidable_smoke_bomb_omission", rescued["issue_counts"]
        )

    def test_smoke_bomb_uses_complete_card_plan_loss_not_raw_end_loss(self):
        smoke = {
            "id": "SmokeBomb", "name": "Smoke Bomb",
            "potion_instance_id": "potion:smoke",
            "can_use": True, "requires_target": False,
        }
        record = decision(
            action="play",
            room_type_before="MonsterRoom",
            hp_before=20,
            projected_hp_loss_before=29,
            projected_attack_hp_loss_before=29,
            card_id="Thunderclap",
            card_instance_id="thunderclap",
            hand_before=[{
                "id": "Thunderclap", "card_instance_id": "thunderclap",
                "type": "ATTACK", "cost": 1, "damage": 4,
                "is_playable": True,
            }],
            potions_before=[smoke],
            decision={"reason": "ordered_turn_search", "search": {
                "projected_loss": 7, "actual_loss": 7,
                "true_combat_end": False,
            }},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "avoidable_smoke_bomb_omission", report["issue_counts"]
        )

    def test_random_attack_cannot_prove_unused_resource_survival_gain(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            hp_before=79,
            energy_before=3,
            projected_hp_loss_before=5,
            projected_attack_hp_loss_before=5,
            player_block_before=9,
            hand_before=[{
                "id": "Sword Boomerang", "type": "ATTACK",
                "cost": 1, "damage": 3, "magic_number": 3,
                "has_target": False, "is_playable": True,
            }],
            monsters_before=[
                {
                    "id": "Spiker", "current_hp": 52, "block": 0,
                    "intent": "ATTACK", "move_adjusted_damage": 7,
                    "move_hits": 1, "is_gone": False,
                    "powers": [{"id": "Thorns", "amount": 5}],
                },
                {
                    "id": "Repulsor", "current_hp": 2, "block": 0,
                    "intent": "DEBUFF", "move_adjusted_damage": 0,
                    "move_hits": 1, "is_gone": False, "powers": [],
                },
            ],
            end_turn_resources={
                "energy_before": 3,
                "playable_card_count": 1,
                "playable_card_ids": ["Sword Boomerang"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn("end_turn_with_resources", report["issue_counts"])

    def test_block_before_thorns_attack_is_not_redundant(self):
        monsters = [{
            "id": "Spiker", "current_hp": 52,
            "intent": "ATTACK", "move_adjusted_damage": 7,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [{"id": "Thorns", "amount": 7}],
        }]
        defend = decision(
            before_seq=10,
            after_seq=11,
            combat_id="combat:spikers",
            turn=5,
            action="play",
            card_id="Defend_R",
            card_instance_id="defend-card",
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_block_before=9,
            player_block_before=9,
            player_before={
                "current_hp": 53, "max_hp": 94, "block": 9,
                "energy": 2, "powers": [], "orbs": [],
            },
            hand_before=[
                {
                    "id": "Defend_R", "card_instance_id": "defend-card",
                    "type": "SKILL", "cost": 1, "block": 9,
                    "base_block": 8, "is_playable": True,
                },
                {
                    "id": "Thunderclap",
                    "card_instance_id": "thunderclap-card",
                    "type": "ATTACK", "cost": 1, "damage": 4,
                    "is_playable": True,
                },
            ],
            monsters_before=monsters,
        )
        thunderclap = decision(
            before_seq=11,
            after_seq=12,
            combat_id="combat:spikers",
            turn=5,
            action="play",
            card_id="Thunderclap",
            card_instance_id="thunderclap-card",
            hand_before=[{
                "id": "Thunderclap",
                "card_instance_id": "thunderclap-card",
                "type": "ATTACK", "cost": 1, "damage": 4,
                "is_playable": True,
            }],
            monsters_before=monsters,
        )

        report = strategy_audit.audit_records(
            [defend, thunderclap], "new"
        )

        self.assertNotIn(
            "redundant_covered_block_play", report["issue_counts"]
        )

    def test_brimstone_purchase_without_downside_price_is_flagged(self):
        candidate = {
            "id": "Brimstone",
            "choice_id": "shop:relic:Brimstone:10",
            "selection_eligible": True,
            "reason": "selected_purchase",
            "score": 23.2,
            "consequences": {
                "acquired_benefit": {
                    "kind": "relic", "id": "Brimstone",
                    "relic_id": "Brimstone",
                }
            },
            "score_inputs": {
                "relic_context_score": 32.4,
                "price": 147,
            },
        }
        record = decision(
            action="choose",
            phase="SHOP_SCREEN",
            combat_id=None,
            turn=None,
            decision={
                "reason": "shop_global_comparison",
                "chosen_id": "shop:relic:Brimstone:10",
                "candidates": [candidate],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["negative_relic_purchase_unpriced"],
        )
        self.assertEqual(
            "issues",
            report["audit_coverage"][
                "negative_relic_purchase_risk"
            ]["status"],
        )

        evaluated = copy.deepcopy(record)
        selected = evaluated["decision"]["candidates"][0]
        selected["strategic_risk"] = {
            "effect": "symmetric_strength_growth",
            "evaluated": True,
            "boss_id": "timeeater",
            "downside_score": 7.3,
        }
        selected["score_inputs"] = {
            "relic_positive_score": 32.4,
            "relic_downside_risk": 7.3,
            "price": 147,
        }

        accepted = strategy_audit.audit_records([evaluated], "new")

        self.assertNotIn(
            "negative_relic_purchase_unpriced", accepted["issue_counts"]
        )
        self.assertEqual(
            "clear",
            accepted["audit_coverage"][
                "negative_relic_purchase_risk"
            ]["status"],
        )

    def test_double_tap_without_same_turn_attack_consumer_is_flagged(self):
        double_tap = {
            "card_instance_id": "double-tap-1",
            "id": "Double Tap",
            "type": "SKILL",
            "cost": 1,
            "is_playable": True,
            "base_block": -1,
            "block": 0,
            "damage": 0,
        }
        pommel = {
            "card_instance_id": "pommel-1",
            "id": "Pommel Strike",
            "type": "ATTACK",
            "cost": 1,
            "is_playable": True,
            "base_block": -1,
            "block": 0,
            "damage": 12,
            "has_target": True,
        }
        record = decision(
            action="play",
            card_id="Double Tap",
            card_instance_id="double-tap-1",
            energy_before=4,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            hand_before=[double_tap, pommel],
            decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1, report["issue_counts"]["unconsumed_double_tap_setup"]
        )

    def test_double_tap_with_same_turn_attack_is_counted_as_evaluated(self):
        double_tap = {
            "card_instance_id": "double-tap-1", "id": "Double Tap",
            "type": "SKILL", "cost": 1, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 0,
        }
        pommel = {
            "card_instance_id": "pommel-1", "id": "Pommel Strike",
            "type": "ATTACK", "cost": 1, "is_playable": True,
            "base_block": -1, "block": 0, "damage": 12,
            "has_target": True,
        }
        records = [
            decision(
                before_seq=10, action="play", card_id="Double Tap",
                card_instance_id="double-tap-1", energy_before=4,
                projected_hp_loss_before=0,
                projected_attack_hp_loss_before=0,
                hand_before=[double_tap, pommel],
                decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
            ),
            decision(
                before_seq=11, action="play", card_id="Pommel Strike",
                card_instance_id="pommel-1", energy_before=3,
                projected_hp_loss_before=0,
                projected_attack_hp_loss_before=0,
                hand_before=[pommel],
                decision_outcome={"enemy_hp_loss": 12, "hp_delta": 0},
            ),
        ]

        report = strategy_audit.audit_records(records, "new")
        coverage = report["audit_coverage"][
            "unconsumed_double_tap_setup"
        ]

        self.assertNotIn(
            "unconsumed_double_tap_setup", report["issue_counts"]
        )
        self.assertEqual(1, coverage["eligible"])
        self.assertEqual(1, coverage["evaluated"])
        self.assertEqual(0, coverage["unknown"])

    def test_affordable_attack_progress_at_end_is_audited_separately(self):
        record = decision(
            action="end",
            decision={"reason": "no_positive_marginal_action"},
            energy_before=2,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            projected_end_turn_hp_loss_before=0,
            monsters_before=[{
                "enemy_instance_id": "enemy:attack-progress",
                "id": "Cultist",
                "monster_index": 0,
                "current_hp": 40,
                "max_hp": 40,
                "block": 0,
                "intent": "BUFF",
                "move_adjusted_damage": 0,
                "move_hits": 0,
                "is_gone": False,
                "half_dead": False,
                "powers": [],
            }],
            hand_before=[{
                "id": "Heavy Blade",
                "card_instance_id": "heavy-1",
                "type": "ATTACK",
                "cost": 2,
                "damage": 14,
                "base_damage": 14,
                "is_playable": True,
                "has_target": True,
            }],
            end_turn_resources={
                "energy_before": 2,
                "playable_card_count": 1,
                "playable_card_ids": ["Heavy Blade"],
                "playable_card_types": ["ATTACK"],
                "reason": "no_positive_marginal_action",
                "safety_class": "evaluated_no_positive_marginal_action",
            },
            decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"]["affordable_attack_progress_ignored"],
        )
        self.assertEqual("issues", report["combat_strategy_review"]["status"])
        self.assertEqual(
            1, report["combat_strategy_review"]["issue_count"]
        )
        self.assertFalse(
            report["combat_strategy_review"]
            ["decision_case_corpus_includes_combat"]
        )

        reactive = copy.deepcopy(record)
        reactive["monsters_before"][0].update({
            "id": "WrithingMass",
            "powers": [{"id": "Compulsive", "amount": 1}],
        })
        reactive_report = strategy_audit.audit_records([reactive], "new")
        self.assertNotIn(
            "affordable_attack_progress_ignored",
            reactive_report["issue_counts"],
        )

    def test_choker_exhaustion_with_proven_attack_progress_is_audited(self):
        records = []
        for index in range(6):
            skill = {
                "card_instance_id": f"skill-{index}",
                "id": "Shrug It Off",
                "type": "SKILL",
                "cost": 0,
                "is_playable": True,
                "base_block": 5,
                "block": 5,
                "damage": 0,
            }
            attack = {
                "card_instance_id": f"attack-{index}",
                "id": "Strike_R",
                "type": "ATTACK",
                "cost": 1,
                "is_playable": True,
                "base_block": -1,
                "block": 0,
                "damage": 8,
                "has_target": True,
            }
            records.append(decision(
                action="play",
                before_seq=200 + index,
                card_id=skill["id"],
                card_instance_id=skill["card_instance_id"],
                energy_before=2,
                projected_hp_loss_before=0,
                relic_ids_before=["Velvet Choker"],
                hand_before=[skill, attack],
                decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
            ))
        records.append(decision(
            action="end",
            before_seq=206,
            energy_before=2,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            relic_ids_before=["Velvet Choker"],
            decision={"reason": "play_phase_exhausted"},
            hand_before=[],
            end_turn_resources={
                "energy_before": 2,
                "playable_card_count": 0,
                "playable_card_ids": [],
                "reason": "play_phase_exhausted",
                "safety_class": "no_legal_resources",
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:choker-progress",
                "id": "Cultist",
                "monster_index": 0,
                "current_hp": 40,
                "max_hp": 40,
                "block": 0,
                "intent": "BUFF",
                "move_adjusted_damage": 0,
                "move_hits": 0,
                "is_gone": False,
                "half_dead": False,
                "powers": [],
            }],
            decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
        ))

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(
            1,
            report["issue_counts"]["choker_slots_without_attack_progress"],
        )

    def test_choker_attack_ledger_updates_after_block_is_stripped(self):
        records = []
        attack = {
            "card_instance_id": "attack-dynamic-block",
            "id": "Strike_R",
            "type": "ATTACK",
            "cost": 1,
            "is_playable": True,
            "base_block": -1,
            "block": 0,
            "damage": 8,
            "has_target": True,
        }
        for index in range(6):
            skill = {
                "card_instance_id": f"skill-dynamic-{index}",
                "id": "Shrug It Off",
                "type": "SKILL",
                "cost": 0,
                "is_playable": True,
                "base_block": 5,
                "block": 5,
                "damage": 0,
            }
            records.append(decision(
                action="play",
                before_seq=300 + index,
                card_id=skill["id"],
                card_instance_id=skill["card_instance_id"],
                energy_before=2,
                projected_hp_loss_before=0,
                relic_ids_before=["Velvet Choker"],
                hand_before=[skill, attack],
                monsters_before=[{
                    "enemy_instance_id": "enemy:dynamic-block",
                    "id": "Cultist",
                    "monster_index": 0,
                    "current_hp": 40,
                    "max_hp": 40,
                    "block": 10 if index == 0 else 0,
                    "intent": "BUFF",
                    "move_adjusted_damage": 0,
                    "move_hits": 0,
                    "is_gone": False,
                    "half_dead": False,
                    "powers": [],
                }],
                decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
            ))
        records.append(decision(
            action="end",
            before_seq=306,
            energy_before=2,
            projected_hp_loss_before=0,
            projected_attack_hp_loss_before=0,
            relic_ids_before=["Velvet Choker"],
            decision={"reason": "play_phase_exhausted"},
            hand_before=[],
            end_turn_resources={
                "energy_before": 2,
                "playable_card_count": 0,
                "playable_card_ids": [],
                "reason": "play_phase_exhausted",
                "safety_class": "no_legal_resources",
            },
            monsters_before=[{
                "enemy_instance_id": "enemy:dynamic-block",
                "id": "Cultist",
                "monster_index": 0,
                "current_hp": 40,
                "max_hp": 40,
                "block": 0,
                "intent": "BUFF",
                "move_adjusted_damage": 0,
                "move_hits": 0,
                "is_gone": False,
                "half_dead": False,
                "powers": [],
            }],
            decision_outcome={"enemy_hp_loss": 0, "hp_delta": 0},
        ))

        report = strategy_audit.audit_records(records, "new")

        self.assertEqual(
            1,
            report["issue_counts"]["choker_slots_without_attack_progress"],
        )

    def test_historical_red_mask_pay_below_candidate_argmax_is_flagged(self):
        def event_option(index, label, text):
            return {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option",
                    "event_id": "Masked Bandits",
                    "label": label,
                    "text": text,
                },
            }

        pay = event_option(0, "Pay", "Lose all Gold.")
        fight = event_option(1, "Fight!", "Fight the bandits.")
        record = decision(
            action="choose",
            phase="EVENT",
            before_seq=36948,
            hp_before=65,
            hp_after=65,
            decision_context={"gold": 485, "max_hp": 75},
            decision_outcome={"hp_delta": 0, "gold_delta": -485},
            decision={
                "reason": "event_default_option",
                "event_id": "maskedbandits",
                "chosen_index": 0,
                "candidates": [
                    {
                        "id": "0", "score": 1.0,
                        "consequences": {"gold_delta": -485},
                    },
                    {
                        "id": "1", "score": 18.0,
                        "consequences": {
                            "combat_delta": 1, "relic_delta": 1,
                        },
                    },
                ],
            },
            chosen_option_before=pay,
            available_options_before=[pay, fight],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "noncombat_candidate_argmax_missed"
            ],
        )
        self.assertEqual(
            "issues", report["strategy_quality"]["status"]
        )
        self.assertEqual(
            "noncombat_candidate_argmax_missed",
            report["anomalies"][0]["kind"],
        )
        self.assertIn(
            "high_value_resource_loss_choice",
            report["review_finding_counts"],
        )

    def test_high_value_spend_with_observed_argmax_benefit_is_resolved(self):
        options = [
            {
                "option_id": "option:buy",
                "choice_index": 0,
                "label": "Pen Nib",
                "target": {
                    "kind": "shop_item", "label": "Pen Nib",
                    "item": {"id": "Pen Nib", "relic_id": "Pen Nib"},
                },
            },
            {
                "option_id": "option:leave",
                "choice_index": 1,
                "label": "Leave",
                "target": {"kind": "shop_leave", "label": "Leave"},
            },
        ]
        benefit = {"kind": "relic", "id": "Pen Nib", "amount": 1}
        record = decision(
            action="choose",
            phase="SHOP_SCREEN",
            decision_context={"gold": 180},
            decision_outcome={
                "hp_delta": 0,
                "gold_delta": -100,
                "observable_delta": benefit,
            },
            decision={
                "reason": "shop_relic_synergy_and_reserve",
                "chosen": "Pen Nib",
                "candidates": [
                    {
                        "id": "Pen Nib", "score": 12.0,
                        "consequences": {
                            "gold_delta": -100,
                            "acquired_benefit": benefit,
                        },
                    },
                    {
                        "id": "Leave", "score": 4.0,
                        "consequences": {"leave": True},
                    },
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "high_value_resource_loss_choice",
            report["review_finding_counts"],
        )
        self.assertEqual(1, report["review_resolution_count"])
        self.assertEqual(
            "high_value_resource_loss_resolved",
            report["review_resolutions"][0]["kind"],
        )

    def test_designer_full_service_deferred_benefit_requires_clear_oracle(self):
        contract = {
            "contract_version": 1,
            "contract_kind": "BASE_GAME_EVENT_OPTION",
            "event_id": "Designer",
            "event_class": "com.megacrit.cardcrawl.events.shrines.Designer",
            "event_stage": "MAIN",
            "original_button_index": 2,
            "option_kind": "FULL_SERVICE",
            "instance_parameters": {
                "adjustment_upgrades_one": True,
                "clean_up_removes_cards": True,
                "adjust_cost": 40,
                "clean_up_cost": 60,
                "full_service_cost": 90,
                "hp_loss": 3,
            },
            "parameters": {
                "gold_cost": 90,
                "purge_select_count": 1,
                "random_upgrade_max_count": 1,
                "selection_mode": "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE",
            },
        }
        mechanism_id = "event-mechanism:" + hashlib.sha256(
            json.dumps(
                contract, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        option = {
            "option_id": "designer:full-service",
            # Disabled earlier buttons may compress the protocol index; the
            # base-game semantic identity is the unfiltered original index.
            "choice_index": 0,
            "original_button_index": 2,
            "label": "Full Service",
            "target": {
                "kind": "event_option",
                "event_id": "Designer",
                "label": "Full Service",
                "original_button_index": 2,
                "event_contract": contract,
                "mechanism_id": mechanism_id,
            },
        }
        event = decision(
            phase="EVENT", action="choose", before_seq=10, after_seq=11,
            selected_choice_ids=["designer:full-service"],
            available_options_before=[option],
        )
        grid = decision(
            phase="GRID", action="choose", before_seq=11, after_seq=12,
        )
        oracle_details = {
            "selected_card_instance_ids": ["uuid-writhe"],
            "designer_full_service": {
                "random_upgrade": {
                    "before": {
                        "id": "Bludgeon", "upgrades": 0,
                        "card_instance_id": "uuid-bludgeon",
                    },
                    "after": {
                        "id": "Bludgeon", "upgrades": 1,
                        "card_instance_id": "uuid-bludgeon",
                    },
                },
            },
        }
        with (
            patch.object(
                strategy_audit._independent_oracle,
                "_one_grid_confirmation_effect",
                return_value={"operation": "grid_purge"},
            ),
            patch.object(
                strategy_audit._independent_oracle,
                "_grid_deferred_settlement",
                return_value=("clear", oracle_details),
            ),
        ):
            deferred = (
                strategy_audit._designer_full_service_deferred_resolution(
                    [event, grid], 0, event
                )
            )

        self.assertEqual(
            "uuid-bludgeon",
            deferred["acquired_benefit"]["upgraded_card_instance_id"],
        )
        resolution = strategy_audit._resource_loss_resolution_evidence(
            event,
            {"consequences": {"gold_delta": -90}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=deferred,
        )
        self.assertTrue(resolution["resolved"], resolution)

        with (
            patch.object(
                strategy_audit._independent_oracle,
                "_one_grid_confirmation_effect",
                return_value={"operation": "grid_purge"},
            ),
            patch.object(
                strategy_audit._independent_oracle,
                "_grid_deferred_settlement",
                return_value=("issue", {
                    "reason": "grid_confirmation_deck_delta_mismatch",
                }),
            ),
        ):
            rejected = (
                strategy_audit._designer_full_service_deferred_resolution(
                    [event, grid], 0, event
                )
            )
        self.assertIsNone(rejected)

        forged_event = copy.deepcopy(event)
        forged_event["available_options_before"][0][
            "original_button_index"
        ] = 1
        self.assertIsNone(
            strategy_audit._designer_full_service_deferred_resolution(
                [forged_event, grid], 0, forged_event
            )
        )

    def test_golden_idol_cost_links_to_adjacent_relic_gain(self):
        take = {
            "option_id": "idol:take", "original_button_index": 0,
            "target": {
                "kind": "event_option", "event_id": "Golden Idol",
                "original_button_index": 0,
            },
        }
        cost = {
            "option_id": "idol:max-hp", "original_button_index": 2,
            "target": {
                "kind": "event_option", "event_id": "Golden Idol",
                "original_button_index": 2,
            },
        }
        gain = decision(
            phase="EVENT", action="choose", before_seq=10, after_seq=11,
            chosen_option_before=take,
            decision_outcome={
                "hp_delta": 0, "max_hp_delta": 0, "gold_delta": 0,
                "relics": {"added": [{"id": "Golden Idol"}]},
            },
        )
        payment = decision(
            phase="EVENT", action="choose", before_seq=11, after_seq=12,
            chosen_option_before=cost,
            decision_outcome={
                "hp_delta": -5, "max_hp_delta": -6, "gold_delta": 0,
            },
        )

        resolution = strategy_audit._golden_idol_cost_resolution(
            [gain, payment], 1, payment
        )

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(
            "Golden Idol", resolution["acquired_benefit"]["relic_id"]
        )

    def test_ghosts_cost_links_to_authoritative_card_package(self):
        preview = {
            "id": "Ghostly", "card_instance_id": "preview-ghostly",
        }
        chosen = {
            "option_id": "ghosts:accept", "original_button_index": 0,
            "target": {
                "kind": "event_option", "event_id": "Ghosts",
                "original_button_index": 0, "card": preview,
            },
        }
        record = decision(
            phase="EVENT", action="choose", before_seq=20, after_seq=21,
            ascension_level=0,
            chosen_option_before=chosen,
            decision_outcome={
                "hp_delta": -35, "max_hp_delta": -35,
                "deck": {
                    "added": [
                        {
                            "id": "Ghostly",
                            "card_instance_id": f"ghostly-{index}",
                        }
                        for index in range(5)
                    ],
                    "removed": [], "changed": [],
                },
            },
        )

        resolution = strategy_audit._ghosts_card_package_resolution(record)

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(5, resolution["acquired_benefit"]["count"])
        self.assertEqual(
            "Ghostly", resolution["observable_delta"]["card_id"]
        )

    def test_apparition_audit_checks_realized_use_and_grid_consistency(self):
        package = [
            {
                "id": "Ghostly",
                "card_instance_id": f"ghostly-{index}",
            }
            for index in range(5)
        ]
        event = decision(
            phase="EVENT", action="choose", before_seq=20, after_seq=21,
            reason="ghosts_signed_post_state",
            decision_outcome={
                "deck": {"added": copy.deepcopy(package)},
            },
            outcome_facts={
                "0": {
                    "post_state": {
                        "apparition_valuation": {
                            "playability_fraction": 0.90,
                            "upgraded": False,
                        },
                    },
                },
            },
            producer_candidates=[{
                "score_rule_id": (
                    "ghosts_contextual_apparition_value_v2"
                ),
                "score": 22.7,
            }],
        )
        grid = decision(
            phase="GRID", action="choose", before_seq=22, after_seq=23,
            chosen_option_before={
                "target": {"card": copy.deepcopy(package[1])},
            },
            producer_candidates=[
                {
                    "score": 101.6,
                    "consequences": {
                        "selected_card": copy.deepcopy(package[1]),
                    },
                },
                {
                    "score": 52.0,
                    "consequences": {
                        "selected_card": {
                            "id": "Strike_G",
                            "card_instance_id": "strike-1",
                        },
                    },
                },
            ],
        )
        play = decision(
            phase="COMBAT_TURN_1", action="play",
            before_seq=30, after_seq=31, turn=1,
            combat_id="combat:sphere", card_id="Ghostly",
            card_instance_id="ghostly-0",
            projected_hp_loss_before=12,
        )
        after_play = decision(
            phase="COMBAT_TURN_1", action="play",
            before_seq=31, after_seq=32, turn=1,
            combat_id="combat:sphere", card_id="Strike_G",
            card_instance_id="strike-2",
            projected_hp_loss_before=5,
        )
        end = decision(
            phase="COMBAT_TURN_1", action="end",
            before_seq=32, after_seq=33, turn=1,
            combat_id="combat:sphere",
            hand_before=[copy.deepcopy(package[index]) for index in (2, 3, 4)],
        )

        findings, coverage = (
            strategy_audit._apparition_realized_value_audit(
                [event, grid, play, after_play, end]
            )
        )

        self.assertEqual(
            {
                "apparition_cross_decision_value_conflict",
                "apparition_playability_overestimated",
            },
            {finding["kind"] for finding in findings},
        )
        realized = coverage["apparition_realized_value"]
        self.assertEqual((1, 1, 1), (
            realized["eligible"], realized["evaluated"],
            realized["violations"],
        ))
        cross = coverage["apparition_cross_decision_consistency"]
        self.assertEqual((1, 1, 1), (
            cross["eligible"], cross["evaluated"], cross["violations"],
        ))

    def test_mind_bloom_lock_links_to_authoritative_upgrade_package(self):
        target = {
            "kind": "event_option", "event_id": "MindBloom",
            "original_button_index": 1,
        }
        record = decision(
            phase="EVENT", action="choose", before_seq=20, after_seq=23,
            chosen={"choice_index": 1},
            selected_choice_ids=["mind-bloom:awake"],
            canonical_choices=[{
                "choice_id": "mind-bloom:awake", "choice_index": 1,
                "selected": True, "target": target,
            }],
            decision_outcome={
                "deck": {
                    "added": [], "removed": [],
                    "changed": [
                        {
                            "before": {
                                "id": "Bash", "upgrades": 0,
                                "card_instance_id": "uuid-bash",
                            },
                            "after": {
                                "id": "Bash", "upgrades": 1,
                                "card_instance_id": "uuid-bash",
                            },
                        },
                        {
                            "before": {
                                "id": "Inflame", "upgrades": 0,
                                "card_instance_id": "uuid-inflame",
                            },
                            "after": {
                                "id": "Inflame", "upgrades": 1,
                                "card_instance_id": "uuid-inflame",
                            },
                        },
                    ],
                },
                "relics": {
                    "added": [{"id": "Mark of the Bloom"}],
                    "removed": [], "changed": [],
                },
            },
        )

        resolution = strategy_audit._mind_bloom_upgrade_package_resolution(
            record
        )

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(2, resolution["acquired_benefit"]["count"])
        self.assertEqual(
            "Mark of the Bloom",
            resolution["observable_delta"]["relic_gained"],
        )

    def test_shining_light_hp_cost_links_to_authoritative_upgrade_package(self):
        target = {
            "kind": "event_option", "event_id": "Shining Light",
            "event_stage": "INTRO", "original_button_index": 0,
        }
        record = decision(
            phase="EVENT", action="choose", before_seq=20, after_seq=23,
            chosen={"choice_index": 0},
            selected_choice_ids=["shining-light:upgrade"],
            canonical_choices=[{
                "choice_id": "shining-light:upgrade", "choice_index": 0,
                "selected": True, "target": target,
            }],
            decision_outcome={
                "hp_delta": -16,
                "deck": {
                    "added": [], "removed": [],
                    "changed": [
                        {
                            "before": {
                                "id": "Strike_R", "upgrades": 0,
                                "card_instance_id": "uuid-strike",
                            },
                            "after": {
                                "id": "Strike_R", "upgrades": 1,
                                "card_instance_id": "uuid-strike",
                            },
                        },
                        {
                            "before": {
                                "id": "Defend_R", "upgrades": 0,
                                "card_instance_id": "uuid-defend",
                            },
                            "after": {
                                "id": "Defend_R", "upgrades": 1,
                                "card_instance_id": "uuid-defend",
                            },
                        },
                    ],
                },
            },
        )

        resolution = strategy_audit._shining_light_upgrade_package_resolution(
            record
        )

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(2, resolution["acquired_benefit"]["count"])
        self.assertEqual(
            ["uuid-defend", "uuid-strike"],
            resolution["observable_delta"]["card_instance_ids"],
        )
        evidence = strategy_audit._resource_loss_resolution_evidence(
            record,
            {"consequences": {}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=resolution,
        )
        self.assertTrue(evidence["resolved"], evidence)

    def test_beggar_payment_links_to_deferred_grid_removal(self):
        pay_option = {
            "option_id": "beggar:pay", "original_button_index": 0,
            "target": {
                "kind": "event_option", "event_id": "Beggar",
                "original_button_index": 0,
            },
        }
        payment = decision(
            phase="EVENT", action="choose", before_seq=10, after_seq=11,
            chosen_option_before=pay_option,
            decision_outcome={"hp_delta": 0, "gold_delta": -75},
        )
        continuation = decision(
            phase="EVENT", action="choose", before_seq=11, after_seq=12,
            chosen_option_before=copy.deepcopy(pay_option),
            decision_outcome={"hp_delta": 0, "gold_delta": 0},
        )
        grid = decision(
            phase="GRID", action="choose", before_seq=12, after_seq=13,
        )
        details = {"selected_card_instance_ids": ["uuid-strike"]}
        with (
            patch.object(
                strategy_audit._independent_oracle,
                "_one_grid_confirmation_effect",
                return_value={"operation": "grid_purge"},
            ),
            patch.object(
                strategy_audit._independent_oracle,
                "_grid_deferred_settlement",
                return_value=("clear", details),
            ),
        ):
            resolution = strategy_audit._beggar_remove_deferred_resolution(
                [payment, continuation, grid], 0, payment
            )

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(
            ["uuid-strike"],
            resolution["acquired_benefit"]["removed_card_instance_ids"],
        )

    def test_neow_transform_cost_links_to_deferred_grid_transaction(self):
        contract = {
            "contract_kind": "NEOW_REWARD",
            "contract_version": 1,
            "drawback_kind": "PERCENT_DAMAGE",
            "parameters": {"drawback_def_kind": "PERCENT_DAMAGE"},
            "reward_kind": "TRANSFORM_TWO_CARDS",
        }
        mechanism_id = strategy_audit._independent_oracle._oracle_neow_mechanism_id(
            contract
        )
        option = {
            "option_id": "neow:transform-two",
            "target": {
                "kind": "event_option", "event_id": "Neow Event",
                "neow_contract": contract, "mechanism_id": mechanism_id,
            },
        }
        event = decision(
            phase="NEOW", action="choose", before_seq=10, after_seq=11,
            selected_choice_ids=["neow:transform-two"],
            available_options_before=[option],
            authoritative_choice_settlement={
                "status": "observed",
                "observed_outcome": {"gold_delta": 0},
            },
        )
        grid = decision(
            phase="GRID", action="choose", before_seq=11, after_seq=12,
        )
        details = {
            "operation": "grid_transform",
            "selected_card_instance_ids": ["strike-a", "strike-b"],
        }
        with (
            patch.object(
                strategy_audit._independent_oracle,
                "_one_grid_confirmation_effect",
                return_value={"operation": "grid_transform"},
            ),
            patch.object(
                strategy_audit._independent_oracle,
                "_grid_deferred_settlement",
                return_value=("clear", details),
            ),
        ):
            resolution = (
                strategy_audit._neow_deck_change_deferred_resolution(
                    [event, grid], 0, event
                )
            )

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(
            "neow_deck_transform",
            resolution["acquired_benefit"]["kind"],
        )
        self.assertEqual(
            ["strike-a", "strike-b"],
            resolution["acquired_benefit"]["selected_card_instance_ids"],
        )

    def test_neow_paid_card_reward_binds_pick_and_exposes_skip(self):
        binding = {
            "attempt_id": "attempt-neow-card",
            "run_id": "IRONCLAD:0:123",
            "seed": 123,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "new",
            "controller_hash": "controller",
            "policy_version": "fast-policy-v5",
            "selection_id": "selection",
            "selection_digest": "digest",
        }
        contract = {
            "contract_kind": "NEOW_REWARD",
            "contract_version": 1,
            "drawback_kind": "PERCENT_DAMAGE",
            "parameters": {
                "cursed": False,
                "hp_bonus": 8,
                "drawback_def_kind": "PERCENT_DAMAGE",
            },
            "reward_kind": "RANDOM_COLORLESS_2",
        }
        mechanism_id = (
            strategy_audit._independent_oracle._oracle_neow_mechanism_id(
                contract
            )
        )
        option = {
            "option_id": "option:paid-colorless",
            "choice_index": 2,
            "target": {
                "kind": "event_option",
                "event_id": "Neow Event",
                "neow_contract": contract,
                "mechanism_id": mechanism_id,
            },
        }
        parent = decision(
            **binding,
            phase="NEOW", action="choose", before_seq=10, after_seq=11,
            selected_choice_ids=["option:paid-colorless"],
            available_options_before=[option],
        )

        def followup(action, added, selected):
            return decision(
                **binding,
                phase="CARD_REWARD", action=action,
                before_seq=11, after_seq=12,
                selected_choice_ids=[selected],
                authoritative_choice_settlement={
                    "status": "observed",
                    "fully_observable": True,
                    "choice_id": selected,
                    "before_seq": 11,
                    "after_seq": 12,
                    "observed_outcome": {
                        "deck": {
                            "added": added, "removed": [], "changed": [],
                        },
                    },
                },
            )

        skipped = followup("return", [], "action:return")
        skipped_resolution = (
            strategy_audit._neow_card_reward_deferred_resolution(
                [parent, skipped], 0, parent
            )
        )
        self.assertEqual(
            "realized_no_benefit", skipped_resolution["status"]
        )
        unresolved = strategy_audit._resource_loss_resolution_evidence(
            parent,
            {"consequences": {"hp_delta": -24}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=skipped_resolution,
        )
        self.assertFalse(unresolved["resolved"])

        picked = followup("choose", [{
            "id": "Apotheosis",
            "card_instance_id": "uuid-apotheosis",
        }], "card:uuid-apotheosis")
        picked_resolution = (
            strategy_audit._neow_card_reward_deferred_resolution(
                [parent, picked], 0, parent
            )
        )
        self.assertEqual("clear", picked_resolution["status"])
        self.assertEqual(
            "Apotheosis", picked_resolution["acquired_benefit"]["card_id"]
        )
        resolved = strategy_audit._resource_loss_resolution_evidence(
            parent,
            {"consequences": {"hp_delta": -24}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=picked_resolution,
        )
        self.assertTrue(resolved["resolved"], resolved)

    def test_neow_immediate_random_relic_binds_authoritative_settlement(self):
        binding = {
            "attempt_id": "attempt-neow-relic",
            "run_id": "IRONCLAD:0:456",
            "seed": 456,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "new",
            "controller_hash": "controller",
            "policy_version": "fast-policy-v5",
            "selection_id": "selection",
            "selection_digest": "b" * 64,
        }
        contract = {
            "contract_kind": "NEOW_REWARD",
            "contract_version": 1,
            "drawback_kind": "TEN_PERCENT_HP_LOSS",
            "parameters": {
                "cursed": False,
                "hp_bonus": 8,
                "drawback_def_kind": "TEN_PERCENT_HP_LOSS",
            },
            "reward_kind": "ONE_RARE_RELIC",
        }
        mechanism_id = (
            strategy_audit._independent_oracle._oracle_neow_mechanism_id(
                contract
            )
        )
        option = {
            "option_id": "option:paid-relic",
            "choice_index": 2,
            "target": {
                "kind": "event_option",
                "event_id": "Neow Event",
                "neow_contract": contract,
                "mechanism_id": mechanism_id,
            },
        }
        record = decision(
            **binding,
            phase="NEOW", action="choose", before_seq=10, after_seq=11,
            selected_choice_ids=["option:paid-relic"],
            available_options_before=[option],
            authoritative_choice_settlement={
                "status": "observed",
                "fully_observable": True,
                "choice_id": "option:paid-relic",
                "before_seq": 10,
                "after_seq": 11,
                "observed_outcome": {
                    "max_hp_delta": -8,
                    "relics": {
                        "added": [{"id": "Bird Faced Urn"}],
                        "removed": [], "changed": [],
                    },
                },
            },
        )

        resolution = strategy_audit._neow_relic_reward_resolution(record)

        self.assertEqual("clear", resolution["status"])
        self.assertEqual(
            "Bird Faced Urn",
            resolution["acquired_benefit"]["relic_id"],
        )
        evidence = strategy_audit._resource_loss_resolution_evidence(
            record,
            {"consequences": {"max_hp_delta": -8}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=resolution,
        )
        self.assertTrue(evidence["resolved"], evidence)

    def test_resource_gain_resolves_immediate_and_deferred_event_benefits(self):
        binding = {
            "attempt_id": "attempt-a",
            "run_id": "IRONCLAD:0:123",
            "seed": 123,
            "character": "IRONCLAD",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "new",
            "controller_hash": "controller",
            "policy_version": "fast-policy-v5",
            "selection_id": "selection",
            "selection_digest": "a" * 64,
        }

        def state(seq, *, hp=70, max_hp=90, gold=161):
            return {
                "protocol_version": 2, "state_seq": seq, **binding,
                "game_state": {
                    "current_hp": hp, "max_hp": max_hp, "gold": gold,
                },
            }

        serpent_option = {
            "option_id": "liars:agree",
            "target": {"event_id": "Liars Game"},
        }
        agree = decision(
            **binding, phase="EVENT", before_seq=10, after_seq=11,
            chosen_option_before=serpent_option,
            authoritative_state_before=state(10),
            authoritative_state_after=state(11),
        )
        continuation = decision(
            **binding, phase="EVENT", before_seq=11, after_seq=12,
            chosen_option_before=serpent_option,
            authoritative_state_before=state(11),
            authoritative_state_after=state(12, gold=336),
        )
        gold_resolution = strategy_audit._positive_resource_gain_resolution(
            [agree, continuation], 0, agree,
            {"consequences": {"gold_delta": 175}},
        )
        self.assertEqual(175, gold_resolution["observable_delta"]["gold_delta"])
        self.assertEqual(2, len(gold_resolution["deferred_evidence"]))

        altar_option = {
            "option_id": "altar:sacrifice",
            "target": {"event_id": "Forgotten Altar"},
        }
        altar = decision(
            **binding, phase="EVENT", before_seq=20, after_seq=21,
            chosen_option_before=altar_option,
            authoritative_state_before=state(20, hp=84),
            authoritative_state_after=state(21, hp=66, max_hp=95),
        )
        max_hp_resolution = strategy_audit._positive_resource_gain_resolution(
            [altar], 0, altar,
            {"consequences": {"hp_delta": -18, "max_hp_delta": 5}},
        )
        self.assertEqual(
            5, max_hp_resolution["observable_delta"]["max_hp_delta"]
        )

    def test_mausoleum_resource_loss_uses_independent_trade_evidence(self):
        option = {
            "option_id": "mausoleum:0",
            "choice_index": 0,
            "target": {
                "kind": "event_option",
                "event_id": "The Mausoleum",
            },
        }
        record = decision(
            phase="EVENT", action="choose",
            selected_choice_ids=["mausoleum:0"],
            available_options_before=[option],
        )
        benefit = {
            "kind": "random_relic_gain", "id": "Anchor",
            "relic_id": "Anchor", "count": 1,
        }
        evidence = {
            "schema_version": 1,
            "mechanism": "mausoleum_expected_curse_for_random_relic",
            "status": "clear",
            "reason": "mausoleum_open_settlement_clear",
            "expected_trade_contract": {
                "operation": "mausoleum_open_coffin",
                "guaranteed_benefit": {
                    "kind": "random_relic_gain", "count": 1,
                },
                "risk": {
                    "effective_curse_probability": 0.5,
                    "omamori_charge_loss_probability": 0.0,
                },
            },
            "realized_settlement": {
                "operation": "mausoleum_open_coffin",
                "authority": "authoritative_protocol_delta",
                "acquired_benefit": benefit,
                "observable_delta": {
                    "relics": {"added": [{"id": "Anchor"}]},
                    "deck": {"added": [{"id": "Writhe"}]},
                },
            },
        }
        # Producer omission (or a forged zero) cannot suppress the independent
        # A0 curse risk.
        losses = strategy_audit._high_value_resource_losses(
            record, None,
            mausoleum_evidence=evidence,
        )
        self.assertEqual("expected_curse", losses[0]["resource"])
        deferred = strategy_audit._mausoleum_trade_resolution(
            record, evidence
        )
        resolution = strategy_audit._resource_loss_resolution_evidence(
            record, {"consequences": {
                "acquired_benefit": {"kind": "forged", "id": "BossRelic"},
            }},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=deferred,
        )
        self.assertTrue(resolution["resolved"], resolution)
        self.assertEqual(
            "Anchor", resolution["observable_delta"]["relic_id"]
        )
        self.assertEqual(
            "Anchor", resolution["acquired_benefit"]["relic_id"]
        )
        self.assertEqual(
            "random_relic_gain",
            deferred["expected_trade_contract"]["guaranteed_benefit"][
                "kind"
            ],
        )

        broken = dict(evidence)
        broken["status"] = "issues"
        broken["reason"] = "mausoleum_relic_gain_count_mismatch"
        rejected = strategy_audit._resource_loss_resolution_evidence(
            record, {"consequences": {}},
            candidate_evidence_complete=True,
            selected_is_argmax=True,
            rationale_complete=True,
            deferred_resolution=strategy_audit._mausoleum_trade_resolution(
                record, broken
            ),
        )
        self.assertFalse(rejected["resolved"])
        self.assertIn(
            "independent_mausoleum_trade_evidence",
            rejected["missing_evidence"],
        )

    def test_high_value_losses_use_bound_authoritative_resource_deltas(self):
        binding = {
            "attempt_id": "attempt-a",
            "run_id": "DEFECT:0:123",
            "seed": 123,
            "character": "DEFECT",
            "ascension_level": 0,
            "run_type": "standard",
            "decision_hash": "new",
            "controller_hash": "controller",
            "policy_version": "fast-policy-v5",
            "selection_id": "selection",
            "selection_digest": "a" * 64,
        }
        before = {
            "protocol_version": 2, "state_seq": 10, **binding,
            "game_state": {"current_hp": 60, "max_hp": 70, "gold": 200},
        }
        after = {
            "protocol_version": 2, "state_seq": 11, **binding,
            "game_state": {"current_hp": 40, "max_hp": 64, "gold": 80},
        }
        record = decision(
            **binding, before_seq=10, after_seq=11,
            decision_outcome={"hp_delta": 0, "gold_delta": 0},
            authoritative_state_before=before,
            authoritative_state_after=after,
        )
        losses = strategy_audit._high_value_resource_losses(record, None)
        self.assertEqual(
            {"gold", "hp", "max_hp"},
            {loss["resource"] for loss in losses},
        )
        self.assertTrue(all(
            loss["authority"] == "authoritative_protocol_delta"
            for loss in losses
        ))

        forged = copy.deepcopy(record)
        forged["authoritative_state_after"]["selection_id"] = "forged"
        self.assertEqual(
            [], strategy_audit._high_value_resource_losses(forged, {})
        )

    def test_bound_applied_model_override_may_choose_below_local_argmax(self):
        def relic_option(index, relic_id):
            return {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": relic_id,
                "target": {
                    "kind": "relic",
                    "relic": {"id": relic_id, "name": relic_id},
                },
            }

        ectoplasm = relic_option(0, "Ectoplasm")
        black_star = relic_option(1, "Black Star")
        candidates = [
            {"id": "Ectoplasm", "score": 20.0},
            {"id": "Black Star", "score": 18.0},
        ]
        record = decision(
            action="choose",
            phase="BOSS_REWARD",
            decision={
                "reason": "boss_relic_context_score",
                "chosen": "Black Star",
                "candidates": candidates,
                "model_advice": {
                    "status": "applied",
                    "applied": True,
                    "rule_choice_id": "Ectoplasm",
                    "model_choice_id": "Black Star",
                    "final_choice_ids": ["Black Star"],
                    "replay": {
                        "rule_choice_ids": ["Ectoplasm"],
                        "candidates": [
                            {
                                "candidate_id": "Ectoplasm",
                                "local_score": 20.0,
                                "facts": {},
                            },
                            {
                                "candidate_id": "Black Star",
                                "local_score": 18.0,
                                "facts": {},
                            },
                        ],
                    },
                },
            },
            chosen_option_before=black_star,
            available_options_before=[ectoplasm, black_star],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "noncombat_candidate_argmax_missed", report["issue_counts"]
        )
        self.assertNotIn(
            "noncombat_invalid_model_override", report["issue_counts"]
        )
        self.assertEqual(
            1,
            report["audit_coverage"][
                "noncombat_candidate_selection"
            ]["accepted_overrides"],
        )
        self.assertEqual("clear", report["strategy_quality"]["status"])

    def test_active_unclassified_mechanic_prevents_clear_status(self):
        monster = {
            "enemy_instance_id": "enemy:test", "id": "Cultist",
            "monster_index": 0, "current_hp": 48, "max_hp": 48,
            "block": 0, "intent": "ATTACK", "move_adjusted_damage": 8,
            "move_hits": 1, "is_gone": False, "half_dead": False,
            "powers": [],
        }
        record = decision(
            energy_before=0,
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
            player_before={
                "current_hp": 20, "max_hp": 70, "block": 0,
                "energy": 0, "powers": [], "orbs": [],
            },
            monsters_before=[monster],
            relic_ids_before=["Totally Unknown Relic"],
            decision_outcome={"hp_delta": -8},
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            "inconclusive", report["mechanics_coverage"]["status"]
        )
        self.assertEqual("inconclusive", report["audit_status"])
        self.assertIn(
            "totally unknown relic",
            report["mechanics_coverage"]["unclassified_ids"],
        )
        self.assertTrue(any(
            item["kind"] == "active_unclassified_mechanics"
            for item in report["anomalies"]
        ))

    def test_event_missing_candidate_consequences_is_inconclusive(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option", "event_id": "Test Event",
                    "label": label, "text": label,
                },
            }
            for index, label in enumerate(("Take", "Leave"))
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "event_semantic_outcome_score",
                "chosen_index": 0,
                "candidates": [
                    {"id": "0", "score": 5.0},
                    {"id": "1", "score": 0.0},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            "inconclusive", report["strategy_quality"]["status"]
        )
        self.assertEqual(
            1,
            report["review_finding_counts"][
                "noncombat_candidate_consequences_incomplete"
            ],
        )

    def test_legacy_one_hot_event_candidates_cannot_self_prove_quality(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option", "event_id": "The Cleric",
                    "label": label, "text": label,
                },
            }
            for index, label in enumerate(("Heal", "Leave"))
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "cleric_low_hp_heal",
                "chosen_index": 0,
                # Historical choose_index synthesized this vector after the
                # rule had already chosen, so it is not an independent oracle.
                "candidates": [
                    {"id": "0", "score": 1.0, "consequences": {}},
                    {"id": "1", "score": 0.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual("inconclusive", report["strategy_quality"]["status"])
        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertEqual(
            "legacy_chosen_one_hot_template", finding["score_evidence"]
        )

    def test_contract_cannot_hide_incomplete_visible_candidate_coverage(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {"kind": "shop_item", "label": label},
            }
            for index, label in enumerate(("A", "B", "C"))
        ]
        record = decision(
            action="choose",
            phase="SHOP_SCREEN",
            decision={
                "reason": "shop_partial_score",
                "chosen": "A",
                "candidate_contract": {
                    "score_source": "explicit_local_utility",
                    "strategy_quality_auditable": True,
                    "all_visible_options_scored": True,
                },
                "candidates": [
                    {"id": "A", "score": 3.0},
                    {"id": "B", "score": 2.0},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual("inconclusive", report["strategy_quality"]["status"])
        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertEqual(["option:2"], finding["unmatched_option_ids"])

    def test_false_contract_does_not_hide_auditor_proven_numeric_bijection(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {"kind": "event_option", "label": label},
            }
            for index, label in enumerate(("Take", "Leave"))
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "structured_event_tradeoff",
                "chosen_index": 0,
                "candidate_contract": {
                    "strategy_quality_auditable": False,
                    "score_source": "unverified_candidate_subset",
                },
                "candidates": [
                    {"id": 0, "score": 7.0, "consequences": {}},
                    {"id": 1, "score": 2.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "noncombat_candidate_evidence_incomplete",
            report["review_finding_counts"],
        )
        self.assertEqual(
            1,
            report["audit_coverage"]["noncombat_candidate_selection"][
                "evaluated"
            ],
        )

    def test_true_contract_cannot_hide_extra_candidate(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {"kind": "shop_item", "label": label},
            }
            for index, label in enumerate(("A", "B"))
        ]
        record = decision(
            action="choose",
            phase="SHOP_SCREEN",
            decision={
                "reason": "shop_complete_score",
                "chosen": "A",
                "candidate_contract": {
                    "strategy_quality_auditable": True,
                    "all_visible_options_scored": True,
                },
                "candidates": [
                    {"id": "A", "score": 3.0},
                    {"id": "B", "score": 2.0},
                    {"id": "invented", "score": 100.0},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertEqual(["invented"], finding["extra_candidate_ids"])

    def test_duplicate_candidate_for_one_option_is_rejected(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {"kind": "event_option", "label": label},
            }
            for index, label in enumerate(("A", "B"))
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "event_score",
                "chosen_index": 0,
                "candidates": [
                    {"id": "A", "score": 4.0, "consequences": {}},
                    {"id": "A", "score": 4.0, "consequences": {}},
                    {"id": "B", "score": 2.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertEqual(["A"], finding["duplicate_candidate_ids"])

    def test_many_to_one_alias_is_rejected_as_ambiguous(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": "Same label",
                "target": {"kind": "event_option", "label": "Same label"},
            }
            for index in range(2)
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "event_score",
                "chosen_index": 0,
                "candidates": [
                    {
                        "id": "Same label", "score": 4.0,
                        "consequences": {},
                    },
                    {"id": 1, "score": 2.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        finding = next(
            item for item in report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertEqual(
            ["Same label"], finding["ambiguous_candidate_ids"]
        )

    def test_candidate_and_option_reordering_preserves_semantic_choice(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {"kind": "event_option", "label": label},
            }
            for index, label in enumerate(("A", "B", "C"))
        ]
        base = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "event_score",
                "chosen_index": 1,
                "candidate_contract": {
                    "strategy_quality_auditable": False,
                },
                "candidates": [
                    {"id": 0, "score": 2.0, "consequences": {}},
                    {"id": 1, "score": 8.0, "consequences": {}},
                    {"id": 2, "score": 4.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[1],
            available_options_before=options,
        )
        reordered = copy.deepcopy(base)
        reordered["available_options_before"].reverse()
        reordered["decision"]["candidates"].reverse()

        original_report = strategy_audit.audit_records([base], "new")
        reordered_report = strategy_audit.audit_records([reordered], "new")

        for report in (original_report, reordered_report):
            self.assertNotIn(
                "noncombat_candidate_evidence_incomplete",
                report["review_finding_counts"],
            )
            self.assertNotIn(
                "noncombat_candidate_argmax_missed",
                report["issue_counts"],
            )
            self.assertEqual(
                1,
                report["audit_coverage"]["noncombat_candidate_selection"][
                    "evaluated"
                ],
            )

    def test_current_canonical_surface_includes_synthetic_command_and_veto(self):
        options = [
            {
                "option_id": "option:gold",
                "choice_index": 0,
                "label": "gold",
                "target": {
                    "kind": "reward",
                    "reward": {
                        "choice_index": 0,
                        "reward_type": "GOLD",
                        "gold": 20,
                    },
                },
            },
            {
                "option_id": "option:potion",
                "choice_index": 1,
                "label": "potion",
                "target": {
                    "kind": "reward",
                    "reward": {
                        "choice_index": 1,
                        "reward_type": "POTION",
                    },
                },
            },
        ]
        producers = [
            {
                "id": "reward:gold:0",
                "choice_id": "reward:gold:0",
                "choice_index": 0,
                "action": "choose",
                "operation": None,
                "semantic_id": "reward:gold:0",
                "score": 99.0,
                "selection_eligible": False,
                "veto_reason": "test_veto",
                "consequences": {
                    "operation": "collect_combat_reward",
                    "reward_type": "gold",
                    "gold_delta": 20,
                },
            },
            {
                "id": "reward:potion:1",
                "choice_id": "reward:potion:1",
                "choice_index": 1,
                "action": "choose",
                "operation": None,
                "semantic_id": "reward:potion:1",
                "score": 3.0,
                "selection_eligible": True,
                "veto_reason": None,
                "consequences": {
                    "operation": "collect_combat_reward",
                    "reward_type": "potion",
                },
            },
            {
                "id": "action:proceed",
                "choice_id": "action:proceed",
                "choice_index": None,
                "action": "proceed",
                "operation": None,
                "semantic_id": "proceed",
                "score": 0.0,
                "selection_eligible": True,
                "veto_reason": None,
                "consequences": {"operation": "proceed"},
            },
        ]

        def canonical(producer, *, choice_id, target):
            return {
                "choice_id": choice_id,
                "choice_index": producer["choice_index"],
                "action": producer["action"],
                "operation": producer["operation"],
                "semantic_id": producer["semantic_id"],
                "target": copy.deepcopy(target),
                "candidate_ids": [producer["choice_id"]],
                "candidate_binding": "unique",
                "producer_candidate_raw": copy.deepcopy(producer),
            }

        choices = [
            canonical(
                producers[0], choice_id=options[0]["option_id"],
                target=options[0]["target"],
            ),
            canonical(
                producers[1], choice_id=options[1]["option_id"],
                target=options[1]["target"],
            ),
            canonical(
                producers[2], choice_id="action:proceed",
                target={"kind": "protocol_action", "action": "proceed"},
            ),
        ]
        record = decision(
            action="choose",
            phase="COMBAT_REWARD",
            decision={
                "reason": "bounded_reward_utility",
                "chosen": "reward:potion:1",
                "candidates": copy.deepcopy(producers),
            },
            chosen_option_before=copy.deepcopy(options[1]),
            available_options_before=copy.deepcopy(options),
            available_commands_before=["choose", "proceed"],
            legal_choices_before=copy.deepcopy(choices),
            selected_choice_ids=[options[1]["option_id"]],
            final_choice_ids=[options[1]["option_id"]],
            requested_target_id=options[1]["option_id"],
            resolved_target_id=options[1]["option_id"],
        )

        report = strategy_audit.audit_records([record], "new")
        self.assertNotIn(
            "noncombat_candidate_evidence_incomplete",
            report["review_finding_counts"],
        )
        self.assertNotIn(
            "noncombat_candidate_argmax_missed", report["issue_counts"],
        )
        self.assertEqual(
            1,
            report["audit_coverage"]["noncombat_candidate_selection"][
                "evaluated"
            ],
        )

        tampered = copy.deepcopy(record)
        tampered["legal_choices_before"][0]["producer_candidate_raw"][
            "score"
        ] = 1000.0
        tampered_report = strategy_audit.audit_records([tampered], "new")
        finding = next(
            item for item in tampered_report["review_findings"]
            if item["kind"] == "noncombat_candidate_evidence_incomplete"
        )
        self.assertIn(
            "canonical_producer_raw_binding:0",
            finding["canonical_surface_violations"],
        )

    def test_sapphire_key_only_competes_with_its_linked_relic(self):
        """Gold is sequentially collectible, not an alternative to the key."""

        options = [
            {
                "option_id": "option:gold", "choice_index": 0,
                "label": "gold",
                "target": {
                    "kind": "reward",
                    "reward": {
                        "choice_index": 0, "reward_type": "GOLD",
                        "gold": 78,
                    },
                },
            },
            {
                "option_id": "option:linked", "choice_index": 1,
                "label": "relic",
                "target": {
                    "kind": "reward",
                    "reward": {
                        "choice_index": 1, "reward_type": "RELIC",
                        "relic": {"id": "Darkstone Periapt"},
                    },
                },
            },
            {
                "option_id": "option:key", "choice_index": 2,
                "label": "sapphire_key",
                "target": {
                    "kind": "sapphire_key",
                    "reward": {
                        "choice_index": 2, "reward_type": "SAPPHIRE_KEY",
                        "link": {"id": "Darkstone Periapt"},
                    },
                    "link": {"id": "Darkstone Periapt"},
                },
            },
        ]

        def record_for(key_score):
            producers = [
                {
                    "id": "reward:gold:0", "choice_id": "reward:gold:0",
                    "choice_index": 0, "action": "choose", "operation": None,
                    "semantic_id": "reward:gold:0", "score": 78.0,
                    "selection_eligible": True,
                    "consequences": {"operation": "collect_independent_reward"},
                },
                {
                    "id": "reward:relic:1", "choice_id": "reward:relic:1",
                    "choice_index": 1, "action": "choose", "operation": None,
                    "semantic_id": "reward:relic:1", "score": 8.0,
                    "selection_eligible": True,
                    "consequences": {"operation": "gain_linked_relic"},
                },
                {
                    "id": "reward:sapphire_key:2",
                    "choice_id": "reward:sapphire_key:2",
                    "choice_index": 2, "action": "choose", "operation": None,
                    "semantic_id": "reward:sapphire_key:2",
                    "score": float(key_score), "selection_eligible": True,
                    "consequences": {"operation": "gain_sapphire_key"},
                },
            ]

            choices = [
                {
                    "choice_id": option["option_id"],
                    "choice_index": producer["choice_index"],
                    "action": "choose", "operation": None,
                    "semantic_id": producer["semantic_id"],
                    "target": copy.deepcopy(option["target"]),
                    "candidate_ids": [producer["choice_id"]],
                    "candidate_binding": "unique",
                    "producer_candidate_raw": copy.deepcopy(producer),
                }
                for option, producer in zip(options, producers)
            ]
            return decision(
                action="choose", phase="SAPPHIRE_KEY", goal_mode="HEART",
                decision={
                    "reason": "heart_plan_sapphire_opportunity_cost",
                    "chosen": "reward:sapphire_key:2",
                    "candidates": copy.deepcopy(producers),
                },
                chosen_option_before=copy.deepcopy(options[2]),
                available_options_before=copy.deepcopy(options),
                available_commands_before=["choose", "proceed"],
                legal_choices_before=choices,
                selected_choice_ids=["option:key"],
                final_choice_ids=["option:key"],
                requested_target_id="option:key",
                resolved_target_id="option:key",
                authoritative_state_before={
                    "game_state": {"has_sapphire_key": False},
                },
                authoritative_state_after={
                    "game_state": {"has_sapphire_key": True},
                },
            )

        accepted = strategy_audit.audit_records([record_for(30)], "new")
        self.assertNotIn(
            "noncombat_candidate_argmax_missed", accepted["issue_counts"]
        )

        rejected = strategy_audit.audit_records([record_for(7)], "new")
        finding = next(
            item for item in rejected["issues"]
            if item["kind"] == "noncombat_candidate_argmax_missed"
        )
        self.assertEqual("sapphire_key_linked_relic", finding["selection_scope"])
        self.assertEqual(["reward:relic:1"], finding["local_best_candidate_ids"])

    def test_model_replay_uses_bound_sapphire_surface_score(self):
        rows = strategy_audit._candidate_rows({
            "decision": {
                "candidates": [{
                    "choice_id": "reward:sapphire_key:2", "score": 78.0,
                }],
                "model_advice": {"replay": {"candidates": [{
                    "candidate_id": "reward:sapphire_key:2",
                    "local_score": 30.0,
                    "decision_surface_score": 78.0,
                }]}},
            },
        })

        self.assertEqual([78.0, 78.0], [row["score"] for row in rows])

    def test_hand_select_canonical_proceed_maps_exactly_to_confirm(self):
        producer = {
            "id": "action:proceed",
            "choice_id": "action:proceed",
            "choice_index": None,
            "action": "proceed",
            "operation": None,
            "semantic_id": "proceed",
            "score": 0.0,
            "selection_eligible": True,
            "veto_reason": None,
            "consequences": {"operation": "proceed"},
        }
        canonical = {
            "choice_id": "action:proceed",
            "choice_index": None,
            "action": "proceed",
            "operation": None,
            "semantic_id": "proceed",
            "raw_text": "confirm",
            "target": {"kind": "protocol_action", "action": "proceed"},
            "candidate_ids": ["action:proceed"],
            "candidate_binding": "unique",
            "producer_candidate_raw": copy.deepcopy(producer),
        }
        record = {
            "phase": "HAND_SELECT",
            "available_options_before": [],
            "available_commands_before": ["choose", "confirm"],
            "legal_choices_before": [canonical],
            "decision": {"candidates": [producer]},
        }

        choices, violations = strategy_audit._canonical_noncombat_surface(
            record
        )

        self.assertEqual([canonical], choices)
        self.assertEqual([], violations)

        forged = copy.deepcopy(record)
        forged["legal_choices_before"][0]["raw_text"] = "proceed"
        _choices, forged_violations = (
            strategy_audit._canonical_noncombat_surface(forged)
        )
        self.assertIn(
            "canonical_command_binding:0", forged_violations
        )

        wrong_phase = copy.deepcopy(record)
        wrong_phase["phase"] = "EVENT"
        _choices, wrong_phase_violations = (
            strategy_audit._canonical_noncombat_surface(wrong_phase)
        )
        self.assertIn(
            "canonical_command_binding:0", wrong_phase_violations
        )

    def test_scored_event_candidate_dicts_bind_every_visible_option(self):
        options = [
            {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option", "event_id": "Golden Idol",
                    "label": label, "text": label,
                },
            }
            for index, label in enumerate(("Take", "Leave"))
        ]
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "golden_idol_signed_resource_trade",
                "chosen_index": 0,
                "candidate_contract": {
                    "score_source": "explicit_local_utility",
                    "strategy_quality_auditable": True,
                    "all_visible_options_scored": True,
                },
                "candidates": [
                    {
                        "id": 0, "score": 8.0, "label": "Take",
                        "text": "Take", "consequences": {"relic_delta": 1},
                    },
                    {
                        "id": 1, "score": 1.0, "label": "Leave",
                        "text": "Leave", "consequences": {"leave": True},
                    },
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "noncombat_candidate_evidence_incomplete",
            report["review_finding_counts"],
        )
        self.assertNotIn(
            "noncombat_candidate_consequences_incomplete",
            report["review_finding_counts"],
        )
        self.assertEqual("clear", report["strategy_quality"]["status"])

    def test_grid_multi_select_accepts_bound_local_top_n_aliases(self):
        def card_option(index, card_id, instance_id):
            return {
                "option_id": f"option:{index}",
                "choice_index": index,
                "label": card_id,
                "target": {
                    "kind": "card",
                    "card_instance_id": instance_id,
                    "card": {
                        "id": card_id, "name": card_id,
                        "card_instance_id": instance_id,
                    },
                },
            }

        weak = card_option(0, "Weak", "uuid-weak")
        second = card_option(1, "Second", "uuid-second")
        strong = card_option(2, "Strong", "uuid-strong")
        record = decision(
            action="choose",
            phase="GRID",
            decision={
                "reason": "grid_remove_or_transform_weakest",
                "chosen": ["Weak", "Second"],
                "candidate_contract": {
                    "score_source": "explicit",
                    "strategy_quality_auditable": True,
                },
                "candidates": [
                    {"id": "Weak", "score": 30.0},
                    {"id": "Second", "score": 20.0},
                    {"id": "Strong", "score": 10.0},
                ],
                "model_advice": {
                    "status": "skipped", "applied": False,
                    "final_choice_ids": [
                        "grid:uuid-weak", "grid:uuid-second",
                    ],
                    "replay": {
                        "selection_count": 2,
                        "candidates": [
                            {
                                "candidate_id": "grid:uuid-weak",
                                "local_score": 30.0,
                            },
                            {
                                "candidate_id": "grid:uuid-second",
                                "local_score": 20.0,
                            },
                            {
                                "candidate_id": "grid:uuid-strong",
                                "local_score": 10.0,
                            },
                        ],
                    },
                },
            },
            chosen_option_before=second,
            available_options_before=[weak, second, strong],
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "noncombat_candidate_argmax_missed", report["issue_counts"]
        )
        self.assertNotIn(
            "noncombat_candidate_evidence_incomplete",
            report["review_finding_counts"],
        )

        continuation = copy.deepcopy(record)
        continuation["before_seq"] = 11
        continuation["chosen_option_before"] = second
        continuation["decision"]["model_advice"] = {}
        record["after_seq"] = 11
        rows = [
            {
                "candidate_id": "grid:uuid-weak",
                "choice_id": "grid:uuid-weak",
                "option_id": "option:0",
            },
            {
                "candidate_id": "grid:uuid-second",
                "choice_id": "grid:uuid-second",
                "option_id": "option:1",
            },
            {
                "candidate_id": "grid:uuid-strong",
                "choice_id": "grid:uuid-strong",
                "option_id": "option:2",
            },
        ]
        selected = strategy_audit._selected_candidate_rows(
            continuation, rows, record
        )
        self.assertEqual(
            {"grid:uuid-weak", "grid:uuid-second"},
            {row["candidate_id"] for row in selected},
        )

    def test_inconsistent_curse_probability_and_omamori_math_is_flagged(self):
        options = [
            {
                "option_id": "option:0", "choice_index": 0,
                "label": "Open",
                "target": {
                    "kind": "event_option", "event_id": "Mausoleum",
                    "label": "Open",
                    "text": "Gain a relic. 50%: become cursed.",
                },
            },
            {
                "option_id": "option:1", "choice_index": 1,
                "label": "Leave",
                "target": {
                    "kind": "event_option", "event_id": "Mausoleum",
                    "label": "Leave", "text": "Leave.",
                },
            },
        ]
        wrong = {
            "curse_delta": 1,
            "raw_curse_delta": 1,
            "omamori_prevented_curse_delta": 0,
            "effective_curse_delta": 1,
            "curse_probability": 0.5,
            # The exact expected value is 0.5, not 0.9.
            "expected_effective_curse_delta": 0.9,
            "expected_omamori_charge_use": 0.0,
            "omamori_charges_before": 0,
            "omamori_charges_after_if_triggered": 0,
            "expected_omamori_charges_after": 0.0,
        }
        record = decision(
            action="choose",
            phase="EVENT",
            decision={
                "reason": "event_semantic_outcome_score",
                "chosen_index": 0,
                "candidates": [
                    {"id": "0", "score": 7.0, "consequences": wrong},
                    {"id": "1", "score": 1.0, "consequences": {}},
                ],
            },
            chosen_option_before=options[0],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "event_curse_probability_inconsistent"
            ],
        )
        self.assertEqual("issues", report["strategy_quality"]["status"])

    def test_mind_bloom_reproducible_totals_still_fail_when_rewards_are_unpriced(self):
        options = [
            {
                "option_id": f"option:{index}", "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option", "event_id": "MindBloom",
                    "label": label, "text": text,
                },
            }
            for index, (label, text) in enumerate((
                ("I am War", "Fight a boss and gain a rare relic."),
                ("I am Awake", "Upgrade all cards. You cannot heal."),
                ("I am Rich", "Gain 999 gold and two Normality curses."),
            ))
        ]

        def collapsed(index, score, consequences):
            return {
                "id": str(index),
                "score": score,
                "consequences": consequences,
                "score_rule_id": "signed_post_state_utility_v1",
                "score_formula": {"kind": "sum_components_v1"},
                "score_inputs": {"signed_post_state_utility": score},
                "score_components": [{
                    "name": "signed_post_state_utility",
                    "input": "signed_post_state_utility",
                    "coefficient": 1.0,
                    "value": score,
                }],
            }

        curse = {
            "curse_delta": 2,
            "raw_curse_delta": 2,
            "effective_curse_delta": 2,
            "curse_probability": 1.0,
            "expected_effective_curse_delta": 2.0,
            "expected_omamori_charge_use": 0.0,
            "omamori_charges_before": 0,
            "omamori_charges_after_if_triggered": 0,
            "expected_omamori_charges_after": 0.0,
            "omamori_prevented_curse_delta": 0,
        }
        record = decision(
            action="choose",
            phase="EVENT",
            event_id="MindBloom",
            decision={
                "reason": "mind_bloom_signed_post_state",
                "chosen_index": 1,
                "candidates": [
                    collapsed(0, -6.677, {
                        "rare_relic_delta": 1,
                        "combat_rewards": True,
                        "gold_delta": 50,
                    }),
                    collapsed(1, 15.96, {
                        "upgraded_card_delta": 18,
                        "healing_locked": True,
                    }),
                    collapsed(2, 2.074, {"gold_delta": 999, **curse}),
                ],
            },
            chosen_option_before=options[1],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertEqual(
            1,
            report["issue_counts"][
                "mind_bloom_value_components_incomplete"
            ],
        )
        issue = next(
            item for item in report["issues"]
            if item["kind"] == "mind_bloom_value_components_incomplete"
        )
        missing = {
            candidate["candidate_id"]: set(
                candidate["missing_score_concepts"]
            )
            for candidate in issue["candidates"]
        }
        self.assertIn("rare_relic_reward", missing["0"])
        self.assertIn("upgrade_package", missing["1"])
        self.assertIn("gold_reward", missing["2"])

    def test_mind_bloom_decomposed_reward_and_cost_terms_pass_semantic_audit(self):
        options = [
            {
                "option_id": f"option:{index}", "choice_index": index,
                "label": label,
                "target": {
                    "kind": "event_option", "event_id": "MindBloom",
                    "label": label, "text": label,
                },
            }
            for index, label in enumerate((
                "I am War", "I am Awake", "I am Rich",
            ))
        ]

        def decomposed(index, score, rule_id, inputs, signs, consequences):
            components = [
                {
                    "name": name,
                    "input": name,
                    "coefficient": coefficient,
                    "value": inputs[name] * coefficient,
                }
                for name, coefficient in signs
            ]
            return {
                "id": str(index), "score": score,
                "consequences": consequences,
                "score_rule_id": rule_id,
                "score_formula": {"kind": "sum_components_v1"},
                "score_inputs": inputs,
                "score_components": components,
            }

        curse = {
            "curse_delta": 2,
            "raw_curse_delta": 2,
            "effective_curse_delta": 2,
            "curse_probability": 1.0,
            "expected_effective_curse_delta": 2.0,
            "expected_omamori_charge_use": 0.0,
            "omamori_charges_before": 0,
            "omamori_charges_after_if_triggered": 0,
            "expected_omamori_charges_after": 0.0,
            "omamori_prevented_curse_delta": 0,
        }
        candidates = [
            decomposed(
                0, 5.0, "mind_bloom_war_expected_utility_v2",
                {
                    "rare_relic_expected_value": 23.0,
                    "combat_gold_expected_value": 7.0,
                    "fight_readiness_credit": 8.0,
                    "low_hp_penalty": 21.0,
                    "weak_deck_penalty": 12.0,
                },
                (
                    ("rare_relic_expected_value", 1.0),
                    ("combat_gold_expected_value", 1.0),
                    ("fight_readiness_credit", 1.0),
                    ("low_hp_penalty", -1.0),
                    ("weak_deck_penalty", -1.0),
                ),
                {
                    "rare_relic_delta": 1,
                    "combat_rewards": True,
                    "future_combat_gold": 50,
                },
            ),
            decomposed(
                1, 10.0, "mind_bloom_awake_expected_utility_v2",
                {
                    "estimated_upgrade_benefit": 50.0,
                    "healing_lock_opportunity_cost": 40.0,
                },
                (
                    ("estimated_upgrade_benefit", 1.0),
                    ("healing_lock_opportunity_cost", -1.0),
                ),
                {"upgraded_card_delta": 18, "healing_locked": True},
            ),
            decomposed(
                2, -10.0, "mind_bloom_rich_expected_utility_v2",
                {
                    "gold_expected_value": 32.0,
                    "curse_opportunity_cost": 42.0,
                },
                (
                    ("gold_expected_value", 1.0),
                    ("curse_opportunity_cost", -1.0),
                ),
                {"gold_delta": 999, **curse},
            ),
        ]
        record = decision(
            action="choose", phase="EVENT", event_id="MindBloom",
            decision={
                "reason": "mind_bloom_signed_post_state",
                "chosen_index": 1,
                "candidates": candidates,
            },
            chosen_option_before=options[1],
            available_options_before=options,
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "mind_bloom_value_components_incomplete",
            report["issue_counts"],
        )
        pricing = report["audit_coverage"][
            "mind_bloom_value_component_pricing"
        ]
        self.assertEqual(3, pricing["eligible"])
        self.assertEqual(3, pricing["evaluated"])
        self.assertEqual(0, pricing["violations"])

    def test_attempt_api_reads_only_current_contiguous_trace_suffix(self):
        old_start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "old",
            "attempt_id": "attempt-old",
        }
        new_start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
        }
        current = decision(
            attempt_id="attempt-new",
            energy_before=0,
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
            decision_outcome={"hp_delta": -8},
        )
        terminal = {
            "record_type": "terminal_result",
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
            "termination_kind": "game_over",
        }
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "autoplay.log"
            trace.write_text(
                "malformed historical data that must not be read\n"
                + json.dumps(old_start) + "\n"
                + json.dumps(new_start) + "\n"
                + json.dumps(current) + "\n"
                + json.dumps(terminal) + "\n",
                encoding="utf-8",
            )

            report = strategy_audit.audit_attempt_trace(
                trace, "new", "attempt-new"
            )

        self.assertEqual(1, report["records"])
        self.assertEqual("attempt-new", report["attempt_id"])

    def test_foreign_auxiliary_after_terminal_is_reported_not_stale(self):
        start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
        }
        current = decision(
            attempt_id="attempt-new",
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
        )
        terminal = {
            "record_type": "terminal_result",
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
            "termination_kind": "game_over",
        }
        escaped = [
            {
                "record_type": record_type,
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": f"synthetic-{index}",
                "advisor": {},
            }
            for index, record_type in enumerate(
                ("cache_warmup", "model_advice"), 1
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "autoplay.log"
            trace.write_text(
                "\n".join(json.dumps(item) for item in (
                    start, current, terminal, *escaped,
                )) + "\n",
                encoding="utf-8",
            )

            report = strategy_audit.audit_attempt_trace(
                trace, "new", "attempt-new"
            )

        self.assertEqual(2, report["issue_counts"][
            "auxiliary_trace_pollution"
        ])
        self.assertEqual("issues", report["protocol_correctness"]["status"])
        self.assertEqual(
            2
            + report["independent_oracle"]["issue_count"]
            + report["death_replay"]["issue_count"],
            report["issue_count"],
        )

    def test_foreign_authoritative_after_terminal_still_marks_attempt_stale(self):
        records = [
            {
                "record_type": "controller_start",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-new",
            },
            decision(attempt_id="attempt-new"),
            {
                "record_type": "terminal_result",
                "schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "attempt-new",
                "termination_kind": "game_over",
            },
            {
                "record_type": "decision",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "newer-real-attempt",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "autoplay.log"
            trace.write_text(
                "\n".join(json.dumps(item) for item in records) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                strategy_audit.TraceAuditError,
                "not the current trace segment",
            ):
                strategy_audit.audit_attempt_trace(
                    trace, "new", "attempt-new"
                )

    def test_attempt_sidecar_is_preferred_over_polluted_shared_trace(self):
        attempt_id = "attempt-sidecar"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "autoplay.log"
            shared.write_text(json.dumps({
                "record_type": "decision",
                "decision_schema_version": 2,
                "decision_hash": "new",
                "attempt_id": "newer-real-attempt",
            }) + "\n", encoding="utf-8")
            sidecar = (
                root / "logs" / "attempts" / attempt_id / "autoplay.log"
            )
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text(
                "\n".join(json.dumps(item) for item in (
                    {
                        "record_type": "controller_start",
                        "decision_schema_version": 2,
                        "decision_hash": "new",
                        "attempt_id": attempt_id,
                    },
                    decision(attempt_id=attempt_id),
                    {
                        "record_type": "terminal_result",
                        "schema_version": 2,
                        "decision_hash": "new",
                        "attempt_id": attempt_id,
                        "termination_kind": "game_over",
                    },
                )) + "\n",
                encoding="utf-8",
            )

            report = strategy_audit.audit_attempt_trace(
                shared, "new", attempt_id
            )

        self.assertEqual(attempt_id, report["attempt_id"])
        self.assertNotIn(
            "auxiliary_trace_pollution", report["issue_counts"]
        )

    def test_attempt_api_rejects_active_suffix_without_terminal_result(self):
        start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
        }
        current = decision(attempt_id="attempt-new")
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "autoplay.log"
            trace.write_text(
                json.dumps(start) + "\n" + json.dumps(current) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                strategy_audit.TraceAuditError, "missing terminal_result"
            ):
                strategy_audit.audit_attempt_trace(
                    trace, "new", "attempt-new"
                )

    def test_bound_protocol_and_model_hash_mismatches_reject_attempt_suffix(self):
        start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
        }
        current = decision(attempt_id="attempt-new")
        wrong_protocol = {
            "record_type": "protocol_event",
            "decision_schema_version": 2,
            "decision_hash": "stale",
            "attempt_id": "attempt-new",
            "event": "receipt_timeout",
        }
        wrong_model = {
            "record_type": "model_advice",
            "decision_schema_version": 2,
            "decision_hash": "stale",
            "attempt_id": "attempt-new",
            "advisor": {"status": "applied"},
        }
        terminal = {
            "record_type": "terminal_result",
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
            "termination_kind": "game_over",
        }
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "autoplay.log"
            trace.write_text(
                "\n".join(json.dumps(item) for item in (
                    start, current, wrong_protocol, wrong_model, terminal,
                )) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                strategy_audit.TraceAuditError,
                "mixes decision hashes",
            ):
                strategy_audit.audit_attempt_trace(
                    trace, "new", "attempt-new"
                )

    def test_attempt_suffix_rejects_non_object_and_unbound_records(self):
        start = {
            "record_type": "controller_start",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
        }
        terminal = {
            "record_type": "terminal_result",
            "schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
            "termination_kind": "game_over",
        }
        invalid_rows = (
            (42, "non-object record"),
            ({"record_type": "future_unbound"}, "unbound record"),
        )
        for invalid, message in invalid_rows:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                trace = Path(directory) / "autoplay.log"
                trace.write_text(
                    "\n".join(
                        json.dumps(item)
                        for item in (start, invalid, terminal)
                    ) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    strategy_audit.TraceAuditError, message,
                ):
                    strategy_audit.audit_attempt_trace(
                        trace, "new", "attempt-new"
                    )

    def test_oracle_contract_rejects_wrong_version_and_missing_coverage(self):
        wrong_version = clear_oracle_report(oracle_version="producer-forged")
        missing_coverage = clear_oracle_report()
        missing_coverage["coverage"].pop("candidate_consequences")

        for value, field in (
            (wrong_version, "oracle_version"),
            (missing_coverage, "coverage.missing"),
        ):
            with self.subTest(field=field):
                normalized = strategy_audit._normalize_external_audit(
                    "independent_oracle", value, disagreements=True,
                )
                self.assertEqual("issues", normalized["status"])
                self.assertTrue(any(
                    item.get("kind") == (
                        "independent_oracle_result_contract_violation"
                    )
                    and item.get("field") == field
                    for item in normalized["issues"]
                ), normalized)

    def test_oracle_contract_rejects_missing_or_mutated_mechanism_receipt(self):
        complete = clear_oracle_report()
        complete["base_game_mechanisms"].update({
            "eligible": 1,
            "evaluated": 1,
            "observations": [{
                "mechanism": "busted_crown_card_reward_count",
                "record_index": 4,
                "status": "clear",
            }],
        })
        self.assertEqual(
            "clear",
            strategy_audit._normalize_external_audit(
                "independent_oracle", complete, disagreements=True,
            )["status"],
        )

        missing = clear_oracle_report()
        missing.pop("base_game_mechanisms")
        wrong_count = clear_oracle_report()
        wrong_count["base_game_mechanisms"]["evaluated"] = 1
        deleted_observation = json.loads(json.dumps(complete))
        deleted_observation["base_game_mechanisms"]["observations"] = []
        wrong_version = clear_oracle_report()
        wrong_version["base_game_mechanisms"]["contract_version"] += 1

        for value, expected_field in (
            (missing, "base_game_mechanisms"),
            (wrong_count, "base_game_mechanisms.evaluated"),
            (deleted_observation, "base_game_mechanisms.eligible"),
            (
                wrong_version,
                "base_game_mechanisms.contract_version",
            ),
        ):
            with self.subTest(field=expected_field):
                normalized = strategy_audit._normalize_external_audit(
                    "independent_oracle", value, disagreements=True,
                )
                self.assertEqual("issues", normalized["status"])
                self.assertTrue(any(
                    item.get("kind") == (
                        "independent_oracle_result_contract_violation"
                    )
                    and item.get("field") == expected_field
                    for item in normalized["issues"]
                ), normalized)

    def test_oracle_mechanism_receipt_conserves_issues_and_unknowns(self):
        mechanism_issue = {
            "kind": "stasis_transition_mismatch",
            "mechanism": "bronze_orb_stasis",
            "record_index": 7,
        }
        mechanism_unknown = {
            "kind": "omamori_counter_or_deck_missing",
            "mechanism": "omamori_curse_prevention",
            "record_index": 9,
        }
        report = clear_oracle_report(
            status="issues",
            issue_count=1,
            eligible_unknown_count=1,
            disagreement_count=1,
            issues=[dict(mechanism_issue)],
            unknowns=[dict(mechanism_unknown)],
        )
        report["base_game_mechanisms"].update({
            "eligible": 2,
            "evaluated": 0,
            "issues": [dict(mechanism_issue)],
            "unknowns": [dict(mechanism_unknown)],
            "observations": [
                {
                    "mechanism": "bronze_orb_stasis",
                    "record_index": 7,
                    "status": "issues",
                },
                {
                    "mechanism": "omamori_curse_prevention",
                    "record_index": 9,
                    "status": "inconclusive",
                },
            ],
        })
        normalized = strategy_audit._normalize_external_audit(
            "independent_oracle", report, disagreements=True,
        )
        self.assertFalse(any(
            item.get("kind") == (
                "independent_oracle_result_contract_violation"
            )
            for item in normalized["issues"]
        ), normalized)

        for field in ("issues", "unknowns"):
            with self.subTest(field=field):
                mutated = json.loads(json.dumps(report))
                mutated["base_game_mechanisms"][field] = []
                rejected = strategy_audit._normalize_external_audit(
                    "independent_oracle", mutated, disagreements=True,
                )
                self.assertTrue(any(
                    item.get("kind") == (
                        "independent_oracle_result_contract_violation"
                    )
                    and item.get("field") in {
                        "base_game_mechanisms.conservation",
                        (
                            "base_game_mechanisms."
                            f"{field}.observation_bindings"
                        ),
                    }
                    for item in rejected["issues"]
                ), rejected)

    def test_oracle_contract_rejects_forged_producer_blind_review(self):
        report = clear_oracle_report(blind_reviews=[{
            "review_version": "independent-blind-consequence-v1",
            "status": "clear",
            "recommended_choice_id": "producer-choice",
            "reason": "producer says this is best",
        }])

        normalized = strategy_audit._normalize_external_audit(
            "independent_oracle", report, disagreements=True,
        )

        self.assertEqual("issues", normalized["status"])
        violated_fields = {
            item.get("field") for item in normalized["issues"]
            if item.get("kind") == (
                "independent_oracle_result_contract_violation"
            )
        }
        self.assertIn("blind_reviews.0.review_authority", violated_fields)
        self.assertIn("blind_reviews.0.evidence_scope", violated_fields)
        self.assertIn("blind_reviews.0.input_fields", violated_fields)
        self.assertIn("blind_reviews.0.excluded_fields", violated_fields)

    def test_unknown_record_type_is_blocking_and_report_counts_are_explicit(self):
        current = decision(
            attempt_id="attempt-new",
            act=2,
            projected_hp_loss_before=8,
            projected_attack_hp_loss_before=8,
        )
        unknown = {
            "record_type": "future_unclassified_trace",
            "decision_schema_version": 2,
            "decision_hash": "new",
            "attempt_id": "attempt-new",
            "act": 3,
        }

        report = strategy_audit.audit_records(
            [current, unknown], "new", attempt_id="attempt-new"
        )

        self.assertEqual(2, report["schema_version"])
        self.assertEqual(["future_unclassified_trace"], report[
            "unknown_record_types"
        ])
        self.assertIn("decision", report["known_record_types"])
        self.assertEqual(3, report["observed_max_act"])
        self.assertEqual(1, report["issue_count"])
        self.assertEqual(
            len(report["review_findings"]),
            report["review_finding_count"],
        )
        self.assertEqual(
            sum(
                check["unknown"]
                for check in report["audit_coverage"].values()
            ),
            report["eligible_unknown_count"],
        )
        self.assertEqual("issues", report["protocol_correctness"]["status"])
        self.assertFalse(report["release_gate_passed"])

    def test_model_conflict_report_classifies_final_resolution(self):
        def advised(seq, decision_type, status, final_ids):
            return decision(
                before_seq=seq,
                action="choose",
                decision={
                    "model_advice": {
                        "decision_type": decision_type,
                        "status": status,
                        "applied": status == "applied",
                        "rule_choice_id": "local",
                        "model_choice_id": "model",
                        "final_choice_ids": final_ids,
                        "replay": {
                            "rule_choice_ids": ["local"],
                            "candidates": [
                                {
                                    "candidate_id": "local",
                                    "local_score": 10,
                                },
                                {
                                    "candidate_id": "model",
                                    "local_score": 9,
                                },
                                {
                                    "candidate_id": "third",
                                    "local_score": 8,
                                },
                            ],
                        },
                    },
                },
            )

        records = [
            advised(1, "EVENT", "applied", ["model"]),
            advised(2, "EVENT", "regret_cap", ["local"]),
            advised(3, "CARD_REWARD", "applied", ["third"]),
            advised(4, "CARD_REWARD", "shadow", []),
        ]

        model = strategy_audit.audit_records(
            records, "new"
        )["model_advice"]

        self.assertEqual(4, model["conflicts"])
        self.assertEqual(
            {"model": 1, "local": 1, "third": 1, "unknown": 1},
            model["conflict_outcome_counts"],
        )
        self.assertEqual(2, model["by_decision_type"]["EVENT"]["conflicts"])
        self.assertEqual(
            {"applied": 2},
            model["by_status"]["applied"]["status_counts"],
        )

    def test_attempt_audit_missing_independent_oracle_is_blocking(self):
        records = [{
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": True,
            "current_hp": 1,
        }]
        replay_module = SimpleNamespace(
            build_death_replay=lambda rows, terminal=None: (
                not_applicable_replay_report()
            )
        )
        with patch.object(
            strategy_audit,
            "load_attempt_trace_suffix",
            return_value=records,
        ) as loader, patch.object(
            strategy_audit,
            "audit_records",
            return_value=clear_core_audit_report(),
        ), patch.object(
            strategy_audit, "_independent_oracle", None
        ), patch.object(
            strategy_audit, "_death_replay", replay_module
        ), patch.object(
            strategy_audit,
            "_attempt_audit_receipt",
            return_value=clear_audit_receipt(),
        ):
            report = strategy_audit.audit_attempt_trace(
                "unused.jsonl", "hash", "attempt"
            )

        loader.assert_called_once_with(
            "unused.jsonl", "hash", "attempt"
        )
        self.assertEqual(
            "independent_oracle_missing",
            report["independent_oracle"]["issues"][0]["kind"],
        )
        self.assertEqual(1, report["issue_count"])
        self.assertEqual("issues", report["audit_status"])
        self.assertFalse(report["release_gate_passed"])

    def test_attempt_audit_loads_once_and_shares_records_with_both_channels(self):
        records = [{
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": True,
            "current_hp": 1,
        }]
        observed = {}

        def oracle_audit(rows, decision_hash, attempt_id, artifacts=None):
            observed["oracle"] = rows
            return clear_oracle_report()

        def replay_audit(rows, terminal=None):
            observed["replay"] = rows
            observed["terminal"] = terminal
            return not_applicable_replay_report()

        oracle_module = SimpleNamespace(audit_records=oracle_audit)
        replay_module = SimpleNamespace(build_death_replay=replay_audit)
        with patch.object(
            strategy_audit,
            "load_attempt_trace_suffix",
            return_value=records,
        ) as loader, patch.object(
            strategy_audit,
            "audit_records",
            return_value=clear_core_audit_report(),
        ), patch.object(
            strategy_audit, "_independent_oracle", oracle_module
        ), patch.object(
            strategy_audit, "_death_replay", replay_module
        ), patch.object(
            strategy_audit,
            "_attempt_audit_receipt",
            return_value=clear_audit_receipt(),
        ):
            report = strategy_audit.audit_attempt_trace(
                "unused.jsonl", "hash", "attempt"
            )

        self.assertEqual(1, loader.call_count)
        self.assertIs(records, observed["oracle"])
        self.assertIs(records, observed["replay"])
        self.assertIs(records[0], observed["terminal"])
        self.assertEqual("clear", report["audit_status"])
        self.assertTrue(report["release_gate_passed"])

        # The final gate independently validates the nested receipt even if a
        # caller bypasses the external-channel normalizer.
        with_observation = json.loads(json.dumps(report))
        with_observation["independent_oracle"][
            "base_game_mechanisms"
        ].update({
            "eligible": 1,
            "evaluated": 1,
            "observations": [{
                "mechanism": "busted_crown_card_reward_count",
                "record_index": 4,
                "status": "clear",
            }],
        })
        observable = with_observation["independent_oracle"]["coverage"][
            "observable_state_deltas"
        ]
        observable.update({
            "eligible": 1, "evaluated": 1, "status": "clear",
        })
        self.assertTrue(
            strategy_audit._release_gate_passed(with_observation)
        )

        # A report assembled by an older auditor without the dedicated
        # combat channel cannot silently regain a clear release gate.
        missing_combat_channel = json.loads(json.dumps(with_observation))
        missing_combat_channel.pop("combat_strategy_review", None)
        self.assertFalse(
            strategy_audit._release_gate_passed(missing_combat_channel)
        )

        gate_mutations = []
        missing_receipt = json.loads(json.dumps(with_observation))
        missing_receipt["independent_oracle"].pop(
            "base_game_mechanisms"
        )
        gate_mutations.append(missing_receipt)
        changed_count = json.loads(json.dumps(with_observation))
        changed_count["independent_oracle"]["base_game_mechanisms"][
            "evaluated"
        ] = 0
        gate_mutations.append(changed_count)
        deleted_observation = json.loads(json.dumps(with_observation))
        deleted_observation["independent_oracle"][
            "base_game_mechanisms"
        ]["observations"] = []
        gate_mutations.append(deleted_observation)
        for mutated in gate_mutations:
            self.assertFalse(strategy_audit._release_gate_passed(mutated))

    def test_attempt_audit_oracle_disagreement_is_merged_and_blocking(self):
        records = [{
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": True,
            "current_hp": 1,
        }]
        disagreement = clear_oracle_report(
            status="issues",
            issue_count=1,
            disagreement_count=1,
            issues=[{"kind": "observable_delta_mismatch"}],
        )
        oracle_module = SimpleNamespace(
            audit_records=lambda rows, decision_hash, attempt_id, artifacts=None: disagreement
        )
        replay_module = SimpleNamespace(
            build_death_replay=lambda rows, terminal=None: (
                not_applicable_replay_report()
            )
        )
        with patch.object(
            strategy_audit,
            "load_attempt_trace_suffix",
            return_value=records,
        ), patch.object(
            strategy_audit,
            "audit_records",
            return_value=clear_core_audit_report(),
        ), patch.object(
            strategy_audit, "_independent_oracle", oracle_module
        ), patch.object(
            strategy_audit, "_death_replay", replay_module
        ):
            report = strategy_audit.audit_attempt_trace(
                "unused.jsonl", "hash", "attempt"
            )

        self.assertEqual(1, report["oracle_disagreement_count"])
        self.assertEqual(1, report["issue_count"])
        self.assertEqual("issues", report["audit_status"])
        self.assertEqual(
            1,
            report["issue_counts"]["observable_delta_mismatch"],
        )
        self.assertFalse(report["release_gate_passed"])

    def test_attempt_audit_death_replay_inconclusive_is_blocking(self):
        records = [{
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": False,
            "current_hp": 0,
        }]
        oracle_module = SimpleNamespace(
            audit_records=lambda rows, decision_hash, attempt_id, artifacts=None: (
                clear_oracle_report()
            )
        )
        replay_module = SimpleNamespace(
            build_death_replay=lambda rows, terminal=None: (
                not_applicable_replay_report(
                    status="inconclusive",
                    eligible_unknown_count=1,
                    unknowns=[{"kind": "insufficient_complete_turns"}],
                )
            )
        )
        with patch.object(
            strategy_audit,
            "load_attempt_trace_suffix",
            return_value=records,
        ), patch.object(
            strategy_audit,
            "audit_records",
            return_value=clear_core_audit_report(),
        ), patch.object(
            strategy_audit, "_independent_oracle", oracle_module
        ), patch.object(
            strategy_audit, "_death_replay", replay_module
        ):
            report = strategy_audit.audit_attempt_trace(
                "unused.jsonl", "hash", "attempt"
            )

        self.assertTrue(report["death_observed"])
        self.assertTrue(report["death_replay"]["death_observed"])
        self.assertEqual(1, report["eligible_unknown_count"])
        self.assertEqual("inconclusive", report["audit_status"])
        self.assertFalse(report["release_gate_passed"])

    def test_attempt_audit_rejects_not_applicable_replay_for_death(self):
        records = [{
            "record_type": "terminal_result",
            "termination_kind": "game_over",
            "victory": False,
            "current_hp": 0,
        }]
        oracle_module = SimpleNamespace(
            audit_records=lambda rows, decision_hash, attempt_id, artifacts=None: (
                clear_oracle_report()
            )
        )
        replay_module = SimpleNamespace(
            build_death_replay=lambda rows, terminal=None: (
                not_applicable_replay_report()
            )
        )
        with patch.object(
            strategy_audit,
            "load_attempt_trace_suffix",
            return_value=records,
        ), patch.object(
            strategy_audit,
            "audit_records",
            return_value=clear_core_audit_report(),
        ), patch.object(
            strategy_audit, "_independent_oracle", oracle_module
        ), patch.object(
            strategy_audit, "_death_replay", replay_module
        ):
            report = strategy_audit.audit_attempt_trace(
                "unused.jsonl", "hash", "attempt"
            )

        self.assertEqual("issues", report["death_replay"]["status"])
        self.assertEqual(
            "death_replay_required_but_not_applicable",
            report["death_replay"]["issues"][0]["kind"],
        )
        self.assertFalse(report["release_gate_passed"])

    def test_require_clear_cli_fails_closed_on_inconclusive_report(self):
        report = {"audit_status": "inconclusive"}
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)

    def test_require_clear_cli_emits_machine_receipt_for_trace_contract_error(self):
        with patch.object(
            strategy_audit,
            "audit_attempt_trace",
            side_effect=strategy_audit.TraceAuditError(
                "current attempt trace contains an unbound record"
            ),
        ), patch("builtins.print") as printer:
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])

        self.assertEqual(2, code)
        receipt = json.loads(printer.call_args.args[0])
        self.assertEqual("issues", receipt["audit_status"])
        self.assertFalse(receipt["release_gate_passed"])
        self.assertEqual(
            "attempt_trace_contract_violation",
            receipt["issues"][0]["kind"],
        )
        self.assertFalse(receipt["audit_receipt"]["exact_attempt_suffix"])
        self.assertEqual("attempt", receipt["audit_receipt"]["attempt_id"])
        self.assertEqual("hash", receipt["audit_receipt"]["decision_hash"])

    def test_require_clear_cli_accepts_only_clear_report(self):
        report = {
            "schema_version": 2,
            "decision_hash": "hash",
            "attempt_id": "attempt",
            "audit_status": "clear",
            "terminal_observed": True,
            "issue_count": 0,
            "review_finding_count": 0,
            "eligible_unknown_count": 0,
            "oracle_disagreement_count": 0,
            "protocol_correctness": {"status": "clear"},
            "mechanics_coverage": {"status": "clear"},
            "strategy_quality": {"status": "clear"},
            "combat_strategy_review": {
                "status": "not_applicable",
                "eligible_records": 0,
                "evaluated_records": 0,
                "unknown_records": 0,
                "issue_count": 0,
                "decision_case_corpus_includes_combat": False,
            },
            "independent_oracle": clear_oracle_report(),
            "death_observed": False,
            "death_replay": {
                "status": "not_applicable",
                "issue_count": 0,
                "eligible_unknown_count": 0,
                "death_observed": False,
            },
            "audit_receipt": clear_audit_receipt(),
        }
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(0, code)

    def test_require_clear_cli_rejects_inconsistent_clear_aggregate(self):
        report = {
            "schema_version": 2,
            "audit_status": "clear",
            "terminal_observed": True,
            "issue_count": 0,
            "review_finding_count": 0,
            "eligible_unknown_count": 0,
            "oracle_disagreement_count": 0,
            "protocol_correctness": {"status": "clear"},
            "mechanics_coverage": {"status": "inconclusive"},
            "strategy_quality": {"status": "clear"},
            "independent_oracle": {
                "status": "clear", "issue_count": 0,
                "eligible_unknown_count": 0, "disagreement_count": 0,
            },
            "death_observed": False,
            "death_replay": {
                "status": "not_applicable", "issue_count": 0,
                "eligible_unknown_count": 0, "death_observed": False,
            },
        }
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)

    def test_require_clear_cli_rejects_missing_top_level_counts(self):
        report = {
            "schema_version": 2,
            "audit_status": "clear",
            "terminal_observed": True,
            "protocol_correctness": {"status": "clear"},
            "mechanics_coverage": {"status": "clear"},
            "strategy_quality": {"status": "clear"},
        }
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)
        self.assertFalse(report["release_gate_passed"])

    def test_require_clear_cli_rejects_missing_required_audit_channels(self):
        report = clear_core_audit_report()
        report["oracle_disagreement_count"] = 0
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)
        self.assertFalse(report["release_gate_passed"])

    def test_require_clear_cli_requires_independent_oracle_to_be_clear(self):
        report = {
            "schema_version": 2,
            "audit_status": "clear",
            "terminal_observed": True,
            "issue_count": 0,
            "review_finding_count": 0,
            "eligible_unknown_count": 0,
            "oracle_disagreement_count": 0,
            "protocol_correctness": {"status": "clear"},
            "mechanics_coverage": {"status": "clear"},
            "strategy_quality": {"status": "clear"},
            "independent_oracle": {
                "status": "inconclusive", "issue_count": 0,
                "eligible_unknown_count": 0, "disagreement_count": 0,
            },
            "death_observed": False,
            "death_replay": {
                "status": "not_applicable", "issue_count": 0,
                "eligible_unknown_count": 0, "death_observed": False,
            },
        }
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)
        self.assertFalse(report["release_gate_passed"])

    def test_attempt_59e6bac7_attrition_race_is_not_a_clear_audit(self):
        records = lagavulin_attrition_incident_records()

        report = strategy_audit.audit_records(records, "new")

        findings = [
            issue for issue in report["issues"]
            if issue.get("kind") == "persistent_attrition_race_missed"
        ]
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual(2, finding["turn"])
        self.assertEqual(291512, finding["before_seq"])
        self.assertTrue(finding["combat_ended_in_death"])
        self.assertFalse(finding["planner_pressure_modeled"])
        self.assertEqual(
            ["Defend_R", "Carnage"],
            finding["safe_tempo_opportunities"][0]["counterfactual"][
                "cards"
            ],
        )
        self.assertEqual(
            "issues",
            report["strategy_quality"]["checks"][
                "persistent_attrition_race_missed"
            ]["status"],
        )
        self.assertIn(
            "persistent_attrition_race_missed",
            report["combat_strategy_review"]["issue_kinds"],
        )

    def test_attrition_audit_accepts_the_safe_tempo_counterfactual(self):
        records = lagavulin_attrition_incident_records(take_tempo=True)

        findings, coverage = (
            strategy_audit._persistent_attrition_race_audit(records)
        )

        self.assertEqual([], findings)
        self.assertGreater(coverage["evaluated"], 0)
        self.assertEqual(0, coverage["violations"])

    def test_neow_lament_audit_rejects_missing_reachable_elite_route(self):
        def route_candidate(candidate_id, elite_depths):
            return {
                "id": candidate_id,
                "choice_id": candidate_id,
                "choice_index": 0 if candidate_id == "map:lament" else 1,
                "action": "choose",
                "operation": None,
                "score": 10,
                "selection_eligible": True,
                "consequences": {
                    "route_summary": {
                        "legal_path_options": [{
                            "sequence": [candidate_id],
                            "neow_lament_one_hp_elite_depths": elite_depths,
                        }],
                    },
                },
            }

        record = decision(
            phase="MAP",
            action="choose",
            turn=None,
            combat_id=None,
            requested_target_id="map:ordinary",
            resolved_target_id="map:ordinary",
            decision={
                "reason": "map_lookahead_with_survival_constraints",
                "neow_lament_charges": 2,
                "candidates": [
                    route_candidate("map:lament", [3]),
                    route_candidate("map:ordinary", []),
                ],
            },
        )

        report = strategy_audit.audit_records([record], "new")

        findings = [
            issue for issue in report["issues"]
            if issue.get("kind") == "neow_lament_elite_route_missed"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual(
            {"map:lament": [3]},
            findings[0]["reachable_lament_elite_routes"],
        )
        self.assertEqual(
            "issues",
            report["strategy_quality"]["checks"][
                "neow_lament_elite_route"
            ]["status"],
        )

    def test_neow_lament_audit_accepts_selected_reachable_elite_route(self):
        candidates = [
            {
                "id": "map:lament",
                "choice_id": "map:lament",
                "choice_index": 0,
                "action": "choose",
                "operation": None,
                "score": 20,
                "selection_eligible": True,
                "consequences": {
                    "route_summary": {
                        "legal_path_options": [{
                            "sequence": ["M@0,0", "E@0,3"],
                            "neow_lament_one_hp_elite_depths": [1],
                        }],
                    },
                },
            },
            {
                "id": "map:ordinary",
                "choice_id": "map:ordinary",
                "choice_index": 1,
                "action": "choose",
                "operation": None,
                "score": 10,
                "selection_eligible": True,
                "consequences": {
                    "route_summary": {
                        "legal_path_options": [{
                            "sequence": ["M@1,0", "R@1,3"],
                            "neow_lament_one_hp_elite_depths": [],
                        }],
                    },
                },
            },
        ]
        record = decision(
            phase="MAP",
            action="choose",
            turn=None,
            combat_id=None,
            requested_target_id="map:lament",
            resolved_target_id="map:lament",
            decision={
                "reason": "map_lookahead_with_survival_constraints",
                "neow_lament_charges": 2,
                "candidates": candidates,
            },
        )

        report = strategy_audit.audit_records([record], "new")

        self.assertNotIn(
            "neow_lament_elite_route_missed",
            {issue.get("kind") for issue in report["issues"]},
        )
        coverage = report["strategy_quality"]["checks"][
            "neow_lament_elite_route"
        ]
        self.assertEqual(1, coverage["evaluated"])
        self.assertEqual(0, coverage["violations"])

    def test_neow_lament_audit_binds_selected_protocol_option_across_replay_rows(self):
        candidate = {
            "id": "map:5,0", "choice_id": "map:5,0",
            "choice_index": 0, "semantic_id": "M@5,0",
            "score": 20, "selection_eligible": True,
            "consequences": {"route_summary": {
                "legal_path_options": [{
                    "sequence": ["M@5,0", "E@5,5"],
                    "neow_lament_one_hp_elite_depths": [5],
                }],
            }},
        }
        protocol_option = {
            "option_id": "option:bound-map-node", "choice_index": 0,
            "label": "x=5",
            "target": {"kind": "map_node", "symbol": "M", "x": 5, "y": 0},
        }
        record = decision(
            phase="MAP", action="choose", turn=None, combat_id=None,
            requested_target_id="option:bound-map-node",
            resolved_target_id="option:bound-map-node",
            selected_choice_ids=["option:bound-map-node"],
            final_choice_ids=["option:bound-map-node"],
            chosen_option_before=copy.deepcopy(protocol_option),
            available_options_before=[copy.deepcopy(protocol_option)],
            decision={
                "reason": "map_lookahead_with_survival_constraints",
                "neow_lament_charges": 3,
                "candidates": [copy.deepcopy(candidate)],
                "model_advice": {"replay": {
                    "candidates": [copy.deepcopy(candidate)],
                }},
            },
        )

        report = strategy_audit.audit_records([record], "new")

        coverage = report["strategy_quality"]["checks"][
            "neow_lament_elite_route"
        ]
        self.assertEqual(1, coverage["evaluated"])
        self.assertEqual(0, coverage["unknown"])
        self.assertEqual(0, coverage["violations"])

    def test_attrition_audit_recognizes_mechanic_driven_enemy_growth(self):
        profile = strategy_audit._independent_persistent_attrition_profile({
            "id": "AnyFutureMonsterId",
            "powers": [{"id": "Ritual", "amount": 3}],
        })

        self.assertEqual(
            "recurring_enemy_growth:ritual",
            profile["effect"],
        )

    def test_require_clear_cli_rejects_inconclusive_death_replay(self):
        report = clear_core_audit_report()
        report.update({
            "audit_status": "inconclusive",
            "eligible_unknown_count": 1,
            "oracle_disagreement_count": 0,
            "independent_oracle": {
                "status": "clear", "issue_count": 0,
                "eligible_unknown_count": 0, "disagreement_count": 0,
            },
            "death_observed": True,
            "death_replay": {
                "status": "inconclusive", "issue_count": 0,
                "eligible_unknown_count": 1, "death_observed": True,
            },
        })
        with patch.object(
            strategy_audit, "audit_attempt_trace", return_value=report
        ), patch("builtins.print"):
            code = strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--attempt-id", "attempt",
                "--require-clear",
            ])
        self.assertEqual(2, code)
        self.assertFalse(report["release_gate_passed"])

    def test_require_clear_cli_requires_bound_attempt(self):
        with self.assertRaises(SystemExit) as raised, patch(
            "sys.stderr"
        ):
            strategy_audit.main([
                "--trace", "unused.jsonl",
                "--decision-hash", "hash",
                "--require-clear",
            ])
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
