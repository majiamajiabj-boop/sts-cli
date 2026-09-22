import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.heart_plan import HeartPlan


def game(*, act=1, floor=8, ruby=False, emerald=False, sapphire=False):
    return SimpleNamespace(
        act=act,
        floor=floor,
        has_ruby_key=ruby,
        has_emerald_key=emerald,
        has_sapphire_key=sapphire,
    )


class HeartPlanTests(unittest.TestCase):
    def test_unlock_mode_never_collects_act_four_keys(self):
        plan = HeartPlan.from_game(game(), "UNLOCK")
        self.assertFalse(plan.active)
        self.assertFalse(plan.ruby_missing)
        self.assertFalse(plan.emerald_missing)

    def test_ready_act_one_burning_elite_is_preferred_window(self):
        plan = HeartPlan.from_game(game(act=1, floor=8), "HEART")
        self.assertGreater(
            plan.emerald_bonus(burning=True, healthy=True, ready=True),
            200,
        )
        self.assertLess(
            plan.emerald_bonus(burning=True, healthy=False, ready=True),
            50,
        )

    def test_act_one_emerald_window_uses_projected_node_floor(self):
        plan = HeartPlan.from_game(game(act=1, floor=0), "HEART")
        self.assertGreater(
            plan.emerald_bonus(
                burning=True,
                healthy=True,
                ready=True,
                floor_in_act=8,
            ),
            200,
        )

    def test_healthy_developing_act_one_route_keeps_floor_thirteen_key_value(self):
        plan = HeartPlan.from_game(game(act=1, floor=9), "HEART")

        self.assertGreater(
            plan.emerald_bonus(
                burning=True,
                healthy=True,
                ready=False,
                floor_in_act=13,
            ),
            100,
        )
        self.assertLess(
            plan.emerald_bonus(
                burning=True,
                healthy=False,
                ready=False,
                floor_in_act=13,
            ),
            50,
        )

    def test_healthy_developing_act_two_route_has_deadline_value(self):
        plan = HeartPlan.from_game(game(act=2, floor=18), "HEART")

        self.assertGreater(
            plan.emerald_bonus(
                burning=True,
                healthy=True,
                ready=False,
            ),
            100,
        )
        self.assertLess(
            plan.emerald_bonus(
                burning=True,
                healthy=False,
                ready=False,
            ),
            50,
        )

    def test_emerald_route_preservation_begins_in_act_two(self):
        self.assertFalse(HeartPlan.from_game(game(act=1), "HEART").must_preserve_emerald_route())
        self.assertTrue(HeartPlan.from_game(game(act=2), "HEART").must_preserve_emerald_route())
        self.assertTrue(HeartPlan.from_game(game(act=3), "HEART").must_preserve_emerald_route())

    def test_sapphire_uses_relic_opportunity_cost_before_deadline(self):
        act_two = HeartPlan.from_game(game(act=2), "HEART")
        self.assertTrue(act_two.should_take_sapphire(25))
        self.assertFalse(act_two.should_take_sapphire(45))
        self.assertTrue(HeartPlan.from_game(game(act=3), "HEART").should_take_sapphire(100))

    def test_sapphire_opportunity_value_respects_target_window_boundaries(self):
        cases = (
            (1, 3.99, True),
            (1, 4.0, True),
            (1, 4.01, False),
            (1, 10.0, False),
            (2, 29.99, True),
            (2, 30.0, True),
            (2, 30.01, False),
            (2, 45.0, False),
            (3, 10000.0, True),
        )
        for act, relic_score, expected in cases:
            with self.subTest(act=act, relic_score=relic_score):
                plan = HeartPlan.from_game(game(act=act), "HEART")
                self.assertEqual(
                    expected,
                    plan.should_take_sapphire(relic_score),
                )

    def test_sapphire_snapshot_exposes_the_same_public_window_value(self):
        act_one = HeartPlan.from_game(game(act=1), "HEART")
        act_two = HeartPlan.from_game(game(act=2), "HEART")
        act_three = HeartPlan.from_game(game(act=3), "HEART")

        self.assertEqual(2, act_one.snapshot()["sapphire_target_act"])
        self.assertEqual(
            act_one.sapphire_opportunity_value(),
            act_one.snapshot()["sapphire_opportunity_value"],
        )
        self.assertEqual(4.0, act_one.sapphire_opportunity_value())
        self.assertFalse(act_one.snapshot()["sapphire_mandatory"])

        self.assertEqual(30.0, act_two.sapphire_opportunity_value())
        self.assertEqual(
            act_two.sapphire_opportunity_value(),
            act_two.snapshot()["sapphire_opportunity_value"],
        )
        self.assertFalse(act_two.snapshot()["sapphire_mandatory"])

        self.assertIsNone(act_three.sapphire_opportunity_value())
        self.assertIsNone(
            act_three.snapshot()["sapphire_opportunity_value"]
        )
        self.assertTrue(act_three.snapshot()["sapphire_mandatory"])

    def test_sapphire_already_collected_has_no_opportunity_value(self):
        plan = HeartPlan.from_game(game(act=1, sapphire=True), "HEART")

        self.assertFalse(plan.should_take_sapphire(0))
        self.assertIsNone(plan.sapphire_opportunity_value())

    def test_ruby_recall_uses_safe_windows_and_exceptional_actions(self):
        act_two = HeartPlan.from_game(game(act=2, floor=28), "HEART")
        self.assertTrue(act_two.should_recall(0.95, 14, can_recover=False))
        self.assertTrue(
            act_two.should_recall(
                0.95,
                14,
                can_recover=True,
                recovery_ratio=0.05,
            )
        )
        self.assertFalse(
            act_two.should_recall(0.95, 38, can_recover=False)
        )
        self.assertFalse(
            act_two.should_recall(
                0.70,
                4,
                can_recover=True,
                recovery_ratio=0.30,
            )
        )
        act_three = HeartPlan.from_game(game(act=3, floor=40), "HEART")
        self.assertTrue(
            act_three.should_recall(
                0.69,
                24,
                can_recover=True,
                recovery_ratio=0.30,
            )
        )
        self.assertFalse(
            act_three.should_recall(
                0.60,
                24,
                can_recover=True,
                recovery_ratio=0.30,
            )
        )
        final_fire = HeartPlan.from_game(game(act=3, floor=49), "HEART")
        self.assertTrue(
            final_fire.should_recall(0.20, 99, can_recover=True)
        )

    def test_recall_uses_healthy_act_two_final_fire_as_safe_deadline(self):
        act_one = HeartPlan.from_game(game(act=1, floor=8), "HEART")
        self.assertEqual(7.0, act_one.recall_opportunity_value())
        self.assertTrue(
            act_one.act_one_recall_is_candidate(
                0.95,
                can_recover=True,
                recovery_ratio=0.05,
            )
        )
        self.assertFalse(
            act_one.act_one_recall_is_candidate(
                0.80,
                can_recover=True,
                recovery_ratio=0.20,
            )
        )

        act_two = HeartPlan.from_game(game(act=2, floor=28), "HEART")
        self.assertFalse(act_two.recall_is_mandatory())
        self.assertEqual(9.0, act_two.recall_opportunity_value())

        act_two_final = HeartPlan.from_game(game(act=2, floor=32), "HEART")
        self.assertTrue(act_two_final.recall_is_mandatory(hp_ratio=0.84))
        self.assertFalse(act_two_final.recall_is_mandatory(hp_ratio=0.65))
        self.assertEqual(
            1000.0,
            act_two_final.recall_opportunity_value(hp_ratio=0.84),
        )

        act_three = HeartPlan.from_game(game(act=3, floor=42), "HEART")
        self.assertFalse(act_three.recall_is_mandatory(14))
        self.assertTrue(act_three.recall_is_mandatory(15))
        self.assertEqual(1000.0, act_three.recall_opportunity_value(15))


if __name__ == "__main__":
    unittest.main()
