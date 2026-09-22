import copy
import unittest

from lightspeed_probe import import_gaps, prospective_spec


class ImportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"game_state": {"class": "IRONCLAD", "deck": [
            {"id": "Bash", "in_bottle_flame": True}], "combat_state": {
            "monsters": [{"move_id": 1}], "cards_played_this_turn": 0,
            "attacks_played_this_turn": 0, "skills_played_this_turn": 0,
            "powers_played_this_combat": 0, "times_damaged": 0}}}

    def test_missing_history_is_not_interpreted_as_zero(self):
        self.assertEqual([], import_gaps(self.state))
        del self.state["game_state"]["combat_state"]["cards_played_this_turn"]
        self.assertIn("combat.cards_played_this_turn", import_gaps(self.state))

    def test_reviving_enemy_still_requires_move_identity(self):
        self.state["game_state"]["combat_state"]["monsters"] = [
            {"is_gone": True, "half_dead": True}]
        self.assertIn("monster.move_id", import_gaps(self.state))

    def test_defect_missing_history_is_rejected(self):
        self.state["game_state"]["class"] = "DEFECT"
        gaps = import_gaps(self.state)
        self.assertIn("combat.emotion_chip_pending", gaps)
        self.assertIn("combat.frost_channeled", gaps)

    def test_actual_protocol_defect_history_names_are_accepted(self):
        self.state["game_state"]["class"]="DEFECT"
        self.state["game_state"]["combat_state"].update(
            lightning_channeled=0,frost_channeled=0,emotion_chip_pending=False)
        self.assertEqual(import_gaps(self.state),[])

    def test_silent_is_not_silently_simulated_as_ironclad(self):
        self.state["game_state"]["class"] = "THE_SILENT"
        self.assertIn("unsupported_character", import_gaps(self.state))
        with self.assertRaises(ValueError):
            prospective_spec(self.state, "CHAMP")

    def test_new_encounter_preserves_bottle_and_does_not_mutate_source(self):
        before = copy.deepcopy(self.state)
        result = prospective_spec(self.state, "CHAMP")
        self.assertTrue(result["game_state"]["deck"][0]["bottled"])
        self.assertNotIn("combat_state", result["game_state"])
        self.assertEqual(before, self.state)

    def test_permanently_scaled_card_cannot_lose_its_history(self):
        self.state["game_state"]["deck"] = [{"id": "RitualDagger"}]
        with self.assertRaises(ValueError):
            prospective_spec(self.state, "CHAMP")


if __name__ == "__main__":
    unittest.main()
