import json
import unittest

from macro_wire import (
    CARD_FACT_COLUMNS,
    POTION_COLUMNS,
    RELIC_CATALOG_COLUMNS,
    RELIC_RUNTIME_COLUMNS,
    decode_candidate_facts,
    decode_deck,
    decode_rows,
    encode_candidate_catalog,
    encode_candidate_facts,
    encode_deck,
    encode_rows,
    wire_schema,
)


def card_facts(**overrides):
    facts = {
        "instance_id": "copy-uuid",
        "card_id": "Strike_R",
        "name": "Strike",
        "type": "ATTACK",
        "rarity": "BASIC",
        "upgrades": 0,
        "cost": 1,
        "cost_kind": "ENERGY",
        "damage": 6,
        "base_damage": 6,
        "block": 0,
        "base_block": 0,
        "magic_number": 0,
        "misc": 0,
        "exhausts": False,
        "ethereal": False,
        "has_target": True,
        "description": "Deal 6 damage.",
        "tags": ["attack"],
    }
    facts.update(overrides)
    return facts


class MacroWireTests(unittest.TestCase):
    def test_deck_round_trip_preserves_every_strategic_fact_and_order(self):
        original = [
            card_facts(instance_id="uuid-a"),
            card_facts(instance_id="uuid-b"),
            card_facts(
                instance_id="uuid-c",
                upgrades=1,
                damage=9,
                description="Deal 9 damage.",
            ),
        ]
        catalog, references = encode_deck(original)
        self.assertEqual([0, 0, 1], references)
        self.assertEqual(2, len(catalog))
        expected = [
            {column: facts.get(column) for column in CARD_FACT_COLUMNS}
            for facts in original
        ]
        self.assertEqual(expected, decode_deck(catalog, references))

    def test_appending_a_new_card_preserves_the_existing_wire_prefix(self):
        initial = [card_facts(), card_facts(instance_id="uuid-b")]
        old_catalog, old_refs = encode_deck(initial)
        new_catalog, new_refs = encode_deck(
            initial + [card_facts(card_id="Defend_R", name="Defend", block=5)]
        )
        self.assertEqual(old_catalog, new_catalog[: len(old_catalog)])
        self.assertEqual(old_refs, new_refs[: len(old_refs)])

    def test_relic_and_potion_rows_are_reversible(self):
        relics = [{"id": "Anchor", "name": "Anchor", "counter": -1,
                   "description": "Start combat with 10 Block."}]
        potions = [{"id": "Dexterity Potion", "name": "Dexterity Potion",
                    "description": "Gain 2 Dexterity.", "can_use": True,
                    "can_discard": True, "requires_target": False}]
        self.assertEqual(
            [{"id": "Anchor", "name": "Anchor"}],
            decode_rows(
                encode_rows(relics, RELIC_CATALOG_COLUMNS),
                RELIC_CATALOG_COLUMNS,
            ),
        )
        self.assertEqual(
            [{"counter": -1, "description": "Start combat with 10 Block."}],
            decode_rows(
                encode_rows(relics, RELIC_RUNTIME_COLUMNS),
                RELIC_RUNTIME_COLUMNS,
            ),
        )
        self.assertEqual(
            potions,
            decode_rows(encode_rows(potions, POTION_COLUMNS), POTION_COLUMNS),
        )

    def test_wire_legend_matches_encoder_columns(self):
        schema = wire_schema()
        self.assertEqual("macro-wire-v4", schema["version"])
        self.assertEqual(list(CARD_FACT_COLUMNS), schema["deck_card_fact_columns"])
        self.assertEqual(
            list(RELIC_CATALOG_COLUMNS), schema["relic_catalog_columns"]
        )
        self.assertEqual(
            list(RELIC_RUNTIME_COLUMNS), schema["relic_runtime_columns"]
        )
        self.assertEqual(list(POTION_COLUMNS), schema["potion_columns"])

    def test_candidate_card_rows_round_trip_strategic_facts(self):
        original = card_facts(instance_id="reward-copy")
        encoded = encode_candidate_facts(original)
        decoded = decode_candidate_facts(encoded)
        expected = {
            column: original.get(column) for column in CARD_FACT_COLUMNS
        }
        self.assertEqual(expected, decoded)
        self.assertNotIn("reward-copy", json.dumps(encoded))

    def test_candidate_catalog_deduplicates_grid_copies_with_exact_ids(self):
        candidates = [
            {
                "candidate_id": f"grid:uuid-{index}",
                "kind": "permanent_card_selection",
                "label": "Strike",
                "local_score": 2.0,
                "facts": card_facts(instance_id=f"uuid-{index}"),
            }
            for index in range(5)
        ]
        catalog, visible = encode_candidate_catalog(candidates)
        self.assertEqual(1, len(catalog))
        self.assertEqual([0] * 5, [item["fact_ref"] for item in visible])
        self.assertEqual(
            [item["candidate_id"] for item in candidates],
            [item["candidate_id"] for item in visible],
        )
        self.assertTrue(all("local_score" not in item for item in visible))

    def test_nested_shop_card_round_trip_preserves_price_and_card_semantics(self):
        original_card = card_facts(instance_id="shop-copy", name="Headbutt")
        original = {
            "price": 55,
            "item_id": "card:Headbutt",
            "description": "Deal damage and retrieve a discarded card.",
            "card": original_card,
        }
        encoded = encode_candidate_facts(original)
        decoded = decode_candidate_facts(encoded)
        self.assertEqual(55, decoded["price"])
        self.assertEqual("card:Headbutt", decoded["item_id"])
        self.assertEqual(original["description"], decoded["description"])
        self.assertEqual(
            {
                column: original_card.get(column)
                for column in CARD_FACT_COLUMNS
            },
            decoded["card"],
        )
        self.assertNotIn("shop-copy", json.dumps(encoded))

    def test_grid_catalog_is_materially_smaller_without_losing_exact_ids(self):
        candidates = [
            {
                "candidate_id": f"grid:uuid-{index}",
                "kind": "permanent_card_selection",
                "label": "Strike",
                "facts": card_facts(instance_id=f"uuid-{index}"),
            }
            for index in range(13)
        ]
        catalog, visible = encode_candidate_catalog(candidates)
        legacy = json.dumps(candidates, separators=(",", ":"))
        compact = json.dumps(
            {"candidate_fact_catalog": catalog, "candidates": visible},
            separators=(",", ":"),
        )
        self.assertLess(len(compact), len(legacy) * 0.45)
        self.assertEqual(
            [item["candidate_id"] for item in candidates],
            [item["candidate_id"] for item in visible],
        )

    def test_duplicate_cards_are_materially_smaller_without_semantic_loss(self):
        legacy = [card_facts(instance_id=f"uuid-{index}") for index in range(5)]
        catalog, references = encode_deck(legacy)
        compact = {"deck_card_facts": catalog, "deck": references}
        legacy_text = json.dumps(legacy, separators=(",", ":"))
        compact_text = json.dumps(compact, separators=(",", ":"))
        self.assertLess(len(compact_text), len(legacy_text) * 0.45)


if __name__ == "__main__":
    unittest.main()
