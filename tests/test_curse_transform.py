import json
import unittest
from copy import deepcopy
from pathlib import Path
import independent_oracle as oracle
from tests.test_terminal_survival import game_for, SimpleAgent
from spirecomm.spire.screen import GridSelectScreen, ScreenType

RECORDS = json.loads((Path(__file__).resolve().parents[1] / "test_fixtures/curse_transform_records.json").read_text(encoding="utf-8"))


class CurseTransformTests(unittest.TestCase):
    def agent(self, purge=False):
        raw = RECORDS[1]["authoritative_state_before"]
        game = game_for(raw)
        screen = deepcopy(raw["game_state"]["screen_state"])
        for card in screen["cards"]:
            card["uuid"] = card["card_instance_id"]
        if purge:
            screen.update(for_purge=True, for_transform=False, parent_choice_context=None)
        game.screen = GridSelectScreen.from_json(screen)
        game.screen_type = ScreenType.GRID
        game.in_combat = False
        agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
        agent.game = game
        return agent

    def test_logged_astrolabe_keeps_curse_out_of_three_transforms(self):
        agent = self.agent()
        action = agent.choose_grid_action()
        self.assertEqual(3, len(action.cards))
        self.assertNotIn("Writhe", [c.card_id for c in action.cards])

    def test_removing_three_still_prioritizes_curse(self):
        agent = self.agent(purge=True)
        action = agent.choose_grid_action()
        self.assertIn("Writhe", [c.card_id for c in action.cards])

    def test_single_transform_and_mandatory_curse_only_grid(self):
        agent = self.agent()
        agent.game.screen.num_cards = 1
        self.assertNotEqual("Writhe", agent.choose_grid_action().cards[0].card_id)
        agent = self.agent()
        agent.game.screen.cards = [c for c in agent.game.screen.cards if c.card_id == "Writhe"]
        agent.game.screen.num_cards = 1
        self.assertEqual("Writhe", agent.choose_grid_action().cards[0].card_id)

    def check_records(self, records):
        record = records[1]
        effect = oracle._one_grid_confirmation_effect(record, "grid_transform")
        expected = {key: record.get(key) for key in oracle.ATTEMPT_BINDING_FIELDS}
        return oracle._grid_deferred_settlement(records, 1, record, effect, expected)

    def test_logged_nonupgradeable_curse_settlement_is_valid(self):
        status, details = self.check_records(deepcopy(RECORDS))
        self.assertEqual("clear", status, details)

    def mutate_added(self, records, mutate):
        # Mutate all mirrored representations consistently: otherwise this
        # would test only envelope disagreement, not the transformation rule.
        target = RECORDS[3]["authoritative_state_after"]["game_state"]["deck"]
        gained = next(c["card_instance_id"] for c in target if c["id"] == "Predator")
        def walk(value):
            if isinstance(value, dict):
                if value.get("card_instance_id") == gained:
                    mutate(value)
                for item in list(value.values()):
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(records)

    def test_ordinary_card_missing_upgrade_is_still_rejected(self):
        records = deepcopy(RECORDS)
        self.mutate_added(records, lambda card: card.update(upgrades=0))
        self.assertNotEqual("clear", self.check_records(records)[0])

    def test_wrong_curse_pool_is_still_rejected(self):
        records = deepcopy(RECORDS)
        self.mutate_added(records, lambda card: card.update(id="Doubt", type="CURSE", rarity="CURSE", upgrades=0))
        self.assertNotEqual("clear", self.check_records(records)[0])

    def test_wrong_selected_instance_is_still_rejected(self):
        records = deepcopy(RECORDS)
        before = records[1]["authoritative_state_before"]["game_state"]["deck"]
        curse = next(card for card in before if card["id"] == "Writhe")
        # Keep the selected curse and remove an unselected Defend instead.
        # Counts remain three-for-three, so the exact selected UUID check
        # must reject this transaction.
        after = records[3]["authoritative_state_after"]["game_state"]["deck"]
        victim = next(card for card in after if card["id"] == "Defend_G")
        after.remove(victim)
        after.append(deepcopy(curse))
        records[3]["observable_state_after"]["deck"] = deepcopy(after)
        self.assertNotEqual("clear", self.check_records(records)[0])


if __name__ == "__main__":
    unittest.main()
