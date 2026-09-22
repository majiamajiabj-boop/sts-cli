"""Offline regressions for shared fight length and delayed damage decisions."""
import copy
import json
import unittest
from pathlib import Path

from tests.test_agent_policy import (
    SimpleAgent, PlayerClass, GameStub, make_monster, PotionAction, EndTurnAction,
    Power, Orb, Relic,
)
from tests.test_winrate_mechanics import Game


FRAMES = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures" /
                     "whole_fight_frames.json").read_text(encoding="utf-8"))


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


def agent_for(game):
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class WholeFightValueTests(unittest.TestCase):
    def potion_game(self, enemy_hp=100, damage=0, hp=15, poison=0):
        game = historical_game(569046)
        game.hand = []
        game.draw_pile = []
        game.discard_pile = []
        game.monsters = [make_monster("Enemy", enemy_hp, damage, poison)]
        game.player.current_hp = game.current_hp = hp
        game.player.max_hp = game.max_hp = 70
        game.player.energy = 0
        game.player.powers = []
        game.relics = []
        game.potions = [p for p in game.potions if p.potion_id == "Poison Potion"]
        return game

    def test_historical_poison_is_used_on_the_healer_before_attrition(self):
        game = historical_game(569046)
        agent = agent_for(game)
        action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, PotionAction)
        self.assertEqual("Poison Potion", action.potion.potion_id)
        self.assertEqual("Healer", action.target_monster.monster_id)
        trace = agent.combat_planner.last_decision
        self.assertEqual(0, trace["potion_candidate_hp_loss"])
        self.assertEqual(18, trace["potion_delayed_damage_projection"]["marginal_damage"])

    def test_live_lightning_and_loop_shorten_the_historical_fight_horizon(self):
        game = historical_game(570180)
        planner = agent_for(game).combat_planner
        profile = planner._combat_progress_profile(game)
        # Three Lightning orbs at 3, plus Loop's extra first-orb trigger.
        self.assertEqual(12, profile["passive_damage_per_turn"])
        self.assertEqual(6, profile["turns"])
        game.player.orbs = []
        self.assertGreater(planner._expected_remaining_turns(game), profile["turns"])

    def test_current_energy_spent_does_not_erase_future_attack_budget(self):
        game = historical_game(570180)
        planner = agent_for(game).combat_planner
        game.player.energy = 0
        empty = planner._combat_progress_profile(game)
        game.player.energy = 4
        self.assertEqual(empty, planner._combat_progress_profile(game))

    def test_frost_and_unevoked_dark_are_not_recurring_hp_damage(self):
        game = historical_game(570180)
        game.player.orbs = [Orb("Frost", "Frost", 5, 2), Orb("Dark", "Dark", 99, 6)]
        planner = agent_for(game).combat_planner
        self.assertEqual(0, planner._combat_progress_profile(game)["passive_damage_per_turn"])

    def test_cables_and_electrodynamics_reuse_the_established_orb_output(self):
        game = historical_game(570180)
        game.relics.append(Relic("Cables", "Gold-Plated Cables"))
        game.player.powers.append(Power("Electrodynamics", "Electrodynamics", 1))
        planner = agent_for(game).combat_planner
        # 9 Lightning + 3 Loop + 3 Cables, hitting three living enemies.
        self.assertEqual(45, planner._combat_progress_profile(game)["passive_damage_per_turn"])

    def test_poison_marginal_damage_respects_existing_stack_decay(self):
        for existing, expected in ((0, 18), (3, 24)):
            with self.subTest(existing=existing):
                game = self.potion_game(poison=existing)
                planner = agent_for(game).combat_planner
                result = planner.delayed_damage_projection(game, game.monsters[0], 6)
                self.assertEqual(4, result["ticks"])
                self.assertEqual(expected, result["marginal_damage"])

    def test_exact_poison_replan_can_save_a_lethal_current_turn(self):
        game = self.potion_game(enemy_hp=6, damage=10, hp=6)
        agent = agent_for(game)
        action = agent.get_next_action_in_game(game)
        self.assertIsInstance(action, PotionAction)
        self.assertTrue(agent.combat_planner.last_decision["potion_candidate_combat_end"])
        self.assertEqual(0, agent.combat_planner.last_decision["potion_candidate_hp_loss"])

    def test_future_poison_damage_does_not_certify_a_lethal_turn_rescue(self):
        game = self.potion_game(enemy_hp=100, damage=10, hp=6)
        agent = agent_for(game)
        action = agent.get_next_action_in_game(game)
        self.assertNotIsInstance(action, PotionAction)

    def test_healthy_ordinary_fight_preserves_the_potion(self):
        game = self.potion_game(hp=70)
        self.assertNotIsInstance(agent_for(game).get_next_action_in_game(game), PotionAction)

    def test_artifact_blocks_both_current_and_future_poison_credit(self):
        game = self.potion_game()
        game.monsters[0].powers.append(Power("Artifact", "Artifact", 1))
        agent = agent_for(game)
        self.assertIsNone(agent._hypothetical_poison_potion_analysis(game.monsters[0]))
        self.assertNotIsInstance(agent.get_next_action_in_game(game), PotionAction)

    def test_naturally_doomed_target_does_not_consume_poison(self):
        game = self.potion_game(enemy_hp=5, damage=10, poison=6)
        agent = agent_for(game)
        self.assertNotIsInstance(agent.get_next_action_in_game(game), PotionAction)

    def test_sacred_bark_doubles_the_poison_application_in_the_exact_plan(self):
        game = self.potion_game(enemy_hp=10)
        agent = agent_for(game)
        before = agent._hypothetical_poison_potion_analysis(game.monsters[0])
        self.assertFalse(before["true_combat_end"])
        game.relics = [Relic("Sacred Bark", "Sacred Bark")]
        after = agent._hypothetical_poison_potion_analysis(game.monsters[0])
        self.assertTrue(after["true_combat_end"])

    def test_counterfactual_poison_never_mutates_the_live_monster(self):
        game = self.potion_game(poison=3)
        agent = agent_for(game)
        agent._hypothetical_poison_potion_analysis(game.monsters[0])
        self.assertEqual(3, game.monsters[0].powers[0].amount)


if __name__ == "__main__":
    unittest.main()
