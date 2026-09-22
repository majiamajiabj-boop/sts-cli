import json
import unittest
from pathlib import Path

from tests.test_terminal_survival import game_for, SimpleAgent
from tests.test_combat_survival_regressions import card, CardType, Orb
from spirecomm.communication.action import EndTurnAction, PlayCardAction

FRAMES = json.loads(
    (Path(__file__).resolve().parents[1] / "test_fixtures/lifecycle_fallback_frames.json")
    .read_text(encoding="utf-8")
)


class LifecycleFallbackTests(unittest.TestCase):
    def replay(self, name="unprotected_champ", change=None):
        game = game_for(FRAMES[name]["frame"])
        if change:
            change(game)
        planner = SimpleAgent(
            game.character, goal_mode="HEART", macro_advisor=None,
        ).combat_planner
        action = planner.choose_card_action(game)
        return game, planner, action

    def test_logged_healthy_champ_does_not_spend_bias_for_five_hp(self):
        _, planner, action = self.replay()
        self.assertIsInstance(action, EndTurnAction)
        self.assertEqual(10, planner.last_decision["search"]["projected_loss"])
        rejected = planner.last_decision["lifecycle_fallback_rejections"]
        self.assertEqual("Biased Cognition", rejected[0]["card_id"])
        self.assertAlmostEqual(84.3, rejected[0]["lifecycle_liability"])
        # Both the simple Block fallback and the exact single-card fallback
        # must respect this rejection.
        self.assertIsNone(planner._last_verified_fallback)

    def test_logged_artifact_protected_counterexample_still_plays(self):
        _, planner, action = self.replay("protected_champ")
        self.assertIsInstance(action, PlayCardAction)
        self.assertEqual("Biased Cognition", action.card.card_id)
        self.assertEqual(0, planner.last_decision["search"]["lifecycle_liability"])

    def test_negative_lifecycle_can_still_prevent_immediate_death(self):
        def change(game):
            game.player.current_hp = game.current_hp = 8
        _, planner, action = self.replay(change=change)
        self.assertEqual("Biased Cognition", action.card.card_id)
        self.assertEqual(0, planner._last_initial_search["tier"])
        self.assertGreater(planner.last_decision["search"]["tier"], 0)
        self.assertEqual(5, planner.last_decision["search"]["projected_loss"])

    def test_lifecycle_guard_does_not_veto_proven_combat_end(self):
        def change(game):
            game.monsters[0].current_hp = 20
            game.monsters[0].powers = []
            game.player.orbs = [Orb("Lightning", "Lightning", 8, 3) for _ in range(3)]
        _, planner, action = self.replay(change=change)
        self.assertEqual("Biased Cognition", action.card.card_id)
        self.assertFalse(planner._last_initial_search["true_combat_end"])
        self.assertTrue(planner.last_decision["search"]["true_combat_end"])

    def test_ordinary_defense_remains_available_beside_rejected_power(self):
        def change(game):
            game.hand.append(card("Defend_B", CardType.SKILL, cost=1, block=5))
        _, planner, action = self.replay(change=change)
        self.assertEqual("Defend_B", action.card.card_id)
        self.assertEqual(5, planner.last_decision["search"]["projected_loss"])


if __name__ == "__main__":
    unittest.main()
