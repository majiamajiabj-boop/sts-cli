"""Offline branch regressions with independently calculated combat outcomes."""
import copy
import json
import unittest
from pathlib import Path

from test_attempt_failure_regressions import (
    FastCombatPlanner, Game, GameStub, Intent, Power, PrioritiesStub,
    SimpleAgent, make_monster, strike,
)
from spirecomm.spire.character import Orb
from spirecomm.spire.relic import Relic


FRAMES = json.loads((Path(__file__).parent / "test_fixtures" /
                     "winrate_mechanics_frames.json").read_text(encoding="utf-8"))


def historical_game(seq):
    frame = FRAMES[str(seq)]
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


class TransientBranchTests(unittest.TestCase):
    def game(self, damage=30, hand=None):
        enemy = make_monster("Transient", 950, max_hp=999)
        enemy.intent = Intent.ATTACK
        enemy.move_base_damage = damage
        enemy.move_adjusted_damage = damage
        enemy.move_hits = 1
        enemy.powers = [Power("Shifting", "Shifting", -1)]
        return GameStub(enemy, hand or [], energy=3)

    def test_historical_hand_can_preserve_fairy_through_the_current_attack(self):
        game = historical_game(568176)
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        planner = FastCombatPlanner(agent.priorities)
        action = planner.choose_card_action(game)
        self.assertEqual("Strike_R", action.card.card_id)
        result = planner.last_decision["search"]
        # Strike (12), then Double Tap + Twin Strike (4 * 8), with 5 Pain
        # loss: 60 - 44 incoming, covered by 11 existing + 5 Defend block.
        self.assertEqual([835], result["final_enemy_hp"])
        self.assertEqual([16], result["branch_enemy_attack_damage_per_hit"])
        self.assertEqual(0, result["projected_attack_hp_loss"])
        self.assertEqual(10, result["projected_player_hp_after_turn"])
        self.assertFalse(result["fairy_revive_consumed"])

    def test_attack_and_passive_damage_reduce_strength_exactly_once(self):
        game = self.game(hand=[strike(6)])
        game.monsters[0].powers.append(Power("Poison", "Poison", 5))
        game.player.orbs = [Orb("Lightning", "Lightning", 8, 3)]
        planner = FastCombatPlanner(PrioritiesStub())
        planner.choose_card_action(game)
        result = planner.last_decision["search"]
        self.assertEqual([936], result["final_enemy_hp"])
        self.assertEqual([16], result["branch_enemy_attack_damage_per_hit"])
        self.assertEqual(16, result["projected_attack_hp_loss"])

    def test_shifting_counts_hp_loss_after_enemy_block(self):
        game = self.game(hand=[strike(6)])
        game.monsters[0].block = 4
        planner = FastCombatPlanner(PrioritiesStub())
        planner.choose_card_action(game)
        self.assertEqual([28], planner.last_decision["search"][
            "branch_enemy_attack_damage_per_hit"])

    def test_authoritative_strength_loss_is_not_applied_again(self):
        game = self.game(damage=60, hand=[strike(10)])
        game.monsters[0].move_adjusted_damage = 50
        game.monsters[0].powers.append(Power("Strength", "Strength", -10))
        planner = FastCombatPlanner(PrioritiesStub())
        planner.choose_card_action(game)
        self.assertEqual([40], planner.last_decision["search"][
            "branch_enemy_attack_damage_per_hit"])

    def test_weak_rounds_the_new_strength_before_player_block(self):
        for relics, displayed, expected in (([], 22, 10),
                                           ([Relic("Paper Crane", "Paper Crane")], 18, 7)):
            with self.subTest(displayed=displayed):
                game = self.game(damage=30, hand=[strike(9)])
                game.monsters[0].move_adjusted_damage = displayed
                game.monsters[0].powers.append(Power("Weakened", "Weakened", 1))
                game.player.block = 5
                game.relics = relics
                planner = FastCombatPlanner(PrioritiesStub())
                planner.choose_card_action(game)
                # floor((30 - 9) * .75/.6) minus 5 player block.
                self.assertEqual(expected, planner.last_decision["search"][
                    "projected_attack_hp_loss"])

    def test_thorns_after_the_enemy_hit_do_not_prevent_that_hit(self):
        game = self.game()
        game.player.powers = [Power("Thorns", "Thorns", 10)]
        planner = FastCombatPlanner(PrioritiesStub())
        self.assertEqual(30, planner.project_end_turn(game)["projected_attack_hp_loss"])

    def test_ordinary_monster_does_not_gain_shifting(self):
        game = self.game(hand=[strike(6)])
        game.monsters[0].monster_id = "Cultist"
        game.monsters[0].powers = []
        planner = FastCombatPlanner(PrioritiesStub())
        planner.choose_card_action(game)
        self.assertEqual(30, planner.last_decision["search"]["projected_attack_hp_loss"])


if __name__ == "__main__":
    unittest.main()
