"""Transaction replay and decision-level regressions from the September cohort."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import bridge
import strategy_audit
from tests.test_bridge import TEST_ATTEMPT_BINDING
from tests.test_terminal_survival import game_for, SimpleAgent
from spirecomm.spire.card import Card, CardType
from spirecomm.spire.relic import Relic
from spirecomm.spire.screen import CardRewardScreen

ROOT = Path(__file__).resolve().parents[1]
FRAMES = json.loads((ROOT / "test_fixtures/p0_resource_frames.json").read_text())


def payload_for(state, **changes):
    return {
        **TEST_ATTEMPT_BINDING, "expected_seq": state["state_seq"],
        "decision_id": state["decision_id"], "phase": state["phase"],
        "id": "test", **changes,
    }


def agent_for(seq):
    game = game_for(FRAMES[str(seq)]["frame"])
    agent = SimpleAgent(game.character, goal_mode="HEART", macro_advisor=None)
    agent.game = game
    return agent


class ResourceDecisionTests(unittest.TestCase):
    def test_giant_head_opening_preserves_premium_resources(self):
        agent = agent_for(788360)
        action = agent.combat_planner.choose_card_action(agent.game)
        self.assertNotEqual("Fiend Fire", action.card.card_id)
        search = agent.combat_planner.last_decision["search"]
        lost = search["forced_exhaust_lifecycle_cards"]
        self.assertLess(sum(c["card_id"] == "Impervious" for c in lost), 2)
        if lost:
            self.assertGreater(search["forced_exhaust_lifecycle_cost"], 0)

    def test_lethal_exhaust_still_beats_preserving_resources(self):
        agent = agent_for(788360)
        game = agent.game
        game.player.energy = 2
        game.monsters[0].current_hp = 35
        game.monsters[0].powers = []
        action = agent.combat_planner.choose_card_action(game)
        self.assertEqual("Fiend Fire", action.card.card_id)
        self.assertEqual(0, agent.combat_planner.last_decision["search"]
                         ["forced_exhaust_lifecycle_cost"])

    def test_resource_estimate_is_bounded_and_multihit_damage_is_not_squared(self):
        agent = agent_for(788360)
        game, planner = agent.game, agent.combat_planner
        card = copy.copy(next(c for c in game.hand if c.card_id == "Whirlwind"))
        card.damage = 6
        # At 3 Energy: 18 damage, exactly three basic attacks; no premium.
        self.assertEqual(0, planner._forced_exhaust_resource_value(game, card))
        card = copy.copy(next(c for c in game.hand if c.card_id == "Impervious"))
        self.assertGreater(planner._forced_exhaust_resource_value(game, card), 0)
        card.type = CardType.STATUS
        self.assertEqual(0, planner._forced_exhaust_resource_value(game, card))

    def reward_agent(self):
        agent = agent_for(791640)
        screen = copy.deepcopy(FRAMES["791640"]["frame"]["game_state"]["screen_state"])
        for row in screen["cards"]:
            row["uuid"] = row["card_instance_id"]
        agent.game.screen = CardRewardScreen.from_json(screen)
        return agent

    def test_actual_reward_selection_skips_unsupported_force_field(self):
        agent = self.reward_agent()
        agent.choose_card_reward()
        chosen = agent.last_noncombat_decision
        self.assertNotEqual("card:45fd7ae5-a295-4857-a876-20487e0a7133",
                            chosen["chosen_id"])

    def test_force_field_support_is_real_and_does_not_require_future_rewards(self):
        agent = self.reward_agent()
        card = next(c for c in agent.game.screen.cards if c.card_id == "Force Field")
        self.assertLess(agent._card_access_cost_adjustment(card, agent.game.deck), 0)
        power = copy.copy(agent.game.deck[0])
        power.type = CardType.POWER
        self.assertEqual(0, agent._card_access_cost_adjustment(card, agent.game.deck + [power]))
        agent.game.relics.append(Relic("Sozu", "Sozu", -1))
        self.assertEqual(0, agent._card_access_cost_adjustment(card, agent.game.deck))


class ResyncEvidenceTests(unittest.TestCase):
    def pair(self):
        base = {
            "record_type": "protocol_event", "decision_schema_version": 2,
            "attempt_id": "a", "run_id": "r", "decision_hash": "d",
            "controller_hash": "c", "before_seq": 10, "decision_id": "surface",
            "phase": "COMBAT_INITIALIZING", "reason": "combat_initializing",
            "resync_attempt": 1, "state_request_id": "request",
        }
        return [
            dict(base, event="state_resync_started"),
            dict(base, event="state_resync_succeeded", receipt_status="succeeded",
                 accepted_state_seq=10, result_state_seq=11),
        ]

    def test_only_bound_ordered_success_pairs_are_exempt(self):
        good = self.pair()
        self.assertEqual({0, 1}, strategy_audit._benign_resync_record_indexes(good))
        mutations = [
            [good[0]], [good[1]], list(reversed(good)), good + [good[1]],
            [good[0], dict(good[1], attempt_id="other")],
            [good[0], dict(good[1], result_state_seq=10)],
            [good[0], dict(good[1], receipt_status="failed")],
            [dict(r, reason="receipt_timeout") for r in good],
            [dict(r, controller_hash=None) for r in good],
        ]
        for records in mutations:
            with self.subTest(records=records):
                self.assertEqual(set(), strategy_audit._benign_resync_record_indexes(records))
                report = strategy_audit.audit_records(records, "d")
                self.assertGreater(len(report["protocol_correctness"]["fatal_event_counts"]), 0)

    def test_successful_readiness_pairs_do_not_hide_real_timeout(self):
        records = self.pair()
        report = strategy_audit.audit_records(records, "d")
        self.assertEqual({}, report["protocol_correctness"]["fatal_event_counts"])
        records.append(dict(records[0], event="receipt_timeout", state_request_id=None))
        report = strategy_audit.audit_records(records, "d")
        self.assertEqual({"receipt_timeout": 1},
                         report["protocol_correctness"]["fatal_event_counts"])


class GamblingTransactionTests(unittest.TestCase):
    def test_main_loop_selects_once_resyncs_then_confirms_only_when_ready(self):
        # Fake CommunicationMod transport; actual bridge loop, validation,
        # UUID rebinding, command dispatch and receipt ownership run unchanged.
        card = {"id": "Strike_R", "name": "Strike", "uuid": "target"}
        raw = {
            "in_game": True, "ready_for_command": True,
            "available_commands": ["choose", "confirm", "wait", "state"],
            "game_state": {
                "screen_type": "HAND_SELECT", "room_phase": "COMBAT",
                "current_action": "GamblingChipAction", "choice_list": ["Strike"],
                "screen_state": {"hand": [card], "selected": [], "max_cards": 99,
                                 "can_pick_zero": True},
                "combat_state": {"turn": 1, "monsters": []},
            },
        }
        selected = copy.deepcopy(raw)
        selected["ready_for_command"] = False
        selected["game_state"]["screen_state"].update(hand=[], selected=[card])
        selected["game_state"]["choice_list"] = []
        ready = copy.deepcopy(selected)
        ready["ready_for_command"] = True
        closed = copy.deepcopy(ready)
        closed["game_state"].update(screen_type="NONE", screen_state={}, current_action=None)
        frames = [raw, raw, raw, selected, selected, ready, closed]
        receipts, commands, latest = [], [], {}
        done = False
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            directory = Path(directory)
            for name in ("COMMAND_PATH", "RECEIPT_PATH", "STATE_PATH", "META_PATH"):
                stack.enter_context(mock.patch.object(bridge, name, directory / name))
            def queue(action, request):
                state = latest["state"]
                payload = payload_for(state, id=request, action=action, target_id="action:" + action)
                if action == "choose":
                    payload["option_id"] = state["options"][0]["option_id"]
                bridge.COMMAND_PATH.write_text(json.dumps(payload))
            def write(path, value):
                nonlocal done
                if path == bridge.STATE_PATH:
                    latest["state"] = value
                    if value["state_seq"] == 1:
                        queue("choose", "select")
                    if value["state_seq"] == 4:
                        for action in ("choose", "proceed"):
                            with self.assertRaises(bridge.CommandRejected):
                                bridge.resolve_command(payload_for(value, action=action), value)
                if path == bridge.RECEIPT_PATH:
                    receipts.append(copy.deepcopy(value))
                    if value["status"] == "succeeded":
                        if value["request_id"] == "select":
                            queue("state", "probe-1")
                        elif value["request_id"] == "probe-1":
                            queue("state", "probe-2")
                        elif value["request_id"] == "probe-2":
                            self.assertTrue(latest["state"]["ready_for_command"])
                            queue("proceed", "confirm")
                        elif value["request_id"] == "confirm":
                            done = True
                    self.assertNotIn(value["status"], {"failed", "rejected"})
            def emit(command, lock):
                commands.append(command)
            stack.enter_context(mock.patch.object(bridge, "atomic_write_json", side_effect=write))
            stack.enter_context(mock.patch.object(bridge, "_emit_command", side_effect=emit))
            stack.enter_context(mock.patch.object(bridge, "_state_command_wakeup"))
            stack.enter_context(mock.patch.object(bridge, "claim_bridge_instance", return_value="test"))
            stack.enter_context(mock.patch.object(bridge, "release_bridge_instance"))
            stack.enter_context(mock.patch.object(bridge, "bridge_instance_is_current",
                                                   side_effect=lambda token: not done))
            stack.enter_context(mock.patch.object(bridge, "parent_process_is_alive", return_value=True))
            stack.enter_context(mock.patch.object(bridge, "active_attempt_binding",
                                                   return_value=TEST_ATTEMPT_BINDING))
            stack.enter_context(mock.patch.object(bridge, "read_initial_sequence", return_value=0))
            stack.enter_context(mock.patch.object(bridge.sys, "stdin",
                io.StringIO(chr(10).join(json.dumps(frame) for frame in frames) + chr(10))))
            bridge.main()
        finals = [r for r in receipts if r["status"] == "succeeded"]
        self.assertEqual(["select", "probe-1", "probe-2", "confirm"],
                         [r["request_id"] for r in finals])
        self.assertEqual(4, finals[0]["result_state_seq"])
        self.assertEqual(1, sum(c.startswith("CHOOSE") for c in commands))
        self.assertEqual(1, sum(c in {"CONFIRM", "PROCEED"} for c in commands))


if __name__ == "__main__":
    unittest.main()
