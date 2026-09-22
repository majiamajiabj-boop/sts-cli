"""Portable regressions from the six-run e548391b0ad3a7ee cohort.

These tests replay decisions, not whole fights or hypothetical wins.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.agent import SimpleAgent
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import IroncladPriority, SilentPriority, DefectPowerPriority
from spirecomm.spire.game import Game
from spirecomm.spire.power import Power

FRAMES = json.loads((ROOT / "test_fixtures" / "six_run_strategy_frames.json").read_text(encoding="utf-8"))


def historical_game(seq):
    raw = copy.deepcopy(FRAMES[str(seq)]["game_state"])
    # Audit envelopes use stable instance IDs rather than wire UUIDs.
    def restore(value):
        if isinstance(value, dict):
            if "card_instance_id" in value:
                value.setdefault("uuid", value["card_instance_id"])
            for item in value.values():
                restore(item)
        elif isinstance(value, list):
            for item in value:
                restore(item)
    restore(raw)
    raw.update(map=[], screen_type="NONE", screen_state={})
    return Game.from_json(raw, ["play", "end", "potion"])


def planner_for(game):
    priorities = {
        "IRONCLAD": IroncladPriority,
        "THE_SILENT": SilentPriority,
        "DEFECT": DefectPowerPriority,
    }
    return FastCombatPlanner(priorities[game.character.name]())


class SixRunStrategyRegressions(unittest.TestCase):
    def test_bandits_reject_historical_low_hp_and_cheap_pay_frames(self):
        for seq in (511352, 512145, 513082):
            with self.subTest(seq=seq):
                game = historical_game(seq)
                agent = SimpleAgent(game.character)
                agent.game = game
                profile = agent._masked_bandits_decision_profile()
                self.assertLess(profile["scores"]["fight"], profile["scores"]["pay"])
                if seq == 512145:
                    self.assertEqual(0, profile["expected_post_fight_healing"])
                if seq != 513082:
                    self.assertGreater(profile["death_risk"], 0)

    def test_bandits_full_health_frame_is_not_blanket_banned(self):
        game = historical_game(513821)
        agent = SimpleAgent(game.character)
        agent.game = game
        profile = agent._masked_bandits_decision_profile()
        self.assertGreater(profile["scores"]["fight"], profile["scores"]["pay"])
        self.assertGreater(profile["upper_hp_loss_estimate"], 20)

    def test_wraith_form_is_not_an_offensive_scaling_engine(self):
        game = historical_game(511352)
        agent = SimpleAgent(game.character)
        agent.game = game
        self.assertLess(agent._deck_readiness()["kill_clock"]["score"], 0.5)

    def test_demon_form_takes_bounded_guardian_setup_window(self):
        game = historical_game(510911)
        planner = planner_for(game)
        action = planner.choose_card_action(game)
        self.assertEqual("Demon Form", getattr(getattr(action, "card", None), "card_id", None))
        self.assertLess(planner.last_decision["search"]["actual_loss"], game.current_hp)

    def test_demon_form_does_not_spend_unsafe_low_hp_window(self):
        game = historical_game(510911)
        game.current_hp = game.player.current_hp = 9
        action = planner_for(game).choose_card_action(game)
        self.assertNotEqual("Demon Form", getattr(getattr(action, "card", None), "card_id", None))

    def test_echo_form_starts_before_spending_energy_on_spheric_block(self):
        game = historical_game(511784)
        action = planner_for(game).choose_card_action(game)
        self.assertEqual("Echo Form", getattr(getattr(action, "card", None), "card_id", None))

    def test_biased_cognition_waits_against_high_hp_giant_head(self):
        game = historical_game(514327)
        action = planner_for(game).choose_card_action(game)
        self.assertNotEqual("Biased Cognition", getattr(getattr(action, "card", None), "card_id", None))

    def test_biased_cognition_with_artifact_keeps_its_value(self):
        game = historical_game(514327)
        game.player.powers.append(Power("Artifact", "Artifact", 1))
        action = planner_for(game).choose_card_action(game)
        self.assertEqual("Biased Cognition", getattr(getattr(action, "card", None), "card_id", None))

    def test_apparition_stacks_protection_before_expiring(self):
        game = historical_game(513321)
        action = planner_for(game).choose_card_action(game)
        self.assertEqual("Ghostly", getattr(getattr(action, "card", None), "card_id", None))

    def test_inserter_empty_slots_do_not_justify_paid_capacitor(self):
        for seq in (511819, 511823, 511854, 511929):
            with self.subTest(seq=seq):
                game = historical_game(seq)
                action = planner_for(game).choose_card_action(game)
                self.assertNotEqual("Capacitor", getattr(getattr(action, "card", None), "card_id", None))

    def test_electrodynamics_beats_redundant_capacity_when_affordable(self):
        game = historical_game(511854)
        game.player.energy = 2
        for card in game.hand:
            card.is_playable = 0 <= card.cost <= 2
        action = planner_for(game).choose_card_action(game)
        self.assertEqual("Electrodynamics", action.card.card_id)

    def test_full_slots_and_real_channels_still_value_capacitor(self):
        game = historical_game(511819)
        game.relics = []
        game.player.energy = 1
        game.player.orbs = [copy.deepcopy(game.player.orbs[0]) for _ in range(3)]
        game.hand = [card for card in game.hand if card.card_id == "Capacitor"]
        action = planner_for(game).choose_card_action(game)
        self.assertEqual("Capacitor", action.card.card_id)

    def test_capacitor_cannot_expand_beyond_ten_slots(self):
        game = historical_game(511819)
        game.player.orbs = [copy.deepcopy(game.player.orbs[0]) for _ in range(10)]
        game.hand = [card for card in game.hand if card.card_id == "Capacitor"]
        action = planner_for(game).choose_card_action(game)
        self.assertNotEqual("Capacitor", getattr(getattr(action, "card", None), "card_id", None))

    def test_apparition_does_not_invent_extra_future_turns(self):
        game = historical_game(513321)
        ghost = next(card for card in game.hand if card.card_id == "Ghostly")
        planner = planner_for(game)
        turns = planner._expected_remaining_turns(game)
        life = planner._lifecycle_evaluation(game, ghost, 0, intangible_turns=turns)
        self.assertEqual(0, life["adjustment"])

    def test_copied_apparition_counts_the_second_future_stack(self):
        game = historical_game(513321)
        ghost = next(card for card in game.hand if card.card_id == "Ghostly")
        planner = planner_for(game)
        life = planner._lifecycle_evaluation(
            game, ghost, 0, intangible_turns=0, resolution_copies=2
        )
        self.assertEqual(2, life["intangible_turns"])
        self.assertGreater(life["adjustment"], 0)

    def test_one_artifact_does_not_protect_both_biased_copies(self):
        game = historical_game(514327)
        card = next(card for card in game.hand if card.card_id == "Biased Cognition")
        planner = planner_for(game)
        unprotected = planner._lifecycle_evaluation(
            game, card, 0, artifact=0, resolution_copies=2
        )
        partial = planner._lifecycle_evaluation(
            game, card, 0, artifact=1, resolution_copies=2
        )
        self.assertGreater(unprotected["cost"], partial["cost"])

    def test_biased_cognition_short_finish_window_is_not_banned(self):
        game = historical_game(514327)
        game.monsters[0].current_hp = 15
        card = next(card for card in game.hand if card.card_id == "Biased Cognition")
        life = planner_for(game)._lifecycle_evaluation(game, card, 0)
        self.assertEqual(0, life["cost"])
        self.assertGreaterEqual(life["adjustment"], 0)

    def test_frost_alone_does_not_satisfy_damage_scaling(self):
        game = historical_game(511819)
        game.deck = [card for card in game.deck if card.card_id in {
            "Cold Snap", "Coolheaded", "Capacitor", "Defragment",
        }]
        agent = SimpleAgent(game.character)
        agent.game = game
        self.assertEqual(0, agent._damage_scaling_support(agent._deck_profile()))


if __name__ == "__main__":
    unittest.main()
