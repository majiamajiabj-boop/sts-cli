import copy
import itertools
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import (
    DefectPowerPriority,
    IroncladPriority,
    SilentPriority,
)
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Player
from spirecomm.spire.game import Game
from spirecomm.spire.power import Power


FIXTURE = (
    ROOT
    / "test_fixtures"
    / "silent_slime_boss_attempt_137a936b_seq88863.json"
)
LAGAVULIN_FIXTURE = (
    ROOT
    / "test_fixtures"
    / "ironclad_lagavulin_attempt_59e6bac7_seq291512.json"
)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def load_lagavulin_fixture():
    return json.loads(LAGAVULIN_FIXTURE.read_text(encoding="utf-8"))


def card_from_lagavulin_fixture(value, index):
    card_type = CardType[str(value["type"])]
    return Card(
        value["id"],
        value["id"],
        card_type,
        CardRarity.COMMON,
        cost=int(value["cost"]),
        uuid=f'{value["id"]}-{index}',
        has_target=card_type == CardType.ATTACK,
        is_playable=True,
        damage=int(value.get("damage", 0)),
        base_damage=int(value.get("damage", 0)),
        block=int(value.get("block", 0)),
        base_block=int(value.get("block", 0)),
    )


def lagavulin_game(fixture):
    enemy_value = fixture["monster"]
    enemy = Monster(
        "Lagavulin",
        enemy_value["id"],
        enemy_value["max_hp"],
        enemy_value["current_hp"],
        enemy_value["block"],
        Intent[enemy_value["intent"]],
        False,
        False,
        move_adjusted_damage=enemy_value["damage"],
        move_hits=enemy_value["hits"],
    )
    enemy.monster_index = 0
    enemy.powers = [
        Power("Vulnerable", "Vulnerable", enemy_value["vulnerable"]),
        Power("Metallicize", "Metallicize", enemy_value["metallicize"]),
    ]
    hand = [
        card_from_lagavulin_fixture(value, index)
        for index, value in enumerate(fixture["hand"])
    ]
    offset = len(hand)
    draw_pile = [
        card_from_lagavulin_fixture(value, offset + index)
        for index, value in enumerate(fixture["remaining_combat_cards"])
    ]
    player_value = fixture["player"]
    player = Player(
        player_value["max_hp"],
        player_value["current_hp"],
        energy=player_value["energy"],
    )
    player.powers = [
        Power("Dexterity", "Dexterity", player_value["dexterity"])
    ]
    return type("LagavulinGame", (), {
        "monsters": [enemy],
        "hand": hand,
        "draw_pile": draw_pile,
        "discard_pile": [],
        "player": player,
        "act": 1,
        "floor": fixture["source"]["floor"],
        "turn": fixture["source"]["turn"],
        "room_type": "MonsterRoomElite",
        "relics": [],
    })()


def replay_cards(game, cards, split_threshold):
    """Independent mechanics oracle for this fully deterministic incident.

    Choke applies its life-loss trigger to each later card. Dagger Spray's
    serialized damage is per hit, so the shared attack profile supplies its
    two guaranteed hits. Piercing Wail reduces the pending Slam. This helper
    intentionally knows nothing about planner scores or transition flags.
    """

    boss = game.monsters[0]
    boss_hp = int(boss.current_hp)
    energy = int(game.player.energy)
    incoming = int(boss.move_adjusted_damage) * max(1, int(boss.move_hits))
    choke_amount = 0
    card_ids = []
    for card in cards:
        card_ids.append(card.card_id)
        energy -= max(0, int(card.cost or 0))
        if energy < 0:
            raise AssertionError("planner emitted an unaffordable sequence")
        if card.type == CardType.ATTACK:
            guaranteed_hits = 2 if card.card_id == "Dagger Spray" else 1
            raw_damage = max(0, int(card.damage or 0)) * guaranteed_hits
            boss_hp = max(0, boss_hp - raw_damage)
        if choke_amount > 0:
            boss_hp = max(0, boss_hp - choke_amount)
        if card.card_id == "Choke":
            choke_amount += max(0, int(card.magic_number or 0))
        elif card.card_id == "PiercingWail":
            incoming = max(0, incoming - max(0, int(card.magic_number or 0)))
    slam_cancelled = boss.current_hp > split_threshold >= boss_hp
    return {
        "card_ids": card_ids,
        "final_boss_hp": boss_hp,
        "slam_cancelled": slam_cancelled,
        "hp_loss": 0 if slam_cancelled or boss_hp <= 0 else incoming,
    }


def replay_planned_damage(game, planned_sequence, split_threshold):
    cards_by_uuid = {card.uuid: card for card in game.hand}
    return replay_cards(
        game,
        [cards_by_uuid[entry["card_uuid"]] for entry in planned_sequence],
        split_threshold,
    )


def best_legal_outcome(game, split_threshold):
    """Exhaustively derive the Pareto-best result without calling planner."""

    outcomes = [replay_cards(game, (), split_threshold)]
    for length in range(1, len(game.hand) + 1):
        for sequence in itertools.permutations(game.hand, length):
            cost = sum(
                max(0, int(card.cost or 0))
                for card in sequence
            )
            if cost <= game.player.energy:
                outcomes.append(replay_cards(game, sequence, split_threshold))
    return min(
        outcomes,
        key=lambda outcome: (outcome["hp_loss"], outcome["final_boss_hp"]),
    )


