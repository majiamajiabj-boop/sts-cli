"""Observation boundaries needed for combat reconstruction, not score mirrors."""
import unittest
import autoplay


class CombatSnapshotHistoryTests(unittest.TestCase):
    def snapshot(self, combat):
        return autoplay.authoritative_state_snapshot({
            "phase": "COMBAT_TURN_2", "game_state": {
                "room_phase": "COMBAT", "combat_state": combat,
            },
        })["game_state"]["combat_state"]

    def test_history_survives_with_zero_distinct_from_missing(self):
        history = {
            "turn": 2, "draw_pile_order_known": False, "cards_played_this_turn": 0,
            "attacks_played_this_turn": 0, "skills_played_this_turn": 0,
            "powers_played_this_combat": 3, "cards_discarded_this_turn": 1,
            "times_damaged": 7, "lightning_channeled": 0, "frost_channeled": 4,
            "emotion_chip_pending": False, "centennial_puzzle_used_this_combat": True,
        }
        actual = self.snapshot(history)
        for key, value in history.items():
            self.assertEqual(actual[key], value, key)
        missing = self.snapshot({"turn": 2})
        for key in history.keys() - {"turn"}:
            self.assertNotIn(key, missing, key)

    def test_move_identity_dynamic_cards_and_orb_capacity_survive(self):
        card = {"id": "Claw", "misc": 0, "damage": 7, "base_damage": 7,
                "combat_cost": 1, "cost": 0, "free_to_play_once": True,
                "retain": True, "ethereal": False, "card_instance_id": "c1"}
        monster = {"id": "TimeEater", "move_id": 2, "last_move_id": 3,
                   "second_last_move_id": 1, "move_base_damage": 9,
                   "miscBool": True, "is_escaping": False,
                   "powers": [{"id": "Stasis", "amount": 1, "card": card}]}
        actual = self.snapshot({"player": {"orb_slots": 0, "max_orbs": 0,
                                           "facing_left": False},
                                "monsters": [monster], "hand": [card], "card_in_play": card})
        for key in monster.keys() - {"powers"}:
            self.assertEqual(actual["monsters"][0][key], monster[key])
        self.assertEqual(actual["hand"][0], card)
        self.assertEqual(actual["card_in_play"], card)
        self.assertEqual(actual["monsters"][0]["powers"][0]["card"], card)
        self.assertEqual(actual["player"]["orb_slots"], 0)
        self.assertNotIn("orb_slots", self.snapshot({})["player"])

    def test_absent_hidden_intents_and_future_rng_are_not_invented(self):
        actual = self.snapshot({"monsters": [{"id": "Champ", "intent": "NONE"}],
                                "rng": {"ai": 123}, "future_draw_order": ["secret"]})
        self.assertNotIn("move_id", actual["monsters"][0])
        self.assertNotIn("rng", actual)
        self.assertNotIn("future_draw_order", actual)


if __name__ == "__main__":
    unittest.main()
