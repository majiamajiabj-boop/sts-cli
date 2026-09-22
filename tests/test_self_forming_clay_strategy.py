import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "spirecomm-master"))

import strategy_audit
from spirecomm.ai.combat_planner import FastCombatPlanner
from spirecomm.ai.priorities import IroncladPriority
from spirecomm.communication.action import PlayCardAction
from spirecomm.spire.card import Card, CardRarity, CardType
from spirecomm.spire.character import Intent, Monster, Player
from spirecomm.spire.power import Power
from spirecomm.spire.relic import Relic


def monster(monster_id, hp):
    value = Monster(
        monster_id,
        monster_id,
        hp,
        hp,
        0,
        Intent.BUFF,
        False,
        False,
        move_adjusted_damage=0,
        move_hits=0,
    )
    value.monster_index = 0
    value.powers = []
    return value


def card(
    card_id,
    card_type,
    *,
    cost=1,
    damage=0,
    target=False,
    playable=True,
    magic=0,
):
    return Card(
        card_id,
        card_id,
        card_type,
        CardRarity.COMMON,
        upgrades=0,
        cost=cost,
        uuid=card_id,
        has_target=target,
        is_playable=playable,
        damage=damage,
        base_damage=damage,
        block=0,
        base_block=-1,
        magic_number=magic,
    )


class GameStub:
    def __init__(self, enemy, hand, *, hp=30, energy=1):
        self.monsters = [enemy]
        self.hand = list(hand)
        self.player = Player(70, hp, block=0, energy=energy)
        self.player.powers = []
        self.player.orbs = []
        self.relics = []
        self.draw_pile = []
        self.discard_pile = []
        self.act = 1
        self.floor = 5
        self.turn = 2
        self.room_type = "MonsterRoom"


def clay():
    return Relic("Self Forming Clay", "Self Forming Clay")


def hemo_plan(*, enemy_hp=50, with_clay=True, powers=()):
    enemy = monster("Dummy", enemy_hp)
    hemokinesis = card(
        "Hemokinesis",
        CardType.ATTACK,
        cost=1,
        damage=15,
        target=True,
    )
    game = GameStub(enemy, [hemokinesis])
    game.relics = [clay()] if with_clay else []
    game.player.powers = list(powers)
    planner = FastCombatPlanner(IroncladPriority())
    action = planner.choose_card_action(game)
    return action, planner.last_decision


def clay_audit_record(search, *, card_self_hp_cost=2):
    return {
        "attempt_id": "attempt-clay",
        "run_id": "IRONCLAD:0:1",
        "before_seq": 1,
        "phase": "COMBAT_TURN_1",
        "action": "play",
        "relic_ids_before": ["Self Forming Clay"],
        "hand_before": [],
        "monsters_before": [],
        "decision": {
            "card_self_hp_cost": card_self_hp_cost,
            "search": dict(search),
        },
    }


