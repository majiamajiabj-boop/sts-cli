"""Power coverage backed by installed game bytecode and historical frames.

DoubleDamagePower.atDamageGive doubles NORMAL damage before card serialization.
PhantasmalPower.atStartOfTurn applies DoubleDamagePower, then reduces itself.
These labels cover the current authoritative frame, not future draw simulation.
"""
import copy
import json
import unittest
from pathlib import Path

from tests.test_winrate_mechanics import Game
from tests.test_strategy_audit import decision
import strategy_audit
from spirecomm.ai import combat_predictor


FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures" /
                     "phantasmal_power_frames.json").read_text(encoding="utf-8"))


def historical_game(seq, side):
    frame = FRAMES[str(seq)][side]
    raw = copy.deepcopy(frame["game_state"])

    def restore(value):
        if isinstance(value, dict):
            if "card_instance_id" in value:
                value.setdefault("uuid", value["card_instance_id"])
            for item in list(value.values()):
                restore(item)
        elif isinstance(value, list):
            for item in value:
                restore(item)

    restore(raw)
    raw.setdefault("map", [])
    raw.update(screen_type="NONE", screen_state={})
    return Game.from_json(raw, frame["available_commands"])


class PhantasmalPowerCoverageTests(unittest.TestCase):
    def test_player_aliases_are_state_reflected_not_exact_future_models(self):
        for power_id in ("Double Damage", "DoubleDamage", "DoubleDamagePower",
                         "Phantasmal", "PhantasmalPower"):
            with self.subTest(power_id=power_id):
                entry = combat_predictor.classify_active_power(
                    {"id": power_id, "amount": 1}, "player")
                self.assertEqual("state_reflected", entry["category"])
                self.assertIsNone(entry["handler"])

    def test_wrong_owner_and_unknown_power_remain_unclassified(self):
        for role, power_id in (("monster", "Double Damage"),
                               ("monster", "Phantasmal"),
                               ("player", "Unverified Damage Multiplier")):
            with self.subTest(role=role, power_id=power_id):
                entry = combat_predictor.classify_active_power(
                    {"id": power_id, "amount": 1}, role)
                self.assertEqual("unclassified", entry["category"])

    def test_pending_phantasmal_does_not_double_current_hand(self):
        game = historical_game(571877, "before")
        self.assertTrue(combat_predictor.has_power(game.player, "Phantasmal"))
        self.assertFalse(combat_predictor.has_power(game.player, "Double Damage"))
        card = next(c for c in game.hand if c.card_id == "Underhanded Strike")
        self.assertEqual(12, card.damage)
        self.assertEqual((12, 1),
                         combat_predictor.card_attack_profile(game, card))

    def test_next_turn_transition_has_already_doubled_displayed_damage(self):
        game = historical_game(571877, "after")
        self.assertFalse(combat_predictor.has_power(game.player, "Phantasmal"))
        self.assertTrue(combat_predictor.has_power(game.player, "Double Damage"))
        card = next(c for c in game.hand if c.card_id == "Dash")
        self.assertEqual(10, card.base_damage)
        self.assertEqual(20, card.damage)
        self.assertEqual((20, 1),
                         combat_predictor.card_attack_profile(game, card))

    def test_observed_dash_removes_twenty_block_without_double_counting(self):
        before = historical_game(571931, "before")
        after = historical_game(571931, "after")
        card = next(c for c in before.hand if c.card_id == "Dash")
        enemy = next(m for m in before.monsters
                     if m.monster_id == "SphericGuardian")
        result = next(m for m in after.monsters
                      if m.monster_id == "SphericGuardian")
        self.assertEqual(44, enemy.block)
        self.assertEqual(24, result.block)
        self.assertEqual(enemy.current_hp, result.current_hp)
        self.assertEqual((enemy.block - result.block, 1),
                         combat_predictor.card_attack_profile(
                             before, card, target=enemy))

    def test_audit_reclassifies_raw_state_instead_of_stale_verdict(self):
        raw = FRAMES["571931"]["before"]["game_state"]["combat_state"]
        record = {
            "player_before": copy.deepcopy(raw["player"]),
            "monsters_before": copy.deepcopy(raw["monsters"]),
            "power_model_coverage": {"unclassified_power_ids": ["double damage"]},
        }
        result = strategy_audit._record_power_coverage(record)
        self.assertIn("double damage", result["state_reflected_power_ids"])
        self.assertNotIn("double damage", result["unclassified_power_ids"])
        record["player_before"]["powers"].append(
            {"id": "Unverified Damage Multiplier", "amount": 1})
        result = strategy_audit._record_power_coverage(record)
        self.assertIn("unverified damage multiplier",
                      result["unclassified_power_ids"])

    def test_known_power_clears_only_mechanics_gate_unknown_still_blocks(self):
        def audit(power_id):
            record = decision(energy_before=0,
                              projected_hp_loss_before=3,
                              projected_attack_hp_loss_before=3,
                              decision_outcome={"hp_delta": -3})
            record["player_before"]["energy"] = 0
            record["player_before"]["powers"] = [{"id": power_id, "amount": 1}]
            return strategy_audit.audit_records([record], "new")

        for power_id in ("Phantasmal", "Double Damage"):
            with self.subTest(power_id=power_id):
                self.assertEqual("clear", audit(power_id)["mechanics_coverage"]["status"])
        unknown = audit("Unverified Damage Multiplier")
        self.assertEqual("inconclusive", unknown["mechanics_coverage"]["status"])
        self.assertEqual("inconclusive", unknown["audit_status"])

    def test_registry_contract_remains_disjoint_and_role_aware(self):
        violations = combat_predictor.power_coverage_contract_violations()
        self.assertTrue(all(not values for values in violations.values()),
                        violations)


if __name__ == "__main__":
    unittest.main()
