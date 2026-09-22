"""Resource conversion regressions, including 38117f5e's logged Turbo turns."""
import json
import unittest
from pathlib import Path

from tests.test_terminal_survival import game_for, SimpleAgent
from tests.test_combat_survival_regressions import (
    card, monster, GameStub, CardType, Intent, Power, Relic,
    FastCombatPlanner, DefectPowerPriority, EndTurnAction, PlayCardAction,
)

FRAMES = json.loads(
    (Path(__file__).resolve().parents[1] / "test_fixtures/energy_conversion_frames.json")
    .read_text(encoding="utf-8")
)


class EnergyConversionTests(unittest.TestCase):
    def replay(self, line):
        game = game_for(FRAMES[str(line)]["frame"])
        planner = SimpleAgent(
            game.character, goal_mode="HEART", macro_advisor=None,
        ).combat_planner
        action = planner.choose_card_action(game)
        return planner, action

    def test_all_six_logged_turbo_end_sequences_stop_wasting_the_card(self):
        for line in [254, 315, 352, 391, 431, 469]:
            with self.subTest(log_line=line):
                planner, action = self.replay(line)
                self.assertIsInstance(action, EndTurnAction)
                self.assertEqual(0, planner.last_decision["search"]["generated_voids"])

    def test_logged_energy_enabling_attack_is_preserved(self):
        planner, action = self.replay(274)
        self.assertEqual("Turbo", action.card.card_id)
        self.assertEqual(
            ["Turbo", "Strike_B"],
            [step["card_id"] for step in planner.last_decision["planned_sequence"]],
        )

    def test_already_affordable_followups_do_not_get_redundant_turbo(self):
        # The Champ frame also has negative lifecycle value: the later
        # lifecycle guard now preserves Biased Cognition as well as Turbo.
        for line, expected in [(210, "Slimed"), (417, None)]:
            with self.subTest(log_line=line):
                _, action = self.replay(line)
                if expected is None:
                    self.assertIsInstance(action, EndTurnAction)
                else:
                    self.assertEqual(expected, action.card.card_id)

    def plan(self, hand, *, energy=0, damage=0, hp=30, relics=(), powers=()):
        enemy = monster(
            "Cultist", 100, intent=Intent.ATTACK if damage else Intent.BUFF,
            damage=damage, hits=1 if damage else 0,
        )
        game = GameStub([enemy], hand, hp=hp, energy=energy)
        game.relics = list(relics)
        game.player.powers = list(powers)
        planner = FastCombatPlanner(DefectPowerPriority())
        action = planner.choose_card_action(game)
        return game, planner, action

    def test_other_current_energy_skills_cannot_score_empty_hand_energy(self):
        for name in ["Seeing Red", "Double Energy", "Bloodletting", "Turbo"]:
            with self.subTest(card=name):
                _, _, action = self.plan(
                    [card(name, CardType.SKILL, cost=0)], energy=2,
                )
                self.assertIsInstance(action, EndTurnAction)

    def test_energy_for_lethal_preventing_block_remains_valuable(self):
        turbo = card("Turbo", CardType.SKILL, cost=0)
        defend = card("Defend_B", CardType.SKILL, cost=1, block=5, playable=False)
        _, planner, action = self.plan([turbo, defend], damage=8, hp=5)
        self.assertIs(action.card, turbo)
        self.assertEqual(3, planner.last_decision["search"]["projected_loss"])
        self.assertEqual(1, planner.last_decision["search"]["generated_voids"])
        self.assertGreater(planner.last_decision["search"]["generated_void_penalty"], 0)

    def test_ice_cream_can_make_empty_hand_energy_useful(self):
        turbo = card("Turbo", CardType.SKILL, cost=0, upgrades=1)
        _, planner, action = self.plan([turbo], relics=[Relic("Ice Cream", "Ice Cream")])
        self.assertIs(action.card, turbo)
        self.assertEqual(3, planner.last_decision["search"]["remaining_energy"])

    def test_exhaust_trigger_can_justify_energy_skill_without_followup(self):
        red = card("Seeing Red", CardType.SKILL, cost=0)
        red.exhausts = True
        _, planner, action = self.plan(
            [red], damage=8, hp=7, powers=[Power("Feel No Pain", "Feel No Pain", 4)],
        )
        self.assertIs(action.card, red)
        self.assertEqual(4, planner.last_decision["search"]["projected_loss"])

    def test_turbo_kill_has_no_future_void_penalty(self):
        turbo = card("Turbo", CardType.SKILL, cost=0)
        attack = card("Strike_B", CardType.ATTACK, cost=1, damage=100,
                      target=True, playable=False)
        _, planner, action = self.plan([turbo, attack], damage=30)
        self.assertIs(action.card, turbo)
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])
        self.assertEqual(0, planner.last_decision["search"]["generated_void_penalty"])

    def test_generated_void_is_counted_in_later_reshuffle_energy_uncertainty(self):
        from unittest.mock import patch
        turbo = card("Turbo", CardType.SKILL, cost=0)
        skim = card("Skim", CardType.SKILL, cost=1, magic=3, playable=False)
        game = GameStub([monster("Cultist", 100)], [turbo, skim], hp=30, energy=0)
        game.draw_pile = []
        game.discard_pile = []
        planner = FastCombatPlanner(DefectPowerPriority())
        original = planner._terminal_turn_score
        seen = []

        def capture(g, state, *args):
            result = original(g, state, *args)
            if [step.card.card_id for step in state.plan] == ["Turbo", "Skim"]:
                seen.append(result[1])
            return result

        with patch.object(planner, "_terminal_turn_score", side_effect=capture):
            planner.choose_card_action(game)
        self.assertTrue(seen)
        for search in seen:
            self.assertEqual(1, search["generated_voids"])
            self.assertEqual(1, search["sundial_shuffle_count"])
            self.assertEqual(0, search["remaining_energy"])

    def test_actual_draw_before_reshuffle_is_not_replaced_by_generated_void(self):
        from unittest.mock import patch
        turbo = card("Turbo", CardType.SKILL, cost=0)
        draw = card("Finesse", CardType.SKILL, cost=0)
        game = GameStub([monster("Cultist", 100)], [turbo, draw], hp=30, energy=0)
        game.draw_pile = [card("Strike_B", CardType.ATTACK, cost=1, damage=6)]
        game.discard_pile = []
        planner = FastCombatPlanner(DefectPowerPriority())
        original = planner._terminal_turn_score
        seen = []

        def capture(g, state, *args):
            result = original(g, state, *args)
            if [step.card.card_id for step in state.plan] == ["Turbo", "Finesse"]:
                seen.append(result[1])
            return result

        with patch.object(planner, "_terminal_turn_score", side_effect=capture):
            planner.choose_card_action(game)
        self.assertTrue(seen)
        for search in seen:
            self.assertEqual(0, search["drawn_void_count"])
            self.assertEqual(2, search["remaining_energy"])


if __name__ == "__main__":
    unittest.main()
