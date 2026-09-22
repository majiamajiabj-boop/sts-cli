import unittest

import strategy_audit


def option(option_id, card_id, card_uuid):
    return {
        "option_id": option_id,
        "target": {
            "kind": "card",
            "card_instance_id": card_uuid,
            "card": {
                "id": card_id,
                "card_instance_id": card_uuid,
            },
        },
    }


class StrategyAuditHandSelectRegressionTests(unittest.TestCase):
    def test_plan_veto_cannot_force_unique_scaling_core_exhaust(self):
        true_grit = "true-grit"
        demon_form = "demon-form"
        defend = "planned-defend"
        flex = "planned-flex"
        source_sequence = [
            {"card_instance_id": true_grit, "card_id": "True Grit"},
            {"card_instance_id": defend, "card_id": "Defend_R"},
            {"card_instance_id": flex, "card_id": "Flex"},
        ]
        selected = {
            **option("option:demon", "Demon Form", demon_form),
            "candidate_id": "option:demon",
            "selection_eligible": True,
            "score": 1.0,
        }
        protected_rows = [
            {
                **option("option:defend", "Defend_R", defend),
                "candidate_id": "option:defend",
                "selection_eligible": False,
                "veto_reason": "exact_combat_plan_card_preservation",
                "score": 9.0,
            },
            {
                **option("option:flex", "Flex", flex),
                "candidate_id": "option:flex",
                "selection_eligible": False,
                "veto_reason": "exact_combat_plan_card_preservation",
                "score": 8.0,
            },
        ]
        rows = [selected, *protected_rows]
        record = {
            "record_type": "decision",
            "attempt_id": "attempt",
            "run_id": "IRONCLAD:0:1",
            "phase": "HAND_SELECT",
            "action": "choose",
            "before_seq": 101,
            "after_seq": 102,
            "act": 2,
            "floor": 18,
            "decision_context": {
                "deck_counts": {
                    "Demon Form": 1,
                    "Defend_R": 4,
                    "Flex": 1,
                },
            },
            "decision": {
                "selection_action": "ExhaustAction",
                "selection_semantics": "remove",
                "combat_plan_protection": {
                    "schema_version": 1,
                    "kind": "exact_combat_plan_card_preservation",
                    "source_combat_context": [9, 2, 18, 3],
                    "source_planned_sequence": source_sequence,
                    "visible_card_instance_ids": sorted([
                        demon_form, defend, flex,
                    ]),
                    "protected_card_instance_ids": sorted([defend, flex]),
                    "selection_action": "ExhaustAction",
                    "selection_semantics": "remove",
                    "required_selection_count": 1,
                },
            },
        }
        previous = {
            "record_type": "decision",
            "attempt_id": "attempt",
            "run_id": "IRONCLAD:0:1",
            "phase": "COMBAT_TURN_3",
            "action": "play",
            "before_seq": 100,
            "after_seq": 101,
            "act": 2,
            "floor": 18,
            "turn": 3,
            "requested_target_id": true_grit,
            "resolved_target_id": true_grit,
            "decision": {
                "_combat_context": [9, 2, 18, 3],
                "planned_sequence": [
                    {
                        "card_id": entry["card_id"],
                        "card_uuid": entry["card_instance_id"],
                    }
                    for entry in source_sequence
                ],
            },
        }

        validation = (
            strategy_audit._hand_select_plan_protection_validation(
                record, rows, [selected], previous
            )
        )

        self.assertEqual("issues", validation["status"])
        self.assertIn(
            "plan_protection_displaces_irreversible_scaling_core",
            validation["violations"],
        )


if __name__ == "__main__":
    unittest.main()
