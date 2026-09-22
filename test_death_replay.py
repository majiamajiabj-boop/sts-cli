import copy
import json
import tempfile
import unittest
from pathlib import Path

import death_replay


ATTEMPT = "attempt-death"
RUN_ID = "IRONCLAD:0:99"
SEED = 99
DECISION_HASH = "decision-death"
CONTROLLER_HASH = "controller-death"
SELECTION_ID = "selection-death"


def terminal():
    return {
        "record_type": "terminal_result",
        "schema_version": 2,
        "policy_version": "fast-policy-v5",
        "attempt_id": ATTEMPT,
        "run_id": RUN_ID,
        "seed": SEED,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "selection_id": SELECTION_ID,
        "termination_kind": "game_over",
        "authoritative_game_over": True,
        "screen_type": "GAME_OVER",
        "state_seq": 31,
        "terminal_state_seq": 31,
        "act": 3,
        "floor": 50,
        "victory": False,
        "heart_defeated": False,
        "current_hp": 0,
    }


def combat_action(turn, before_seq, hp_after):
    return {
        "record_type": "decision",
        "policy_version": "fast-policy-v5",
        "attempt_id": ATTEMPT,
        "run_id": RUN_ID,
        "seed": SEED,
        "character": "IRONCLAD",
        "ascension_level": 0,
        "run_type": "standard",
        "decision_hash": DECISION_HASH,
        "controller_hash": CONTROLLER_HASH,
        "selection_id": SELECTION_ID,
        "combat_id": "combat:death",
        "turn": turn,
        "before_seq": before_seq,
        "after_seq": before_seq + 1,
        "phase": f"COMBAT_TURN_{turn}",
        "action": "end",
        "resolved_target_id": "action:end",
        "hp_after": hp_after,
        "player_before": {
            "current_hp": max(1, hp_after + 5),
            "max_hp": 80,
            "block": 0,
            "energy": 1,
            "powers": [],
            "orbs": [],
        },
        "monsters_before": [{
            "enemy_instance_id": "enemy:a",
            "id": "Cultist",
            "current_hp": 20,
            "block": 0,
            "intent": "ATTACK",
            "move_adjusted_damage": 5,
            "move_hits": 1,
            "powers": [],
            "is_gone": False,
            "half_dead": False,
        }],
        "hand_before": [{
            "card_instance_id": "card:a", "id": "Strike_R",
            "type": "ATTACK", "damage": 1, "block": 0,
            "cost": 1, "is_playable": True, "has_target": True,
        }],
        "draw_pile_before": [],
        "discard_pile_before": [],
        "exhaust_pile_before": [],
        "potions_before": [{
            "potion_instance_id": "potion:slot", "id": "Potion Slot",
            "can_use": False,
        }],
        "relics_before": [],
        "legal_actions_before": [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {
                "choice_id": "play:card:a:enemy:a", "action": "play",
                "legal": True,
            },
        ],
        "decision": {
            "reason": "play_phase_exhausted",
            "candidates": [
                {
                    "choice_id": "action:end",
                    "selected": True,
                },
                {
                    "choice_id": "play:card:a:enemy:a",
                    "selected": False,
                    "counterfactual": {
                        "evidence_level": "production_planner",
                        "predicted_survives": hp_after > 0,
                        "basis": "non-independent projection",
                    },
                },
            ],
            "model_advice": {"status": "skipped"},
        },
        "damage_model": {
            "deterministic": True,
            "monsters_to_hero_predicted": 5,
            "monsters_to_hero_actual": 5,
        },
        "decision_outcome": {
            "player_hp_loss": 5,
            "hp_delta": -5,
        },
        "authoritative_state_after": {
            "state_seq": before_seq + 1,
            "phase": "GAME_OVER" if hp_after <= 0 else f"COMBAT_TURN_{turn + 1}",
            "game_state": {
                "class": "IRONCLAD",
                "ascension_level": 0,
                "seed": SEED,
                "act": 3,
                "floor": 50,
                "current_hp": hp_after,
                "screen_type": "GAME_OVER" if hp_after <= 0 else "NONE",
            },
        },
    }


def records():
    return [
        combat_action(1, 10, 10),
        combat_action(2, 20, 5),
        combat_action(3, 30, 0),
    ]


def second_wind_fatal_end():
    record = combat_action(3, 30, 0)
    record["player_before"].update({
        "current_hp": 25,
        "block": 0,
        "energy": 1,
        "powers": [
            {"id": "Strength", "amount": 1},
            {"id": "No Draw", "amount": -1},
        ],
    })
    record["monsters_before"] = [
        {
            "enemy_instance_id": "enemy:torch", "id": "TorchHead",
            "current_hp": 22, "block": 0, "intent": "ATTACK",
            "move_adjusted_damage": 7, "move_hits": 1,
            "powers": [
                {"id": "Minion", "amount": -1},
                {"id": "Vulnerable", "amount": 1},
            ],
            "is_gone": False, "half_dead": False,
        },
        {
            "enemy_instance_id": "enemy:collector", "id": "TheCollector",
            "current_hp": 157, "block": 0, "intent": "ATTACK",
            "move_adjusted_damage": 24, "move_hits": 1,
            "powers": [
                {"id": "Strength", "amount": 6},
                {"id": "Vulnerable", "amount": 2},
            ],
            "is_gone": False, "half_dead": False,
        },
    ]
    record["hand_before"] = [
        {
            "card_instance_id": "perfected-uuid", "id": "Perfected Strike",
            "type": "ATTACK", "damage": 9, "block": 0,
            "cost": 2, "is_playable": False, "has_target": True,
            "exhausts": False,
        },
        {
            "card_instance_id": "second-wind-uuid", "id": "Second Wind",
            "type": "SKILL", "damage": 0, "block": 5,
            "cost": 1, "is_playable": True, "has_target": False,
            "exhausts": False,
        },
        {
            "card_instance_id": "bash-uuid", "id": "Bash",
            "type": "ATTACK", "damage": 9, "block": 0,
            "cost": 2, "is_playable": False, "has_target": True,
            "exhausts": False,
        },
    ]
    record["relics_before"] = [
        {"id": relic_id, "counter": -1}
        for relic_id in (
            "Burning Blood", "HornCleat", "Pantograph", "Pandora's Box",
            "Vajra", "Eternal Feather",
        )
    ]
    second_wind = "play:second-wind-uuid"
    record["legal_actions_before"] = [
        {"choice_id": second_wind, "action": "play", "legal": True},
        {"choice_id": "action:end", "action": "end", "legal": True},
    ]
    record["decision"]["candidates"] = [
        {
            "choice_id": second_wind,
            "selected": False,
            "counterfactual": {"evidence_level": "unable_to_determine"},
        },
        {"choice_id": "action:end", "selected": True},
    ]
    record["damage_model"].update({
        "monsters_to_hero_predicted": 25,
        "monsters_to_hero_actual": 25,
    })
    record["decision_outcome"].update({
        "player_hp_loss": 25,
        "hp_delta": -25,
    })
    return record, second_wind


