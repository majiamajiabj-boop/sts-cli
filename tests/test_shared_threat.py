import json
import unittest
from pathlib import Path

from tests.test_terminal_survival import game_for
from tests.test_combat_survival_regressions import GameStub, monster, card, PrioritiesStub
from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.spire.card import CardType
from spirecomm.spire.power import Power
from spirecomm.communication.action import PotionAction

ROOT = Path(__file__).resolve().parents[1]
FRAMES = json.loads((ROOT / 'test_fixtures/shared_threat_frames.json').read_text(encoding='utf-8'))

class SharedThreatTests(unittest.TestCase):
    def replay_potion(self, name):
        entry = FRAMES[name]
        game = game_for(entry['frame'])
        agent = SimpleAgent(game.character, goal_mode='HEART', macro_advisor=None)
        agent.game = game
        planned = agent.combat_planner.choose_card_action(game)
        loss = agent._planned_turn_hp_loss(planned, agent.combat_planner.last_decision)
        action = agent.use_best_potion(entry['incoming'], planned_turn_loss=loss, planned_action=planned)
        return game, agent, action

    def test_event_orb_walkers_spend_weak_before_lethal_turn(self):
        game, agent, action = self.replay_potion('orb_potion')
        self.assertEqual('EventRoom', game.room_type)
        self.assertIsInstance(action, PotionAction)
        evidence = agent.combat_planner.last_decision['potion_evaluation']
        self.assertEqual(47, evidence['baseline_loss'])
        self.assertEqual(41, evidence['candidates'][0]['protected_loss'])
        self.assertAlmostEqual(13.2, evidence['selected_score'])
        self.assertEqual(12, evidence['threshold'])
        self.assertTrue(evidence['accepted'])
        self.assertTrue(evidence['threat']['has_scaling_threat'])

    def test_retained_weak_is_not_wasted_on_already_weakened_boss(self):
        _, agent, action = self.replay_potion('already_weak')
        self.assertIsNone(action)
        self.assertIsNone(agent._weak_potion_analysis())

    def test_unknown_enemy_serialized_growth_is_shared(self):
        enemy = monster('UnlistedEnemy', 100, powers=[Power('Generic Strength Up Power', 'localized', 3)])
        game = GameStub([enemy], [])
        planner = FastCombatPlanner(PrioritiesStub())
        profile = planner.combat_threat_profile(game)
        self.assertTrue(profile['has_scaling_threat'])
        self.assertGreater(profile['enemies'][0]['pressure'], 0)

    def test_existing_strength_is_not_evidence_of_future_growth(self):
        enemy = monster('UnlistedEnemy', 100, powers=[Power('Strength', 'Strength', 10)])
        game = GameStub([enemy], [])
        self.assertFalse(FastCombatPlanner(PrioritiesStub()).combat_threat_profile(game)['has_scaling_threat'])

    def test_one_hp_enemy_does_not_trigger_setup_potion_urgency(self):
        game = GameStub([monster('Orb Walker', 1)], [])
        self.assertFalse(FastCombatPlanner(PrioritiesStub()).combat_threat_profile(game)['has_scaling_threat'])

    def test_time_eater_keeps_special_counter_instead_of_generic_race_budget(self):
        game = GameStub([monster('TimeEater', 456)], [])
        planner = FastCombatPlanner(PrioritiesStub())
        self.assertEqual(0, planner._monster_scaling_pressure(game.monsters[0], game))
        profile = planner.combat_threat_profile(game)
        self.assertTrue(profile['has_scaling_threat'])
        self.assertEqual('time_warp_and_haste', profile['enemies'][0]['model'])

    def test_transient_retains_finite_survival_model(self):
        game = GameStub([monster('Transient', 999)], [])
        profile = FastCombatPlanner(PrioritiesStub()).combat_threat_profile(game)
        self.assertEqual('finite_survival_clock', profile['enemies'][0]['model'])
        self.assertEqual(0, profile['enemies'][0]['pressure'])

    def test_boss_name_alone_does_not_create_growth_pressure(self):
        planner = FastCombatPlanner(PrioritiesStub())
        for enemy_id in ['Champ', 'Donu', 'Deca', 'TheCollector']:
            with self.subTest(enemy=enemy_id):
                game = GameStub([monster(enemy_id, 300)], [])
                self.assertFalse(planner.combat_threat_profile(game)['has_scaling_threat'])

    def test_boss_clock_does_not_buy_damage_with_extra_hp(self):
        # A naive growth allowance changed these exact 0-loss plans to 6-loss.
        for name in ['champ_defect', 'champ_ironclad']:
            with self.subTest(frame=name):
                game = game_for(FRAMES[name]['frame'])
                planner = FastCombatPlanner(SimpleAgent(
                    game.character, goal_mode='HEART', macro_advisor=None,
                ).priorities)
                planner.choose_card_action(game)
                search = planner.last_decision['search']
                self.assertEqual(0, search['actual_loss'])
                self.assertEqual(0, search['scaling_risk_allowance'])
                self.assertEqual(0, search['race_budget_pressure'])
                self.assertEqual(search['scaling_pressure'], 0)
                self.assertFalse(planner._last_search['combat_threat']['has_scaling_threat'])


    def test_collector_damage_reduces_summon_cleanup_cost(self):
        entry = json.loads((ROOT / 'test_fixtures/collector_target_frame.json').read_text(encoding='utf-8'))
        game = game_for(entry['frame'])
        planner = FastCombatPlanner(SimpleAgent(
            game.character, goal_mode='HEART', macro_advisor=None,
        ).priorities)
        planner.choose_card_action(game)
        search = planner.last_decision['search']
        self.assertEqual(0, search['actual_loss'])
        # T4 Heavy Blade and T5 Anger/Flame Barrier deliver 33 more damage.
        # Leaving this Torch at 39 instead requires a T6 cleanup attack.
        self.assertEqual([33, 38, 247], search['final_enemy_hp'])
        self.assertLessEqual(search['final_enemy_hp'][0] - 33, 0)

if __name__ == '__main__':
    unittest.main()
