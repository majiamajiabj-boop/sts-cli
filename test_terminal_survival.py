import json,copy,sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src/spirecomm-master'))
from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.spire.game import Game
def game_for(frame):
 raw=copy.deepcopy(frame['game_state'])
 def restore(v):
  if isinstance(v,dict):
   if 'card_instance_id' in v:v.setdefault('uuid',v['card_instance_id'])
   for val in list(v.values()):restore(val)
  elif isinstance(v,list):
   for val in v:restore(val)
 restore(raw);raw.setdefault('map',[]);raw.update(screen_type='NONE',screen_state={})
 return Game.from_json(raw,frame['available_commands'])

import unittest
from spirecomm.communication.action import EndTurnAction, PlayCardAction
from spirecomm.spire.power import Power

FRAMES = json.loads((ROOT / 'test_fixtures/terminal_survival_frames.json').read_text(encoding='utf-8'))

class TerminalSurvivalTests(unittest.TestCase):
    def replay(self, name, change=None):
        game = game_for(FRAMES[name]['frame'])
        if change:
            change(game)
        planner = FastCombatPlanner(SimpleAgent(
            game.character, goal_mode='HEART', macro_advisor=None,
        ).priorities)
        action = planner.choose_card_action(game)
        return game, planner, action

    def test_heart_opening_pays_bounded_tax_for_meaningful_progress(self):
        # c55a996b, F55 T1, seq 583799: formerly END at 60 HP/4 energy.
        game, planner, action = self.replay('heart_opening')
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual('Dramatic Entrance', action.card.card_id)
        self.assertEqual('reactive_progress_fallback', planner.last_decision['reason'])
        self.assertEqual(1, planner.last_decision['reactive_added_hp_loss'])
        self.assertGreaterEqual(
            planner.last_decision['search']['player_hp_after_cards']
            - planner.last_decision['search']['projected_loss'],
            planner._reactive_safety_reserve(game, game.player.current_hp),
        )

    def test_heart_opening_preserves_low_hp_reserve(self):
        _, _, action = self.replay('heart_opening', lambda g: setattr(g.player, 'current_hp', 1))
        self.assertIsInstance(action, EndTurnAction)

    def test_heart_doomed_turn_draws_and_reports_unproven_continuation(self):
        # c55a996b, F55 T3, seq 583803: END dies; Coolheaded itself survives.
        _, planner, action = self.replay('heart_draw')
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual('Coolheaded', action.card.card_id)
        self.assertEqual('doomed_turn_draw_replan', planner.last_decision['reason'])
        self.assertTrue(planner.last_decision['draw_continuation_unproven'])
        self.assertGreater(planner.last_decision['search']['player_hp_after_cards'], 0)
        self.assertGreater(planner.last_decision['search']['remaining_energy'], 0)
        self.assertEqual(0, planner.last_decision['search']['tier'])

    def test_draw_replan_never_pays_immediate_lethal_tax(self):
        _, planner, action = self.replay('heart_draw', lambda g: setattr(g.player, 'current_hp', 1))
        self.assertIsInstance(action, EndTurnAction)
        self.assertNotEqual('doomed_turn_draw_replan', planner.last_decision['reason'])

    def test_draw_replan_requires_draw_allowed(self):
        def change(g):
            g.player.powers.append(Power('No Draw', 'No Draw', 1))
        _, planner, _ = self.replay('heart_draw', change)
        self.assertNotEqual('doomed_turn_draw_replan', planner.last_decision['reason'])

    def test_draw_replan_requires_cards_to_draw(self):
        def change(g):
            g.draw_pile = []
            g.discard_pile = []
        _, planner, _ = self.replay('heart_draw', change)
        self.assertNotEqual('doomed_turn_draw_replan', planner.last_decision['reason'])

    def test_draw_replan_preserves_energy_for_continuation(self):
        _, planner, _ = self.replay('heart_draw', lambda g: setattr(g.player, 'energy', 1))
        self.assertNotEqual('doomed_turn_draw_replan', planner.last_decision['reason'])

    def test_panic_compares_affordable_ordinary_defense_with_relic_energy(self):
        # 96e5493c, F33 T2, seq 586557: Nunchaku at 9 funds the 5-cost line.
        game, planner, action = self.replay('panic_alternative')
        self.assertEqual(4, game.player.energy)
        self.assertEqual('Conserve Battery', action.card.card_id)
        search = planner.last_decision['search']
        comparison = search['panic_button_comparison']
        self.assertTrue(comparison['preserved_future_card_block'])
        self.assertEqual(2, comparison['selected_with_panic_loss'])
        self.assertEqual(1, comparison['ordinary_plan_loss'])
        self.assertEqual(0, search['remaining_energy'])
        self.assertEqual(31, search['final_player_block'])
        self.assertNotIn('PanicButton', comparison['ordinary_plan'])

    def test_panic_remains_available_without_energy_for_ordinary_defense(self):
        _, planner, action = self.replay('panic_alternative', lambda g: setattr(g.player, 'energy', 0))
        self.assertEqual('PanicButton', action.card.card_id)
        self.assertFalse(planner.last_decision['search']['panic_button_comparison']['preserved_future_card_block'])

    def test_panic_remains_available_without_ordinary_defense(self):
        def change(g):
            g.hand = [c for c in g.hand if c.card_id not in ['Conserve Battery', 'Undo', 'Hologram']]
        _, planner, action = self.replay('panic_alternative', change)
        self.assertEqual('PanicButton', action.card.card_id)
        self.assertFalse(planner.last_decision['search']['panic_button_comparison']['preserved_future_card_block'])

    def test_panic_survival_line_wins_when_ordinary_defense_is_lethal(self):
        _, planner, _ = self.replay('panic_alternative', lambda g: setattr(g.player, 'current_hp', 1))
        comparison = planner.last_decision['search']['panic_button_comparison']
        self.assertFalse(comparison['preserved_future_card_block'])
        self.assertEqual(0, comparison['ordinary_plan_tier'])
        self.assertEqual(0, comparison['selected_with_panic_loss'])
        self.assertIn('PanicButton', [c['card_id'] for c in planner.last_decision['planned_sequence']])

if __name__ == '__main__':
    unittest.main()