class DeathReplayTests(unittest.TestCase):
    def test_beneficial_envenom_and_reflected_draw_reduction_are_safe_not_exact(self):
        for power_id in (
            "Envenom", "Draw Reduction", "Draw Card", "Equilibrium",
        ):
            with self.subTest(power_id=power_id):
                record = records()[1]
                record["player_before"]["powers"] = [
                    {"id": power_id, "name": power_id, "amount": 1}
                ]

                safe, exact, reasons = death_replay._safe_reactive_environment(
                    record
                )

                self.assertTrue(safe)
                self.assertFalse(exact)
                self.assertEqual(["player_power_card_math"], reasons)

    def test_death_classifier_requires_complete_schema2_terminal_contract(self):
        valid = terminal()
        self.assertTrue(death_replay._is_death(valid))

        mutations = (
            ("record_type", None),
            ("schema_version", 1),
            ("schema_version", 2.0),
            ("termination_kind", "operational_error"),
            ("victory", None),
            ("victory", True),
            ("current_hp", -1),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                malformed = copy.deepcopy(valid)
                if value is None:
                    malformed.pop(field)
                else:
                    malformed[field] = value
                self.assertFalse(death_replay._is_death(malformed))

    def test_missing_victory_cannot_be_silently_not_applicable(self):
        result = terminal()
        result.pop("victory")

        replay = death_replay.build_death_replay(records(), result)

        self.assertEqual("inconclusive", replay["status"])
        self.assertEqual("unknown", replay["replay_kind"])
        self.assertIn(
            "terminal_death_contract_invalid",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_game_over_authority_requires_both_markers(self):
        mutations = (
            {"authoritative_game_over": False},
            {"screen_type": "NONE"},
            {"screen_type": "game_over"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result = terminal()
                result.update(mutation)

                replay = death_replay.build_death_replay(records(), result)

                self.assertEqual("inconclusive", replay["status"])
                self.assertFalse(replay["death_observed"])
                self.assertIn(
                    "authoritative_game_over_unproven",
                    {item["kind"] for item in replay["unknowns"]},
                )

    def test_extracts_last_three_complete_turns_and_evidence_levels(self):
        replay = death_replay.build_death_replay(records(), terminal())

        self.assertEqual("clear", replay["status"])
        self.assertEqual([1, 2, 3], [row["turn"] for row in replay["turns"]])
        self.assertTrue(all(row["complete"] for row in replay["turns"]))
        self.assertEqual(
            3, replay["evidence_level_counts"]["independent_oracle"]
        )
        self.assertEqual(30, replay["turns"][-1]["first_state_seq"])

    def test_missing_required_replay_field_is_inconclusive(self):
        incomplete = records()
        incomplete[1].pop("draw_pile_before")

        replay = death_replay.build_death_replay(incomplete, terminal())

        self.assertEqual("inconclusive", replay["status"])
        findings = [
            item for item in replay["unknowns"]
            if item["kind"] == "death_replay_fields_missing"
        ]
        self.assertEqual(1, len(findings))
        self.assertIn("draw_pile_before", findings[0]["fields"])

    def test_observed_player_delta_contradiction_is_an_issue(self):
        rows = records()
        rows[1]["decision_outcome"]["hp_delta"] = -1

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "death_replay_player_delta_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_unplayable_card_cannot_be_declared_legal(self):
        rows = records()
        rows[1]["hand_before"][0]["is_playable"] = False

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        finding = next(
            item for item in replay["issues"]
            if item["kind"] == "legal_action_reconstruction_mismatch"
        )
        self.assertEqual(
            ["play:card:a:enemy:a"], finding["declared_only"]
        )

    def test_missing_card_target_metadata_is_inconclusive(self):
        rows = records()
        rows[0]["hand_before"][0].pop("has_target")

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "death_replay_legality_inputs_incomplete",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_retained_usable_potion_cannot_be_omitted_from_legal_surface(self):
        rows = records()
        rows[-1]["potions_before"][0].update({
            "id": "Block Potion",
            "can_use": True,
            "requires_target": False,
        })

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        finding = next(
            item for item in replay["issues"]
            if item["kind"] == "legal_action_reconstruction_mismatch"
            and item["turn"] == 3
        )
        self.assertEqual(
            ["potion:potion:slot"], finding["reconstructed_only"]
        )

    def test_unable_to_determine_counterfactual_is_eligible_unknown(self):
        rows = records()
        rows[-1]["hand_before"][0]["id"] = "Bash"
        rows[-1]["decision"]["candidates"][1].pop("counterfactual")

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "critical_counterfactual_unresolved",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_defends_are_exact_with_flex_calipers_and_dormant_shuriken(self):
        record = combat_action(9, 90, 0)
        record["player_before"].update({
            "current_hp": 8, "block": 0, "energy": 3,
            "powers": [
                {"id": "Strength", "amount": 11},
                {"id": "Flex", "amount": 4},
            ],
        })
        record["monsters_before"][0].update({
            "current_hp": 72, "move_adjusted_damage": 22,
            "move_hits": 2,
            "powers": [{"id": "Strength", "amount": 12}],
        })
        record["hand_before"] = [
            {
                "card_instance_id": f"defend:{index}", "id": "Defend_R",
                "type": "SKILL", "damage": 0, "block": 5,
                "cost": 1, "is_playable": True, "has_target": False,
            }
            for index in range(2)
        ]
        record["relics_before"] = [
            {"id": relic_id, "counter": counter}
            for relic_id, counter in (
                ("Burning Blood", -1), ("Frozen Egg 2", -1),
                ("Fusion Hammer", -1), ("Coffee Dripper", -1),
                ("Calipers", -1), ("Anchor", -1), ("Shuriken", 2),
            )
        ]

        result = death_replay._best_simple_turn_survival(
            record, "play:defend:0"
        )

        self.assertEqual("proven", result["status"])
        self.assertFalse(result["predicted_survives"])
        self.assertEqual(34, result["predicted_hp_loss"])

    def test_no_block_defends_cannot_rescue_tungsten_multi_hit_death(self):
        record = combat_action(4, 40, 0)
        record["player_before"].update({
            "current_hp": 24, "block": 0, "energy": 2,
            "powers": [
                {"id": "Berserk", "amount": 1},
                {"id": "Strength", "amount": 2},
                {"id": "NoBlockPower", "amount": 1},
            ],
        })
        record["monsters_before"] = [{
            "enemy_instance_id": "enemy:repto", "id": "Reptomancer",
            "current_hp": 140, "block": 0, "intent": "ATTACK_DEBUFF",
            "move_adjusted_damage": 13, "move_hits": 2,
            "powers": [{"id": "Vulnerable", "amount": 2}],
            "is_gone": False, "half_dead": False,
        }]
        record["hand_before"] = [
            {
                "card_instance_id": f"defend-{index}", "id": "Defend_R",
                "type": "SKILL", "damage": 1, "block": 0,
                "cost": 1, "is_playable": True, "has_target": False,
            }
            for index in range(4)
        ]
        record["relics_before"] = [
            {"id": relic_id, "counter": -1}
            for relic_id in (
                "Burning Blood", "Golden Idol", "Bag of Marbles",
                "Strawberry", "Fusion Hammer", "Bag of Preparation",
                "Red Mask", "Frozen Egg 2", "Runic Cube", "Anchor",
                "TungstenRod",
            )
        ]

        result = death_replay._best_simple_turn_survival(
            record, "play:defend-0"
        )

        self.assertEqual("proven", result["status"])
        self.assertFalse(result["predicted_survives"])
        self.assertEqual(24, result["predicted_hp_loss"])

        changed = copy.deepcopy(record)
        changed["player_before"]["powers"] = [
            power for power in changed["player_before"]["powers"]
            if power["id"] != "NoBlockPower"
        ]
        self.assertIsNone(
            death_replay._no_block_defend_nonrescue(
                changed, "play:defend-0"
            )
        )

    def test_artifact_blocked_weak_potion_is_proven_not_to_rescue(self):
        rows = records()
        final = rows[-1]
        final["player_before"]["energy"] = 0
        final["hand_before"][0]["is_playable"] = False
        final["monsters_before"][0]["powers"] = [
            {"id": "Artifact", "amount": 1}
        ]
        final["potions_before"] = [{
            "potion_instance_id": "potion:slot",
            "id": "WeakPotion",
            "can_use": True,
            "requires_target": True,
        }]
        potion_choice = "potion:potion:slot:enemy:a"
        final["legal_actions_before"] = [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {"choice_id": potion_choice, "action": "potion", "legal": True},
        ]
        final["decision"]["candidates"] = [
            {"choice_id": "action:end", "selected": True},
            {
                "choice_id": potion_choice,
                "selected": False,
                "counterfactual": {
                    "evidence_level": "unable_to_determine",
                },
            },
        ]

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"])
        resolved = next(
            item for item in replay["counterfactuals"]
            if item["choice_id"] == potion_choice
        )
        self.assertEqual("independent_oracle", resolved["evidence_level"])
        self.assertFalse(resolved["predicted_survives"])
        self.assertIn("Artifact consumption", resolved["basis"])

    def test_liquid_bronze_cannot_rescue_single_lethal_hit(self):
        rows = records()
        final = rows[-1]
        final["player_before"].update({
            "current_hp": 4, "block": 6, "energy": 0,
        })
        final["hand_before"][0]["is_playable"] = False
        final["monsters_before"][0].update({
            "current_hp": 18, "move_adjusted_damage": 18,
            "move_hits": 1,
        })
        final["potions_before"] = [{
            "potion_instance_id": "potion:slot",
            "id": "LiquidBronze",
            "can_use": True,
            "requires_target": False,
        }]
        final["relics_before"] = [{"id": "Ring of the Snake"}]
        potion_choice = "potion:potion:slot"

        result = death_replay._best_simple_turn_survival(
            final, potion_choice
        )

        self.assertEqual("proven", result["status"])
        self.assertFalse(result["predicted_survives"])
        self.assertEqual(4, result["predicted_hp_loss"])
        self.assertIn("Liquid Bronze ordering", result["basis"])

        final["relics_before"].append({"id": "Toy Ornithopter"})
        self.assertIsNone(
            death_replay._liquid_bronze_single_hit_nonrescue(
                final, potion_choice
            )
        )

    def test_zero_energy_elixir_swift_bronze_set_cannot_rescue(self):
        final = combat_action(8, 90, 0)
        final["player_before"].update({
            "current_hp": 7, "block": 8, "energy": 0,
            "powers": [
                {"id": "Hex", "amount": 1},
                {"id": "Vulnerable", "amount": 2},
                {"id": "Weakened", "amount": 2},
            ],
        })
        final["hand_before"] = [{
            "card_instance_id": "card:dazed", "id": "Dazed",
            "type": "STATUS", "cost": -2, "is_playable": False,
            "has_target": False,
        }]
        final["draw_pile_before"] = [
            {
                "card_instance_id": f"card:draw:{index}",
                "id": "Defend_R", "type": "SKILL", "cost": 1,
                "is_playable": False, "has_target": False,
            }
            for index in range(3)
        ]
        final["discard_pile_before"] = []
        final["monsters_before"][0].update({
            "current_hp": 23, "move_adjusted_damage": 36,
            "move_hits": 1, "intent": "ATTACK",
            "powers": [{"id": "Strength", "amount": 6}],
        })
        final["potions_before"] = [
            {
                "potion_instance_id": "potion:elixir",
                "id": "ElixirPotion", "can_use": True,
                "requires_target": False,
            },
            {
                "potion_instance_id": "potion:swift",
                "id": "Swift Potion", "can_use": True,
                "requires_target": False,
            },
            {
                "potion_instance_id": "potion:bronze",
                "id": "LiquidBronze", "can_use": True,
                "requires_target": False,
            },
        ]
        final["relics_before"] = [
            {"id": "Burning Blood"}, {"id": "Whetstone"},
            {"id": "Pear"}, {"id": "Empty Cage"},
            {"id": "MutagenicStrength"},
        ]

        for potion in final["potions_before"]:
            result = death_replay._best_simple_turn_survival(
                final, f"potion:{potion['potion_instance_id']}"
            )
            self.assertEqual("proven", result["status"])
            self.assertFalse(result["predicted_survives"])
            self.assertEqual(7, result["predicted_hp_loss"])
            self.assertIn("zero-energy potion-set bound", result["basis"])

        final["draw_pile_before"][0]["cost"] = 0
        self.assertIsNone(
            death_replay._zero_energy_nonrescue_potion_set(
                final, "potion:potion:swift"
            )
        )

    def test_essence_of_steel_cannot_rescue_fatal_end_turn(self):
        rows = records()
        final = rows[-1]
        final["player_before"].update({
            "current_hp": 11, "block": 8, "energy": 0,
        })
        final["hand_before"] = [{
            "card_instance_id": "card:wound", "id": "Wound",
            "type": "STATUS", "cost": -2, "is_playable": False,
            "has_target": False,
        }]
        final["monsters_before"] = [{
            "enemy_instance_id": "enemy:deca", "id": "Deca",
            "current_hp": 54, "block": 0, "intent": "ATTACK_DEBUFF",
            "move_adjusted_damage": 19, "move_hits": 2,
            "powers": [{"id": "Strength", "amount": 9}],
            "is_gone": False, "half_dead": False,
        }]
        final["potions_before"] = [{
            "potion_instance_id": "potion:steel", "id": "EssenceOfSteel",
            "can_use": True, "requires_target": False,
        }]
        final["relics_before"] = [
            {"id": "Black Blood", "counter": -1},
            {"id": "Sundial", "counter": 2},
            {"id": "Pear", "counter": -1},
            {"id": "Champion Belt", "counter": -1},
            {"id": "Bag of Preparation", "counter": -1},
        ]
        potion_choice = "potion:potion:steel"

        result = death_replay._best_simple_turn_survival(
            final, potion_choice
        )

        self.assertEqual("proven", result["status"])
        self.assertFalse(result["predicted_survives"])
        self.assertEqual(11, result["predicted_hp_loss"])
        self.assertIn("Essence of Steel", result["basis"])

        final["player_before"]["current_hp"] = 30
        self.assertIsNone(
            death_replay._essence_of_steel_end_turn_nonrescue(
                final, potion_choice
            )
        )

    def test_transient_weak_and_forge_are_proven_not_to_rescue(self):
        """Replay seq 286411: both retained potions still leave lethal."""

        rows = records()
        final = rows[-1]
        final["player_before"].update({
            "current_hp": 1, "block": 24, "energy": 0,
        })
        final["hand_before"] = [
            {
                "card_instance_id": "card:armaments", "id": "Armaments",
                "type": "SKILL", "cost": 1, "is_playable": False,
                "has_target": False, "upgrades": 1,
            },
            {
                "card_instance_id": "card:strike", "id": "Strike_R",
                "type": "ATTACK", "cost": 1, "is_playable": False,
                "has_target": True, "upgrades": 1,
            },
        ]
        final["monsters_before"] = [{
            "enemy_instance_id": "enemy:transient", "id": "Transient",
            "current_hp": 954, "block": 0, "intent": "ATTACK",
            "move_adjusted_damage": 38, "move_hits": 1,
            "powers": [
                {"id": "Fading", "amount": 3},
                {"id": "Shifting", "amount": 0},
                {"id": "Shackled", "amount": 12},
                {"id": "Strength", "amount": -12},
            ],
            "is_gone": False, "half_dead": False,
        }]
        final["relics_before"] = [{"id": "Black Blood", "counter": -1}]
        final["decision_outcome"]["player_hp_loss"] = 1
        final["decision_outcome"]["hp_delta"] = -1
        final["damage_model"]["monsters_to_hero_predicted"] = 1
        final["damage_model"]["monsters_to_hero_actual"] = 1
        final["potions_before"] = [
            {
                "potion_instance_id": "potion:weak", "id": "Weak Potion",
                "can_use": True, "requires_target": True,
            },
            {
                "potion_instance_id": "potion:forge",
                "id": "BlessingOfTheForge", "can_use": True,
                "requires_target": False,
            },
        ]
        weak_choice = "potion:potion:weak:enemy:transient"
        forge_choice = "potion:potion:forge"
        final["legal_actions_before"] = [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {"choice_id": weak_choice, "action": "potion", "legal": True},
            {"choice_id": forge_choice, "action": "potion", "legal": True},
        ]
        final["decision"]["candidates"] = [
            {"choice_id": "action:end", "selected": True},
            {"choice_id": weak_choice, "selected": False},
            {"choice_id": forge_choice, "selected": False},
        ]

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"])
        potion_rows = [
            item for item in replay["counterfactuals"]
            if item["choice_id"] in {weak_choice, forge_choice}
        ]
        self.assertEqual(2, len(potion_rows))
        self.assertTrue(all(
            item["evidence_level"] == "independent_oracle"
            and item["predicted_survives"] is False
            for item in potion_rows
        ))

    def test_production_counterfactual_does_not_clear_unsupported_card(self):
        rows = records()
        rows[-1]["hand_before"][0]["id"] = "Bash"
        rows[-1]["decision"]["candidates"][1]["counterfactual"] = {
            "evidence_level": "production_planner",
            "predicted_survives": False,
            "predicted_hp_loss": 5,
        }

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "critical_counterfactual_not_independent",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_producer_labeled_independent_proof_cannot_clear_unsupported_card(self):
        rows = records()
        final = rows[-1]
        final["hand_before"][0]["id"] = "Bash"
        final["decision"]["candidates"][1]["counterfactual"] = {
            "evidence_level": "independent_oracle",
            "predicted_survives": False,
            "predicted_hp_loss": 5,
            "proof": {
                "status": "clear",
                "source": "independent_mechanics_oracle",
                "attempt_id": ATTEMPT,
                "decision_hash": DECISION_HASH,
                "before_seq": 30,
                "choice_id": "play:card:a:enemy:a",
                "predicted_survives": False,
                "predicted_hp_loss": 5,
            },
        }

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        final_counterfactual = replay["counterfactuals"][-1]
        self.assertEqual(
            "unable_to_determine", final_counterfactual["evidence_level"]
        )
        self.assertEqual(
            "independent_oracle",
            final_counterfactual["producer_counterfactual"]["evidence_level"],
        )

    def test_unmodeled_relic_prevents_false_no_survival_proof(self):
        rows = records()
        rows[-1]["relics_before"] = [{"id": "Orichalcum", "counter": -1}]

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "critical_counterfactual_not_independent",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_starter_relic_does_not_block_exact_simple_proof(self):
        rows = records()
        for row in rows:
            row["relics_before"] = [{"id": "Burning Blood", "counter": -1}]

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"])

    def test_zero_energy_whirlwind_is_proven_nonrescue(self):
        rows = records()
        final = rows[-1]
        final["player_before"].update({
            "current_hp": 1, "block": 6, "energy": 0,
            "powers": [
                {"id": "Corruption", "amount": -1},
                {"id": "Strength", "amount": 3},
                {"id": "Frail", "amount": 2},
                {"id": "Weakened", "amount": 2},
            ],
        })
        final["monsters_before"] = [{
            "enemy_instance_id": "enemy:plant", "id": "SnakePlant",
            "current_hp": 30, "block": 4, "intent": "ATTACK",
            "move_adjusted_damage": 5, "move_hits": 3,
            "powers": [
                {"id": "Malleable", "amount": 5},
                {"id": "Strength", "amount": -2},
                {"id": "Vulnerable", "amount": 1},
            ],
            "is_gone": False, "half_dead": False,
        }]
        final["hand_before"] = [
            {
                "card_instance_id": "whirlwind-uuid", "id": "Whirlwind",
                "type": "ATTACK", "damage": 6, "block": 0,
                "cost": -1, "is_playable": True, "has_target": False,
            },
            {
                "card_instance_id": "card:strike", "id": "Strike_R",
                "type": "ATTACK", "damage": 6, "block": 0,
                "cost": 1, "is_playable": False, "has_target": True,
            },
        ]
        final["relics_before"] = [
            {"id": relic_id, "counter": -1}
            for relic_id in (
                "Burning Blood", "Shovel", "Regal Pillow", "Pear",
                "PreservedInsect", "Tiny House", "Red Mask",
            )
        ]
        whirlwind = "play:whirlwind-uuid"
        final["legal_actions_before"] = [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {"choice_id": whirlwind, "action": "play", "legal": True},
        ]
        final["decision"]["candidates"] = [
            {"choice_id": "action:end", "selected": True},
            {
                "choice_id": whirlwind, "selected": False,
                "counterfactual": {
                    "evidence_level": "unable_to_determine",
                },
            },
        ]
        final["damage_model"].update({
            "monsters_to_hero_predicted": 1,
            "monsters_to_hero_actual": 1,
        })
        final["decision_outcome"].update({
            "player_hp_loss": 1, "hp_delta": -1,
        })

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"], replay["unknowns"])
        counterfactual = next(
            row for row in replay["counterfactuals"]
            if row["before_seq"] == final["before_seq"]
            and row["choice_id"] == whirlwind
        )
        self.assertEqual("independent_oracle", counterfactual["evidence_level"])
        self.assertFalse(counterfactual["predicted_survives"])
        self.assertEqual(1, counterfactual["predicted_hp_loss"])

    def test_second_wind_with_only_unplayable_attacks_is_proven_nonrescue(self):
        final, second_wind = second_wind_fatal_end()
        rows = [
            combat_action(1, 10, 30),
            combat_action(2, 20, 25),
            final,
        ]

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"], replay["unknowns"])
        counterfactual = next(
            row for row in replay["counterfactuals"]
            if row["before_seq"] == final["before_seq"]
            and row["choice_id"] == second_wind
        )
        self.assertEqual("independent_oracle", counterfactual["evidence_level"])
        self.assertFalse(counterfactual["predicted_survives"])
        self.assertEqual(25, counterfactual["predicted_hp_loss"])
        self.assertIn(
            "exhausts zero non-Attack cards", counterfactual["basis"]
        )

    def test_second_wind_zero_exhaust_proof_fails_closed_outside_exact_shape(self):
        final, second_wind = second_wind_fatal_end()
        mutations = {
            "non_attack_card": lambda row: row["hand_before"].append({
                "card_instance_id": "wound-uuid", "id": "Wound",
                "type": "STATUS", "damage": 0, "block": 0,
                "cost": -2, "is_playable": False, "has_target": False,
                "exhausts": False,
            }),
            "other_playable_card": lambda row: row["hand_before"][0].update({
                "cost": 1, "is_playable": True,
            }),
            "card_play_reactive_relic": lambda row: row[
                "relics_before"
            ].append({"id": "Letter Opener", "counter": 2}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(final)
                mutate(changed)

                result = death_replay._best_simple_turn_survival(
                    changed, second_wind
                )

                self.assertEqual("unknown", result["status"])

    def test_independent_oracle_detects_production_counterfactual_disagreement(self):
        rows = records()
        rows[-1]["decision"]["candidates"][1]["counterfactual"][
            "predicted_survives"
        ] = True

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "counterfactual_oracle_disagreement",
            {item["kind"] for item in replay["issues"]},
        )

    def test_simple_lethal_strike_is_proven_survival_alternative(self):
        rows = records()
        rows[-1]["hand_before"][0]["damage"] = 30
        rows[-1]["decision"]["candidates"][1]["counterfactual"][
            "predicted_survives"
        ] = True

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "proven_survival_alternative",
            {item["kind"] for item in replay["issues"]},
        )

    def test_writhing_mass_nonlethal_reroll_is_not_a_guaranteed_rescue(self):
        rows = records()
        final = rows[-1]
        final["player_before"].update({
            "current_hp": 2, "block": 15, "energy": 0,
            "powers": [
                {"id": "Strength", "amount": 20},
                {"id": "Vulnerable", "amount": 1},
                {"id": "Weakened", "amount": 1},
            ],
        })
        final["monsters_before"] = [{
            "enemy_instance_id": "enemy:mass", "id": "WrithingMass",
            "current_hp": 67, "block": 9, "intent": "ATTACK_DEFEND",
            "move_adjusted_damage": 22, "move_hits": 1,
            "powers": [
                {"id": "Malleable", "amount": 10},
                {"id": "Compulsive", "amount": -1},
            ],
            "is_gone": False, "half_dead": False,
        }]
        final["hand_before"] = [{
            "card_instance_id": "card:anger", "id": "Anger",
            "type": "ATTACK", "damage": 19, "block": 0,
            "cost": 0, "is_playable": True, "has_target": True,
        }]
        final["potions_before"] = [{
            "potion_instance_id": "potion:explosive",
            "id": "Explosive Potion", "can_use": True,
            "requires_target": True,
        }]
        anger = "play:card:anger:enemy:mass"
        explosive = "potion:potion:explosive:enemy:mass"
        final["legal_actions_before"] = [
            {"choice_id": "action:end", "action": "end", "legal": True},
            {"choice_id": anger, "action": "play", "legal": True},
            {"choice_id": explosive, "action": "potion", "legal": True},
        ]
        final["decision"]["candidates"] = [
            {"choice_id": "action:end", "selected": True},
            {
                "choice_id": anger, "selected": False,
                "counterfactual": {"evidence_level": "unable_to_determine"},
            },
            {
                "choice_id": explosive, "selected": False,
                "counterfactual": {"evidence_level": "unable_to_determine"},
            },
        ]
        final["decision_outcome"].update({
            "player_hp_loss": 2, "hp_delta": -2,
        })
        final["damage_model"].update({
            "monsters_to_hero_predicted": 2,
            "monsters_to_hero_actual": 2,
        })

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"], replay["unknowns"])
        reactive = [
            item for item in replay["counterfactuals"]
            if item["choice_id"] in {anger, explosive}
        ]
        self.assertEqual(2, len(reactive))
        self.assertTrue(all(
            item["evidence_level"] == "independent_oracle"
            and item["predicted_survives"] is None
            and item["counterfactual_eligibility"]
            == "non_guaranteed_reactive_reroll"
            for item in reactive
        ))

    def test_writhing_mass_exact_lethal_first_hit_is_a_proven_rescue(self):
        record = combat_action(3, 30, 0)
        record["player_before"].update({
            "current_hp": 2, "block": 0, "energy": 0,
        })
        record["monsters_before"][0].update({
            "id": "WrithingMass", "current_hp": 10, "block": 0,
            "move_adjusted_damage": 22,
            "powers": [{"id": "Compulsive", "amount": -1}],
        })
        record["hand_before"][0].update({
            "id": "Anger", "damage": 10, "cost": 0,
        })
        choice_id = "play:card:a:enemy:a"

        result = death_replay._best_simple_turn_survival(record, choice_id)

        self.assertEqual("proven", result["status"])
        self.assertTrue(result["predicted_survives"])

    def test_short_complete_combat_from_turn_one_can_clear(self):
        rows = [records()[0], combat_action(2, 20, 0)]
        result = terminal()
        result["state_seq"] = 21
        result["terminal_state_seq"] = 21

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("clear", replay["status"])
        self.assertEqual([1, 2], [item["turn"] for item in replay["turns"]])

    def test_async_terminal_sequence_after_authoritative_game_over_is_valid(self):
        result = terminal()
        result["state_seq"] = 40
        result["terminal_state_seq"] = 40

        replay = death_replay.build_death_replay(records(), result)

        self.assertEqual("clear", replay["status"])
        self.assertEqual(31, replay["turns"][-1]["last_state_seq"])
        self.assertEqual(40, replay["terminal_state_seq"])

    def test_terminal_sequence_cannot_precede_fatal_action(self):
        result = terminal()
        result["state_seq"] = 30
        result["terminal_state_seq"] = 30

        replay = death_replay.build_death_replay(records(), result)

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "death_terminal_state_seq_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_terminal_result_sequence_fields_must_agree(self):
        result = terminal()
        result["state_seq"] = 30

        replay = death_replay.build_death_replay(records(), result)

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "terminal_result_state_seq_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_appended_low_sequence_decoy_is_rejected(self):
        rows = records()
        decoy = combat_action(4, 5, 0)
        decoy["combat_id"] = "combat:decoy"
        rows.append(decoy)

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertEqual("combat:death", replay["combat_id"])
        self.assertIn(
            "combat_trace_sequence_regression",
            {item["kind"] for item in replay["issues"]},
        )

    def test_later_nonfatal_action_cannot_be_spliced_into_death(self):
        rows = records()
        rows.append(combat_action(4, 31, 1))
        result = terminal()
        result["state_seq"] = 40
        result["terminal_state_seq"] = 40

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "final_authoritative_combat_after_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_terminal_death_hp_must_be_exactly_zero(self):
        rows = records()
        rows[-1]["hp_after"] = -1
        rows[-1]["player_before"]["current_hp"] = 4
        rows[-1]["authoritative_state_after"]["game_state"]["current_hp"] = -1
        result = terminal()
        result["current_hp"] = -1

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("inconclusive", replay["status"])
        self.assertFalse(replay["death_observed"])
        self.assertIn(
            "terminal_death_contract_invalid",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_turn_transition_does_not_require_end_action(self):
        rows = records()
        first = rows[0]
        first.update({
            "action": "play",
            "card_instance_id": "card:a",
            "enemy_instance_id": "enemy:a",
            "resolved_target_id": "play:card:a:enemy:a",
        })
        first["decision"]["candidates"][0]["selected"] = False
        first["decision"]["candidates"][0]["counterfactual"] = {
            "evidence_level": "production_planner",
            "predicted_survives": True,
        }
        first["decision"]["candidates"][1]["selected"] = True
        first["decision"]["candidates"][1].pop("counterfactual")

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"])

    def test_act4_heart_terminal_is_replayed_not_not_applicable(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": True,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 55,
        })

        rows = records()[:-1] + [combat_action(3, 30, 12)]
        for row in rows:
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 55,
            })
            row["monsters_before"][0]["id"] = "Corrupt Heart"
            row["monsters_before"][0]["powers"] = [
                {"id": "BeatOfDeathPower", "amount": 1},
                {"id": "InvinciblePower", "amount": 300},
            ]
        rows[-1]["authoritative_state_after"]["phase"] = "GAME_OVER"
        rows[-1]["authoritative_state_after"]["game_state"][
            "screen_type"
        ] = "GAME_OVER"
        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("act4_terminal", replay["replay_kind"])
        self.assertEqual("clear", replay["status"])
        self.assertEqual(3, len(replay["turns"]))

    def test_act4_nonheart_victory_is_replayed_not_not_applicable(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": False,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 54,
        })
        rows = records()[:-1] + [combat_action(3, 30, 12)]
        for row in rows:
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 54,
            })
        rows[-1]["authoritative_state_after"]["phase"] = "GAME_OVER"
        rows[-1]["authoritative_state_after"]["game_state"][
            "screen_type"
        ] = "GAME_OVER"

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("act4_terminal", replay["replay_kind"])
        self.assertEqual("clear", replay["status"])

    def test_heart_replay_requires_beat_of_death_and_invincible_evidence(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": True,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 55,
        })
        rows = records()[:-1] + [combat_action(3, 30, 12)]
        for row in rows:
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 55,
            })
            row["monsters_before"][0]["id"] = "Corrupt Heart"
        rows[-1]["authoritative_state_after"]["phase"] = "GAME_OVER"
        rows[-1]["authoritative_state_after"]["game_state"][
            "screen_type"
        ] = "GAME_OVER"

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "heart_active_mechanism_evidence_missing",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_heart_loss_also_requires_active_mechanism_evidence(self):
        result = terminal()
        result.update({"observed_max_act": 4, "act": 4, "floor": 55})
        rows = records()
        for row in rows:
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 55,
            })
            row["monsters_before"][0]["id"] = "CorruptHeart"

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("death", replay["replay_kind"])
        self.assertIn(
            "heart_active_mechanism_evidence_missing",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_heart_mechanics_must_coexist_on_one_monster_row(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": True,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 55,
        })
        rows = records()[:-1] + [combat_action(3, 30, 12)]
        split_powers = (
            [{"id": "BeatOfDeathPower", "amount": 1}],
            [{"id": "InvinciblePower", "amount": 300}],
            [{"id": "BeatOfDeathPower", "amount": 1}],
        )
        for row, powers in zip(rows, split_powers):
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 55,
            })
            row["monsters_before"][0].update({
                "id": "CorruptHeart", "powers": powers,
            })
        rows[-1]["authoritative_state_after"]["phase"] = "GAME_OVER"
        rows[-1]["authoritative_state_after"]["game_state"][
            "screen_type"
        ] = "GAME_OVER"

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("inconclusive", replay["status"])
        finding = next(
            item for item in replay["unknowns"]
            if item["kind"] == "heart_active_mechanism_evidence_missing"
        )
        self.assertEqual(
            ["BeatOfDeath", "Invincible"],
            finding["required_same_monster_row"],
        )

    def test_heart_terminal_cannot_bind_to_non_heart_combat(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": True,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 55,
        })

        rows = records()[:-1] + [combat_action(3, 30, 12)]
        for row in rows:
            row["authoritative_state_after"]["game_state"].update({
                "act": 4, "floor": 55,
            })
        rows[-1]["authoritative_state_after"]["phase"] = "GAME_OVER"
        rows[-1]["authoritative_state_after"]["game_state"][
            "screen_type"
        ] = "GAME_OVER"
        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "heart_terminal_combat_identity_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_act4_terminal_without_combat_is_inconclusive(self):
        result = terminal()
        result.update({
            "victory": True,
            "heart_defeated": True,
            "current_hp": 12,
            "observed_max_act": 4,
            "act": 4,
            "floor": 55,
        })

        replay = death_replay.build_death_replay([], result)

        self.assertEqual("act4_terminal", replay["replay_kind"])
        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "death_combat_missing",
            {item["kind"] for item in replay["unknowns"]},
        )

    def test_authoritative_survival_alternative_is_an_issue(self):
        rows = records()
        final = rows[-1]
        final["hand_before"][0].update({
            "id": "Defend_R",
            "type": "SKILL",
            "damage": 0,
            "block": 5,
            "has_target": False,
        })
        final["legal_actions_before"][1]["choice_id"] = "play:card:a"
        final["decision"]["candidates"][1]["choice_id"] = "play:card:a"
        final["decision"]["candidates"][1]["counterfactual"].update({
            "evidence_level": "authoritative",
            "predicted_survives": True,
        })

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "proven_survival_alternative",
            {item["kind"] for item in replay["issues"]},
        )

    def test_write_death_replay_round_trips_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "death-replay.json"
            replay = death_replay.write_death_replay(
                path, records(), terminal()
            )
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(replay, loaded)
        self.assertEqual(ATTEMPT, loaded["attempt_id"])

    def test_binding_mismatch_is_an_issue(self):
        rows = records()
        rows[0] = copy.deepcopy(rows[0])
        rows[0]["decision_hash"] = "wrong"

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("issues", replay["status"])
        self.assertIn(
            "death_replay_binding_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_nonfatal_unsupported_counterfactual_does_not_block_replay(self):
        rows = records()
        rows[0]["hand_before"][0]["id"] = "Bash"

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("clear", replay["status"])
        row = next(
            item for item in replay["counterfactuals"]
            if item["before_seq"] == 10
        )
        self.assertEqual("unable_to_determine", row["evidence_level"])
        self.assertEqual(
            "not_applicable_nonfatal_action",
            row["counterfactual_eligibility"],
        )

    def test_final_nonfatal_combat_after_cannot_clear_a_death(self):
        rows = records()
        rows[-1]["hp_after"] = 1
        rows[-1]["decision_outcome"].update({
            "player_hp_loss": 4,
            "hp_delta": -4,
        })
        rows[-1]["authoritative_state_after"].update({
            "phase": "COMBAT_TURN_4",
            "game_state": {
                **rows[-1]["authoritative_state_after"]["game_state"],
                "current_hp": 1,
                "screen_type": "NONE",
            },
        })

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertNotEqual("clear", replay["status"])
        self.assertIn(
            "final_authoritative_combat_after_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_fatal_retain_overlay_is_replayed_as_deferred_combat_end(self):
        rows = records()
        end = rows[-1]
        end["player_before"]["current_hp"] = 4
        end["hp_after"] = 4
        end["damage_model"].update({
            "monsters_to_hero_predicted": 6,
            "monsters_to_hero_actual": 0,
            "monsters_to_hero_basis": "end_turn_player_hp_delta",
        })
        end["decision_outcome"].update({
            "player_hp_loss": 0,
            "player_hp_delta": 0,
            "hp_delta": 0,
        })
        hand_select_game = {
            "class": "IRONCLAD", "ascension_level": 0, "seed": SEED,
            "act": 3, "floor": 50, "current_hp": 4,
            "room_phase": "COMBAT", "screen_type": "HAND_SELECT",
            "current_action": "RetainCardsAction",
            "combat_state": {
                "player": {"current_hp": 4},
                "monsters": copy.deepcopy(end["monsters_before"]),
            },
        }
        end["authoritative_state_after"] = {
            "state_seq": 31, "phase": "HAND_SELECT",
            "game_state": copy.deepcopy(hand_select_game),
        }
        game_over = copy.deepcopy(hand_select_game)
        game_over.update({"current_hp": 0, "screen_type": "GAME_OVER"})
        game_over["combat_state"]["player"]["current_hp"] = 0
        settlement = {
            key: end.get(key)
            for key in (
                "record_type", "policy_version", "attempt_id", "run_id",
                "seed", "character", "ascension_level", "run_type",
                "decision_hash", "controller_hash", "selection_id",
                "selection_digest",
            )
        }
        settlement.update({
            "before_seq": 31, "after_seq": 32,
            "phase": "HAND_SELECT", "action": "proceed",
            "requested_target_id": "action:proceed",
            "resolved_target_id": "action:proceed",
            "hp_before": 4, "hp_after": 0,
            "decision_outcome": {
                "player_hp_loss": 4, "player_hp_delta": -4, "hp_delta": -4,
                "phase_after": "GAME_OVER",
                "room_phase_after": "COMBAT",
                "screen_type_after": "GAME_OVER",
            },
            "authoritative_state_before": {
                "state_seq": 31, "phase": "HAND_SELECT",
                "game_state": copy.deepcopy(hand_select_game),
            },
            "authoritative_state_after": {
                "state_seq": 32, "phase": "GAME_OVER",
                "game_state": game_over,
            },
        })
        rows.append(settlement)
        result = terminal()
        result["state_seq"] = result["terminal_state_seq"] = 32

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual(
            "deferred_combat_end", replay["terminal_cause"]["kind"]
        )
        self.assertEqual("combat:death", replay["combat_id"])
        self.assertEqual(3, len(replay["turns"]))
        self.assertNotIn(
            "death_combat_not_applicable_noncombat_cause",
            {item["kind"] for item in replay["unknowns"]},
        )
        self.assertNotIn(
            "death_replay_damage_prediction_mismatch",
            {item["kind"] for item in replay["issues"]},
        )
        self.assertNotIn(
            "final_authoritative_combat_after_mismatch",
            {item["kind"] for item in replay["issues"]},
        )

    def test_noncombat_event_death_does_not_replay_prior_combat(self):
        rows = records()[:-1]
        event = {
            "record_type": "decision",
            "policy_version": "fast-policy-v5",
            "attempt_id": ATTEMPT, "run_id": RUN_ID, "seed": SEED,
            "character": "IRONCLAD", "ascension_level": 0,
            "run_type": "standard", "decision_hash": DECISION_HASH,
            "controller_hash": CONTROLLER_HASH,
            "selection_id": SELECTION_ID,
            "before_seq": 226532, "after_seq": 226535,
            "phase": "EVENT", "action": "choose",
            "requested_target_id": "skull:gold",
            "resolved_target_id": "skull:gold",
            "hp_before": 3, "hp_after": 0,
            "authoritative_state_after": {
                "state_seq": 226535, "phase": "GAME_OVER",
                "game_state": {
                    "screen_type": "GAME_OVER", "current_hp": 0,
                },
            },
        }
        rows.append(event)
        result = terminal()
        result["state_seq"] = result["terminal_state_seq"] = 226535

        replay = death_replay.build_death_replay(rows, result)

        self.assertEqual("death", replay["replay_kind"])
        self.assertEqual("noncombat_decision", replay["terminal_cause"]["kind"])
        self.assertIsNone(replay["combat_id"])
        self.assertEqual([], replay["turns"])
        kinds = {item["kind"] for item in replay["unknowns"]}
        self.assertIn("death_combat_not_applicable_noncombat_cause", kinds)
        self.assertNotIn("final_authoritative_combat_after_mismatch", {
            item["kind"] for item in replay["issues"]
        })

    def test_missing_final_authoritative_after_is_inconclusive(self):
        rows = records()
        rows[-1].pop("authoritative_state_after")

        replay = death_replay.build_death_replay(rows, terminal())

        self.assertEqual("inconclusive", replay["status"])
        self.assertIn(
            "final_authoritative_combat_after_missing",
            {item["kind"] for item in replay["unknowns"]},
        )


if __name__ == "__main__":
    unittest.main()
