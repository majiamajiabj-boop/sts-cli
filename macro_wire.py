"""Lossless compact wire encoding for DeepSeek macro snapshots.

The model does not need per-copy deck UUIDs, but it does need every strategic
card fact and the deck's acquisition order. A first-occurrence catalog plus an
ordered reference list preserves those semantics while avoiding repeated JSON
keys and duplicate starter-card descriptions.
"""

from __future__ import annotations

import json


WIRE_SCHEMA_VERSION = "macro-wire-v4"

CARD_FACT_COLUMNS = (
    "card_id",
    "name",
    "type",
    "rarity",
    "upgrades",
    "cost",
    "cost_kind",
    "damage",
    "base_damage",
    "block",
    "base_block",
    "magic_number",
    "misc",
    "exhausts",
    "ethereal",
    "has_target",
    "description",
    "tags",
)

RELIC_CATALOG_COLUMNS = ("id", "name")
RELIC_RUNTIME_COLUMNS = ("counter", "description")
POTION_COLUMNS = (
    "id", "name", "description", "can_use", "can_discard", "requires_target"
)


def wire_schema():
    """Return the immutable legend placed in the provider-cache prefix."""

    return {
        "version": WIRE_SCHEMA_VERSION,
        "deck_card_fact_columns": list(CARD_FACT_COLUMNS),
        "deck_encoding": (
            "deck_card_facts contains unique rows in first-occurrence order; "
            "deck is the ordered list of integer row references, so repeated "
            "references are exact copy counts. Deck UUIDs carry no strategic "
            "meaning and are intentionally omitted."
        ),
        "card_value_semantics": (
            "damage and block are resolved values: current nonnegative value "
            "else nonnegative base value else null. cost_kind is ENERGY, X, "
            "or UNPLAYABLE; null means unknown rather than zero. misc preserves "
            "permanent card counters such as Ritual Dagger scaling."
        ),
        "relic_catalog_columns": list(RELIC_CATALOG_COLUMNS),
        "relic_runtime_columns": list(RELIC_RUNTIME_COLUMNS),
        "relic_encoding": (
            "relic_catalog and relic_runtime are aligned by row index. The "
            "catalog contains stable identity while runtime contains mutable "
            "counter and description values."
        ),
        "potion_columns": list(POTION_COLUMNS),
        "candidate_binding": (
            "Candidate candidate_id remains the exact selectable identity."
        ),
        "candidate_fact_encoding": (
            "candidate_fact_catalog contains unique fact objects. Each "
            "candidate.fact_ref selects one catalog entry. A card_row uses "
            "deck_card_fact_columns; candidate_id remains authoritative and "
            "per-copy UUIDs are intentionally omitted."
        ),
        "heart_plan_encoding": (
            "heart_plan contains stable/deadline facts; heart_plan_runtime "
            "contains the volatile floor_in_act field. Together they equal "
            "the complete Heart plan snapshot."
        ),
    }


def encode_rows(items, columns):
    """Encode dictionaries as positional rows without dropping field values."""

    return [
        [item.get(column) for column in columns]
        for item in items
    ]


def decode_rows(rows, columns):
    """Decode positional rows; used by semantic-equivalence verification."""

    return [dict(zip(columns, row)) for row in rows]


def encode_deck(card_facts):
    """Return a unique fact catalog and acquisition-ordered references."""

    catalog = []
    references = []
    row_indexes = {}
    for facts in card_facts:
        row = [facts.get(column) for column in CARD_FACT_COLUMNS]
        key = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        index = row_indexes.get(key)
        if index is None:
            index = len(catalog)
            row_indexes[key] = index
            catalog.append(row)
        references.append(index)
    return catalog, references


def decode_deck(catalog, references):
    """Reconstruct every strategic deck fact in acquisition order."""

    decoded_catalog = decode_rows(catalog, CARD_FACT_COLUMNS)
    return [dict(decoded_catalog[index]) for index in references]


def encode_candidate_facts(facts):
    """Compact candidate facts without dropping strategic information."""

    if not isinstance(facts, dict):
        return facts
    encoded = dict(facts)
    nested_card = encoded.pop("card", None)
    if isinstance(nested_card, dict):
        encoded["card_row"] = [
            nested_card.get(column) for column in CARD_FACT_COLUMNS
        ]
        return encoded
    if "card_id" in encoded:
        row = [encoded.get(column) for column in CARD_FACT_COLUMNS]
        extras = {
            key: value
            for key, value in encoded.items()
            if key not in CARD_FACT_COLUMNS and key != "instance_id"
        }
        return {"card_row": row, **extras}
    return encoded


def decode_candidate_facts(encoded):
    """Decode candidate facts for semantic-equivalence tests and tooling."""

    if not isinstance(encoded, dict) or "card_row" not in encoded:
        return encoded
    decoded = dict(encoded)
    row = decoded.pop("card_row")
    card = dict(zip(CARD_FACT_COLUMNS, row))
    if any(key in decoded for key in ("price", "item_id")):
        decoded["card"] = card
        return decoded
    return {**card, **decoded}


def encode_candidate_catalog(candidates):
    """Return a unique fact catalog and candidates bound to exact references."""

    catalog = []
    indexes = {}
    visible = []
    for original in candidates:
        candidate = dict(original)
        candidate.pop("local_score", None)
        facts = candidate.pop("facts", None)
        if facts is not None:
            compact = encode_candidate_facts(facts)
            key = json.dumps(
                compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            index = indexes.get(key)
            if index is None:
                index = len(catalog)
                indexes[key] = index
                catalog.append(compact)
            candidate["fact_ref"] = index
        visible.append(candidate)
    return catalog, visible