class SelfFormingClayStrategyTests(unittest.TestCase):
    def test_clay_does_not_blanket_exempt_low_efficiency_self_damage(self):
        enemy = monster("Dummy", 50)
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            damage=1,
            target=True,
        )
        game = GameStub(enemy, [hemokinesis])
        game.relics = [clay()]
        candidate = SimpleNamespace(
            kills=False,
            intrinsic_mitigation=0,
            healing_gain=0,
        )
        planner = FastCombatPlanner(IroncladPriority())

        penalty = planner._low_efficiency_self_damage_penalty(
            game,
            hemokinesis,
            candidate,
            hp_damage=1,
            self_hp_cost=2,
            killed=False,
            neutralized=False,
            generated_block=0,
            hand_additions=0,
            energy_gain=0,
            secondary_damage=0,
            targets=[enemy],
        )

        self.assertGreater(penalty, 0.0)

    def test_one_real_hp_loss_event_adds_only_bounded_future_value(self):
        plain_action, plain = hemo_plan(with_clay=False)
        clay_action, valued = hemo_plan(with_clay=True)

        self.assertIsInstance(plain_action, PlayCardAction)
        self.assertIsInstance(clay_action, PlayCardAction)
        plain_search = plain["search"]
        search = valued["search"]
        self.assertEqual(1, search["self_forming_clay_hp_loss_events"])
        self.assertEqual(3, search["self_forming_clay_future_block"])
        self.assertEqual(1.35, search["self_forming_clay_credit"])
        self.assertEqual(1.35, search["self_forming_clay_applied_credit"])
        self.assertEqual(0, search["self_forming_clay_current_turn_mitigation"])
        self.assertEqual(
            plain_search["projected_loss"], search["projected_loss"]
        )
        # This also proves the static estimate was replaced, not added again.
        self.assertAlmostEqual(
            1.35, valued["plan_score"] - plain["plan_score"], places=3
        )

    def test_clay_credit_does_not_unlock_preferred_target_bonus(self):
        def candidate(with_clay, *, cost=1):
            enemy = monster("TheGuardian", 208)
            enemy.max_hp = 240
            enemy.block = 20
            enemy.powers = [Power("Vulnerable", "Vulnerable", 1)]
            hemokinesis = card(
                "Hemokinesis",
                CardType.ATTACK,
                cost=cost,
                damage=15,
                target=True,
            )
            game = GameStub(enemy, [hemokinesis], hp=73)
            game.relics = [clay()] if with_clay else []
            planner = FastCombatPlanner(IroncladPriority())
            return planner._candidate(
                game, hemokinesis, enemy, enemy, 0, 0, 0
            )

        plain = candidate(False)
        valued = candidate(True)

        self.assertLess(plain.base_score, 0)
        self.assertAlmostEqual(
            1.35, valued.base_score - plain.base_score, places=6
        )
        self.assertAlmostEqual(
            1.35, valued.order_score - plain.order_score, places=6
        )

        free_plain = candidate(False, cost=0)
        free_valued = candidate(True, cost=0)
        self.assertEqual(0.0, free_plain.base_score)
        self.assertAlmostEqual(
            1.35,
            free_valued.order_score - free_plain.order_score,
            places=6,
        )

    def test_buffer_and_tungsten_zero_loss_events_get_zero_credit(self):
        enemy = monster("Dummy", 50)
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            damage=15,
            target=True,
        )
        buffered = GameStub(enemy, [hemokinesis])
        buffered.relics = [clay()]
        buffered.player.powers = [Power("Buffer", "Buffer", 1)]
        planner = FastCombatPlanner(IroncladPriority())
        buffered_candidate = planner._candidate(
            buffered, hemokinesis, enemy, enemy, 0, 0, 0
        )

        strike = card(
            "Strike_R", CardType.ATTACK, damage=6, target=True
        )
        pain = card("Pain", CardType.CURSE, cost=-2, playable=False)
        tungsten = GameStub(enemy, [strike, pain])
        tungsten.relics = [
            clay(),
            Relic("Tungsten Rod", "Tungsten Rod"),
        ]
        tungsten_candidate = planner._candidate(
            tungsten, strike, enemy, enemy, 0, 0, 0
        )

        for candidate in (buffered_candidate, tungsten_candidate):
            self.assertEqual(0, candidate.self_forming_clay_hp_loss_events)
            self.assertEqual(0, candidate.self_forming_clay_future_block)
            self.assertEqual(0.0, candidate.self_forming_clay_credit)

    def test_duplicated_self_damage_counts_each_real_event(self):
        action, decision = hemo_plan(
            with_clay=True,
            powers=[Power("DuplicationPower", "Duplication", 1)],
        )

        self.assertIsInstance(action, PlayCardAction)
        search = decision["search"]
        self.assertEqual(2, search["self_forming_clay_hp_loss_events"])
        self.assertEqual(6, search["self_forming_clay_future_block"])
        self.assertEqual(2.7, search["self_forming_clay_credit"])

    def test_true_combat_end_reports_zero_applied_future_value(self):
        _, plain = hemo_plan(enemy_hp=10, with_clay=False)
        _, valued = hemo_plan(enemy_hp=10, with_clay=True)

        search = valued["search"]
        self.assertTrue(search["true_combat_end"])
        self.assertEqual(1, search["self_forming_clay_hp_loss_events"])
        self.assertEqual(1.35, search["self_forming_clay_credit"])
        self.assertEqual(0.0, search["self_forming_clay_applied_credit"])
        self.assertEqual(plain["plan_score"], valued["plan_score"])

    def test_no_clay_has_no_events_or_future_value(self):
        enemy = monster("Dummy", 50)
        hemokinesis = card(
            "Hemokinesis",
            CardType.ATTACK,
            damage=15,
            target=True,
        )
        game = GameStub(enemy, [hemokinesis])
        planner = FastCombatPlanner(IroncladPriority())

        candidate = planner._candidate(
            game, hemokinesis, enemy, enemy, 0, 0, 0
        )

        self.assertEqual(0, candidate.self_forming_clay_hp_loss_events)
        self.assertEqual(0, candidate.self_forming_clay_future_block)
        self.assertEqual(0.0, candidate.self_forming_clay_credit)

    def test_offering_prefix_is_exact_and_no_longer_fails_closed(self):
        enemy = monster("Dummy", 100)
        game = GameStub(enemy, [])
        game.relics = [clay()]
        game.draw_pile = [
            card("Anger", CardType.ATTACK, cost=0, damage=6, target=True)
            for _ in range(3)
        ]
        for index, drawn in enumerate(game.draw_pile):
            drawn.uuid = f"anger-{index}"
        offering = card(
            "Offering", CardType.SKILL, cost=0, magic=3
        )
        planner = FastCombatPlanner(IroncladPriority())

        def continuation(inner, _hypothetical):
            inner.last_decision = {
                "reason": "test_continuation",
                "plan_score": 0.0,
                "planned_sequence": [],
                "search": {
                    "actual_loss": 0,
                    "final_enemy_hp": [100],
                    "true_combat_end": False,
                },
            }

        with patch.object(
            FastCombatPlanner, "choose_card_action", continuation
        ):
            result = planner._temporary_offering_continuation(
                game, offering
            )

        self.assertIsNot(result, False)
        _, prefix_loss, metadata = result
        self.assertEqual(6, prefix_loss)
        self.assertEqual(1, metadata["self_forming_clay_hp_loss_events"])
        self.assertEqual(3, metadata["self_forming_clay_future_block"])
        self.assertEqual(1.35, metadata["self_forming_clay_credit"])
        self.assertEqual(1.35, metadata["self_forming_clay_applied_credit"])
        self.assertEqual(
            "offering_prefix_hp_loss_events",
            metadata["self_forming_clay_credit_authority"],
        )

    def test_clay_audit_requires_exact_new_controller_fields(self):
        valid = {
            "voluntary_self_hp_cost": 2,
            "true_combat_end": False,
            "self_forming_clay_hp_loss_events": 1,
            "self_forming_clay_future_block": 3,
            "self_forming_clay_credit": 1.35,
            "self_forming_clay_applied_credit": 1.35,
            "self_forming_clay_current_turn_mitigation": 0,
            "self_forming_clay_credit_authority": (
                "planner_dynamic_hp_loss_events"
            ),
        }
        report = strategy_audit._relic_strategy_audit_report(
            [clay_audit_record(valid)], []
        )
        relic = report["per_relic"]["self forming clay"]
        self.assertEqual("clear", relic["status"])
        self.assertEqual(1, relic["evaluated"])

        missing = strategy_audit._relic_strategy_audit_report(
            [clay_audit_record({"voluntary_self_hp_cost": 2})], []
        )["per_relic"]["self forming clay"]
        self.assertEqual("inconclusive", missing["status"])
        self.assertEqual(1, missing["unknown"])

        ordinary = strategy_audit._relic_strategy_audit_report(
            [clay_audit_record({}, card_self_hp_cost=0)], []
        )["per_relic"]["self forming clay"]
        self.assertEqual("not_applicable", ordinary["status"])
        self.assertEqual(0, ordinary["eligible"])

    def test_clay_audit_requires_zero_applied_credit_at_combat_end(self):
        search = {
            "voluntary_self_hp_cost": 2,
            "true_combat_end": True,
            "self_forming_clay_hp_loss_events": 1,
            "self_forming_clay_future_block": 3,
            "self_forming_clay_credit": 1.35,
            "self_forming_clay_applied_credit": 0.0,
            "self_forming_clay_current_turn_mitigation": 0,
            "self_forming_clay_credit_authority": (
                "planner_dynamic_hp_loss_events"
            ),
        }
        relic = strategy_audit._relic_strategy_audit_report(
            [clay_audit_record(search)], []
        )["per_relic"]["self forming clay"]

        self.assertEqual("clear", relic["status"])


if __name__ == "__main__":
    unittest.main()