class HistoricalCombatFixtureTests(unittest.TestCase):
    def assert_slime_split_outcome(self, fixture, hand_order):
        raw_game = copy.deepcopy(fixture["game_state"])
        original_hand = raw_game["combat_state"]["hand"]
        raw_game["combat_state"]["hand"] = [
            original_hand[index] for index in hand_order
        ]
        game = Game.from_json(raw_game, fixture["available_commands"])
        planner = FastCombatPlanner(SilentPriority())

        planner.choose_card_action(game)

        planned_sequence = planner.last_decision.get("planned_sequence") or []
        outcome = replay_planned_damage(
            game,
            planned_sequence,
            fixture["oracle"]["split_threshold"],
        )
        oracle = best_legal_outcome(
            game, fixture["oracle"]["split_threshold"]
        )
        search = planner.last_decision.get("search") or {}
        observed = {
            "outcome_vector": (
                outcome["hp_loss"], outcome["final_boss_hp"]
            ),
            "projected_loss": search.get("projected_loss"),
            "actual_loss": search.get("actual_loss"),
            "boss_crossed_split_threshold": (
                outcome["final_boss_hp"]
                <= fixture["oracle"]["split_threshold"]
            ),
            "slam_cancelled": outcome["slam_cancelled"],
        }
        expected = {
            "outcome_vector": (
                oracle["hp_loss"], oracle["final_boss_hp"]
            ),
            "projected_loss": fixture["oracle"]["projected_loss"],
            "actual_loss": fixture["oracle"]["actual_loss"],
            "boss_crossed_split_threshold": True,
            "slam_cancelled": True,
        }
        self.assertEqual(expected, observed, msg={
            "plan": outcome["card_ids"],
            "final_boss_hp": outcome["final_boss_hp"],
            "oracle_plan": oracle["card_ids"],
            "decision": planner.last_decision,
        })

    def test_attempt_137a936b_seq88863_cancels_slam_by_split(self):
        fixture = load_fixture()

        self.assertEqual(88863, fixture["source"]["before_seq"])
        self.assertEqual(60, fixture["game_state"]["current_hp"])
        self.assert_slime_split_outcome(fixture, [0, 1, 2, 3, 4])

    def test_slime_split_outcome_is_invariant_to_hand_order(self):
        fixture = load_fixture()
        orders = (
            (4, 3, 2, 1, 0),
            (3, 2, 4, 0, 1),
            (2, 0, 3, 1, 4),
            (1, 4, 0, 2, 3),
        )

        for order in orders:
            with self.subTest(order=order):
                self.assert_slime_split_outcome(fixture, order)

    def test_split_interrupt_boundary_matches_independent_oracle(self):
        """Exercise the mechanic boundary, not one memorized fixture value.

        With this hand and three energy, the independent oracle can push the
        boss through its 70-HP Split threshold from every HP in [71, 93].
        Vary both HP and serialized hand order so a one-off score bonus or an
        implementation-order accident cannot make this regression test pass.
        """

        fixture = load_fixture()
        orders = (
            (0, 1, 2, 3, 4),
            (4, 3, 2, 1, 0),
            (3, 2, 4, 0, 1),
        )
        for boss_hp in range(71, 94):
            for order in orders:
                with self.subTest(boss_hp=boss_hp, order=order):
                    varied = copy.deepcopy(fixture)
                    varied["game_state"]["combat_state"]["monsters"][0][
                        "current_hp"
                    ] = boss_hp
                    self.assert_slime_split_outcome(varied, order)

    def test_attempt_59e6bac7_trades_affordable_hp_for_lagavulin_tempo(self):
        fixture = load_lagavulin_fixture()
        game = lagavulin_game(fixture)
        planner = FastCombatPlanner(IroncladPriority())

        planner.choose_card_action(game)

        search = planner.last_decision["search"]
        observed_plan = [
            item["card_id"]
            for item in planner.last_decision["planned_sequence"]
        ]
        self.assertEqual(fixture["oracle"]["required_plan"], observed_plan)
        self.assertNotEqual(fixture["observed_old_plan"], observed_plan)
        self.assertEqual(
            fixture["oracle"]["projected_hp_loss"],
            search["projected_loss"],
        )
        self.assertEqual(
            fixture["oracle"]["enemy_hp_progress"],
            search["enemy_hp_progress"],
        )
        self.assertGreaterEqual(
            search["persistent_debuff_pressure"],
            fixture["oracle"]["minimum_persistent_debuff_pressure"],
        )

    def test_lagavulin_attrition_policy_is_shared_by_all_characters(self):
        fixture = load_lagavulin_fixture()
        for priority_type in (
            IroncladPriority,
            SilentPriority,
            DefectPowerPriority,
        ):
            with self.subTest(priority=priority_type.__name__):
                game = lagavulin_game(fixture)
                planner = FastCombatPlanner(priority_type())

                planner.choose_card_action(game)

                plan = [
                    item["card_id"]
                    for item in planner.last_decision["planned_sequence"]
                ]
                self.assertEqual(
                    fixture["oracle"]["required_plan"], plan
                )
                self.assertGreater(
                    planner.last_decision["search"][
                        "persistent_debuff_pressure"
                    ],
                    0,
                )

    def test_lagavulin_attrition_pressure_does_not_break_low_hp_survival(self):
        fixture = load_lagavulin_fixture()
        fixture["player"]["current_hp"] = 12
        game = lagavulin_game(fixture)
        planner = FastCombatPlanner(IroncladPriority())

        planner.choose_card_action(game)

        plan = [
            item["card_id"]
            for item in planner.last_decision["planned_sequence"]
        ]
        self.assertEqual(["Defend_R", "Defend_R", "Defend_R"], plan)
        self.assertEqual(0, planner.last_decision["search"]["actual_loss"])
        self.assertGreater(
            planner.last_decision["search"]["persistent_debuff_pressure"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
